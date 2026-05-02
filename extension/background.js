const DEFAULT_WS_URL = "ws://localhost:8765/";
const STORAGE_KEYS = {
  sample: "latestSample",
  status: "connectionStatus",
  flowLog: "flowLog",
  metricLog: "metricLog",
  wsUrl: "wsUrl"
};

const RECONNECT_ALARM = "fatigue-cat-reconnect";
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const STALE_AFTER_MS = 2000;
const FATIGUE_ALERT_THRESHOLD = 0.75;
const FATIGUE_ALERT_MS = 30000;
const NOTIFICATION_COOLDOWN_MS = 5 * 60 * 1000;
const METRIC_LOG_INTERVAL_MS = 1000;
const METRIC_LOG_MAX_ENTRIES = 300;
const FLOW_LOG_INTERVAL_MS = 5000;
const FLOW_LOG_MAX_ENTRIES = 360;

let socket = null;
let reconnectDelayMs = RECONNECT_MIN_MS;
let reconnectTimer = null;
let staleTimer = null;
let highFatigueSinceMs = null;
let notificationCooldownUntilMs = 0;
let wsUrl = DEFAULT_WS_URL;
let configLoading = false;
let lastMetricLogAtMs = 0;
let lastMetricLogState = null;
let lastFlowLogAtMs = 0;
let lastFlowLogState = null;

function clamp01(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return null;
  }
  return Math.min(1, Math.max(0, value));
}

function score01(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return null;
  }
  return clamp01(value / 100);
}

function sourceAvailability(rawSources, fallback = { eeg: false, ecg: false, emg: false }) {
  return {
    eeg: Boolean(rawSources?.eeg ?? fallback.eeg),
    ecg: Boolean(rawSources?.ecg ?? fallback.ecg),
    emg: Boolean(rawSources?.emg ?? fallback.emg)
  };
}

function normalizeScoreFrame(raw) {
  const scores = raw?.scores;
  if (!scores || typeof scores !== "object") {
    return null;
  }

  const flags = raw.flags && typeof raw.flags === "object" ? raw.flags : {};
  const state = typeof raw.state === "string" ? raw.state : "";
  const openbciMissing = Boolean(flags.openbci_missing || state === "openbci_missing");
  const polarMissing = Boolean(flags.polar_missing || state === "polar_missing");
  const calibrating = Boolean(raw.calibrating || state === "calibrating");
  const sources = sourceAvailability(raw.sources, {
    eeg: !openbciMissing,
    ecg: !polarMissing,
    emg: !openbciMissing
  });

  return {
    ts: typeof raw.timestamp === "number" ? raw.timestamp : Date.now() / 1000,
    focus: calibrating || openbciMissing ? null : score01(scores.focus_score_0_100),
    fatigue: calibrating || openbciMissing ? null : score01(scores.fatigue_drift_score_0_100),
    calibrating,
    subscores: {
      emg_strain_score_0_100: scores.emg_strain_score_0_100 ?? null,
      signal_quality_score_0_100: scores.signal_quality_score_0_100 ?? null,
      recovery_context_score_0_100: scores.recovery_context_score_0_100 ?? null
    },
    sources
  };
}

function normalizeSample(raw) {
  if (!raw || typeof raw !== "object") {
    return null;
  }

  const scoreFrame = normalizeScoreFrame(raw);
  if (scoreFrame) {
    return scoreFrame;
  }

  return {
    ts: typeof raw.ts === "number" ? raw.ts : Date.now() / 1000,
    focus: raw.focus === null ? null : clamp01(raw.focus),
    fatigue: raw.fatigue === null ? null : clamp01(raw.fatigue),
    calibrating: Boolean(raw.calibrating),
    subscores: raw.subscores && typeof raw.subscores === "object" ? raw.subscores : {},
    sources: sourceAvailability(raw.sources)
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

function metricState(sample, connectionState) {
  const connected = connectionState === "connected" || connectionState === "calibrating";

  if (!connected) {
    return "offline";
  }
  if (sample?.sources?.eeg === false) {
    return "waiting";
  }
  if (sample?.calibrating) {
    return "calibrating";
  }
  return "live";
}

function roundedMetric(value) {
  const bounded = clamp01(value);
  return bounded === null ? null : Number(bounded.toFixed(3));
}

function maybeLogMetrics(sample, connectionState) {
  const now = Date.now();
  const state = metricState(sample, connectionState);
  const stateChanged = state !== lastMetricLogState;

  if (!stateChanged && now - lastMetricLogAtMs < METRIC_LOG_INTERVAL_MS) {
    return;
  }

  lastMetricLogAtMs = now;
  lastMetricLogState = state;

  const entry = {
    ts: sample?.ts ?? now / 1000,
    loggedAt: now,
    state,
    focus: state === "live" ? roundedMetric(sample?.focus) : null,
    fatigue: state === "live" ? roundedMetric(sample?.fatigue) : null
  };

  chrome.storage.session.get({ [STORAGE_KEYS.metricLog]: [] }, (items) => {
    const existing = Array.isArray(items[STORAGE_KEYS.metricLog]) ? items[STORAGE_KEYS.metricLog] : [];
    const next = existing.concat(entry).slice(-METRIC_LOG_MAX_ENTRIES);
    chrome.storage.session.set({ [STORAGE_KEYS.metricLog]: next });
  });
}

function classifyFlow(sample, connectionState) {
  const focus = clamp01(sample?.focus);
  const fatigue = clamp01(sample?.fatigue);
  const connected = connectionState === "connected" || connectionState === "calibrating";

  if (!connected) {
    return { state: "offline", score: null };
  }
  if (sample?.sources?.eeg === false) {
    return { state: "waiting", score: null };
  }
  if (sample?.calibrating || focus === null || fatigue === null) {
    return { state: "calibrating", score: null };
  }
  if (fatigue >= 0.75 || focus < 0.35) {
    return { state: "break", score: focus * (1 - fatigue) };
  }
  if (focus >= 0.72 && fatigue < 0.45) {
    return { state: "flow", score: focus * (1 - fatigue) };
  }
  if (focus >= 0.55 && fatigue < 0.6) {
    return { state: "steady", score: focus * (1 - fatigue) };
  }
  return { state: "drifting", score: focus * (1 - fatigue) };
}

function maybeLogFlow(sample, connectionState) {
  const now = Date.now();
  const flow = classifyFlow(sample, connectionState);
  const stateChanged = flow.state !== lastFlowLogState;

  if (!stateChanged && now - lastFlowLogAtMs < FLOW_LOG_INTERVAL_MS) {
    return;
  }

  lastFlowLogAtMs = now;
  lastFlowLogState = flow.state;

  const entry = {
    ts: sample?.ts ?? now / 1000,
    loggedAt: now,
    state: flow.state,
    score: flow.score === null ? null : Number(flow.score.toFixed(3)),
    focus: clamp01(sample?.focus),
    fatigue: clamp01(sample?.fatigue),
    sources: sourceAvailability(sample?.sources)
  };

  chrome.storage.session.get({ [STORAGE_KEYS.flowLog]: [] }, (items) => {
    const existing = Array.isArray(items[STORAGE_KEYS.flowLog]) ? items[STORAGE_KEYS.flowLog] : [];
    const next = existing.concat(entry).slice(-FLOW_LOG_MAX_ENTRIES);
    chrome.storage.session.set({ [STORAGE_KEYS.flowLog]: next });
  });
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
  const connected = state === "connected" || state === "calibrating";
  const calibrating = state === "calibrating" || sample?.calibrating;
  const headsetReady = sample?.sources?.eeg !== false;
  const focus = clamp01(sample?.focus);
  const fatigue = clamp01(sample?.fatigue);
  const live = connected && !calibrating && headsetReady && focus !== null;
  const text = live ? String(Math.min(99, Math.round(focus * 100))) : "...";
  let title = `Cat fatigue: waiting for ${shortWsUrl()}`;
  if (live) {
    title = `Cat fatigue: focus ${text}, fatigue ${Math.round((fatigue ?? 0) * 100)}`;
  } else if (connected && !headsetReady) {
    title = "Cat fatigue: waiting for headset";
  } else if (connected && calibrating) {
    title = "Cat fatigue: calibrating";
  }

  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color: live ? badgeColor(fatigue) : "#6b7280" });
  chrome.action.setTitle({ title });
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
  maybeLogMetrics(detail.sample ?? null, state);
  maybeLogFlow(detail.sample ?? null, state);
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
