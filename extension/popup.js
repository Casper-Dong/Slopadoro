const DEFAULT_WS_URL = "ws://localhost:8765/";
const STORAGE_KEYS = {
  sample: "latestSample",
  status: "connectionStatus",
  flowLog: "flowLog",
  wsUrl: "wsUrl"
};

const els = {
  statusText: document.getElementById("statusText"),
  calibratingLabel: document.getElementById("calibratingLabel"),
  endpointForm: document.getElementById("endpointForm"),
  wsUrlInput: document.getElementById("wsUrlInput"),
  endpointMessage: document.getElementById("endpointMessage"),
  focusValue: document.getElementById("focusValue"),
  fatigueValue: document.getElementById("fatigueValue"),
  focusBar: document.getElementById("focusBar"),
  fatigueBar: document.getElementById("fatigueBar"),
  flowState: document.getElementById("flowState"),
  flowHeatmap: document.getElementById("flowHeatmap"),
  eegDot: document.getElementById("eegDot"),
  ecgDot: document.getElementById("ecgDot"),
  emgDot: document.getElementById("emgDot")
};

let latestSample = null;
let connectionStatus = null;
let configuredWsUrl = DEFAULT_WS_URL;
let flowLog = [];

function clamp01(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return null;
  }
  return Math.min(1, Math.max(0, value));
}

function setBar(valueEl, barEl, value, disabled) {
  const bounded = disabled ? null : clamp01(value);
  valueEl.textContent = bounded === null ? "--" : String(Math.round(bounded * 100));
  barEl.style.width = `${Math.round((bounded ?? 0) * 100)}%`;
}

function setDot(el, active) {
  el.classList.toggle("active", Boolean(active));
}

function classifyFlow(sample, status) {
  const focus = clamp01(sample?.focus);
  const fatigue = clamp01(sample?.fatigue);
  const state = status?.state ?? "disconnected";
  const connected = state === "connected" || state === "calibrating";

  if (!connected) {
    return "offline";
  }
  if (sample?.sources?.eeg === false) {
    return "waiting";
  }
  if (sample?.calibrating || focus === null || fatigue === null) {
    return "calibrating";
  }
  if (fatigue >= 0.75 || focus < 0.35) {
    return "break";
  }
  if (focus >= 0.72 && fatigue < 0.45) {
    return "flow";
  }
  if (focus >= 0.55 && fatigue < 0.6) {
    return "steady";
  }
  return "drifting";
}

function flowLabel(state) {
  return {
    flow: "Flow",
    steady: "Steady",
    drifting: "Drifting",
    break: "Break",
    waiting: "Waiting",
    calibrating: "Calibrating",
    offline: "Offline"
  }[state] ?? "--";
}

function heatClass(state) {
  return {
    flow: "flow",
    steady: "steady",
    drifting: "drifting",
    break: "break",
    waiting: "waiting",
    calibrating: "waiting",
    offline: "offline"
  }[state] ?? "empty";
}

function renderHeatmap() {
  const visibleCount = 72;
  const entries = Array.isArray(flowLog) ? flowLog.slice(-visibleCount) : [];
  const fragment = document.createDocumentFragment();
  const blanks = Math.max(0, visibleCount - entries.length);

  for (let index = 0; index < blanks; index += 1) {
    const cell = document.createElement("span");
    cell.className = "heat-cell empty";
    fragment.appendChild(cell);
  }

  for (const entry of entries) {
    const cell = document.createElement("span");
    const state = heatClass(entry?.state);
    const score = clamp01(entry?.score);
    cell.className = `heat-cell ${state}`;
    if (score !== null && (state === "flow" || state === "steady" || state === "drifting" || state === "break")) {
      cell.style.opacity = String(0.42 + score * 0.58);
    }
    const focus = clamp01(entry?.focus);
    const fatigue = clamp01(entry?.fatigue);
    cell.title = `${flowLabel(entry?.state)} · focus ${focus === null ? "--" : Math.round(focus * 100)} · fatigue ${fatigue === null ? "--" : Math.round(fatigue * 100)}`;
    fragment.appendChild(cell);
  }

  els.flowHeatmap.replaceChildren(fragment);
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

function shortWsUrl(value) {
  const normalized = normalizeWsUrl(value) ?? DEFAULT_WS_URL;
  const url = new URL(normalized);
  return `${url.protocol}//${url.host}`;
}

function statusText(sample, status) {
  const stream = shortWsUrl(status?.url ?? configuredWsUrl);
  if (status?.state === "connecting") {
    return `Connecting to ${stream}`;
  }
  if (status?.nextRetryMs) {
    return `Disconnected; retrying in ${Math.round(status.nextRetryMs / 1000)}s`;
  }
  if (sample?.sources?.eeg === false) {
    return "Waiting for headset";
  }
  if (status?.state === "calibrating" || (status?.state === "connected" && sample?.calibrating)) {
    return "Calibrating baseline";
  }
  if (status?.state === "connected") {
    return `Live from ${stream}`;
  }
  return "Disconnected";
}

function render() {
  const sample = latestSample;
  const state = connectionStatus?.state ?? "disconnected";
  const headsetReady = sample?.sources?.eeg === true;
  const connected = state === "connected" || state === "calibrating";
  const calibrating = Boolean(headsetReady && (state === "calibrating" || (state === "connected" && sample?.calibrating)));
  const live = state === "connected" && !calibrating && headsetReady;

  els.statusText.textContent = statusText(sample, connectionStatus);
  els.calibratingLabel.hidden = !calibrating;
  setBar(els.focusValue, els.focusBar, sample?.focus, !live);
  setBar(els.fatigueValue, els.fatigueBar, sample?.fatigue, !live);
  setDot(els.eegDot, connected && sample?.sources?.eeg);
  setDot(els.ecgDot, connected && sample?.sources?.ecg);
  setDot(els.emgDot, connected && sample?.sources?.emg);

  const currentFlow = flowLog.at(-1)?.state ?? classifyFlow(sample, connectionStatus);
  els.flowState.textContent = flowLabel(currentFlow);
  renderHeatmap();
}

chrome.storage.session.get([STORAGE_KEYS.sample, STORAGE_KEYS.status, STORAGE_KEYS.flowLog], (items) => {
  latestSample = items[STORAGE_KEYS.sample] ?? null;
  connectionStatus = items[STORAGE_KEYS.status] ?? null;
  flowLog = Array.isArray(items[STORAGE_KEYS.flowLog]) ? items[STORAGE_KEYS.flowLog] : [];
  render();
});

chrome.storage.local.get({ [STORAGE_KEYS.wsUrl]: DEFAULT_WS_URL }, (items) => {
  configuredWsUrl = normalizeWsUrl(items[STORAGE_KEYS.wsUrl]) ?? DEFAULT_WS_URL;
  els.wsUrlInput.value = configuredWsUrl;
  render();
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === "local" && changes[STORAGE_KEYS.wsUrl]) {
    configuredWsUrl = normalizeWsUrl(changes[STORAGE_KEYS.wsUrl].newValue) ?? DEFAULT_WS_URL;
    if (document.activeElement !== els.wsUrlInput) {
      els.wsUrlInput.value = configuredWsUrl;
    }
    els.endpointMessage.textContent = "Saved";
  }

  if (areaName === "session") {
    if (changes[STORAGE_KEYS.sample]) {
      latestSample = changes[STORAGE_KEYS.sample].newValue ?? null;
    }
    if (changes[STORAGE_KEYS.status]) {
      connectionStatus = changes[STORAGE_KEYS.status].newValue ?? null;
    }
    if (changes[STORAGE_KEYS.flowLog]) {
      flowLog = Array.isArray(changes[STORAGE_KEYS.flowLog].newValue) ? changes[STORAGE_KEYS.flowLog].newValue : [];
    }
  }
  render();
});

els.endpointForm.addEventListener("submit", (event) => {
  event.preventDefault();

  const nextUrl = normalizeWsUrl(els.wsUrlInput.value);
  if (!nextUrl) {
    els.endpointMessage.textContent = "Use a ws:// or wss:// URL";
    return;
  }

  els.wsUrlInput.value = nextUrl;
  els.endpointMessage.textContent = "Saving...";
  chrome.storage.local.set({ [STORAGE_KEYS.wsUrl]: nextUrl });
});
