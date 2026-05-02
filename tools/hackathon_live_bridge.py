#!/usr/bin/env python3
"""Hackathon bridge and browser dashboard for live Slopodoro features.

This bypasses strict calibration scoring. It reads slopodoro_features from LSL,
derives rough bandpower-based focus/fatigue plus EMG/ECG context, serves the
Chrome extension WebSocket contract, and opens a lightweight dashboard tab.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import websockets

try:
    from pylsl import StreamInlet, resolve_byprop
except Exception as exc:  # pragma: no cover - depends on local liblsl install.
    StreamInlet = None
    resolve_byprop = None
    _PYLSL_IMPORT_ERROR = exc
else:
    _PYLSL_IMPORT_ERROR = None


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Slopodoro Live</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, Segoe UI, system-ui, sans-serif; }
    body { margin: 0; background: #0d1117; color: #e6edf3; }
    main { max-width: 1180px; margin: 0 auto; padding: 18px; }
    header { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
    h1 { font-size: 22px; margin: 0; font-weight: 650; }
    #status { color: #9da7b3; font-size: 13px; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 12px; }
    .panel { background: #151b23; border: 1px solid #30363d; border-radius: 8px; padding: 12px; }
    .metric span { display: block; color: #9da7b3; font-size: 12px; }
    .metric strong { display: block; margin-top: 4px; font-size: 28px; }
    canvas { width: 100%; height: 180px; display: block; background: #0f141b; border-radius: 6px; }
    .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    h2 { margin: 0 0 8px; font-size: 14px; font-weight: 620; color: #c9d1d9; }
    .detail { margin-top: 12px; color: #9da7b3; font-size: 13px; line-height: 1.45; white-space: pre-wrap; }
    @media (max-width: 760px) { .grid, .charts { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Slopodoro Live</h1>
      <div id="status">connecting</div>
    </header>
    <section class="grid">
      <div class="panel metric"><span>Focus</span><strong id="focus">--</strong></div>
      <div class="panel metric"><span>Fatigue</span><strong id="fatigue">--</strong></div>
      <div class="panel metric"><span>EMG Strain</span><strong id="strain">--</strong></div>
      <div class="panel metric"><span>Signal Quality</span><strong id="quality">--</strong></div>
    </section>
    <section class="charts">
      <div class="panel"><h2>Scores</h2><canvas id="scores"></canvas></div>
      <div class="panel"><h2>EEG Bands, percent of total power</h2><canvas id="bands"></canvas></div>
      <div class="panel"><h2>Posture EMG</h2><canvas id="emg"></canvas></div>
      <div class="panel"><h2>ECG Context</h2><canvas id="ecg"></canvas></div>
      <div class="panel"><h2>Raw EEG</h2><canvas id="rawEeg"></canvas></div>
      <div class="panel"><h2>Raw Posture EMG</h2><canvas id="rawEmg"></canvas></div>
      <div class="panel"><h2>Raw ECG</h2><canvas id="rawEcg"></canvas></div>
    </section>
    <section class="panel detail" id="detail">Waiting for LSL feature frames...</section>
  </main>
  <script>
    const history = [];
    const maxPoints = 180;
    let latestFrame = null;
    const colors = {
      focus: '#2f81f7', fatigue: '#f0883e', quality: '#3fb950', strain: '#d2a8ff',
      delta: '#8b949e', theta: '#ff7b72', alpha: '#56d364', beta: '#79c0ff',
      left: '#ffa657', right: '#d2a8ff', hr: '#ff7b72', rmssd: '#7ee787'
    };
    const $ = (id) => document.getElementById(id);
    function pct(v) { return Number.isFinite(v) ? String(Math.round(v)) : '--'; }
    function push(frame) {
      latestFrame = frame;
      history.push(frame);
      while (history.length > maxPoints) history.shift();
      $('status').textContent = `${frame.state || 'live'} | ${new Date().toLocaleTimeString()}`;
      $('focus').textContent = pct(frame.scores.focus);
      $('fatigue').textContent = pct(frame.scores.fatigue);
      $('strain').textContent = pct(frame.scores.strain);
      $('quality').textContent = pct(frame.scores.quality);
      $('detail').textContent = [
        `state: ${frame.state}`,
        `sources: EEG=${frame.sources.eeg} ECG=${frame.sources.ecg} EMG=${frame.sources.emg}`,
        `bad channels: ${(frame.validity.bad_channels || []).join(', ') || 'none'}`,
        `artifact fraction: ${Number(frame.validity.artifact_fraction || 0).toFixed(3)}`,
        `emg mode: ${frame.emg?.mode || 'feature amplitude'} L=${Number(frame.emg?.left_ratio || 0).toFixed(2)}x R=${Number(frame.emg?.right_ratio || 0).toFixed(2)}x`,
        `score reason: ${frame.reason}`
      ].join('\\n');
      draw();
    }
    function drawLineChart(id, series, yMin, yMax) {
      const canvas = $(id);
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, canvas.clientWidth * dpr);
      canvas.height = Math.max(1, canvas.clientHeight * dpr);
      const ctx = canvas.getContext('2d');
      ctx.scale(dpr, dpr);
      const w = canvas.clientWidth, h = canvas.clientHeight, pad = 24;
      ctx.clearRect(0, 0, w, h);
      ctx.strokeStyle = '#30363d';
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i++) {
        const y = pad + (i / 4) * (h - pad * 2);
        ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(w - 8, y); ctx.stroke();
      }
      for (const spec of series) {
        ctx.strokeStyle = spec.color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        let drawing = false;
        history.forEach((frame, i) => {
          const value = spec.get(frame);
          if (!Number.isFinite(value)) { drawing = false; return; }
          const x = pad + (i / Math.max(1, maxPoints - 1)) * (w - pad - 12);
          const y = h - pad - ((value - yMin) / Math.max(1, yMax - yMin)) * (h - pad * 2);
          if (!drawing) { ctx.moveTo(x, y); drawing = true; } else { ctx.lineTo(x, y); }
        });
        ctx.stroke();
        ctx.fillStyle = spec.color;
        ctx.fillText(spec.name, w - 92, 18 + series.indexOf(spec) * 16);
      }
    }
    function median(values) {
      const finite = values.filter(Number.isFinite).sort((a, b) => a - b);
      if (!finite.length) return 0;
      return finite[Math.floor(finite.length / 2)];
    }
    function span(values) {
      const finite = values.filter(Number.isFinite).sort((a, b) => a - b);
      if (finite.length < 2) return 1;
      return finite[Math.floor(finite.length * 0.95)] - finite[Math.floor(finite.length * 0.05)];
    }
    function prepCanvas(id) {
      const canvas = $(id);
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, canvas.clientWidth * dpr);
      canvas.height = Math.max(1, canvas.clientHeight * dpr);
      const ctx = canvas.getContext('2d');
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
      return { canvas, ctx, w: canvas.clientWidth, h: canvas.clientHeight };
    }
    function drawRawStacked(id, raw) {
      const { ctx, w, h } = prepCanvas(id);
      if (!raw || !raw.channels || !raw.channels.length) {
        ctx.fillStyle = '#8b949e';
        ctx.fillText('waiting for raw stream', 16, 24);
        return;
      }
      const labels = raw.labels || [];
      const channels = raw.channels.map(ch => ch.map(Number));
      const spans = channels.map(span).filter(Number.isFinite);
      const spacing = Math.max(1, median(spans) * 1.6);
      const pad = 20;
      channels.forEach((ch, idx) => {
        const center = median(ch);
        const offset = pad + idx * ((h - pad * 2) / Math.max(1, channels.length - 1));
        ctx.strokeStyle = idx % 2 ? '#79c0ff' : '#56d364';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ch.forEach((value, i) => {
          const x = pad + (i / Math.max(1, ch.length - 1)) * (w - pad - 10);
          const y = offset - ((value - center) / spacing) * 16;
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.stroke();
        ctx.fillStyle = '#8b949e';
        ctx.fillText(labels[idx] || String(idx + 1), 4, offset + 4);
      });
    }
    function drawRawSingle(id, raw) {
      const { ctx, w, h } = prepCanvas(id);
      const values = raw?.samples?.map(Number) || [];
      if (!values.length) {
        ctx.fillStyle = '#8b949e';
        ctx.fillText('waiting for raw stream', 16, 24);
        return;
      }
      const center = median(values);
      const scale = Math.max(1, span(values));
      const pad = 20;
      ctx.strokeStyle = '#ff7b72';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      values.forEach((value, i) => {
        const x = pad + (i / Math.max(1, values.length - 1)) * (w - pad - 10);
        const y = h / 2 - ((value - center) / scale) * (h - pad * 2);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
    function draw() {
      drawLineChart('scores', [
        { name: 'focus', color: colors.focus, get: f => f.scores.focus },
        { name: 'fatigue', color: colors.fatigue, get: f => f.scores.fatigue },
        { name: 'quality', color: colors.quality, get: f => f.scores.quality },
        { name: 'strain', color: colors.strain, get: f => f.scores.strain },
      ], 0, 100);
      drawLineChart('bands', [
        { name: 'delta', color: colors.delta, get: f => f.eeg_bands.delta },
        { name: 'theta', color: colors.theta, get: f => f.eeg_bands.theta },
        { name: 'alpha', color: colors.alpha, get: f => f.eeg_bands.alpha },
        { name: 'beta', color: colors.beta, get: f => f.eeg_bands.beta },
      ], 0, 100);
      drawLineChart('emg', [
        { name: 'left', color: colors.left, get: f => f.emg.left },
        { name: 'right', color: colors.right, get: f => f.emg.right },
        { name: 'total', color: colors.strain, get: f => f.scores.strain },
      ], 0, 100);
      drawLineChart('ecg', [
        { name: 'HR', color: colors.hr, get: f => f.ecg.hr },
        { name: 'RMSSD', color: colors.rmssd, get: f => f.ecg.rmssd },
      ], 0, 140);
      drawRawStacked('rawEeg', latestFrame?.raw?.eeg);
      drawRawStacked('rawEmg', latestFrame?.raw?.emg);
      drawRawSingle('rawEcg', latestFrame?.raw?.ecg);
    }
    function connect() {
      const ws = new WebSocket(`ws://${location.hostname}:8767/`);
      ws.onopen = () => $('status').textContent = 'connected';
      ws.onmessage = (event) => push(JSON.parse(event.data));
      ws.onclose = () => { $('status').textContent = 'disconnected; retrying'; setTimeout(connect, 1000); };
      ws.onerror = () => ws.close();
    }
    addEventListener('resize', draw);
    connect();
  </script>
</body>
</html>
"""


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def finite_number(value: Any, default: float = 0.0) -> float:
    if not isinstance(value, (int, float)):
        return default
    value = float(value)
    return value if math.isfinite(value) else default


def score01(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return clamp(value / 100.0, 0.0, 1.0)


def dormant(reason: str) -> dict[str, Any]:
    now = time.time()
    return {
        "ts": now,
        "focus": None,
        "fatigue": None,
        "calibrating": True,
        "subscores": {"bridge_state": reason},
        "sources": {"eeg": False, "ecg": False, "emg": False},
        "dashboard": {
            "timestamp": now,
            "state": "waiting",
            "reason": reason,
            "scores": {"focus": None, "fatigue": None, "quality": 0.0, "strain": 0.0},
            "sources": {"eeg": False, "ecg": False, "emg": False},
            "validity": {"bad_channels": []},
            "eeg_bands": {"delta": 0.0, "theta": 0.0, "alpha": 0.0, "beta": 0.0},
            "emg": {"left": 0.0, "right": 0.0},
            "ecg": {"hr": None, "rmssd": None},
        },
    }


def compute_demo_frame(feature_frame: dict[str, Any]) -> dict[str, Any]:
    features = feature_frame.get("features") if isinstance(feature_frame.get("features"), dict) else {}
    validity = feature_frame.get("validity") if isinstance(feature_frame.get("validity"), dict) else {}
    ts = finite_number(feature_frame.get("timestamp"), time.time())

    delta = max(0.0, finite_number(features.get("eeg.global_delta")))
    theta = max(0.0, finite_number(features.get("eeg.global_theta")))
    alpha = max(0.0, finite_number(features.get("eeg.global_alpha")))
    beta = max(0.0, finite_number(features.get("eeg.global_beta")))
    total_band = max(delta + theta + alpha + beta, 1e-9)
    band_pct = {
        "delta": 100.0 * delta / total_band,
        "theta": 100.0 * theta / total_band,
        "alpha": 100.0 * alpha / total_band,
        "beta": 100.0 * beta / total_band,
    }

    artifact = clamp(finite_number(validity.get("artifact_fraction"), 1.0), 0.0, 1.0)
    bad_count = int(finite_number(validity.get("bad_channel_count"), len(validity.get("bad_channels", []))))
    quality = clamp(100.0 - artifact * 45.0 - bad_count * 2.0 - (5.0 if validity.get("line_noise_heavy") else 0.0), 0.0, 100.0)
    has_bandpower = (delta + theta + alpha + beta) > 1e-9
    severe_bad_signal = not has_bandpower

    engagement_z = features.get("eeg.engagement_index_z")
    theta_beta_z = features.get("eeg.theta_beta_ratio_z")
    theta_alpha_z = features.get("eeg.theta_alpha_ratio_z")
    if all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in (engagement_z, theta_beta_z, theta_alpha_z)):
        slope = finite_number(features.get("eeg.engagement_index_z_slope"))
        focus = 70.0 + 12.0 * float(engagement_z) - 9.0 * float(theta_beta_z) - 4.0 * float(theta_alpha_z) + 20.0 * slope
        fatigue = 35.0 + 13.0 * float(theta_beta_z) - 10.0 * float(engagement_z) - 20.0 * slope
        reason = "calibrated_feature_z_scores_relaxed"
    else:
        eps = 1e-9
        engagement_log = math.log((beta + eps) / (theta + alpha + eps))
        slow_log = math.log((theta + delta + eps) / (alpha + beta + eps))
        alpha_log = math.log((alpha + eps) / (theta + delta + eps))
        focus = 58.0 + 20.0 * math.tanh(engagement_log) + 8.0 * math.tanh(alpha_log) - 10.0 * artifact
        fatigue = 42.0 + 22.0 * math.tanh(slow_log) - 12.0 * math.tanh(engagement_log) + 14.0 * artifact
        reason = "uncalibrated_bandpower_ratios"

    focus = clamp(focus, 0.0, 100.0)
    fatigue = clamp(fatigue, 0.0, 100.0)
    strain = clamp(finite_number(features.get("emg.emg_strain_score_0_100")), 0.0, 100.0)
    left_strain = clamp(finite_number(features.get("emg.left_strain_score_0_100")), 0.0, 100.0)
    right_strain = clamp(finite_number(features.get("emg.right_strain_score_0_100")), 0.0, 100.0)
    recovery = _recovery(features)
    state = "bad_signal" if severe_bad_signal else "strain_notice" if strain >= 70.0 else "focused_work"
    emg_available = any(
        name in features
        for name in ("emg.emg_strain_score_0_100", "emg.left_strain_score_0_100", "emg.right_strain_score_0_100")
    )
    sources = {
        "eeg": not severe_bad_signal,
        "ecg": bool(validity.get("ecg_valid") or validity.get("hrv_window_valid")),
        "emg": bool(emg_available),
    }

    dashboard = {
        "timestamp": ts,
        "state": state,
        "reason": reason,
        "scores": {"focus": focus, "fatigue": fatigue, "quality": quality, "strain": strain, "recovery": recovery},
        "sources": sources,
        "validity": {
            "bad_channels": validity.get("bad_channels", []),
            "artifact_fraction": artifact,
            "bad_channel_count": bad_count,
            "line_noise_heavy": bool(validity.get("line_noise_heavy", False)),
        },
        "eeg_bands": band_pct,
        "emg": {"left": left_strain, "right": right_strain},
        "ecg": {
            "hr": features.get("ecg.heart_rate"),
            "rmssd": features.get("ecg.rmssd_ms"),
        },
    }
    return {
        "ts": ts,
        "focus": score01(focus) if not severe_bad_signal else None,
        "fatigue": score01(fatigue) if not severe_bad_signal else None,
        "calibrating": False,
        "subscores": {
            "emg_strain_score_0_100": strain,
            "signal_quality_score_0_100": quality,
            "recovery_context_score_0_100": recovery,
            "score_state": state,
            "demo_reason": reason,
        },
        "sources": sources,
        "dashboard": dashboard,
    }


def _recovery(features: dict[str, Any]) -> float:
    score = 50.0
    rmssd = features.get("ecg.rmssd_ms")
    hr = features.get("ecg.heart_rate")
    if isinstance(rmssd, (int, float)) and math.isfinite(float(rmssd)):
        score += clamp((float(rmssd) - 30.0) * 0.5, -20.0, 25.0)
    if isinstance(hr, (int, float)) and math.isfinite(float(hr)):
        score -= clamp((float(hr) - 75.0) * 0.25, -10.0, 20.0)
    return clamp(score, 0.0, 100.0)


@dataclass
class RawBuffers:
    openbci_timestamps: deque[float] = field(default_factory=lambda: deque(maxlen=5000))
    openbci_samples: deque[list[float]] = field(default_factory=lambda: deque(maxlen=5000))
    ecg_timestamps: deque[float] = field(default_factory=lambda: deque(maxlen=5000))
    ecg_samples: deque[float] = field(default_factory=lambda: deque(maxlen=5000))


@dataclass
class LiveState:
    latest: dict[str, Any] = field(default_factory=lambda: dormant("waiting_for_lsl_features"))
    raw: RawBuffers = field(default_factory=RawBuffers)


def resolve_feature_stream(args: argparse.Namespace) -> Any | None:
    by_name = resolve_byprop("name", args.feature_stream_name, minimum=1, timeout=args.resolve_timeout)
    if by_name:
        return by_name[0]
    by_type = resolve_byprop("type", "Features", minimum=1, timeout=args.resolve_timeout)
    return by_type[0] if by_type else None


def resolve_named_stream(name: str, stream_type: str, timeout: float) -> Any | None:
    by_name = resolve_byprop("name", name, minimum=1, timeout=timeout)
    if by_name:
        return by_name[0]
    by_type = resolve_byprop("type", stream_type, minimum=1, timeout=timeout)
    return by_type[0] if by_type else None


def parse_indices(value: str) -> list[int]:
    indices: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = [int(piece.strip()) for piece in part.split("-", 1)]
            indices.extend(range(start - 1, end))
        else:
            indices.append(int(part) - 1)
    return indices


def parse_labels(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _finite_values(values: list[float]) -> list[float]:
    return [float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(float(value))]


def _median(values: list[float]) -> float:
    finite = sorted(_finite_values(values))
    if not finite:
        return 0.0
    midpoint = len(finite) // 2
    if len(finite) % 2:
        return finite[midpoint]
    return 0.5 * (finite[midpoint - 1] + finite[midpoint])


def _percentile(values: list[float], pct: float) -> float:
    finite = sorted(_finite_values(values))
    if not finite:
        return 0.0
    if len(finite) == 1:
        return finite[0]
    pct = clamp(pct, 0.0, 100.0)
    position = (pct / 100.0) * (len(finite) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return finite[lower]
    weight = position - lower
    return finite[lower] * (1.0 - weight) + finite[upper] * weight


def _mad(values: list[float], center: float) -> float:
    return _median([abs(value - center) for value in values])


def _channel_derivative_score(
    rows: list[tuple[float, list[float]]],
    channel_index: int,
    end_timestamp: float,
    args: argparse.Namespace,
) -> dict[str, Any]:
    values: list[tuple[float, float]] = []
    for ts, row in rows:
        if channel_index >= len(row):
            continue
        value = float(row[channel_index])
        if math.isfinite(value):
            values.append((ts, value))

    if len(values) < 4:
        return {"available": False, "score": 0.0, "ratio": 0.0, "recent": 0.0, "noise": 0.0}

    derivatives: list[tuple[float, float]] = []
    previous_ts, previous_value = values[0]
    for ts, value in values[1:]:
        dt = ts - previous_ts
        if 0.0 < dt <= args.emg_derivative_max_gap_seconds:
            derivatives.append((previous_ts + dt * 0.5, abs((value - previous_value) / dt)))
        previous_ts, previous_value = ts, value

    if len(derivatives) < 3:
        return {"available": False, "score": 0.0, "ratio": 0.0, "recent": 0.0, "noise": 0.0}

    recent_cutoff = end_timestamp - args.emg_derivative_recent_seconds
    recent = [value for ts, value in derivatives if ts >= recent_cutoff]
    baseline = [value for ts, value in derivatives if ts < recent_cutoff]
    if len(baseline) < args.emg_derivative_min_baseline_points:
        baseline = [value for _ts, value in derivatives]

    baseline_center = _median(baseline)
    baseline_noise = baseline_center + 1.4826 * _mad(baseline, baseline_center)
    noise = max(float(args.emg_derivative_noise_floor_uv_per_s), baseline_noise)
    recent_level = _percentile(recent, 95.0) if recent else 0.0
    ratio = recent_level / max(noise, 1e-9)
    denominator = max(args.emg_derivative_ratio_full_scale - args.emg_derivative_ratio_threshold, 1e-9)
    score = clamp(((ratio - args.emg_derivative_ratio_threshold) / denominator) * 100.0, 0.0, 100.0)
    return {
        "available": True,
        "score": score,
        "ratio": ratio,
        "recent": recent_level,
        "noise": noise,
    }


def emg_derivative_strain(args: argparse.Namespace, state: LiveState) -> dict[str, Any]:
    timestamps = list(state.raw.openbci_timestamps)
    samples = list(state.raw.openbci_samples)
    if not timestamps or not samples:
        return {"available": False}

    end = timestamps[-1]
    start = end - args.emg_derivative_window_seconds
    rows = [(ts, row) for ts, row in zip(timestamps, samples, strict=False) if ts >= start]
    if len(rows) < 4:
        return {"available": False}

    indices = parse_indices(args.emg_indices)
    channel_scores = [_channel_derivative_score(rows, idx, end, args) for idx in indices[:2]]
    available_scores = [entry["score"] for entry in channel_scores if entry.get("available")]
    if not available_scores:
        return {"available": False}

    left = float(channel_scores[0]["score"]) if len(channel_scores) > 0 and channel_scores[0].get("available") else 0.0
    right = float(channel_scores[1]["score"]) if len(channel_scores) > 1 and channel_scores[1].get("available") else 0.0
    total = max(available_scores)
    return {
        "available": True,
        "total": total,
        "left": left,
        "right": right,
        "left_ratio": float(channel_scores[0].get("ratio", 0.0)) if len(channel_scores) > 0 else 0.0,
        "right_ratio": float(channel_scores[1].get("ratio", 0.0)) if len(channel_scores) > 1 else 0.0,
        "left_recent_uv_per_s": float(channel_scores[0].get("recent", 0.0)) if len(channel_scores) > 0 else 0.0,
        "right_recent_uv_per_s": float(channel_scores[1].get("recent", 0.0)) if len(channel_scores) > 1 else 0.0,
        "left_noise_uv_per_s": float(channel_scores[0].get("noise", 0.0)) if len(channel_scores) > 0 else 0.0,
        "right_noise_uv_per_s": float(channel_scores[1].get("noise", 0.0)) if len(channel_scores) > 1 else 0.0,
    }


def payload_with_live_emg_derivative(args: argparse.Namespace, state: LiveState) -> dict[str, Any]:
    payload = copy.deepcopy(state.latest)
    derivative = emg_derivative_strain(args, state)
    if not derivative.get("available"):
        payload = _override_emg_derivative_payload(
            payload,
            strain=0.0,
            left=0.0,
            right=0.0,
            derivative={
                "available": False,
                "left_ratio": 0.0,
                "right_ratio": 0.0,
                "left_recent_uv_per_s": 0.0,
                "right_recent_uv_per_s": 0.0,
                "left_noise_uv_per_s": 0.0,
                "right_noise_uv_per_s": 0.0,
            },
            args=args,
            emg_source=False,
        )
        return payload

    strain = clamp(float(derivative["total"]), 0.0, 100.0)
    left = clamp(float(derivative["left"]), 0.0, 100.0)
    right = clamp(float(derivative["right"]), 0.0, 100.0)
    return _override_emg_derivative_payload(payload, strain, left, right, derivative, args, emg_source=True)


def _override_emg_derivative_payload(
    payload: dict[str, Any],
    strain: float,
    left: float,
    right: float,
    derivative: dict[str, Any],
    args: argparse.Namespace,
    emg_source: bool,
) -> dict[str, Any]:
    sources = dict(payload.get("sources") or {})
    sources["emg"] = emg_source
    payload["sources"] = sources

    subscores = dict(payload.get("subscores") or {})
    current_state = str(subscores.get("score_state") or payload.get("dashboard", {}).get("state") or "focused_work")
    if current_state not in {"bad_signal", "waiting", "openbci_missing"}:
        current_state = "strain_notice" if strain >= args.emg_derivative_notice_threshold else "focused_work"
    subscores.update(
        {
            "emg_strain_score_0_100": strain,
            "emg_left_derivative_strain_0_100": left,
            "emg_right_derivative_strain_0_100": right,
            "emg_derivative_left_ratio": derivative["left_ratio"],
            "emg_derivative_right_ratio": derivative["right_ratio"],
            "emg_strain_mode": "derivative_spike",
            "score_state": current_state,
        }
    )
    payload["subscores"] = subscores

    dashboard = dict(payload.get("dashboard") or {})
    scores = dict(dashboard.get("scores") or {})
    scores["strain"] = strain
    dashboard["scores"] = scores
    dashboard["sources"] = {**dict(dashboard.get("sources") or {}), "emg": emg_source}
    if dashboard.get("state") not in {"bad_signal", "waiting", "openbci_missing"}:
        dashboard["state"] = current_state
    reason = str(dashboard.get("reason") or "live")
    if "emg_derivative_spike" not in reason:
        reason = f"{reason}_emg_derivative_spike"
    dashboard["reason"] = reason
    dashboard["emg"] = {
        "mode": "derivative_spike",
        "left": left,
        "right": right,
        "left_ratio": derivative["left_ratio"],
        "right_ratio": derivative["right_ratio"],
        "left_recent_uv_per_s": derivative["left_recent_uv_per_s"],
        "right_recent_uv_per_s": derivative["right_recent_uv_per_s"],
        "left_noise_uv_per_s": derivative["left_noise_uv_per_s"],
        "right_noise_uv_per_s": derivative["right_noise_uv_per_s"],
    }
    payload["dashboard"] = dashboard
    return payload


async def read_openbci_raw(args: argparse.Namespace, state: LiveState) -> None:
    inlet = None
    while True:
        if inlet is None:
            info = await asyncio.to_thread(resolve_named_stream, args.openbci_stream_name, "ExG", args.resolve_timeout)
            if info is None:
                await asyncio.sleep(args.resolve_retry_seconds)
                continue
            inlet = StreamInlet(info, max_buflen=max(10, int(args.raw_window_seconds * 2)))
            print(f"resolved raw OpenBCI stream: {info.name()} ({info.type()})", flush=True)

        chunk, timestamps = inlet.pull_chunk(timeout=0.0, max_samples=512)
        if timestamps:
            for ts, row in zip(timestamps, chunk, strict=False):
                state.raw.openbci_timestamps.append(float(ts))
                state.raw.openbci_samples.append([float(value) for value in row])
        await asyncio.sleep(args.raw_poll_seconds)


async def read_ecg_raw(args: argparse.Namespace, state: LiveState) -> None:
    inlet = None
    while True:
        if inlet is None:
            info = await asyncio.to_thread(resolve_named_stream, args.ecg_stream_name, "ECG", args.resolve_timeout)
            if info is None:
                await asyncio.sleep(args.resolve_retry_seconds)
                continue
            inlet = StreamInlet(info, max_buflen=max(10, int(args.raw_window_seconds * 2)))
            print(f"resolved raw ECG stream: {info.name()} ({info.type()})", flush=True)

        chunk, timestamps = inlet.pull_chunk(timeout=0.0, max_samples=512)
        if timestamps:
            for ts, row in zip(timestamps, chunk, strict=False):
                state.raw.ecg_timestamps.append(float(ts))
                state.raw.ecg_samples.append(float(row[0]) if row else 0.0)
        await asyncio.sleep(args.raw_poll_seconds)


def raw_snapshot(args: argparse.Namespace, state: LiveState) -> dict[str, Any]:
    eeg_indices = parse_indices(args.eeg_indices)
    emg_indices = parse_indices(args.emg_indices)
    eeg_labels = parse_labels(args.eeg_labels)
    emg_labels = parse_labels(args.emg_labels)
    open_ts = list(state.raw.openbci_timestamps)
    open_samples = list(state.raw.openbci_samples)
    ecg_ts = list(state.raw.ecg_timestamps)
    ecg_samples = list(state.raw.ecg_samples)
    return {
        "eeg": _openbci_raw_view(open_ts, open_samples, eeg_indices, eeg_labels, args.raw_window_seconds, args.raw_max_points),
        "emg": _openbci_raw_view(open_ts, open_samples, emg_indices, emg_labels, args.raw_window_seconds, args.raw_max_points),
        "ecg": _single_raw_view(ecg_ts, ecg_samples, args.raw_window_seconds, args.raw_max_points),
    }


def _openbci_raw_view(
    timestamps: list[float],
    samples: list[list[float]],
    indices: list[int],
    labels: list[str],
    window_seconds: float,
    max_points: int,
) -> dict[str, Any]:
    if not timestamps or not samples:
        return {"labels": labels, "t": [], "channels": []}
    end = timestamps[-1]
    rows = [(ts, row) for ts, row in zip(timestamps, samples, strict=False) if ts >= end - window_seconds]
    rows = _downsample(rows, max_points)
    t = [float(ts - end) for ts, _row in rows]
    channels: list[list[float]] = []
    for idx in indices:
        values = [float(row[idx]) for _ts, row in rows if idx < len(row)]
        channels.append(values)
    return {"labels": labels[: len(channels)], "t": t, "channels": channels}


def _single_raw_view(timestamps: list[float], samples: list[float], window_seconds: float, max_points: int) -> dict[str, Any]:
    if not timestamps or not samples:
        return {"t": [], "samples": []}
    end = timestamps[-1]
    rows = [(ts, value) for ts, value in zip(timestamps, samples, strict=False) if ts >= end - window_seconds]
    rows = _downsample(rows, max_points)
    return {"t": [float(ts - end) for ts, _value in rows], "samples": [float(value) for _ts, value in rows]}


def _downsample(rows: list[tuple[Any, Any]], max_points: int) -> list[tuple[Any, Any]]:
    if len(rows) <= max_points:
        return rows
    step = max(1, math.ceil(len(rows) / max_points))
    return rows[::step][-max_points:]


async def read_features(args: argparse.Namespace, state: LiveState) -> None:
    inlet = None
    last_seen = 0.0
    while True:
        if inlet is None:
            state.latest = dormant("waiting_for_lsl_features")
            info = await asyncio.to_thread(resolve_feature_stream, args)
            if info is None:
                await asyncio.sleep(args.resolve_retry_seconds)
                continue
            inlet = StreamInlet(info, max_buflen=5)
            last_seen = time.monotonic()
            print(f"resolved LSL feature stream: {info.name()} ({info.type()})", flush=True)

        sample, _lsl_ts = inlet.pull_sample(timeout=0.0)
        if sample:
            try:
                state.latest = compute_demo_frame(json.loads(sample[0]))
                last_seen = time.monotonic()
            except (TypeError, json.JSONDecodeError, ValueError) as exc:
                print(f"ignoring malformed feature frame: {exc}", flush=True)
        elif time.monotonic() - last_seen > args.stale_seconds:
            state.latest = dormant("lsl_features_stale")
            inlet.close_stream()
            inlet = None

        await asyncio.sleep(args.poll_seconds)


async def extension_client(websocket: Any, state: LiveState, args: argparse.Namespace) -> None:
    print("extension connected to hackathon feature bridge", flush=True)
    try:
        while True:
            live_payload = payload_with_live_emg_derivative(args, state)
            payload = {key: value for key, value in live_payload.items() if key != "dashboard"}
            await websocket.send(json.dumps(payload, separators=(",", ":")))
            await asyncio.sleep(args.output_interval_seconds)
    except websockets.ConnectionClosed:
        print("extension disconnected from hackathon feature bridge", flush=True)


async def dashboard_client(websocket: Any, state: LiveState, args: argparse.Namespace) -> None:
    try:
        while True:
            live_payload = payload_with_live_emg_derivative(args, state)
            payload = dict(live_payload["dashboard"])
            payload["ts"] = live_payload.get("ts")
            payload["focus"] = live_payload.get("focus")
            payload["fatigue"] = live_payload.get("fatigue")
            payload["calibrating"] = live_payload.get("calibrating", False)
            payload["subscores"] = live_payload.get("subscores", {})
            payload["sources"] = live_payload.get("sources", payload.get("sources", {}))
            payload["raw"] = raw_snapshot(args, state)
            await websocket.send(json.dumps(payload, separators=(",", ":")))
            await asyncio.sleep(args.output_interval_seconds)
    except websockets.ConnectionClosed:
        pass


async def http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await reader.readline()
        while True:
            line = await reader.readline()
            if not line or line == b"\r\n":
                break
    except Exception:
        request_line = b""

    path = request_line.decode(errors="ignore").split(" ")[1] if b" " in request_line else "/"
    if path not in {"/", "/index.html"}:
        body = b"not found"
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\nConnection: close\r\n\r\n" + body)
    else:
        body = HTML.encode("utf-8")
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            + b"Content-Type: text/html; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + body
        )
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def main_async(args: argparse.Namespace) -> None:
    if _PYLSL_IMPORT_ERROR is not None:
        raise SystemExit(f"pylsl import failed: {_PYLSL_IMPORT_ERROR}")

    state = LiveState()
    reader_tasks = [
        asyncio.create_task(read_features(args, state)),
        asyncio.create_task(read_openbci_raw(args, state)),
        asyncio.create_task(read_ecg_raw(args, state)),
    ]

    async def ext_handler(websocket: Any, *_args: Any) -> None:
        await extension_client(websocket, state, args)

    async def dash_handler(websocket: Any, *_args: Any) -> None:
        await dashboard_client(websocket, state, args)

    http_server = await asyncio.start_server(http_handler, args.host, args.dashboard_port)
    async with http_server:
        async with websockets.serve(ext_handler, args.host, args.extension_port):
            async with websockets.serve(dash_handler, args.host, args.dashboard_ws_port):
                dashboard_url = f"http://{args.host}:{args.dashboard_port}/"
                print(f"Extension demo WebSocket listening on ws://{args.host}:{args.extension_port}", flush=True)
                print(f"Dashboard listening on {dashboard_url}", flush=True)
                if args.open_dashboard:
                    webbrowser.open(dashboard_url)
                try:
                    await asyncio.Future()
                finally:
                    for task in reader_tasks:
                        task.cancel()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve demo Slopodoro scores from live feature frames.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--extension-port", default=8765, type=int)
    parser.add_argument("--dashboard-port", default=8766, type=int)
    parser.add_argument("--dashboard-ws-port", default=8767, type=int)
    parser.add_argument("--feature-stream-name", default="slopodoro_features")
    parser.add_argument("--openbci-stream-name", default="slopodoro_openbci_raw")
    parser.add_argument("--ecg-stream-name", default="slopodoro_polar_ecg")
    parser.add_argument("--eeg-indices", default="1-12,15-16", help="1-based OpenBCI raw channel indices for EEG")
    parser.add_argument("--emg-indices", default="13-14", help="1-based OpenBCI raw channel indices for posture EMG")
    parser.add_argument("--eeg-labels", default="Fp1,Fp2,C3,C4,P7,P8,O1,O2,F7,F8,F3,F4,P3,P4")
    parser.add_argument("--emg-labels", default="left_trap,right_trap")
    parser.add_argument("--raw-window-seconds", default=8.0, type=float)
    parser.add_argument("--raw-max-points", default=320, type=int)
    parser.add_argument("--emg-derivative-window-seconds", default=1.5, type=float)
    parser.add_argument("--emg-derivative-recent-seconds", default=0.25, type=float)
    parser.add_argument("--emg-derivative-max-gap-seconds", default=0.2, type=float)
    parser.add_argument("--emg-derivative-min-baseline-points", default=20, type=int)
    parser.add_argument("--emg-derivative-noise-floor-uv-per-s", default=30000.0, type=float)
    parser.add_argument("--emg-derivative-ratio-threshold", default=3.0, type=float)
    parser.add_argument("--emg-derivative-ratio-full-scale", default=5.0, type=float)
    parser.add_argument("--emg-derivative-notice-threshold", default=70.0, type=float)
    parser.add_argument("--resolve-timeout", default=1.0, type=float)
    parser.add_argument("--resolve-retry-seconds", default=1.0, type=float)
    parser.add_argument("--poll-seconds", default=0.05, type=float)
    parser.add_argument("--raw-poll-seconds", default=0.02, type=float)
    parser.add_argument("--output-interval-seconds", default=0.25, type=float)
    parser.add_argument("--stale-seconds", default=2.0, type=float)
    parser.add_argument("--open-dashboard", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(main_async(parse_args()))
    except KeyboardInterrupt:
        print("\nHackathon feature bridge stopped", flush=True)
