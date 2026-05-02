from __future__ import annotations

from typing import Any

import numpy as np
from scipy import signal

from .preprocess import validate_ecg_window


def compute_ecg_features(
    *,
    timestamp: float,
    session_id: str,
    rr_ms: np.ndarray | list[float] | None = None,
    ecg_samples: np.ndarray | list[float] | None = None,
    ecg_timestamps: np.ndarray | list[float] | None = None,
    sample_rate_hz: float = 130.0,
) -> dict[str, Any]:
    rr = np.asarray(rr_ms if rr_ms is not None else [], dtype=float)
    rr = rr[np.isfinite(rr)]
    validity = validate_ecg_window(np.asarray(ecg_samples, dtype=float) if ecg_samples is not None else None, rr)
    valid_rr = rr[(rr >= 300.0) & (rr <= 2000.0)]

    if valid_rr.size < 2 and ecg_samples is not None:
        detected_rr = _rr_from_raw_ecg(np.asarray(ecg_samples, dtype=float), sample_rate_hz)
        if detected_rr.size >= 2:
            valid_rr = detected_rr[(detected_rr >= 300.0) & (detected_rr <= 2000.0)]
            validity["raw_r_peak_rr_count"] = int(valid_rr.size)

    features: dict[str, float] = {}
    if valid_rr.size:
        rr_mean = float(np.mean(valid_rr))
        features["ecg.heart_rate"] = float(60000.0 / rr_mean) if rr_mean > 0 else float("nan")
        features["ecg.rr_mean_ms"] = rr_mean
        features["ecg.rr_valid_fraction"] = float(valid_rr.size / max(rr.size, valid_rr.size, 1))
        features["ecg.sdnn"] = float(np.std(valid_rr, ddof=1)) if valid_rr.size > 1 else 0.0
        diffs = np.diff(valid_rr)
        features["ecg.rmssd_ms"] = float(np.sqrt(np.mean(np.square(diffs)))) if diffs.size else 0.0
        features["ecg.pnn50"] = float(np.mean(np.abs(diffs) > 50.0)) if diffs.size else 0.0
        validity["hrv_window_valid"] = bool(valid_rr.size >= 5 and features["ecg.rr_valid_fraction"] >= 0.75)
    else:
        features.update(
            {
                "ecg.heart_rate": float("nan"),
                "ecg.rr_mean_ms": float("nan"),
                "ecg.rr_valid_fraction": 0.0,
                "ecg.sdnn": float("nan"),
                "ecg.rmssd_ms": float("nan"),
                "ecg.pnn50": float("nan"),
            }
        )
        validity["hrv_window_valid"] = False

    if rr.size and valid_rr.size:
        validity["rr_invalid_count"] = int(rr.size - valid_rr.size)

    if ecg_timestamps is not None and len(ecg_timestamps):
        window_start = float(np.asarray(ecg_timestamps, dtype=float)[0])
        window_end = float(np.asarray(ecg_timestamps, dtype=float)[-1])
    else:
        window_start = float(timestamp)
        window_end = float(timestamp)
    return {
        "timestamp": float(timestamp),
        "session_id": session_id,
        "window_start": window_start,
        "window_end": window_end,
        "features": features,
        "validity": validity,
    }


def _rr_from_raw_ecg(ecg: np.ndarray, fs_hz: float) -> np.ndarray:
    x = ecg[np.isfinite(ecg)]
    if x.size < int(fs_hz * 5):
        return np.asarray([], dtype=float)
    x = signal.detrend(x, type="constant")
    x = x - np.median(x)
    try:
        sos = signal.butter(2, [5.0, min(35.0, fs_hz * 0.45)], btype="bandpass", fs=fs_hz, output="sos")
        x = signal.sosfiltfilt(sos, x)
    except ValueError:
        pass
    threshold = np.percentile(x, 90)
    distance = max(1, int(0.3 * fs_hz))
    peaks, _ = signal.find_peaks(x, height=threshold, distance=distance)
    if len(peaks) < 2:
        return np.asarray([], dtype=float)
    return np.diff(peaks) / fs_hz * 1000.0
