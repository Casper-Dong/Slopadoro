const DEFAULT_GATE_BLOCKLIST = [
  "reddit.com",
  "x.com",
  "twitter.com",
  "news.ycombinator.com",
  "youtube.com",
  "instagram.com",
  "tiktok.com"
];

const GATE_KEYS = {
  enabled: "gateEnabled",
  blocklist: "gateBlocklist",
  sample: "latestSample",
  status: "connectionStatus"
};

const GATE_ROOT_ID = "slopadoro-distraction-gate-root";
const GATE_STYLE_ID = "slopadoro-distraction-gate-style";
const GATE_CLASS = "slopadoro-distraction-gate-active";
const GATE_POPUP_DELAY_MS = 4000;
const CONFIRM_DELAY_SECONDS = 5;
const SNOOZE_MS = 10 * 60 * 1000;

let enabled = true;
let blocklist = DEFAULT_GATE_BLOCKLIST;
let latestSample = null;
let connectionStatus = null;
let gateDelayTimer = null;
let countdownTimer = null;
let dismissedUntilMs = 0;
let lastPath = location.href;

function clamp01(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return null;
  }
  return Math.min(1, Math.max(0, value));
}

function parseBlocklist(value) {
  const rows = Array.isArray(value) ? value : String(value ?? "").split(/\n|,/);
  const parsed = rows
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

  return parsed.length ? [...new Set(parsed)] : DEFAULT_GATE_BLOCKLIST;
}

function hostMatches() {
  const host = location.hostname.toLowerCase().replace(/^www\./, "");
  return blocklist.some((entry) => host === entry || host.endsWith(`.${entry}`));
}

function shouldGate() {
  if (!enabled || !hostMatches() || Date.now() < dismissedUntilMs) {
    return false;
  }

  return true;
}

function injectStyle() {
  if (document.getElementById(GATE_STYLE_ID)) {
    return;
  }

  const style = document.createElement("style");
  style.id = GATE_STYLE_ID;
  style.textContent = `
    html.${GATE_CLASS} > body {
      filter: grayscale(1) saturate(0.18) contrast(0.9);
      transition: filter 180ms ease-out;
    }
    #${GATE_ROOT_ID} {
      position: fixed;
      inset: 0;
      z-index: 2147483646;
      display: grid;
      place-items: center;
      pointer-events: auto;
      background: rgba(15, 23, 42, 0.22);
      font: 13px/1.4 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    #${GATE_ROOT_ID} .slopadoro-gate-dialog {
      width: min(360px, calc(100vw - 32px));
      border: 1px solid rgba(15, 23, 42, 0.16);
      border-radius: 8px;
      padding: 18px;
      background: #ffffff;
      color: #111827;
      box-shadow: 0 24px 80px rgba(15, 23, 42, 0.32);
    }
    #${GATE_ROOT_ID} h2 {
      margin: 0 0 8px;
      font-size: 16px;
      letter-spacing: 0;
    }
    #${GATE_ROOT_ID} p {
      margin: 0 0 14px;
      color: #475569;
    }
    #${GATE_ROOT_ID} .slopadoro-gate-actions {
      display: flex;
      gap: 8px;
      justify-content: flex-end;
    }
    #${GATE_ROOT_ID} button {
      min-height: 32px;
      border: 0;
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
      font-weight: 700;
    }
    #${GATE_ROOT_ID} .slopadoro-gate-primary {
      background: #111827;
      color: #ffffff;
    }
    #${GATE_ROOT_ID} .slopadoro-gate-primary:disabled {
      background: #94a3b8;
      cursor: wait;
    }
    #${GATE_ROOT_ID} .slopadoro-gate-secondary {
      background: #e5e7eb;
      color: #111827;
    }
  `;
  document.documentElement.appendChild(style);
}

function removeGate() {
  document.documentElement.classList.remove(GATE_CLASS);
  document.getElementById(GATE_ROOT_ID)?.remove();
  if (gateDelayTimer !== null) {
    clearTimeout(gateDelayTimer);
    gateDelayTimer = null;
  }
  if (countdownTimer !== null) {
    clearInterval(countdownTimer);
    countdownTimer = null;
  }
}

function leavePage() {
  if (history.length > 1) {
    history.back();
  } else {
    location.href = "about:blank";
  }
}

function showGate() {
  if (document.getElementById(GATE_ROOT_ID)) {
    return;
  }

  injectStyle();
  document.documentElement.classList.add(GATE_CLASS);

  const root = document.createElement("div");
  root.id = GATE_ROOT_ID;
  root.innerHTML = `
    <section class="slopadoro-gate-dialog" role="dialog" aria-modal="true" aria-labelledby="slopadoro-gate-title">
      <h2 id="slopadoro-gate-title">Tabbi opened the distraction gate</h2>
      <p>This page is on your distraction list. Take five seconds before continuing.</p>
      <div class="slopadoro-gate-actions">
        <button type="button" class="slopadoro-gate-secondary">Leave</button>
        <button type="button" class="slopadoro-gate-primary" disabled>Continue in ${CONFIRM_DELAY_SECONDS}</button>
      </div>
    </section>
  `;

  const leaveButton = root.querySelector(".slopadoro-gate-secondary");
  const continueButton = root.querySelector(".slopadoro-gate-primary");
  let remaining = CONFIRM_DELAY_SECONDS;

  leaveButton.addEventListener("click", leavePage);
  continueButton.addEventListener("click", () => {
    dismissedUntilMs = Date.now() + SNOOZE_MS;
    removeGate();
  });

  countdownTimer = setInterval(() => {
    remaining -= 1;
    if (remaining > 0) {
      continueButton.textContent = `Continue in ${remaining}`;
      return;
    }

    clearInterval(countdownTimer);
    countdownTimer = null;
    continueButton.disabled = false;
    continueButton.textContent = "Continue";
    continueButton.focus();
  }, 1000);

  document.documentElement.appendChild(root);
}

function scheduleGate() {
  if (document.getElementById(GATE_ROOT_ID) || gateDelayTimer !== null) {
    return;
  }

  gateDelayTimer = setTimeout(() => {
    gateDelayTimer = null;
    if (shouldGate()) {
      showGate();
    }
  }, GATE_POPUP_DELAY_MS);
}

function evaluateGate() {
  if (lastPath !== location.href) {
    lastPath = location.href;
    dismissedUntilMs = 0;
    if (gateDelayTimer !== null) {
      clearTimeout(gateDelayTimer);
      gateDelayTimer = null;
    }
  }

  if (shouldGate()) {
    scheduleGate();
  } else {
    removeGate();
  }
}

function applyFatigueMessage(message) {
  latestSample = {
    focus: clamp01(message.focus),
    fatigue: clamp01(message.value),
    calibrating: Boolean(message.calibrating),
    sources: message.sources && typeof message.sources === "object" ? message.sources : { eeg: false, ecg: false, emg: false }
  };
  connectionStatus = { state: latestSample.calibrating ? "calibrating" : "connected" };
  evaluateGate();
}

function applySnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== "object") {
    return;
  }

  enabled = Boolean(snapshot.gateEnabled);
  blocklist = parseBlocklist(snapshot.gateBlocklist);
  latestSample = snapshot.sample ?? latestSample;
  connectionStatus = snapshot.status ?? connectionStatus;
  evaluateGate();
}

function requestGateSnapshot() {
  chrome.runtime.sendMessage({ type: "getGateState" }, (snapshot) => {
    if (chrome.runtime.lastError) {
      return;
    }
    applySnapshot(snapshot);
  });
}

function loadInitialState() {
  chrome.storage.local.get({
    [GATE_KEYS.enabled]: true,
    [GATE_KEYS.blocklist]: DEFAULT_GATE_BLOCKLIST.join("\n")
  }, (items) => {
    enabled = Boolean(items[GATE_KEYS.enabled]);
    blocklist = parseBlocklist(items[GATE_KEYS.blocklist]);
    evaluateGate();
  });

  chrome.storage.session.get([GATE_KEYS.sample, GATE_KEYS.status], (items) => {
    if (chrome.runtime.lastError || !items) {
      return;
    }
    latestSample = items[GATE_KEYS.sample] ?? latestSample;
    connectionStatus = items[GATE_KEYS.status] ?? connectionStatus;
    evaluateGate();
  });

  requestGateSnapshot();
}

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "fatigue") {
    applyFatigueMessage(message);
  }
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === "local") {
    if (changes[GATE_KEYS.enabled]) {
      enabled = Boolean(changes[GATE_KEYS.enabled].newValue);
    }
    if (changes[GATE_KEYS.blocklist]) {
      blocklist = parseBlocklist(changes[GATE_KEYS.blocklist].newValue);
    }
    evaluateGate();
  }

  if (areaName === "session") {
    if (changes[GATE_KEYS.sample]) {
      latestSample = changes[GATE_KEYS.sample].newValue ?? latestSample;
    }
    if (changes[GATE_KEYS.status]) {
      connectionStatus = changes[GATE_KEYS.status].newValue ?? connectionStatus;
    }
    evaluateGate();
  }
});

window.addEventListener("pageshow", evaluateGate);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    requestGateSnapshot();
  }
});

loadInitialState();
