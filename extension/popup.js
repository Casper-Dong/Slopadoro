const DEFAULT_WS_URL = "ws://localhost:8765/";
const STORAGE_KEYS = {
  sample: "latestSample",
  status: "connectionStatus",
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
  eegDot: document.getElementById("eegDot"),
  ecgDot: document.getElementById("ecgDot"),
  emgDot: document.getElementById("emgDot")
};

let latestSample = null;
let connectionStatus = null;
let configuredWsUrl = DEFAULT_WS_URL;

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
  if (status?.state === "calibrating" || (status?.state === "connected" && sample?.calibrating)) {
    return "Calibrating baseline";
  }
  if (status?.state === "connected") {
    return `Live from ${stream}`;
  }
  if (status?.state === "connecting") {
    return `Connecting to ${stream}`;
  }
  if (status?.nextRetryMs) {
    return `Disconnected; retrying in ${Math.round(status.nextRetryMs / 1000)}s`;
  }
  return "Disconnected";
}

function render() {
  const sample = latestSample;
  const state = connectionStatus?.state ?? "disconnected";
  const calibrating = Boolean(state === "calibrating" || (state === "connected" && sample?.calibrating));
  const live = state === "connected" && !calibrating;

  els.statusText.textContent = statusText(sample, connectionStatus);
  els.calibratingLabel.hidden = !calibrating;
  setBar(els.focusValue, els.focusBar, sample?.focus, !live);
  setBar(els.fatigueValue, els.fatigueBar, sample?.fatigue, !live);
  setDot(els.eegDot, live && sample?.sources?.eeg);
  setDot(els.ecgDot, live && sample?.sources?.ecg);
  setDot(els.emgDot, live && sample?.sources?.emg);
}

chrome.storage.session.get([STORAGE_KEYS.sample, STORAGE_KEYS.status], (items) => {
  latestSample = items[STORAGE_KEYS.sample] ?? null;
  connectionStatus = items[STORAGE_KEYS.status] ?? null;
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
