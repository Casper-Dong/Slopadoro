from __future__ import annotations

from collections import deque
from typing import Any

import numpy as np
from scipy import signal

from .config import FeatureConfig
from .preprocess import preprocess_eeg_window, robust_z


class EEGFeatureExtractor:
    def __init__(self, feature_cfg: FeatureConfig, sample_rate_hz: float, labels: list[str]) -> None:
        self.feature_cfg = feature_cfg
        self.sample_rate_hz = float(sample_rate_hz)
        self.labels = labels
        self._recent_engagement: deque[tuple[float, float]] = deque(maxlen=180)

    def compute(
        self,
        timestamps: np.ndarray,
        samples: np.ndarray,
        *,
        session_id: str,
        calibration_model: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if len(timestamps) == 0:
            return _empty_frame(session_id, "eeg")
        window_start = float(timestamps[0])
        window_end = float(timestamps[-1])
        prep = preprocess_eeg_window(
            samples,
            self.sample_rate_hz,
            notch_hz=self.feature_cfg.notch_hz,
            bandpass_hz=self.feature_cfg.eeg_bandpass_hz,
            labels=self.labels,
        )
        x = prep.samples
        features: dict[str, float] = {}

        if x.shape[0] >= max(32, int(self.sample_rate_hz)):
            freqs, psd = signal.welch(
                x,
                fs=self.sample_rate_hz,
                axis=0,
                nperseg=min(x.shape[0], int(self.sample_rate_hz * 2)),
                scaling="density",
            )
            bandpowers: dict[str, np.ndarray] = {}
            for band_name, band in self.feature_cfg.eeg_bands.items():
                bandpowers[band_name] = _bandpower(freqs, psd, band)
                for idx, label in enumerate(self.labels):
                    features[f"eeg.{label}.{band_name}_power"] = float(bandpowers[band_name][idx])

            frontal = _label_indices(self.labels, prefixes=("fp", "f"))
            posterior = _label_indices(self.labels, prefixes=("p", "o"))
            for band_name, values in bandpowers.items():
                features[f"eeg.global_{band_name}"] = _safe_mean(values)
                if frontal:
                    features[f"eeg.frontal_{band_name}"] = _safe_mean(values[frontal])
                if posterior:
                    features[f"eeg.posterior_{band_name}"] = _safe_mean(values[posterior])

            theta = features.get("eeg.global_theta", 0.0)
            alpha = features.get("eeg.global_alpha", 0.0)
            beta = features.get("eeg.global_beta", 0.0)
            epsilon = 1e-9
            features["eeg.theta_beta_ratio"] = float(theta / (beta + epsilon))
            features["eeg.theta_alpha_ratio"] = float(theta / (alpha + epsilon))
            features["eeg.engagement_index"] = float(beta / (theta + alpha + epsilon))
            if "eeg.frontal_theta" in features and "eeg.posterior_alpha" in features:
                features["eeg.frontal_theta_posterior_alpha_ratio"] = float(
                    features["eeg.frontal_theta"] / (features["eeg.posterior_alpha"] + epsilon)
                )
        else:
            prep.validity["eeg_valid"] = False

        features["eeg.artifact_fraction"] = float(prep.validity.get("artifact_fraction", 1.0))
        features["eeg.bad_channel_count"] = float(prep.validity.get("bad_channel_count", len(self.labels)))
        features["eeg.blink_like_frontal_transient_count"] = float(_blink_like_count(samples, self.labels, self.sample_rate_hz))

        _apply_z_scores(features, calibration_model, phase="focused_task_baseline")
        engagement_z = features.get("eeg.engagement_index_z")
        if engagement_z is not None:
            self._recent_engagement.append((window_end, float(engagement_z)))
            features["eeg.engagement_index_z_slope"] = _slope(self._recent_engagement)

        return {
            "timestamp": window_end,
            "session_id": session_id,
            "window_start": window_start,
            "window_end": window_end,
            "features": features,
            "validity": prep.validity,
            "channel_labels": self.labels,
        }


def compute_eeg_features(
    timestamps: np.ndarray,
    samples: np.ndarray,
    *,
    feature_cfg: FeatureConfig,
    sample_rate_hz: float,
    labels: list[str],
    session_id: str = "test",
    calibration_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return EEGFeatureExtractor(feature_cfg, sample_rate_hz, labels).compute(
        timestamps,
        samples,
        session_id=session_id,
        calibration_model=calibration_model,
    )


def _bandpower(freqs: np.ndarray, psd: np.ndarray, band: tuple[float, float]) -> np.ndarray:
    mask = (freqs >= band[0]) & (freqs < band[1])
    if not np.any(mask):
        return np.zeros(psd.shape[1], dtype=float)
    return np.trapezoid(psd[mask], freqs[mask], axis=0)


def _label_indices(labels: list[str], prefixes: tuple[str, ...]) -> list[int]:
    indices: list[int] = []
    for idx, label in enumerate(labels):
        clean = label.lower().replace("_", "")
        if any(clean.startswith(prefix) for prefix in prefixes):
            indices.append(idx)
    return indices


def _safe_mean(values: np.ndarray) -> float:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    return float(np.mean(arr)) if arr.size else float("nan")


def _apply_z_scores(features: dict[str, float], calibration_model: dict[str, Any] | None, phase: str) -> None:
    if not calibration_model:
        return
    phase_stats = calibration_model.get("phases", {}).get(phase, {}).get("feature_stats", {})
    for name, value in list(features.items()):
        stats = phase_stats.get(name)
        if not stats:
            continue
        scale = stats.get("std")
        if not scale or abs(float(scale)) < 1e-9:
            mad = stats.get("mad")
            scale = 1.4826 * float(mad) if mad is not None else 0.0
        features[f"{name}_z"] = robust_z(float(value), float(stats.get("mean", stats.get("median", 0.0))), float(scale))


def _blink_like_count(samples: np.ndarray, labels: list[str], fs_hz: float) -> int:
    frontal = _label_indices(labels, prefixes=("fp",))
    if not frontal:
        frontal = _label_indices(labels, prefixes=("f",))
    if not frontal:
        return 0
    x = np.asarray(samples, dtype=float)
    if x.ndim != 2 or x.shape[0] < int(0.25 * fs_hz):
        return 0
    frontal_signal = np.nanmean(np.abs(np.diff(x[:, frontal], axis=0)), axis=1)
    threshold = np.nanmedian(frontal_signal) + 6.0 * np.nanstd(frontal_signal)
    if not np.isfinite(threshold) or threshold <= 0:
        return 0
    peaks, _ = signal.find_peaks(frontal_signal, height=threshold, distance=max(1, int(0.25 * fs_hz)))
    return int(len(peaks))


def _slope(points: deque[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    arr = np.asarray(points, dtype=float)
    t = arr[:, 0] - arr[0, 0]
    y = arr[:, 1]
    if np.ptp(t) <= 0:
        return 0.0
    return float(np.polyfit(t, y, 1)[0])


def _empty_frame(session_id: str, prefix: str) -> dict[str, Any]:
    return {
        "timestamp": 0.0,
        "session_id": session_id,
        "window_start": 0.0,
        "window_end": 0.0,
        "features": {},
        "validity": {f"{prefix}_valid": False},
    }
