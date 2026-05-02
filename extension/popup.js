const STORAGE_KEYS = {
  sample: "latestSample",
  status: "connectionStatus"
};

const MAX_HISTORY = 240;
const NEUTRAL_VALUE = 0.5;

const els = {
  connectionText: document.getElementById("connectionText"),
  statusDot: document.getElementById("statusDot"),
  focusValue: document.getElementById("focusValue"),
  fatigueValue: document.getElementById("fatigueValue"),
  focusBar: document.getElementById("focusBar"),
  fatigueBar: document.getElementById("fatigueBar"),
  sparkline: document.getElementById("sparkline"),
  sourceEeg: document.getElementById("sourceEeg"),
  sourceEcg: document.getElementById("sourceEcg"),
  sourceEmg: document.getElementById("sourceEmg"),
  subscores: document.getElementById("subscores")
};

let latestSample = null;
let connectionStatus = null;
let lastHistoryTs = null;
let history = [];

function clamp01(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return null;
  }
  return Math.min(1, Math.max(0, value));
}

function formatPercent(value) {
  const bounded = clamp01(value);
  return bounded === null ? "--" : String(Math.round(bounded * 100));
}

function displayValue(value, neutralWhenNull = false) {
  const bounded = clamp01(value);
  return bounded === null ? (neutralWhenNull ? NEUTRAL_VALUE : null) : bounded;
}

function setMetric(valueEl, barEl, value, neutral) {
  const bounded = displayValue(value, neutral);
  valueEl.textContent = neutral || bounded === null ? "--" : formatPercent(bounded);
  barEl.style.width = `${Math.round((bounded ?? 0) * 100)}%`;
}

function setSource(el, isActive) {
  el.classList.toggle("active", Boolean(isActive));
}

function renderSubscores(subscores = {}) {
  const entries = Object.entries(subscores);
  els.subscores.replaceChildren();

  if (entries.length === 0) {
    const term = document.createElement("dt");
    term.textContent = "No subscores";
    const value = document.createElement("dd");
    value.textContent = "--";
    els.subscores.append(term, value);
    return;
  }

  for (const [name, rawValue] of entries) {
    const term = document.createElement("dt");
    term.textContent = name;
    const value = document.createElement("dd");
    value.textContent = typeof rawValue === "number" ? rawValue.toFixed(2) : "null";
    els.subscores.append(term, value);
  }
}

function statusLabel(sample, status) {
  const state = status?.state ?? "disconnected";
  if (state === "calibrating" || sample?.calibrating) {
    return "Calibrating baseline";
  }
  if (state === "connected") {
    return "Live from localhost:8765";
  }
  if (state === "connecting") {
    return "Connecting to localhost:8765";
  }
  if (status?.nextRetryMs) {
    return `Disconnected; retrying in ${Math.round(status.nextRetryMs / 1000)}s`;
  }
  return "Disconnected";
}

function pushHistory(sample) {
  if (!sample || sample.ts === lastHistoryTs) {
    return;
  }

  lastHistoryTs = sample.ts;
  history.push({
    focus: clamp01(sample.focus),
    fatigue: clamp01(sample.fatigue)
  });

  if (history.length > MAX_HISTORY) {
    history = history.slice(history.length - MAX_HISTORY);
  }
}

function resizeCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const targetWidth = Math.round(width * dpr);
  const targetHeight = Math.round(height * dpr);

  if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
    canvas.width = targetWidth;
    canvas.height = targetHeight;
  }

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, width, height };
}

function drawSeries(ctx, points, width, height, color) {
  if (points.length === 0) {
    return;
  }

  ctx.beginPath();
  points.forEach((point, index) => {
    const x = points.length === 1 ? width - 8 : 8 + (index / (points.length - 1)) * (width - 16);
    const y = 8 + (1 - point) * (height - 16);
    if (index === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke();
}

function drawSparkline() {
  const { ctx, width, height } = resizeCanvas(els.sparkline);
  ctx.clearRect(0, 0, width, height);

  ctx.strokeStyle = "#e5e7eb";
  ctx.lineWidth = 1;
  for (const fraction of [0.25, 0.5, 0.75]) {
    const y = Math.round(height * fraction) + 0.5;
    ctx.beginPath();
    ctx.moveTo(8, y);
    ctx.lineTo(width - 8, y);
    ctx.stroke();
  }

  const focus = history.map((point) => point.focus).filter((value) => value !== null);
  const fatigue = history.map((point) => point.fatigue).filter((value) => value !== null);
  drawSeries(ctx, focus, width, height, "#2563eb");
  drawSeries(ctx, fatigue, width, height, "#c2410c");
}

function render() {
  const sample = latestSample;
  const state = sample?.calibrating ? "calibrating" : connectionStatus?.state ?? "disconnected";
  const neutral = state !== "connected";

  els.connectionText.textContent = statusLabel(sample, connectionStatus);
  els.statusDot.className = `status-dot ${state}`;

  setMetric(els.focusValue, els.focusBar, sample?.focus, neutral || sample?.focus === null);
  setMetric(els.fatigueValue, els.fatigueBar, sample?.fatigue, neutral || sample?.fatigue === null);

  setSource(els.sourceEeg, state !== "disconnected" && sample?.sources?.eeg);
  setSource(els.sourceEcg, state !== "disconnected" && sample?.sources?.ecg);
  setSource(els.sourceEmg, state !== "disconnected" && sample?.sources?.emg);
  renderSubscores(sample?.subscores);

  pushHistory(sample);
  drawSparkline();
}

chrome.storage.session.get([STORAGE_KEYS.sample, STORAGE_KEYS.status], (items) => {
  latestSample = items[STORAGE_KEYS.sample] ?? null;
  connectionStatus = items[STORAGE_KEYS.status] ?? null;
  render();
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== "session") {
    return;
  }

  if (changes[STORAGE_KEYS.sample]) {
    latestSample = changes[STORAGE_KEYS.sample].newValue ?? null;
  }
  if (changes[STORAGE_KEYS.status]) {
    connectionStatus = changes[STORAGE_KEYS.status].newValue ?? null;
  }

  render();
});

window.addEventListener("resize", drawSparkline);
