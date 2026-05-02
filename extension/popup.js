const DEFAULT_WS_URL = "ws://localhost:8765/";
const STORAGE_KEYS = {
  sample: "latestSample",
  status: "connectionStatus",
  flowLog: "flowLog",
  metricLog: "metricLog",
  gateEnabled: "gateEnabled",
  gateBlocklist: "gateBlocklist",
  wsUrl: "wsUrl",
  focusTestActive: "focusTestActive",
  focusTestRuns: "focusTestRuns"
};

const DEFAULT_GATE_BLOCKLIST = [
  "reddit.com",
  "x.com",
  "twitter.com",
  "news.ycombinator.com",
  "youtube.com",
  "instagram.com",
  "tiktok.com"
];

const CHART_W = 240;
const CHART_H = 64;
const CHART_PAD_X = 5;
const CHART_PAD_Y = 5;
const CHART_VISIBLE_COUNT = 90;
const GATE_FOCUS_THRESHOLD = 0.38;
const GATE_FATIGUE_THRESHOLD = 0.72;
const GATE_SAMPLE_FRESH_MS = 10000;

const els = {
  statusText: document.getElementById("statusText"),
  calibratingLabel: document.getElementById("calibratingLabel"),
  endpointForm: document.getElementById("endpointForm"),
  wsUrlInput: document.getElementById("wsUrlInput"),
  endpointMessage: document.getElementById("endpointMessage"),
  gateForm: document.getElementById("gateForm"),
  gateEnabledInput: document.getElementById("gateEnabledInput"),
  gateBlocklistInput: document.getElementById("gateBlocklistInput"),
  gateCurrentSiteButton: document.getElementById("gateCurrentSiteButton"),
  gateMessage: document.getElementById("gateMessage"),
  gateStatus: document.getElementById("gateStatus"),
  gateSiteStatus: document.getElementById("gateSiteStatus"),
  focusTestStatus: document.getElementById("focusTestStatus"),
  focusTestForm: document.getElementById("focusTestForm"),
  focusTestNameInput: document.getElementById("focusTestNameInput"),
  focusTestVariantInput: document.getElementById("focusTestVariantInput"),
  focusTestScopeInput: document.getElementById("focusTestScopeInput"),
  startFocusTestButton: document.getElementById("startFocusTestButton"),
  stopFocusTestButton: document.getElementById("stopFocusTestButton"),
  clearFocusTestsButton: document.getElementById("clearFocusTestsButton"),
  exportFocusTestsButton: document.getElementById("exportFocusTestsButton"),
  focusTestMessage: document.getElementById("focusTestMessage"),
  abResults: document.getElementById("abResults"),
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
let gateEnabled = true;
let gateBlocklist = DEFAULT_GATE_BLOCKLIST.join("\n");
let currentTabHost = null;
let focusTestActive = null;
let focusTestRuns = [];

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

function saveLocal(update, messageEl) {
  chrome.storage.local.set(update, () => {
    if (chrome.runtime.lastError) {
      messageEl.textContent = chrome.runtime.lastError.message;
      return;
    }
    messageEl.textContent = "Saved";
  });
}

function sendMessage(message, callback) {
  chrome.runtime.sendMessage(message, (response) => {
    if (chrome.runtime.lastError) {
      callback(null, chrome.runtime.lastError.message);
      return;
    }
    callback(response, null);
  });
}

function percent(value) {
  const bounded = clamp01(value);
  return bounded === null ? "--" : String(Math.round(bounded * 100));
}

function aggregateAverage(aggregate, key) {
  if (!aggregate?.sampleCount || !Number.isFinite(aggregate[key])) {
    return null;
  }
  return aggregate[key] / aggregate.sampleCount;
}

function testDurationText(aggregate) {
  if (!aggregate?.sampleCount) {
    return "0s";
  }

  const start = aggregate.firstSampleAt ?? aggregate.startedAt;
  const end = aggregate.lastSampleAt ?? aggregate.endedAt;
  const seconds = start && end ? Math.max(aggregate.sampleCount, Math.round((end - start) / 1000)) : aggregate.sampleCount;
  if (seconds >= 60) {
    return `${Math.round(seconds / 60)}m`;
  }
  return `${seconds}s`;
}

function siteLabel(value) {
  return value || "all sites";
}

function isoTime(value) {
  return typeof value === "number" ? new Date(value).toISOString() : "";
}

function csvNumber(value, digits = 4) {
  return Number.isFinite(value) ? Number(value.toFixed(digits)) : "";
}

function csvCell(value) {
  const text = String(value ?? "");
  return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function csvLine(values) {
  return values.map(csvCell).join(",");
}

function focusTestCsvRows(runs) {
  const rows = [];

  for (const run of Array.isArray(runs) ? runs : []) {
    if (!run || typeof run !== "object") {
      continue;
    }

    const sites = run.sites && typeof run.sites === "object" ? run.sites : {};
    for (const [site, aggregate] of Object.entries(sites)) {
      const count = aggregate?.sampleCount ?? 0;
      if (!count) {
        continue;
      }

      const durationMs = (aggregate.lastSampleAt ?? run.endedAt ?? 0) - (aggregate.firstSampleAt ?? run.startedAt ?? 0);
      rows.push({
        exportedAt: isoTime(Date.now()),
        testId: run.id ?? "",
        test: run.label ?? "Focus test",
        variant: run.variant ?? "A",
        targetSite: run.targetSite ?? "",
        site,
        runStartedAt: isoTime(run.startedAt),
        runEndedAt: isoTime(run.endedAt),
        firstSampleAt: isoTime(aggregate.firstSampleAt),
        lastSampleAt: isoTime(aggregate.lastSampleAt),
        sampleCount: count,
        durationSeconds: Math.max(count, Math.round(durationMs / 1000)),
        avgFocus: csvNumber((aggregate.focusSum ?? 0) / count),
        avgFatigue: csvNumber((aggregate.fatigueSum ?? 0) / count),
        focusRating: csvNumber((aggregate.ratingSum ?? 0) / count),
        lowFocusRate: csvNumber((aggregate.lowFocusCount ?? 0) / count),
        highFatigueRate: csvNumber((aggregate.highFatigueCount ?? 0) / count),
        minFocus: csvNumber(aggregate.minFocus),
        maxFocus: csvNumber(aggregate.maxFocus)
      });
    }
  }

  return rows;
}

function exportFocusTestsCsv() {
  const rows = focusTestCsvRows(focusTestRuns);
  if (!rows.length) {
    els.focusTestMessage.textContent = "No A/B test data to export";
    return;
  }

  const headers = [
    "exported_at",
    "test_id",
    "test",
    "variant",
    "target_site",
    "site",
    "run_started_at",
    "run_ended_at",
    "first_sample_at",
    "last_sample_at",
    "sample_count",
    "duration_seconds",
    "avg_focus",
    "avg_fatigue",
    "focus_rating",
    "low_focus_rate",
    "high_fatigue_rate",
    "min_focus",
    "max_focus"
  ];

  const lines = [
    csvLine(headers),
    ...rows.map((row) => csvLine([
      row.exportedAt,
      row.testId,
      row.test,
      row.variant,
      row.targetSite,
      row.site,
      row.runStartedAt,
      row.runEndedAt,
      row.firstSampleAt,
      row.lastSampleAt,
      row.sampleCount,
      row.durationSeconds,
      row.avgFocus,
      row.avgFatigue,
      row.focusRating,
      row.lowFocusRate,
      row.highFatigueRate,
      row.minFocus,
      row.maxFocus
    ]))
  ];

  const blob = new Blob([`${lines.join("\n")}\n`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const stamp = new Date().toISOString().slice(0, 19).replaceAll(":", "-");
  link.href = url;
  link.download = `tabbi-focus-tests-${stamp}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  els.focusTestMessage.textContent = `Exported ${rows.length} CSV row${rows.length === 1 ? "" : "s"}`;
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
    flow: "productivity",
    steady: "productivity",
    drifting: "productivity",
    break: "productivity",
    waiting: "waiting",
    calibrating: "waiting",
    offline: "offline"
  }[state] ?? "empty";
}

function productivityScore(entry) {
  const score = clamp01(entry?.score);
  if (score !== null) {
    return score;
  }

  const focus = clamp01(entry?.focus);
  const fatigue = clamp01(entry?.fatigue);
  if (focus === null || fatigue === null) {
    return null;
  }
  return focus * (1 - fatigue);
}

function productivityColor(value) {
  const score = clamp01(value) ?? 0;
  const lightness = 22 + score * 66;
  const alpha = 0.42 + score * 0.5;
  return `hsl(142 58% ${lightness.toFixed(1)}% / ${alpha.toFixed(2)})`;
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
    const score = productivityScore(entry);
    cell.className = `heat-cell ${state}`;
    if (score !== null && state === "productivity") {
      cell.style.backgroundColor = productivityColor(score);
    }
    const focus = clamp01(entry?.focus);
    const fatigue = clamp01(entry?.fatigue);
    cell.title = `${flowLabel(entry?.state)} · focus ${focus === null ? "--" : Math.round(focus * 100)} · fatigue ${fatigue === null ? "--" : Math.round(fatigue * 100)}`;
    fragment.appendChild(cell);
  }

  els.flowHeatmap.replaceChildren(fragment);
}

function aggregateFocusTests() {
  const groups = new Map();

  for (const run of Array.isArray(focusTestRuns) ? focusTestRuns : []) {
    if (!run || typeof run !== "object") {
      continue;
    }

    const sites = run.sites && typeof run.sites === "object" ? run.sites : {};
    for (const [site, aggregate] of Object.entries(sites)) {
      if (!aggregate?.sampleCount) {
        continue;
      }

      const key = `${run.label ?? "Focus test"}\n${site}\n${run.variant ?? "A"}`;
      const existing = groups.get(key) ?? {
        label: run.label ?? "Focus test",
        site,
        variant: run.variant ?? "A",
        sampleCount: 0,
        focusSum: 0,
        fatigueSum: 0,
        ratingSum: 0,
        lowFocusCount: 0,
        highFatigueCount: 0,
        firstSampleAt: null,
        lastSampleAt: null,
        updatedAt: 0
      };

      existing.sampleCount += aggregate.sampleCount;
      existing.focusSum += aggregate.focusSum ?? 0;
      existing.fatigueSum += aggregate.fatigueSum ?? 0;
      existing.ratingSum += aggregate.ratingSum ?? 0;
      existing.lowFocusCount += aggregate.lowFocusCount ?? 0;
      existing.highFatigueCount += aggregate.highFatigueCount ?? 0;
      existing.firstSampleAt = existing.firstSampleAt === null
        ? aggregate.firstSampleAt ?? null
        : Math.min(existing.firstSampleAt, aggregate.firstSampleAt ?? existing.firstSampleAt);
      existing.lastSampleAt = Math.max(existing.lastSampleAt ?? 0, aggregate.lastSampleAt ?? 0) || null;
      existing.updatedAt = Math.max(existing.updatedAt, aggregate.lastSampleAt ?? run.endedAt ?? run.startedAt ?? 0);
      groups.set(key, existing);
    }
  }

  return [...groups.values()].sort((a, b) => b.updatedAt - a.updatedAt);
}

function renderFocusTestStatus() {
  if (!focusTestActive) {
    els.focusTestStatus.textContent = "Off";
    els.stopFocusTestButton.disabled = true;
    els.startFocusTestButton.disabled = false;
    return;
  }

  const site = focusTestActive.targetSite ? siteLabel(focusTestActive.targetSite) : "all sites";
  const samples = focusTestActive.sampleCount ?? 0;
  els.focusTestStatus.textContent = `${focusTestActive.variant ?? "A"} · ${samples}s`;
  els.focusTestStatus.title = `${focusTestActive.label ?? "Focus test"} on ${site}`;
  els.stopFocusTestButton.disabled = false;
  els.startFocusTestButton.disabled = true;
}

function renderAbResults() {
  const rows = aggregateFocusTests().slice(0, 8);
  const fragment = document.createDocumentFragment();

  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "ab-empty";
    empty.textContent = "Start a focus test to compare variants by site.";
    fragment.appendChild(empty);
    els.abResults.replaceChildren(fragment);
    return;
  }

  for (const row of rows) {
    const card = document.createElement("article");
    card.className = "ab-row";

    const meta = document.createElement("div");
    meta.className = "ab-meta";
    const title = document.createElement("strong");
    title.textContent = `${row.label} · ${row.variant}`;
    const sub = document.createElement("span");
    sub.textContent = `${row.site} · ${testDurationText(row)}`;
    meta.append(title, sub);

    const score = document.createElement("strong");
    score.className = "ab-score";
    score.textContent = percent(aggregateAverage(row, "ratingSum"));

    const details = document.createElement("span");
    details.className = "ab-detail";
    const lowFocusRate = row.sampleCount ? row.lowFocusCount / row.sampleCount : null;
    details.textContent = `F ${percent(aggregateAverage(row, "focusSum"))} · T ${percent(aggregateAverage(row, "fatigueSum"))} · low ${percent(lowFocusRate)}`;

    card.append(meta, score, details);
    fragment.appendChild(card);
  }

  els.abResults.replaceChildren(fragment);
}

function renderFocusTests() {
  renderFocusTestStatus();
  renderAbResults();
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

function normalizeBlocklistText(value) {
  const rows = String(value ?? "")
    .split(/\n|,/)
    .map((row) => row.trim().toLowerCase())
    .filter(Boolean)
    .map((row) => {
      try {
        return new URL(row.includes("://") ? row : `https://${row}`).hostname;
      } catch {
        return row.replace(/^(\*\.)?/, "").replace(/^www\./, "").replace(/\/.*$/, "");
      }
    })
    .map((host) => host.replace(/^www\./, ""))
    .filter(Boolean);

  return (rows.length ? [...new Set(rows)] : DEFAULT_GATE_BLOCKLIST).join("\n");
}

function hostInBlocklist(host, blocklistText) {
  const normalizedHost = String(host ?? "").toLowerCase().replace(/^www\./, "");
  if (!normalizedHost) {
    return false;
  }
  return normalizeBlocklistText(blocklistText)
    .split("\n")
    .some((entry) => normalizedHost === entry || normalizedHost.endsWith(`.${entry}`));
}

function updateGateSiteStatus() {
  if (!currentTabHost) {
    els.gateSiteStatus.textContent = "Current site: unavailable";
    if (!focusTestActive) {
      els.focusTestMessage.textContent = "Open a normal web page before starting a current-site test";
    }
    return;
  }

  els.gateSiteStatus.textContent = hostInBlocklist(currentTabHost, gateBlocklist)
    ? `Current site: ${currentTabHost} is gated`
    : `Current site: ${currentTabHost} is not gated`;
  if (!focusTestActive && !els.focusTestMessage.textContent) {
    els.focusTestMessage.textContent = `Current site: ${currentTabHost}`;
  }
}

function loadCurrentTabHost() {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (chrome.runtime.lastError || !tabs?.[0]?.url) {
      updateGateSiteStatus();
      return;
    }

    try {
      const url = new URL(tabs[0].url);
      if (url.protocol !== "http:" && url.protocol !== "https:") {
        updateGateSiteStatus();
        return;
      }
      currentTabHost = url.hostname.toLowerCase().replace(/^www\./, "");
    } catch {
      currentTabHost = null;
    }
    updateGateSiteStatus();
  });
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

function gateStatusText(sample, live) {
  if (!gateEnabled) {
    return "Gate status: off";
  }
  const sampleFresh = typeof sample?.ts === "number" && Math.abs(Date.now() - sample.ts * 1000) <= GATE_SAMPLE_FRESH_MS;
  if (!live && !sampleFresh) {
    return "Gate status: waiting for live focus";
  }

  const focus = clamp01(sample?.focus);
  const fatigue = clamp01(sample?.fatigue);
  const wouldGate = focus !== null && fatigue !== null && (focus <= GATE_FOCUS_THRESHOLD || fatigue >= GATE_FATIGUE_THRESHOLD);
  if (wouldGate) {
    return "Gate status: armed on blocklisted sites";
  }
  return "Gate status: sharp, not gating";
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
  els.gateStatus.textContent = gateStatusText(sample, live);
  renderHeatmap();
  renderFocusTests();
}

chrome.storage.session.get([
  STORAGE_KEYS.sample,
  STORAGE_KEYS.status,
  STORAGE_KEYS.flowLog,
  STORAGE_KEYS.metricLog,
  STORAGE_KEYS.focusTestActive
], (items) => {
  latestSample = items[STORAGE_KEYS.sample] ?? null;
  connectionStatus = items[STORAGE_KEYS.status] ?? null;
  flowLog = Array.isArray(items[STORAGE_KEYS.flowLog]) ? items[STORAGE_KEYS.flowLog] : [];
  metricLog = Array.isArray(items[STORAGE_KEYS.metricLog]) ? items[STORAGE_KEYS.metricLog] : [];
  focusTestActive = items[STORAGE_KEYS.focusTestActive] ?? null;
  render();
});

chrome.storage.local.get({ [STORAGE_KEYS.wsUrl]: DEFAULT_WS_URL }, (items) => {
  configuredWsUrl = normalizeWsUrl(items[STORAGE_KEYS.wsUrl]) ?? DEFAULT_WS_URL;
  els.wsUrlInput.value = configuredWsUrl;
  render();
});

chrome.storage.local.get({
  [STORAGE_KEYS.gateEnabled]: true,
  [STORAGE_KEYS.gateBlocklist]: DEFAULT_GATE_BLOCKLIST.join("\n")
}, (items) => {
  gateEnabled = Boolean(items[STORAGE_KEYS.gateEnabled]);
  gateBlocklist = normalizeBlocklistText(items[STORAGE_KEYS.gateBlocklist]);
  els.gateEnabledInput.checked = gateEnabled;
  els.gateBlocklistInput.value = gateBlocklist;
  updateGateSiteStatus();
});

chrome.storage.local.get({ [STORAGE_KEYS.focusTestRuns]: [] }, (items) => {
  focusTestRuns = Array.isArray(items[STORAGE_KEYS.focusTestRuns]) ? items[STORAGE_KEYS.focusTestRuns] : [];
  renderFocusTests();
});

sendMessage({ type: "getFocusTestState" }, (response) => {
  if (!response) {
    return;
  }
  focusTestActive = response.active ?? focusTestActive;
  focusTestRuns = Array.isArray(response.runs) ? response.runs : focusTestRuns;
  renderFocusTests();
});

loadCurrentTabHost();

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === "local" && changes[STORAGE_KEYS.wsUrl]) {
    configuredWsUrl = normalizeWsUrl(changes[STORAGE_KEYS.wsUrl].newValue) ?? DEFAULT_WS_URL;
    if (document.activeElement !== els.wsUrlInput) {
      els.wsUrlInput.value = configuredWsUrl;
    }
    els.endpointMessage.textContent = "Saved";
  }

  if (areaName === "local" && changes[STORAGE_KEYS.gateEnabled]) {
    gateEnabled = Boolean(changes[STORAGE_KEYS.gateEnabled].newValue);
    els.gateEnabledInput.checked = gateEnabled;
    els.gateMessage.textContent = "Saved";
  }

  if (areaName === "local" && changes[STORAGE_KEYS.gateBlocklist]) {
    gateBlocklist = normalizeBlocklistText(changes[STORAGE_KEYS.gateBlocklist].newValue);
    if (document.activeElement !== els.gateBlocklistInput) {
      els.gateBlocklistInput.value = gateBlocklist;
    }
    els.gateMessage.textContent = "Saved";
    updateGateSiteStatus();
  }

  if (areaName === "local" && changes[STORAGE_KEYS.focusTestRuns]) {
    focusTestRuns = Array.isArray(changes[STORAGE_KEYS.focusTestRuns].newValue) ? changes[STORAGE_KEYS.focusTestRuns].newValue : [];
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
    if (changes[STORAGE_KEYS.focusTestActive]) {
      focusTestActive = changes[STORAGE_KEYS.focusTestActive].newValue ?? null;
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
  saveLocal({ [STORAGE_KEYS.wsUrl]: nextUrl }, els.endpointMessage);
});

els.gateForm.addEventListener("submit", (event) => {
  event.preventDefault();

  gateEnabled = els.gateEnabledInput.checked;
  gateBlocklist = normalizeBlocklistText(els.gateBlocklistInput.value);
  els.gateBlocklistInput.value = gateBlocklist;
  els.gateMessage.textContent = "Saving...";
  saveLocal({
    [STORAGE_KEYS.gateEnabled]: gateEnabled,
    [STORAGE_KEYS.gateBlocklist]: gateBlocklist
  }, els.gateMessage);
});

els.gateCurrentSiteButton.addEventListener("click", () => {
  if (!currentTabHost) {
    els.gateMessage.textContent = "Open a normal web page first";
    return;
  }

  const existing = normalizeBlocklistText(els.gateBlocklistInput.value).split("\n");
  if (!hostInBlocklist(currentTabHost, existing.join("\n"))) {
    existing.push(currentTabHost);
  }

  gateEnabled = true;
  gateBlocklist = normalizeBlocklistText(existing.join("\n"));
  els.gateEnabledInput.checked = true;
  els.gateBlocklistInput.value = gateBlocklist;
  els.gateMessage.textContent = "Saving...";
  updateGateSiteStatus();
  saveLocal({
    [STORAGE_KEYS.gateEnabled]: gateEnabled,
    [STORAGE_KEYS.gateBlocklist]: gateBlocklist
  }, els.gateMessage);
});

els.focusTestForm.addEventListener("submit", (event) => {
  event.preventDefault();

  const label = els.focusTestNameInput.value.trim() || "Focus test";
  const variant = els.focusTestVariantInput.value.trim() || "A";
  const site = els.focusTestScopeInput.value === "current" ? currentTabHost : null;

  if (els.focusTestScopeInput.value === "current" && !site) {
    els.focusTestMessage.textContent = "Open a normal web page first";
    return;
  }

  els.focusTestMessage.textContent = "Starting...";
  sendMessage({ type: "startFocusTest", label, variant, site }, (response, error) => {
    if (error) {
      els.focusTestMessage.textContent = error;
      return;
    }

    focusTestActive = response?.active ?? focusTestActive;
    focusTestRuns = Array.isArray(response?.runs) ? response.runs : focusTestRuns;
    els.focusTestMessage.textContent = site ? `Tracking ${site}` : "Tracking all visible sites";
    renderFocusTests();
  });
});

els.stopFocusTestButton.addEventListener("click", () => {
  els.focusTestMessage.textContent = "Stopping...";
  sendMessage({ type: "stopFocusTest" }, (response, error) => {
    if (error) {
      els.focusTestMessage.textContent = error;
      return;
    }

    focusTestActive = null;
    focusTestRuns = Array.isArray(response?.runs) ? response.runs : focusTestRuns;
    els.focusTestMessage.textContent = "Stopped";
    renderFocusTests();
  });
});

els.clearFocusTestsButton.addEventListener("click", () => {
  els.focusTestMessage.textContent = "Clearing...";
  sendMessage({ type: "clearFocusTestHistory" }, (response, error) => {
    if (error) {
      els.focusTestMessage.textContent = error;
      return;
    }

    focusTestActive = null;
    focusTestRuns = Array.isArray(response?.runs) ? response.runs : [];
    els.focusTestMessage.textContent = "Cleared";
    renderFocusTests();
  });
});

els.exportFocusTestsButton.addEventListener("click", () => {
  chrome.storage.local.get({ [STORAGE_KEYS.focusTestRuns]: [] }, (items) => {
    focusTestRuns = Array.isArray(items[STORAGE_KEYS.focusTestRuns]) ? items[STORAGE_KEYS.focusTestRuns] : [];
    exportFocusTestsCsv();
    renderFocusTests();
  });
});
