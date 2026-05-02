from __future__ import annotations

from collections import deque
from typing import Any

import numpy as np

from .config import FeatureConfig
from .preprocess import preprocess_emg_window, robust_z


class EMGFeatureExtractor:
    def __init__(self, feature_cfg: FeatureConfig, sample_rate_hz: float, labels: list[str]) -> None:
        self.feature_cfg = feature_cfg
        self.sample_rate_hz = float(sample_rate_hz)
        self.labels = labels
        self._activation_history: deque[tuple[float, bool]] = deque(maxlen=600)

    def compute(
        self,
        timestamps: np.ndarray,
        samples: np.ndarray,
        *,
        session_id: str,
        calibration_model: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if len(timestamps) == 0:
            return {
                "timestamp": 0.0,
                "session_id": session_id,
                "window_start": 0.0,
                "window_end": 0.0,
                "features": {},
                "validity": {"emg_valid": False},
                "channel_labels": self.labels,
            }
        window_start = float(timestamps[0])
        window_end = float(timestamps[-1])
        prep = preprocess_emg_window(
            samples,
            self.sample_rate_hz,
            bandpass_hz=self.feature_cfg.emg_bandpass_hz,
            labels=self.labels,
        )
        envelope = prep.samples
        features: dict[str, float] = {}
        neutral_stats = _phase_stats(calibration_model, "emg_neutral_baseline")
        strain_stats = _phase_stats(calibration_model, "emg_strain_reference")

        channel_scores: list[float] = []
        for idx, label in enumerate(self.labels):
            y = envelope[:, idx] if envelope.ndim == 2 and envelope.shape[1] > idx else np.asarray([])
            rms = float(np.sqrt(np.mean(np.square(y)))) if y.size else float("nan")
            mav = float(np.mean(np.abs(y))) if y.size else float("nan")
            env_mean = float(np.mean(y)) if y.size else float("nan")
            env_p95 = float(np.percentile(y, 95)) if y.size else float("nan")

            prefix = f"emg.{_side_name(idx, label)}"
            features[f"{prefix}_rms"] = rms
            features[f"{prefix}_mean_absolute_value"] = mav
            features[f"{prefix}_envelope_mean"] = env_mean
            features[f"{prefix}_envelope_p95"] = env_p95

            neutral = neutral_stats.get(f"{prefix}_rms", {})
            z = robust_z(rms, float(neutral.get("mean", neutral.get("median", 0.0))), _stats_scale(neutral), default=0.0)
            features[f"{prefix}_rms_z"] = z
            activation_threshold = float(neutral.get("mean", 0.0)) + 2.0 * max(_stats_scale(neutral), 1e-6)
            features[f"{prefix}_activation_fraction"] = (
                float(np.mean(y > activation_threshold)) if y.size and np.isfinite(activation_threshold) else 0.0
            )
            channel_scores.append(_channel_strain_score(rms, f"{prefix}_rms", neutral_stats, strain_stats))

        left = channel_scores[0] if channel_scores else 0.0
        right = channel_scores[1] if len(channel_scores) > 1 else left
        bilateral = float(np.mean(channel_scores)) if channel_scores else 0.0
        asymmetry = abs(left - right)
        active = bilateral >= 65.0
        self._activation_history.append((window_end, active))
        sustained = _sustained_seconds(self._activation_history, window_end)

        features["emg.left_right_asymmetry"] = float(asymmetry)
        features["emg.bilateral_strain_score"] = bilateral
        features["emg.sustained_activation_seconds"] = sustained
        features["emg.left_strain_score_0_100"] = float(left)
        features["emg.right_strain_score_0_100"] = float(right)
        features["emg.emg_strain_score_0_100"] = float(max(bilateral, left, right))
        features["emg.validity"] = 1.0 if prep.validity.get("emg_valid") else 0.0

        return {
            "timestamp": window_end,
            "session_id": session_id,
            "window_start": window_start,
            "window_end": window_end,
            "features": features,
            "validity": prep.validity,
            "channel_labels": self.labels,
        }


def compute_emg_features(
    timestamps: np.ndarray,
    samples: np.ndarray,
    *,
    feature_cfg: FeatureConfig,
    sample_rate_hz: float,
    labels: list[str],
    session_id: str = "test",
    calibration_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return EMGFeatureExtractor(feature_cfg, sample_rate_hz, labels).compute(
        timestamps,
        samples,
        session_id=session_id,
        calibration_model=calibration_model,
    )


def _phase_stats(model: dict[str, Any] | None, phase: str) -> dict[str, dict[str, float]]:
    if not model:
        return {}
    return model.get("phases", {}).get(phase, {}).get("feature_stats", {})


def _stats_scale(stats: dict[str, Any]) -> float:
    std = stats.get("std")
    if std is not None and abs(float(std)) > 1e-9:
        return float(std)
    mad = stats.get("mad", 0.0)
    return float(1.4826 * float(mad))


def _side_name(idx: int, label: str) -> str:
    lower = label.lower()
    if "left" in lower or idx == 0:
        return "left"
    if "right" in lower or idx == 1:
        return "right"
    return label.lower().replace(" ", "_")


def _channel_strain_score(
    value: float,
    feature_name: str,
    neutral_stats: dict[str, dict[str, float]],
    strain_stats: dict[str, dict[str, float]],
) -> float:
    neutral = neutral_stats.get(feature_name, {})
    strain = strain_stats.get(feature_name, {})
    neutral_center = float(neutral.get("mean", neutral.get("median", 0.0)))
    strain_center = float(strain.get("mean", strain.get("median", neutral_center + max(_stats_scale(neutral), 1.0) * 5.0)))
    denom = max(strain_center - neutral_center, max(_stats_scale(neutral), 1.0) * 4.0, 1e-6)
    normalized = (float(value) - neutral_center) / denom
    return float(np.clip(100.0 * normalized, 0.0, 100.0))


def _sustained_seconds(history: deque[tuple[float, bool]], now: float) -> float:
    active_points = [ts for ts, active in history if active]
    if not active_points or not history[-1][1]:
        return 0.0
    first_active = active_points[-1]
    for ts, active in reversed(history):
        if not active:
            break
        first_active = ts
    return float(max(0.0, now - first_active))
