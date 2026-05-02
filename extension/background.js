const DEFAULT_WS_URL = "ws://localhost:8765/";
const STORAGE_KEYS = {
  sample: "latestSample",
  status: "connectionStatus",
  wsUrl: "wsUrl"
};

const RECONNECT_ALARM = "fatigue-cat-reconnect";
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const STALE_AFTER_MS = 2000;
const FATIGUE_ALERT_THRESHOLD = 0.75;
const FATIGUE_ALERT_MS = 30000;
const NOTIFICATION_COOLDOWN_MS = 5 * 60 * 1000;

let socket = null;
let reconnectDelayMs = RECONNECT_MIN_MS;
let reconnectTimer = null;
let staleTimer = null;
let highFatigueSinceMs = null;
let notificationCooldownUntilMs = 0;
let wsUrl = DEFAULT_WS_URL;
let configLoading = false;

function clamp01(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return null;
  }
  return Math.min(1, Math.max(0, value));
}

function normalizeSample(raw) {
  if (!raw || typeof raw !== "object") {
    return null;
  }

  return {
    ts: typeof raw.ts === "number" ? raw.ts : Date.now() / 1000,
    focus: raw.focus === null ? null : clamp01(raw.focus),
    fatigue: raw.fatigue === null ? null : clamp01(raw.fatigue),
    calibrating: Boolean(raw.calibrating),
    subscores: raw.subscores && typeof raw.subscores === "object" ? raw.subscores : {},
    sources: {
      eeg: Boolean(raw.sources?.eeg),
      ecg: Boolean(raw.sources?.ecg),
      emg: Boolean(raw.sources?.emg)
    }
  };
}

function unavailableSample() {
  return {
    focus: null,
    fatigue: null,
    calibrating: true,
    sources: { eeg: false, ecg: false, emg: false }
  };
}

function normalizeWsUrl(value) {
  if (typeof value !== "string") {
    return null;
  }

  try {
    const url = new URL(value.trim());
    if (url.protocol !== "ws:" && url.protocol !== "wss:") {
      return null;
    }
    return url.href;
  } catch {
    return null;
  }
}

function shortWsUrl() {
  try {
    const url = new URL(wsUrl);
    return `${url.protocol}//${url.host}`;
  } catch {
    return wsUrl;
  }
}

function writeSession(update) {
  chrome.storage.session.set(update);
}

function badgeColor(fatigue) {
  const value = clamp01(fatigue);
  if (value === null) {
    return "#6b7280";
  }
  if (value < 0.35) {
    return "#15803d";
  }
  if (value < 0.7) {
    return "#d97706";
  }
  return "#dc2626";
}

function setBadge(sample, state) {
  const connected = state === "connected";
  const calibrating = state === "calibrating" || sample?.calibrating;
  const focus = clamp01(sample?.focus);
  const fatigue = clamp01(sample?.fatigue);
  const text = connected && !calibrating && focus !== null ? String(Math.min(99, Math.round(focus * 100))) : "...";

  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color: connected && !calibrating ? badgeColor(fatigue) : "#6b7280" });
  chrome.action.setTitle({
    title: connected && !calibrating
      ? `Cat fatigue: focus ${text}, fatigue ${Math.round((fatigue ?? 0) * 100)}`
      : `Cat fatigue: waiting for ${shortWsUrl()}`
  });
}

function broadcastFatigue(sample) {
  const payload = {
    type: "fatigue",
    value: sample?.fatigue ?? null,
    focus: sample?.focus ?? null,
    calibrating: Boolean(sample?.calibrating),
    sources: sample?.sources ?? { eeg: false, ecg: false, emg: false }
  };

  chrome.tabs.query({}, (tabs) => {
    if (chrome.runtime.lastError) {
      return;
    }

    for (const tab of tabs) {
      if (typeof tab.id !== "number") {
        continue;
      }
      chrome.tabs.sendMessage(tab.id, payload, () => {
        void chrome.runtime.lastError;
      });
    }
  });
}

function setStatus(state, detail = {}) {
  const status = {
    state,
    connected: state === "connected" || state === "calibrating",
    url: wsUrl,
    updatedAt: Date.now(),
    ...detail
  };
  writeSession({ [STORAGE_KEYS.status]: status });
  setBadge(detail.sample ?? null, state);
}

function updateNotification(sample) {
  const now = Date.now();
  const fatigue = clamp01(sample?.fatigue);

  if (sample?.calibrating || fatigue === null || fatigue <= FATIGUE_ALERT_THRESHOLD) {
    highFatigueSinceMs = null;
    return;
  }

  highFatigueSinceMs ??= now;
  if (now - highFatigueSinceMs < FATIGUE_ALERT_MS || now < notificationCooldownUntilMs) {
    return;
  }

  chrome.notifications.create({
    type: "basic",
    iconUrl: "icons/128.png",
    title: "Take a break",
    message: "Fatigue has stayed high for 30 seconds. Step away, stretch, or rest your eyes."
  });
  notificationCooldownUntilMs = now + NOTIFICATION_COOLDOWN_MS;
}

function clearStaleTimer() {
  if (staleTimer !== null) {
    clearTimeout(staleTimer);
    staleTimer = null;
  }
}

function armStaleTimer(ws) {
  clearStaleTimer();
  staleTimer = setTimeout(() => {
    if (socket === ws) {
      ws.close();
      scheduleReconnect("stale");
    }
  }, STALE_AFTER_MS);
}

function clearReconnectTimer() {
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  chrome.alarms.clear(RECONNECT_ALARM);
}

function scheduleReconnect(reason) {
  clearStaleTimer();

  if (socket) {
    socket.onopen = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;
    socket = null;
  }

  highFatigueSinceMs = null;
  const sample = unavailableSample();
  setStatus("disconnected", { reason, nextRetryMs: reconnectDelayMs, sample });
  broadcastFatigue(sample);

  const delay = reconnectDelayMs;
  reconnectDelayMs = Math.min(reconnectDelayMs * 2, RECONNECT_MAX_MS);
  clearReconnectTimer();
  reconnectTimer = setTimeout(connect, delay);
  chrome.alarms.create(RECONNECT_ALARM, { when: Date.now() + delay });
}

function connect() {
  if (socket && (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN)) {
    return;
  }

  clearReconnectTimer();
  setStatus("connecting", { sample: unavailableSample() });
  broadcastFatigue(unavailableSample());

  const ws = new WebSocket(wsUrl);
  socket = ws;

  ws.onopen = () => {
    if (socket !== ws) {
      return;
    }
    reconnectDelayMs = RECONNECT_MIN_MS;
    armStaleTimer(ws);
  };

  ws.onmessage = (event) => {
    if (socket !== ws) {
      return;
    }

    armStaleTimer(ws);

    try {
      const sample = normalizeSample(JSON.parse(event.data));
      if (!sample) {
        return;
      }

      const state = sample.calibrating ? "calibrating" : "connected";
      writeSession({ [STORAGE_KEYS.sample]: sample });
      setStatus(state, { sample });
      broadcastFatigue(sample);
      updateNotification(sample);
    } catch (error) {
      console.warn("Ignoring malformed BCI sample", error);
    }
  };

  ws.onerror = () => {
    if (socket === ws) {
      console.warn("BCI WebSocket error");
    }
  };

  ws.onclose = () => {
    if (socket === ws) {
      scheduleReconnect("closed");
    }
  };
}

function restartConnection(reason) {
  clearReconnectTimer();
  clearStaleTimer();

  if (socket) {
    socket.onopen = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;
    socket.close();
    socket = null;
  }

  reconnectDelayMs = RECONNECT_MIN_MS;
  const sample = unavailableSample();
  setStatus("connecting", { reason, sample });
  broadcastFatigue(sample);
  connect();
}

function loadConfigAndConnect() {
  if (configLoading) {
    return;
  }

  configLoading = true;
  chrome.storage.local.get({ [STORAGE_KEYS.wsUrl]: DEFAULT_WS_URL }, (items) => {
    configLoading = false;
    wsUrl = normalizeWsUrl(items[STORAGE_KEYS.wsUrl]) ?? DEFAULT_WS_URL;
    connect();
  });
}

chrome.runtime.onInstalled.addListener(loadConfigAndConnect);
chrome.runtime.onStartup.addListener(loadConfigAndConnect);
chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== "local" || !changes[STORAGE_KEYS.wsUrl]) {
    return;
  }

  const nextUrl = normalizeWsUrl(changes[STORAGE_KEYS.wsUrl].newValue);
  if (!nextUrl || nextUrl === wsUrl) {
    return;
  }

  wsUrl = nextUrl;
  restartConnection("endpoint changed");
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RECONNECT_ALARM) {
    connect();
  }
});

loadConfigAndConnect();
