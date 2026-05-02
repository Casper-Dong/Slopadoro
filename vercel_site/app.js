const historyFrames = [];
const maxHistory = 180;
let socket = null;
let reconnectTimer = null;
let latestFrame = null;
let activeUrl = "";

const colors = {
  focus: "#60a5fa",
  fatigue: "#fb923c",
  quality: "#4ade80",
  strain: "#c084fc",
  delta: "#a3a3a3",
  theta: "#f87171",
  alpha: "#34d399",
  beta: "#38bdf8",
  left: "#fbbf24",
  right: "#e879f9",
  hr: "#fb7185",
  rmssd: "#86efac"
};

const $ = (id) => document.getElementById(id);

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

function finiteNumber(value, fallback = 0) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function pct(value) {
  return Number.isFinite(value) ? String(Math.round(value)) : "--";
}

function isLocalHost(hostname) {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
}

function defaultStreamUrl() {
  if (isLocalHost(location.hostname)) {
    return "ws://localhost:8767/";
  }
  return "";
}

function queryStreamUrl() {
  const params = new URLSearchParams(location.search);
  return params.get("ws") || params.get("dashboardWs") || params.get("extensionWs") || defaultStreamUrl();
}

function normalizeWsUrl(value) {
  if (typeof value !== "string" || !value.trim()) {
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

function updateShareUrl(value) {
  const normalized = normalizeWsUrl(value);
  $("extensionUrl").textContent = normalized || "--";
  if (normalized) {
    const next = new URL(location.href);
    next.searchParams.set("ws", normalized);
    history.replaceState(null, "", next);
  }
}

function normalizeIncoming(raw) {
  if (!raw || typeof raw !== "object") {
    return null;
  }

  if (raw.scores && typeof raw.scores === "object" && "focus" in raw.scores) {
    return {
      timestamp: finiteNumber(raw.timestamp, finiteNumber(raw.ts, Date.now() / 1000)),
      state: raw.state || raw.subscores?.score_state || "live",
      reason: raw.reason || raw.subscores?.demo_reason || "dashboard_stream",
      scores: {
        focus: finiteNumber(raw.scores.focus, null),
        fatigue: finiteNumber(raw.scores.fatigue, null),
        quality: finiteNumber(raw.scores.quality, null),
        strain: finiteNumber(raw.scores.strain, null),
        recovery: finiteNumber(raw.scores.recovery, null)
      },
      sources: raw.sources || { eeg: false, ecg: false, emg: false },
      validity: raw.validity || { bad_channels: [] },
      eeg_bands: raw.eeg_bands || { delta: 0, theta: 0, alpha: 0, beta: 0 },
      emg: raw.emg || {
        left: finiteNumber(raw.subscores?.emg_left_derivative_strain_0_100, 0),
        right: finiteNumber(raw.subscores?.emg_right_derivative_strain_0_100, 0),
        mode: raw.subscores?.emg_strain_mode || "extension_stream"
      },
      ecg: raw.ecg || { hr: null, rmssd: null },
      raw: raw.raw || {}
    };
  }

  if (raw.scores && typeof raw.scores === "object" && "focus_score_0_100" in raw.scores) {
    return {
      timestamp: finiteNumber(raw.timestamp, Date.now() / 1000),
      state: raw.state || "live",
      reason: raw.explanation?.primary || "score_stream",
      scores: {
        focus: finiteNumber(raw.scores.focus_score_0_100, null),
        fatigue: finiteNumber(raw.scores.fatigue_drift_score_0_100, null),
        quality: finiteNumber(raw.scores.signal_quality_score_0_100, null),
        strain: finiteNumber(raw.scores.emg_strain_score_0_100, null),
        recovery: finiteNumber(raw.scores.recovery_context_score_0_100, null)
      },
      sources: raw.sources || {
        eeg: !raw.flags?.openbci_missing,
        ecg: !raw.flags?.polar_missing,
        emg: !raw.flags?.openbci_missing
      },
      validity: { bad_channels: raw.validity?.bad_channels || [] },
      eeg_bands: { delta: 0, theta: 0, alpha: 0, beta: 0 },
      emg: { left: null, right: null, mode: "score_stream" },
      ecg: { hr: null, rmssd: null },
      raw: {}
    };
  }

  if ("focus" in raw || "fatigue" in raw) {
    const subscores = raw.subscores && typeof raw.subscores === "object" ? raw.subscores : {};
    return {
      timestamp: finiteNumber(raw.ts, Date.now() / 1000),
      state: subscores.score_state || (raw.calibrating ? "calibrating" : "live"),
      reason: subscores.demo_reason || "extension_stream",
      scores: {
        focus: raw.focus === null ? null : finiteNumber(raw.focus, 0) * 100,
        fatigue: raw.fatigue === null ? null : finiteNumber(raw.fatigue, 0) * 100,
        quality: finiteNumber(subscores.signal_quality_score_0_100, null),
        strain: finiteNumber(subscores.emg_strain_score_0_100, null),
        recovery: finiteNumber(subscores.recovery_context_score_0_100, null)
      },
      sources: raw.sources || { eeg: false, ecg: false, emg: false },
      validity: { bad_channels: [] },
      eeg_bands: { delta: 0, theta: 0, alpha: 0, beta: 0 },
      emg: {
        left: finiteNumber(subscores.emg_left_derivative_strain_0_100, null),
        right: finiteNumber(subscores.emg_right_derivative_strain_0_100, null),
        left_ratio: finiteNumber(subscores.emg_derivative_left_ratio, 0),
        right_ratio: finiteNumber(subscores.emg_derivative_right_ratio, 0),
        mode: subscores.emg_strain_mode || "extension_stream"
      },
      ecg: { hr: null, rmssd: null },
      raw: {}
    };
  }

  return null;
}

function pushFrame(raw) {
  const frame = normalizeIncoming(raw);
  if (!frame) {
    return;
  }
  latestFrame = frame;
  historyFrames.push(frame);
  while (historyFrames.length > maxHistory) {
    historyFrames.shift();
  }
  $("status").textContent = `${frame.state} | ${new Date().toLocaleTimeString()}`;
  $("focus").textContent = pct(frame.scores.focus);
  $("fatigue").textContent = pct(frame.scores.fatigue);
  $("strain").textContent = pct(frame.scores.strain);
  $("quality").textContent = pct(frame.scores.quality);
  renderDetail(frame);
  draw();
}

function renderDetail(frame) {
  const raw = frame.raw || {};
  const eegCount = raw.eeg?.channels?.length || 0;
  const emgCount = raw.emg?.channels?.length || 0;
  const ecgCount = raw.ecg?.samples?.length || 0;
  $("detail").textContent = [
    `stream: ${activeUrl || "--"}`,
    `state: ${frame.state}`,
    `sources: EEG=${Boolean(frame.sources.eeg)} ECG=${Boolean(frame.sources.ecg)} EMG=${Boolean(frame.sources.emg)}`,
    `bad channels: ${(frame.validity.bad_channels || []).join(", ") || "none"}`,
    `artifact fraction: ${Number(frame.validity.artifact_fraction || 0).toFixed(3)}`,
    `emg mode: ${frame.emg?.mode || "--"} L=${Number(frame.emg?.left_ratio || 0).toFixed(2)}x R=${Number(frame.emg?.right_ratio || 0).toFixed(2)}x`,
    `raw: EEG=${eegCount}ch EMG=${emgCount}ch ECG=${ecgCount}pts`,
    `reason: ${frame.reason}`
  ].join("\n");
}

function connect(urlValue) {
  const normalized = normalizeWsUrl(urlValue);
  if (!normalized) {
    $("status").textContent = "enter a ws:// or wss:// stream";
    return;
  }
  if (location.protocol === "https:" && normalized.startsWith("ws://") && !normalized.includes("localhost")) {
    $("status").textContent = "https pages need a wss:// stream";
    return;
  }

  if (socket) {
    socket.onopen = null;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    socket.close();
    socket = null;
  }
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  activeUrl = normalized;
  updateShareUrl(normalized);
  $("wsUrl").value = normalized;
  $("status").textContent = `connecting to ${normalized}`;

  socket = new WebSocket(normalized);
  socket.onopen = () => {
    $("status").textContent = `connected | ${new Date().toLocaleTimeString()}`;
  };
  socket.onmessage = (event) => {
    try {
      pushFrame(JSON.parse(event.data));
    } catch {
      $("status").textContent = "ignoring malformed frame";
    }
  };
  socket.onerror = () => {
    $("status").textContent = "stream error";
  };
  socket.onclose = () => {
    $("status").textContent = "disconnected; retrying";
    reconnectTimer = setTimeout(() => connect(activeUrl), 1200);
  };
}

function median(values) {
  const finite = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!finite.length) {
    return 0;
  }
  const midpoint = Math.floor(finite.length / 2);
  return finite.length % 2 ? finite[midpoint] : (finite[midpoint - 1] + finite[midpoint]) / 2;
}

function span(values) {
  const finite = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (finite.length < 2) {
    return 1;
  }
  return finite[Math.floor(finite.length * 0.95)] - finite[Math.floor(finite.length * 0.05)];
}

function prepCanvas(id) {
  const canvas = $(id);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(canvas.clientWidth * dpr));
  canvas.height = Math.max(1, Math.floor(canvas.clientHeight * dpr));
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  return { canvas, ctx, w: canvas.clientWidth, h: canvas.clientHeight };
}

function drawLineChart(id, series, yMin, yMax) {
  const { ctx, w, h } = prepCanvas(id);
  const pad = 24;
  ctx.strokeStyle = "#333333";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const y = pad + (i / 4) * (h - pad * 2);
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(w - 8, y);
    ctx.stroke();
  }

  series.forEach((spec, specIndex) => {
    ctx.strokeStyle = spec.color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    let drawing = false;
    historyFrames.forEach((frame, i) => {
      const value = spec.get(frame);
      if (!Number.isFinite(value)) {
        drawing = false;
        return;
      }
      const x = pad + (i / Math.max(1, maxHistory - 1)) * (w - pad - 12);
      const bounded = clamp(value, yMin, yMax);
      const y = h - pad - ((bounded - yMin) / Math.max(1, yMax - yMin)) * (h - pad * 2);
      if (!drawing) {
        ctx.moveTo(x, y);
        drawing = true;
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.stroke();
    ctx.fillStyle = spec.color;
    ctx.fillText(spec.name, w - 92, 18 + specIndex * 16);
  });
}

function drawRawStacked(id, raw) {
  const { ctx, w, h } = prepCanvas(id);
  if (!raw || !Array.isArray(raw.channels) || !raw.channels.length) {
    ctx.fillStyle = "#8a8a8a";
    ctx.fillText("waiting for raw stream", 16, 24);
    return;
  }
  const labels = raw.labels || [];
  const channels = raw.channels.map((channel) => channel.map(Number));
  const spacing = Math.max(1, median(channels.map(span).filter(Number.isFinite)) * 1.6);
  const pad = 20;
  channels.forEach((channel, idx) => {
    const center = median(channel);
    const offset = pad + idx * ((h - pad * 2) / Math.max(1, channels.length - 1));
    ctx.strokeStyle = idx % 2 ? colors.beta : colors.alpha;
    ctx.lineWidth = 1;
    ctx.beginPath();
    channel.forEach((value, i) => {
      const x = pad + (i / Math.max(1, channel.length - 1)) * (w - pad - 10);
      const y = offset - ((value - center) / spacing) * 16;
      if (i === 0) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.stroke();
    ctx.fillStyle = "#8a8a8a";
    ctx.fillText(labels[idx] || String(idx + 1), 4, offset + 4);
  });
}

function drawRawSingle(id, raw) {
  const { ctx, w, h } = prepCanvas(id);
  const values = raw?.samples?.map(Number) || [];
  if (!values.length) {
    ctx.fillStyle = "#8a8a8a";
    ctx.fillText("waiting for raw stream", 16, 24);
    return;
  }
  const center = median(values);
  const scale = Math.max(1, span(values));
  const pad = 20;
  ctx.strokeStyle = colors.hr;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  values.forEach((value, i) => {
    const x = pad + (i / Math.max(1, values.length - 1)) * (w - pad - 10);
    const y = h / 2 - ((value - center) / scale) * (h - pad * 2);
    if (i === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();
}

function draw() {
  drawLineChart("scores", [
    { name: "focus", color: colors.focus, get: (frame) => frame.scores.focus },
    { name: "fatigue", color: colors.fatigue, get: (frame) => frame.scores.fatigue },
    { name: "quality", color: colors.quality, get: (frame) => frame.scores.quality },
    { name: "strain", color: colors.strain, get: (frame) => frame.scores.strain }
  ], 0, 100);
  drawLineChart("bands", [
    { name: "delta", color: colors.delta, get: (frame) => frame.eeg_bands.delta },
    { name: "theta", color: colors.theta, get: (frame) => frame.eeg_bands.theta },
    { name: "alpha", color: colors.alpha, get: (frame) => frame.eeg_bands.alpha },
    { name: "beta", color: colors.beta, get: (frame) => frame.eeg_bands.beta }
  ], 0, 100);
  drawLineChart("emg", [
    { name: "left", color: colors.left, get: (frame) => frame.emg.left },
    { name: "right", color: colors.right, get: (frame) => frame.emg.right },
    { name: "total", color: colors.strain, get: (frame) => frame.scores.strain }
  ], 0, 100);
  drawLineChart("ecg", [
    { name: "HR", color: colors.hr, get: (frame) => frame.ecg.hr },
    { name: "RMSSD", color: colors.rmssd, get: (frame) => frame.ecg.rmssd }
  ], 0, 140);
  drawRawStacked("rawEeg", latestFrame?.raw?.eeg);
  drawRawStacked("rawEmg", latestFrame?.raw?.emg);
  drawRawSingle("rawEcg", latestFrame?.raw?.ecg);
}

$("streamForm").addEventListener("submit", (event) => {
  event.preventDefault();
  connect($("wsUrl").value);
});

$("copyStreamUrl").addEventListener("click", async () => {
  const normalized = normalizeWsUrl($("wsUrl").value);
  if (!normalized) {
    $("status").textContent = "no stream URL to copy";
    return;
  }
  await navigator.clipboard.writeText(normalized);
  $("status").textContent = "stream URL copied";
});

addEventListener("resize", draw);

const initialUrl = queryStreamUrl();
$("wsUrl").value = initialUrl;
updateShareUrl(initialUrl);
if (normalizeWsUrl(initialUrl)) {
  connect(initialUrl);
}
