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
const FOCUS_TEST_LOG_INTERVAL_MS = 1000;
const FOCUS_TEST_HISTORY_LIMIT = 80;
const ACTIVE_SITE_FRESH_MS = 45000;

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
let activeSite = null;
let activeFocusTest = null;
let lastFocusTestLogAtMs = 0;

if (chrome.storage.session.setAccessLevel) {
  chrome.storage.session.setAccessLevel({ accessLevel: "TRUSTED_AND_UNTRUSTED_CONTEXTS" });
}

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

function normalizeHost(value) {
  if (typeof value !== "string") {
    return null;
  }

  const trimmed = value.trim().toLowerCase();
  if (!trimmed) {
    return null;
  }

  try {
    return new URL(trimmed.includes("://") ? trimmed : `https://${trimmed}`).hostname.replace(/^www\./, "");
  } catch {
    const host = trimmed.replace(/^(\*\.)?/, "").replace(/^www\./, "").replace(/\/.*$/, "");
    return host || null;
  }
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

function focusTestRating(focus, fatigue) {
  return 0.65 * focus + 0.35 * (1 - fatigue);
}

function defaultAggregate() {
  return {
    sampleCount: 0,
    focusSum: 0,
    fatigueSum: 0,
    ratingSum: 0,
    lowFocusCount: 0,
    highFatigueCount: 0,
    minFocus: null,
    maxFocus: null,
    firstSampleAt: null,
    lastSampleAt: null
  };
}

function sanitizeFocusTestRun(run) {
  if (!run || typeof run !== "object") {
    return null;
  }

  return {
    id: String(run.id ?? `${Date.now()}`),
    label: String(run.label ?? "Focus test").trim() || "Focus test",
    variant: String(run.variant ?? "A").trim() || "A",
    targetSite: normalizeHost(run.targetSite) ?? null,
    startedAt: typeof run.startedAt === "number" ? run.startedAt : Date.now(),
    endedAt: typeof run.endedAt === "number" ? run.endedAt : null,
    sampleCount: Number.isFinite(run.sampleCount) ? run.sampleCount : 0,
    focusSum: Number.isFinite(run.focusSum) ? run.focusSum : 0,
    fatigueSum: Number.isFinite(run.fatigueSum) ? run.fatigueSum : 0,
    ratingSum: Number.isFinite(run.ratingSum) ? run.ratingSum : 0,
    lowFocusCount: Number.isFinite(run.lowFocusCount) ? run.lowFocusCount : 0,
    highFatigueCount: Number.isFinite(run.highFatigueCount) ? run.highFatigueCount : 0,
    firstSampleAt: typeof run.firstSampleAt === "number" ? run.firstSampleAt : null,
    lastSampleAt: typeof run.lastSampleAt === "number" ? run.lastSampleAt : null,
    sites: run.sites && typeof run.sites === "object" ? run.sites : {}
  };
}

function createFocusTestRun(message) {
  const label = String(message?.label ?? "").trim() || "Focus test";
  const variant = String(message?.variant ?? "").trim() || "A";
  const targetSite = normalizeHost(message?.site);
  const now = Date.now();

  return {
    id: `${now}-${Math.random().toString(16).slice(2)}`,
    label,
    variant,
    targetSite,
    startedAt: now,
    endedAt: null,
    sampleCount: 0,
    focusSum: 0,
    fatigueSum: 0,
    ratingSum: 0,
    lowFocusCount: 0,
    highFatigueCount: 0,
    firstSampleAt: null,
    lastSampleAt: null,
    sites: {}
  };
}

function usableActiveSite() {
  if (!activeSite?.visible || !activeSite.host) {
    return null;
  }
  if (Date.now() - activeSite.updatedAt > ACTIVE_SITE_FRESH_MS) {
    return null;
  }
  return activeSite.host;
}

function hostMatchesTarget(host, targetSite) {
  if (!targetSite) {
    return true;
  }
  return host === targetSite || host.endsWith(`.${targetSite}`);
}

function addSampleToAggregate(aggregate, focus, fatigue, now) {
  const next = { ...defaultAggregate(), ...(aggregate ?? {}) };
  const rating = focusTestRating(focus, fatigue);

  next.sampleCount += 1;
  next.focusSum += focus;
  next.fatigueSum += fatigue;
  next.ratingSum += rating;
  next.lowFocusCount += focus < 0.4 ? 1 : 0;
  next.highFatigueCount += fatigue >= 0.75 ? 1 : 0;
  next.minFocus = next.minFocus === null ? focus : Math.min(next.minFocus, focus);
  next.maxFocus = next.maxFocus === null ? focus : Math.max(next.maxFocus, focus);
  next.firstSampleAt ??= now;
  next.lastSampleAt = now;
  return next;
}

function updateFocusTestRun(run, sample, host, now) {
  const focus = clamp01(sample?.focus);
  const fatigue = clamp01(sample?.fatigue);
  if (focus === null || fatigue === null) {
    return null;
  }

  const site = normalizeHost(host);
  if (!site || !hostMatchesTarget(site, run.targetSite)) {
    return null;
  }

  const next = {
    ...run,
    sampleCount: run.sampleCount + 1,
    focusSum: run.focusSum + focus,
    fatigueSum: run.fatigueSum + fatigue,
    ratingSum: run.ratingSum + focusTestRating(focus, fatigue),
    lowFocusCount: run.lowFocusCount + (focus < 0.4 ? 1 : 0),
    highFatigueCount: run.highFatigueCount + (fatigue >= 0.75 ? 1 : 0),
    firstSampleAt: run.firstSampleAt ?? now,
    lastSampleAt: now,
    sites: { ...run.sites }
  };

  next.sites[site] = addSampleToAggregate(next.sites[site], focus, fatigue, now);
  return next;
}

function persistFocusTestRun(run) {
  chrome.storage.session.set({ [STORAGE_KEYS.focusTestActive]: run });
  chrome.storage.local.get({ [STORAGE_KEYS.focusTestRuns]: [] }, (items) => {
    const existing = Array.isArray(items[STORAGE_KEYS.focusTestRuns]) ? items[STORAGE_KEYS.focusTestRuns] : [];
    const withoutCurrent = existing.filter((entry) => entry?.id !== run.id);
    const next = withoutCurrent.concat(run).slice(-FOCUS_TEST_HISTORY_LIMIT);
    chrome.storage.local.set({ [STORAGE_KEYS.focusTestRuns]: next });
  });
}

function maybeLogFocusTest(sample, connectionState) {
  if (!activeFocusTest) {
    return;
  }

  const now = Date.now();
  if (now - lastFocusTestLogAtMs < FOCUS_TEST_LOG_INTERVAL_MS) {
    return;
  }

  const connected = connectionState === "connected" || connectionState === "calibrating";
  const focus = clamp01(sample?.focus);
  const fatigue = clamp01(sample?.fatigue);
  if (!connected || sample?.calibrating || sample?.sources?.eeg === false || focus === null || fatigue === null) {
    return;
  }

  const host = usableActiveSite();
  if (!host) {
    return;
  }

  const nextRun = updateFocusTestRun(activeFocusTest, sample, host, now);
  if (!nextRun) {
    return;
  }

  lastFocusTestLogAtMs = now;
  activeFocusTest = nextRun;
  persistFocusTestRun(nextRun);
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
  let title = `Monitor your flow state: waiting for ${shortWsUrl()}`;
  if (live) {
    title = `Monitor your flow state: focus ${text}, fatigue ${Math.round((fatigue ?? 0) * 100)}`;
  } else if (connected && !headsetReady) {
    title = "Monitor your flow state: waiting for headset";
  } else if (connected && calibrating) {
    title = "Monitor your flow state: calibrating";
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
  maybeLogFocusTest(detail.sample ?? null, state);
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

function sendFocusTestState(sendResponse) {
  chrome.storage.local.get({ [STORAGE_KEYS.focusTestRuns]: [] }, (localItems) => {
    sendResponse({
      active: activeFocusTest,
      activeSite,
      runs: Array.isArray(localItems[STORAGE_KEYS.focusTestRuns]) ? localItems[STORAGE_KEYS.focusTestRuns] : []
    });
  });
}

function startFocusTest(message, sendResponse) {
  const run = createFocusTestRun(message);
  activeFocusTest = run;
  lastFocusTestLogAtMs = 0;
  persistFocusTestRun(run);
  sendFocusTestState(sendResponse);
}

function stopFocusTest(sendResponse) {
  if (activeFocusTest) {
    activeFocusTest = { ...activeFocusTest, endedAt: Date.now() };
    chrome.storage.session.set({ [STORAGE_KEYS.focusTestActive]: null });
    chrome.storage.local.get({ [STORAGE_KEYS.focusTestRuns]: [] }, (items) => {
      const existing = Array.isArray(items[STORAGE_KEYS.focusTestRuns]) ? items[STORAGE_KEYS.focusTestRuns] : [];
      const withoutCurrent = existing.filter((entry) => entry?.id !== activeFocusTest.id);
      const next = withoutCurrent.concat(activeFocusTest).slice(-FOCUS_TEST_HISTORY_LIMIT);
      const stoppedRun = activeFocusTest;
      activeFocusTest = null;
      chrome.storage.local.set({ [STORAGE_KEYS.focusTestRuns]: next }, () => {
        sendResponse({ active: null, stopped: stoppedRun, runs: next, activeSite });
      });
    });
    return;
  }

  chrome.storage.session.set({ [STORAGE_KEYS.focusTestActive]: null }, () => {
    sendFocusTestState(sendResponse);
  });
}

function clearFocusTestHistory(sendResponse) {
  activeFocusTest = null;
  chrome.storage.session.set({ [STORAGE_KEYS.focusTestActive]: null });
  chrome.storage.local.set({ [STORAGE_KEYS.focusTestRuns]: [] }, () => {
    sendResponse({ active: null, runs: [], activeSite });
  });
}

function handleSitePresence(message, sender) {
  const host = normalizeHost(message?.host);
  if (!host) {
    return;
  }

  const tabId = typeof sender?.tab?.id === "number" ? sender.tab.id : null;
  const visible = Boolean(message.visible);
  if (!visible && activeSite?.tabId !== tabId) {
    return;
  }

  activeSite = {
    host,
    href: typeof message.href === "string" ? message.href.slice(0, 500) : "",
    visible,
    tabId,
    updatedAt: Date.now()
  };
}

chrome.runtime.onInstalled.addListener(loadConfigAndConnect);
chrome.runtime.onStartup.addListener(loadConfigAndConnect);
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "getGateState") {
    chrome.storage.session.get([STORAGE_KEYS.sample, STORAGE_KEYS.status], (sessionItems) => {
      chrome.storage.local.get({
        [STORAGE_KEYS.gateEnabled]: true,
        [STORAGE_KEYS.gateBlocklist]: DEFAULT_GATE_BLOCKLIST.join("\n")
      }, (localItems) => {
        sendResponse({
          sample: sessionItems[STORAGE_KEYS.sample] ?? unavailableSample(),
          status: sessionItems[STORAGE_KEYS.status] ?? { state: "disconnected", connected: false },
          gateEnabled: Boolean(localItems[STORAGE_KEYS.gateEnabled]),
          gateBlocklist: localItems[STORAGE_KEYS.gateBlocklist] ?? DEFAULT_GATE_BLOCKLIST.join("\n")
        });
      });
    });
    return true;
  }

  if (message?.type === "sitePresence") {
    handleSitePresence(message, sender);
    return false;
  }

  if (message?.type === "getFocusTestState") {
    sendFocusTestState(sendResponse);
    return true;
  }

  if (message?.type === "startFocusTest") {
    startFocusTest(message, sendResponse);
    return true;
  }

  if (message?.type === "stopFocusTest") {
    stopFocusTest(sendResponse);
    return true;
  }

  if (message?.type === "clearFocusTestHistory") {
    clearFocusTestHistory(sendResponse);
    return true;
  }

  return false;
});
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
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "restartConnection") {
    return false;
  }

  const nextUrl = normalizeWsUrl(message.wsUrl);
  if (!nextUrl) {
    sendResponse({ ok: false, error: "invalid websocket url" });
    return false;
  }

  wsUrl = nextUrl;
  restartConnection("manual reconnect");
  sendResponse({ ok: true, url: wsUrl });
  return false;
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RECONNECT_ALARM) {
    connect();
  }
});

chrome.storage.session.get({ [STORAGE_KEYS.focusTestActive]: null }, (items) => {
  activeFocusTest = sanitizeFocusTestRun(items[STORAGE_KEYS.focusTestActive]);
});

loadConfigAndConnect();
