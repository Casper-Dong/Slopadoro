const WS_URL = "ws://localhost:8765";
const STORAGE_KEYS = {
  sample: "latestSample",
  status: "connectionStatus"
};

const RECONNECT_ALARM = "bci-ws-reconnect";
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const STALE_AFTER_MS = 2000;

let socket = null;
let reconnectDelayMs = RECONNECT_MIN_MS;
let staleTimer = null;
let reconnectTimer = null;

function clamp01(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return null;
  }
  return Math.min(1, Math.max(0, value));
}

function writeSession(update) {
  chrome.storage.session.set(update);
}

function setBadge(sample, statusState) {
  const disconnected = statusState !== "connected" && statusState !== "calibrating";
  const calibrating = statusState === "calibrating" || sample?.calibrating;
  const focus = clamp01(sample?.focus);
  const fatigue = clamp01(sample?.fatigue);
  const text = disconnected || calibrating || focus === null ? "..." : String(Math.round(focus * 100));

  let color = "#6b7280";
  if (!disconnected && !calibrating && fatigue !== null) {
    if (fatigue < 0.34) {
      color = "#15803d";
    } else if (fatigue < 0.67) {
      color = "#b45309";
    } else {
      color = "#b91c1c";
    }
  }

  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setTitle({
    title: disconnected
      ? "BCI Focus & Fatigue: disconnected"
      : calibrating
        ? "BCI Focus & Fatigue: calibrating"
        : `BCI Focus & Fatigue: focus ${text}, fatigue ${Math.round((fatigue ?? 0) * 100)}`
  });
}

function setStatus(state, detail = {}) {
  const status = {
    state,
    connected: state === "connected" || state === "calibrating",
    updatedAt: Date.now(),
    ...detail
  };

  writeSession({ [STORAGE_KEYS.status]: status });
  setBadge(detail.sample ?? null, state);
}

function normalizeSample(raw) {
  if (!raw || typeof raw !== "object") {
    return null;
  }

  const sample = {
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

  return sample;
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
      console.warn("BCI WebSocket became stale; reconnecting");
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

  const delay = reconnectDelayMs;
  reconnectDelayMs = Math.min(reconnectDelayMs * 2, RECONNECT_MAX_MS);
  console.info(`BCI WebSocket disconnected (${reason}); reconnecting in ${delay} ms`);
  setStatus("disconnected", { reason, nextRetryMs: delay });

  clearReconnectTimer();
  reconnectTimer = setTimeout(connect, delay);
  chrome.alarms.create(RECONNECT_ALARM, { when: Date.now() + delay });
}

function connect() {
  if (socket && (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN)) {
    return;
  }

  clearReconnectTimer();
  setStatus("connecting");
  console.info(`Connecting to BCI WebSocket at ${WS_URL}`);

  const ws = new WebSocket(WS_URL);
  socket = ws;

  ws.onopen = () => {
    if (socket !== ws) {
      return;
    }
    reconnectDelayMs = RECONNECT_MIN_MS;
    setStatus("connected");
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

      writeSession({ [STORAGE_KEYS.sample]: sample });
      setStatus(sample.calibrating ? "calibrating" : "connected", { sample });
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

chrome.runtime.onInstalled.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RECONNECT_ALARM) {
    connect();
  }
});

connect();
