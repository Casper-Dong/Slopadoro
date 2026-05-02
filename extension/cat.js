const SPRITE_URL = chrome.runtime.getURL("cat-sprite-sheet.png");
const FRAME_W = 32;
const FRAME_H = 32;
const SCALE = 4;
const DISPLAY_W = FRAME_W * SCALE;
const DISPLAY_H = FRAME_H * SCALE;
const SHEET_W = 256 * SCALE;
const SHEET_H = 320 * SCALE;

const ANIMATIONS = {
  alert_dash: { row: 9, frames: 8, fps: 14, moving: true, speed: 170, jump: 22 },
  alert_walk: { row: 5, frames: 8, fps: 9, moving: true, speed: 54 },
  alert_step: { row: 4, frames: 8, fps: 7, moving: true, speed: 34 },
  attentive_idle: { row: 0, frames: 4, fps: 4, moving: true, speed: 24 },
  soft_idle: { row: 1, frames: 4, fps: 4, moving: true, speed: 18 },
  tired_idle: { row: 7, frames: 6, fps: 5, moving: true, speed: 28 },
  yawn_light: { row: 2, frames: 4, fps: 4, moving: false },
  yawn_heavy: { row: 3, frames: 4, fps: 4, moving: false },
  doze: { row: 8, frames: 7, fps: 5, moving: false, corner: true },
  sleep: { row: 6, frames: 4, fps: 2, moving: false, corner: true }
};

let host = null;
let x = 24;
let direction = 1;
let frame = 0;
let activeName = null;
let visible = false;
let focus = 0.5;
let fatigue = null;
let calibrating = true;
let sources = { eeg: false, ecg: false, emg: false };
let lastFrameAt = 0;
let lastMoveAt = 0;
let jumpOffset = 0;

function clamp01(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return null;
  }
  return Math.min(1, Math.max(0, value));
}

function ensureHost() {
  if (host || document.getElementById("slopadoro-fatigue-cat")) {
    host = document.getElementById("slopadoro-fatigue-cat");
    return;
  }

  host = document.createElement("div");
  host.id = "slopadoro-fatigue-cat";
  host.style.position = "fixed";
  host.style.left = "0";
  host.style.bottom = "0";
  host.style.width = `${DISPLAY_W}px`;
  host.style.height = `${DISPLAY_H}px`;
  host.style.pointerEvents = "none";
  host.style.zIndex = "2147483647";
  host.style.imageRendering = "pixelated";
  host.style.backgroundImage = `url("${SPRITE_URL}")`;
  host.style.backgroundRepeat = "no-repeat";
  host.style.backgroundSize = `${SHEET_W}px ${SHEET_H}px`;
  host.style.transformOrigin = "left bottom";
  host.style.willChange = "transform, background-position";
  host.style.display = "none";
  document.documentElement.appendChild(host);
}

function animationFor(nextFocus, nextFatigue) {
  const f = clamp01(nextFatigue);
  const focusValue = clamp01(nextFocus) ?? 0.5;
  if (f === null) {
    return null;
  }

  if (focusValue >= 0.72 && f < 0.5) {
    return f < 0.28 ? "sleep" : "doze";
  }

  if (f >= 0.75 || focusValue < 0.22) {
    return "alert_dash";
  }

  if (f >= 0.6 || focusValue < 0.36) {
    return focusValue >= 0.45 ? "yawn_light" : "yawn_heavy";
  }

  if (focusValue >= 0.58) {
    return f < 0.35 ? "attentive_idle" : "soft_idle";
  }

  if (focusValue >= 0.42) {
    return f < 0.42 ? "alert_step" : "alert_walk";
  }

  return "tired_idle";
}

function setAnimation(name, now) {
  if (activeName === name) {
    return;
  }

  activeName = name;
  frame = 0;
  lastFrameAt = now;

  if (!name) {
    host.style.display = "none";
    visible = false;
    return;
  }

  const animation = ANIMATIONS[name];
  visible = true;
  host.style.display = "block";
  if (animation.corner) {
    x = Math.max(0, window.innerWidth - DISPLAY_W - 12);
    direction = -1;
  }
  // The sheet is a fixed 8-column canvas; shorter rows use transparent trailing cells.
  host.style.backgroundSize = `${SHEET_W}px ${SHEET_H}px`;
  host.style.backgroundPositionY = `${-(animation.row * DISPLAY_H)}px`;
  host.style.backgroundPositionX = "0px";
}

function clampX() {
  const maxX = Math.max(0, window.innerWidth - DISPLAY_W);
  x = Math.min(maxX, Math.max(0, x));
}

function updatePosition() {
  clampX();
  const translateX = direction === 1 ? x : x + DISPLAY_W;
  host.style.transform = `translate3d(${Math.round(translateX)}px, ${Math.round(-jumpOffset)}px, 0) scaleX(${direction})`;
}

function tick(now) {
  ensureHost();

  const waitingForStream = calibrating || fatigue === null;
  const headsetMissing = sources.eeg === false;
  const nextAnimation = waitingForStream || headsetMissing ? "sleep" : animationFor(focus, fatigue);
  setAnimation(nextAnimation, now);

  if (visible && activeName) {
    const animation = ANIMATIONS[activeName];
    if (animation.corner) {
      x = Math.max(0, window.innerWidth - DISPLAY_W - 12);
      direction = -1;
    }
    const frameDuration = 1000 / animation.fps;
    if (now - lastFrameAt >= frameDuration) {
      const steps = Math.floor((now - lastFrameAt) / frameDuration);
      frame = (frame + steps) % animation.frames;
      lastFrameAt += steps * frameDuration;
      host.style.backgroundPositionX = `${-(frame * DISPLAY_W)}px`;
    }

    if (animation.moving) {
      const dt = lastMoveAt ? Math.min(0.08, (now - lastMoveAt) / 1000) : 0;
      x += direction * animation.speed * dt;
      jumpOffset = animation.jump ? Math.abs(Math.sin(now / 115)) * animation.jump : 0;
      const maxX = Math.max(0, window.innerWidth - DISPLAY_W);
      if (x <= 0) {
        x = 0;
        direction = 1;
      } else if (x >= maxX) {
        x = maxX;
        direction = -1;
      }
    } else {
      jumpOffset = 0;
    }
    updatePosition();
  }

  lastMoveAt = now;
  requestAnimationFrame(tick);
}

function applyFatigueMessage(message) {
  focus = clamp01(message.focus) ?? focus;
  fatigue = clamp01(message.value);
  calibrating = Boolean(message.calibrating);
  if (message.sources && typeof message.sources === "object") {
    sources = {
      eeg: Boolean(message.sources.eeg),
      ecg: Boolean(message.sources.ecg),
      emg: Boolean(message.sources.emg)
    };
  }
}

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "fatigue") {
    applyFatigueMessage(message);
  }
});

window.addEventListener("resize", () => {
  clampX();
  updatePosition();
});

ensureHost();
requestAnimationFrame(tick);
