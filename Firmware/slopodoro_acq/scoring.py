from __future__ import annotations

from collections import deque
from typing import Any

import numpy as np

from .config import AcquisitionConfig


class RuleBasedScorer:
    def __init__(self, cfg: AcquisitionConfig) -> None:
        self.cfg = cfg
        self._focus_history: deque[tuple[float, bool]] = deque(maxlen=1200)
        self._strain_history: deque[tuple[float, bool]] = deque(maxlen=1200)
        self._bad_signal_history: deque[tuple[float, bool]] = deque(maxlen=120)
        self._last_state = "ready"
        self._last_state_change_ts = 0.0

    def score(
        self,
        feature_frame: dict[str, Any],
        *,
        source_health: dict[str, Any] | None = None,
        current_mode: str = "run",
    ) -> dict[str, Any]:
        ts = float(feature_frame.get("timestamp", 0.0))
        features = feature_frame.get("features", {})
        validity = feature_frame.get("validity", {})
        source_health = source_health or {}

        signal_quality = _signal_quality_score(validity, self.cfg)
        focus_score, fatigue_score = _focus_scores(features, validity, self.cfg)
        emg_score = float(np.clip(features.get("emg.emg_strain_score_0_100", 0.0), 0.0, 100.0))
        recovery_score = _recovery_context_score(features)

        focus_bad = focus_score < self.cfg.scoring.focus_break_threshold and signal_quality >= self.cfg.scoring.signal_quality_bad_threshold
        strain_bad = emg_score >= self.cfg.scoring.strain_notice_threshold
        bad_signal = _bad_signal(validity, signal_quality, self.cfg)
        self._focus_history.append((ts, focus_bad))
        self._strain_history.append((ts, strain_bad))
        self._bad_signal_history.append((ts, bad_signal))

        sustained_focus = _sustained_fraction(self._focus_history, ts, self.cfg.scoring.slop_detection_minutes * 60.0) >= 0.65
        sustained_strain = _sustained_fraction(self._strain_history, ts, self.cfg.scoring.strain_detection_seconds) >= 0.65
        sustained_bad_signal = _sustained_fraction(self._bad_signal_history, ts, 4.0) >= 0.5

        flags = {
            "break_recommended": bool(sustained_focus),
            "strain_notice": bool(sustained_strain),
            "bad_signal": bool(sustained_bad_signal),
            "polar_missing": bool(source_health.get("polar_missing", False)),
            "openbci_missing": bool(source_health.get("openbci_missing", False)),
        }
        state = self._choose_state(flags, current_mode, ts)

        explanation = _explain(features, flags, state)
        return {
            "timestamp": ts,
            "session_id": feature_frame.get("session_id", self.cfg.session.session_id),
            "scores": {
                "focus_score_0_100": float(focus_score),
                "fatigue_drift_score_0_100": float(fatigue_score),
                "emg_strain_score_0_100": float(emg_score),
                "signal_quality_score_0_100": float(signal_quality),
                "recovery_context_score_0_100": float(recovery_score),
            },
            "state": state,
            "flags": flags,
            "explanation": explanation,
        }

    def _choose_state(self, flags: dict[str, bool], current_mode: str, ts: float) -> str:
        if flags["openbci_missing"]:
            candidate = "openbci_missing"
        elif flags["bad_signal"]:
            candidate = "bad_signal"
        elif flags["polar_missing"] and self.cfg.polar.required:
            candidate = "polar_missing"
        elif current_mode == "calibration":
            candidate = "calibrating"
        elif flags["break_recommended"]:
            candidate = "break_recommended"
        elif flags["strain_notice"]:
            candidate = "strain_notice"
        else:
            candidate = "focused_work" if current_mode == "run" else "ready"

        if candidate == self._last_state:
            return candidate
        priority_states = {"bad_signal", "openbci_missing", "polar_missing", "break_recommended"}
        cooldown_elapsed = (ts - self._last_state_change_ts) >= self.cfg.scoring.state_cooldown_seconds
        if candidate not in priority_states and not cooldown_elapsed:
            return self._last_state
        self._last_state = candidate
        self._last_state_change_ts = ts
        return candidate


def _signal_quality_score(validity: dict[str, Any], cfg: AcquisitionConfig) -> float:
    artifact_fraction = float(validity.get("artifact_fraction", 0.0))
    bad_count = float(validity.get("bad_channel_count", len(validity.get("bad_channels", []))))
    eeg_valid = bool(validity.get("eeg_valid", True))
    emg_valid = bool(validity.get("emg_valid", True))
    ecg_valid = bool(validity.get("ecg_valid", True) or validity.get("hrv_window_valid", True))

    if cfg.scoring.hackathon_mode:
        score = 100.0
        score -= artifact_fraction * 45.0
        score -= bad_count * 2.0
        if bool(validity.get("line_noise_heavy", False)):
            score -= 5.0
        if not emg_valid:
            score -= 6.0
        if not ecg_valid:
            score -= 3.0
        finite_fraction = float(validity.get("finite_fraction", 1.0))
        if finite_fraction < 0.95:
            score -= (0.95 - finite_fraction) * 100.0
        return float(np.clip(score, 0.0, 100.0))

    score = 100.0
    score -= artifact_fraction * 100.0
    score -= bad_count * 8.0
    if not eeg_valid:
        score -= 30.0
    if not emg_valid:
        score -= 8.0
    if not ecg_valid:
        score -= 4.0
    return float(np.clip(score, 0.0, 100.0))


def _bad_signal(validity: dict[str, Any], signal_quality: float, cfg: AcquisitionConfig) -> bool:
    if not cfg.scoring.hackathon_mode:
        return signal_quality < cfg.scoring.signal_quality_bad_threshold

    artifact_fraction = float(validity.get("artifact_fraction", 1.0))
    finite_fraction = float(validity.get("finite_fraction", 1.0))
    return bool(finite_fraction < 0.50 or artifact_fraction >= 0.98)


def _focus_scores(features: dict[str, Any], validity: dict[str, Any], cfg: AcquisitionConfig) -> tuple[float, float]:
    if not validity.get("eeg_valid", True) and not cfg.scoring.hackathon_mode:
        return 50.0, 50.0

    engagement_z = float(features.get("eeg.engagement_index_z", 0.0))
    theta_beta_z = float(features.get("eeg.theta_beta_ratio_z", 0.0))
    theta_alpha_z = float(features.get("eeg.theta_alpha_ratio_z", 0.0))
    slope = float(features.get("eeg.engagement_index_z_slope", 0.0))

    has_calibrated_features = any(
        name in features
        for name in ("eeg.engagement_index_z", "eeg.theta_beta_ratio_z", "eeg.theta_alpha_ratio_z")
    )
    if has_calibrated_features:
        focus = 70.0 + 12.0 * engagement_z - 9.0 * theta_beta_z - 4.0 * theta_alpha_z + 20.0 * slope
        fatigue = 35.0 + 13.0 * theta_beta_z - 10.0 * engagement_z - 20.0 * slope
    else:
        focus, fatigue = _uncalibrated_band_scores(features, validity)

    return float(np.clip(focus, 0.0, 100.0)), float(np.clip(fatigue, 0.0, 100.0))


def _uncalibrated_band_scores(features: dict[str, Any], validity: dict[str, Any]) -> tuple[float, float]:
    delta = _finite_feature(features, "eeg.global_delta")
    theta = _finite_feature(features, "eeg.global_theta")
    alpha = _finite_feature(features, "eeg.global_alpha")
    beta = _finite_feature(features, "eeg.global_beta")
    artifact = float(np.clip(validity.get("artifact_fraction", 0.0), 0.0, 1.0))
    epsilon = 1e-9
    engagement_log = np.log((beta + epsilon) / (theta + alpha + epsilon))
    slow_log = np.log((theta + delta + epsilon) / (alpha + beta + epsilon))
    alpha_log = np.log((alpha + epsilon) / (theta + delta + epsilon))
    focus = 58.0 + 20.0 * np.tanh(engagement_log) + 8.0 * np.tanh(alpha_log) - 10.0 * artifact
    fatigue = 42.0 + 22.0 * np.tanh(slow_log) - 12.0 * np.tanh(engagement_log) + 14.0 * artifact
    return float(focus), float(fatigue)


def _finite_feature(features: dict[str, Any], name: str, default: float = 0.0) -> float:
    value = features.get(name, default)
    if not isinstance(value, (int, float, np.number)):
        return float(default)
    value = float(value)
    return value if np.isfinite(value) and value >= 0.0 else float(default)


def _recovery_context_score(features: dict[str, Any]) -> float:
    rmssd = features.get("ecg.rmssd_ms")
    hr = features.get("ecg.heart_rate")
    score = 50.0
    if isinstance(rmssd, (int, float, np.number)) and np.isfinite(float(rmssd)):
        score += np.clip((float(rmssd) - 30.0) * 0.5, -20.0, 25.0)
    if isinstance(hr, (int, float, np.number)) and np.isfinite(float(hr)):
        score -= np.clip((float(hr) - 75.0) * 0.25, -10.0, 20.0)
    return float(np.clip(score, 0.0, 100.0))


def _sustained_fraction(history: deque[tuple[float, bool]], now: float, duration_seconds: float) -> float:
    if duration_seconds <= 0 or not history:
        return 0.0
    cutoff = now - duration_seconds
    values = [active for ts, active in history if ts >= cutoff]
    if not values:
        return 0.0
    coverage = max(0.0, min(duration_seconds, now - min(ts for ts, _active in history if ts >= cutoff)))
    if coverage < min(duration_seconds, 3.0):
        return 0.0
    return float(np.mean(values))


def _explain(features: dict[str, Any], flags: dict[str, bool], state: str) -> dict[str, Any]:
    if flags["bad_signal"]:
        return {"primary": "bad_signal", "supporting_features": ["eeg.artifact_fraction", "eeg.bad_channel_count"]}
    if flags["break_recommended"] and flags["strain_notice"]:
        return {
            "primary": "focus_drift_with_sustained_emg_strain",
            "supporting_features": ["eeg.engagement_index_z", "eeg.theta_beta_ratio_z", "emg.emg_strain_score_0_100"],
        }
    if flags["break_recommended"]:
        return {
            "primary": "sustained_focus_drift",
            "supporting_features": ["eeg.engagement_index_z", "eeg.theta_beta_ratio_z"],
        }
    if flags["strain_notice"]:
        right = features.get("emg.right_strain_score_0_100", 0.0)
        left = features.get("emg.left_strain_score_0_100", 0.0)
        primary = "sustained_right_trap_emg" if right >= left else "sustained_left_trap_emg"
        return {
            "primary": primary,
            "supporting_features": ["emg.left_rms_z", "emg.right_rms_z", "emg.bilateral_strain_score"],
        }
    return {"primary": state, "supporting_features": []}
