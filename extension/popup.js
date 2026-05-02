const DEFAULT_WS_URL = "ws://localhost:8765/";
const STORAGE_KEYS = {
  sample: "latestSample",
  status: "connectionStatus",
  flowLog: "flowLog",
  metricLog: "metricLog",
  wsUrl: "wsUrl"
};

const CHART_W = 240;
const CHART_H = 64;
const CHART_PAD_X = 5;
const CHART_PAD_Y = 5;
const CHART_VISIBLE_COUNT = 90;

const els = {
  statusText: document.getElementById("statusText"),
  calibratingLabel: document.getElementById("calibratingLabel"),
  endpointForm: document.getElementById("endpointForm"),
  wsUrlInput: document.getElementById("wsUrlInput"),
  endpointMessage: document.getElementById("endpointMessage"),
  focusValue: document.getElementById("focusValue"),
  fatigueValue: document.getElementById("fatigueValue"),
  focusLine: document.getElementById("focusLine"),
  focusDot: document.getElementById("focusDot"),
  fatigueLine: document.getElementById("fatigueLine"),
  fatigueDot: document.getElementById("fatigueDot"),
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
let metricLog = [];

function clamp01(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return null;
  }
  return Math.min(1, Math.max(0, value));
}

function setMetricValue(valueEl, value, disabled) {
  const bounded = disabled ? null : clamp01(value);
  valueEl.textContent = bounded === null ? "--" : String(Math.round(bounded * 100));
}

function setDot(el, active) {
  el.classList.toggle("active", Boolean(active));
}

function entryTime(entry) {
  if (typeof entry?.loggedAt === "number") {
    return entry.loggedAt;
  }
  if (typeof entry?.ts === "number") {
    return entry.ts * 1000;
  }
  return null;
}

function chartEntries(sample, live) {
  const entries = Array.isArray(metricLog) ? metricLog.slice(-CHART_VISIBLE_COUNT) : [];
  const focus = clamp01(sample?.focus);
  const fatigue = clamp01(sample?.fatigue);

  if (live && (focus !== null || fatigue !== null)) {
    const loggedAt = Date.now();
    const latest = entries.at(-1);
    const latestTime = entryTime(latest);
    const currentEntry = {
      ts: typeof sample?.ts === "number" ? sample.ts : loggedAt / 1000,
      loggedAt,
      state: "live",
      focus,
      fatigue
    };

    if (latestTime !== null && Math.abs(loggedAt - latestTime) < 500 && entries.length > 0) {
      entries[entries.length - 1] = currentEntry;
    } else {
      entries.push(currentEntry);
    }
  }

  return entries.slice(-CHART_VISIBLE_COUNT);
}

function chartPoint(entry, key, startTime, endTime) {
  const value = clamp01(entry?.[key]);
  const time = entryTime(entry);

  if (value === null || time === null) {
    return null;
  }

  const range = Math.max(1, endTime - startTime);
  const x = CHART_PAD_X + ((time - startTime) / range) * (CHART_W - CHART_PAD_X * 2);
  const y = CHART_PAD_Y + (1 - value) * (CHART_H - CHART_PAD_Y * 2);
  return { x, y };
}

function renderLineChart(lineEl, dotEl, entries, key) {
  const times = entries.map(entryTime).filter((time) => time !== null);
  const startTime = times[0] ?? 0;
  const endTime = times.at(-1) ?? startTime + 1;
  let path = "";
  let drawing = false;
  let lastPoint = null;

  for (const entry of entries) {
    const point = chartPoint(entry, key, startTime, endTime);
    if (!point) {
      drawing = false;
      continue;
    }

    path += `${drawing ? "L" : "M"}${point.x.toFixed(1)} ${point.y.toFixed(1)}`;
    drawing = true;
    lastPoint = point;
  }

  lineEl.setAttribute("d", path);
  if (lastPoint) {
    dotEl.setAttribute("cx", lastPoint.x.toFixed(1));
    dotEl.setAttribute("cy", lastPoint.y.toFixed(1));
    dotEl.style.display = "block";
  } else {
    dotEl.style.display = "none";
  }
}

function renderMetricCharts(sample, live) {
  const entries = chartEntries(sample, live);
  renderLineChart(els.focusLine, els.focusDot, entries, "focus");
  renderLineChart(els.fatigueLine, els.fatigueDot, entries, "fatigue");
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
      if (url.protocol === "http:" || url.protocol === "https:") {
        const embedded = url.searchParams.get("ws") || url.searchParams.get("extensionWs") || url.searchParams.get("dashboardWs");
        return embedded ? normalizeWsUrl(embedded) : null;
      }
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
  setMetricValue(els.focusValue, sample?.focus, !live);
  setMetricValue(els.fatigueValue, sample?.fatigue, !live);
  renderMetricCharts(sample, live);
  setDot(els.eegDot, connected && sample?.sources?.eeg);
  setDot(els.ecgDot, connected && sample?.sources?.ecg);
  setDot(els.emgDot, connected && sample?.sources?.emg);

  const currentFlow = flowLog.at(-1)?.state ?? classifyFlow(sample, connectionStatus);
  els.flowState.textContent = flowLabel(currentFlow);
  renderHeatmap();
}

chrome.storage.session.get([STORAGE_KEYS.sample, STORAGE_KEYS.status, STORAGE_KEYS.flowLog, STORAGE_KEYS.metricLog], (items) => {
  latestSample = items[STORAGE_KEYS.sample] ?? null;
  connectionStatus = items[STORAGE_KEYS.status] ?? null;
  flowLog = Array.isArray(items[STORAGE_KEYS.flowLog]) ? items[STORAGE_KEYS.flowLog] : [];
  metricLog = Array.isArray(items[STORAGE_KEYS.metricLog]) ? items[STORAGE_KEYS.metricLog] : [];
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
    if (changes[STORAGE_KEYS.metricLog]) {
      metricLog = Array.isArray(changes[STORAGE_KEYS.metricLog].newValue) ? changes[STORAGE_KEYS.metricLog].newValue : [];
    }
  }
  render();
});

els.endpointForm.addEventListener("submit", (event) => {
  event.preventDefault();

  const nextUrl = normalizeWsUrl(els.wsUrlInput.value);
  if (!nextUrl) {
    els.endpointMessage.textContent = "Use ws://, wss://, or a dashboard URL with ?ws=";
    return;
  }

  els.wsUrlInput.value = nextUrl;
  els.endpointMessage.textContent = "Reconnecting...";
  chrome.storage.local.set({ [STORAGE_KEYS.wsUrl]: nextUrl }, () => {
    chrome.runtime.sendMessage({ type: "restartConnection", wsUrl: nextUrl }, (response) => {
      if (chrome.runtime.lastError) {
        els.endpointMessage.textContent = "Saved; reopen extension if needed";
        return;
      }
      els.endpointMessage.textContent = response?.ok ? "Reconnected" : "Saved; reconnect failed";
    });
  });
});
