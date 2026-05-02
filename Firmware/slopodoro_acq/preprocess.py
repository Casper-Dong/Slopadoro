from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import signal


@dataclass(frozen=True)
class PreprocessResult:
    samples: np.ndarray
    validity: dict[str, Any]


def preprocess_eeg_window(
    samples: np.ndarray,
    fs_hz: float,
    *,
    notch_hz: float = 60.0,
    bandpass_hz: tuple[float, float] = (1.0, 40.0),
    labels: list[str] | None = None,
) -> PreprocessResult:
    x = _ensure_2d(samples)
    labels = labels or [f"eeg_{i + 1:02d}" for i in range(x.shape[1])]
    bad_channels: set[str] = set()

    finite_fraction = float(np.isfinite(x).mean()) if x.size else 0.0
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    x = signal.detrend(x, axis=0, type="constant")
    x = x - np.median(x, axis=0, keepdims=True)

    peak_to_peak = np.ptp(x, axis=0) if x.size else np.asarray([])
    channel_std = np.std(x, axis=0) if x.size else np.asarray([])
    for idx, (ptp, std) in enumerate(zip(peak_to_peak, channel_std, strict=False)):
        if std < 0.05 or ptp < 0.5:
            bad_channels.add(labels[idx])
        if ptp > 500.0 or np.nanmax(np.abs(x[:, idx])) > 300.0:
            bad_channels.add(labels[idx])

    filtered = _safe_notch(x, fs_hz, notch_hz)
    filtered = _safe_bandpass(filtered, fs_hz, bandpass_hz[0], bandpass_hz[1])

    artifact_samples = np.any(np.abs(filtered) > 250.0, axis=1) if filtered.size else np.asarray([])
    artifact_fraction = float(np.mean(artifact_samples)) if artifact_samples.size else 1.0
    line_noise_heavy = _line_noise_ratio(x, fs_hz, notch_hz) > 0.45 if x.shape[0] >= max(32, fs_hz) else False

    valid_channel_fraction = 1.0 - (len(bad_channels) / max(len(labels), 1))
    eeg_valid = finite_fraction >= 0.95 and artifact_fraction < 0.35 and valid_channel_fraction >= 0.65 and not line_noise_heavy
    return PreprocessResult(
        samples=filtered,
        validity={
            "eeg_valid": bool(eeg_valid),
            "finite_fraction": finite_fraction,
            "artifact_fraction": artifact_fraction,
            "bad_channels": sorted(bad_channels),
            "bad_channel_count": len(bad_channels),
            "line_noise_heavy": bool(line_noise_heavy),
        },
    )


def preprocess_emg_window(
    samples: np.ndarray,
    fs_hz: float,
    *,
    bandpass_hz: tuple[float, float] = (20.0, 45.0),
    labels: list[str] | None = None,
) -> PreprocessResult:
    x = _ensure_2d(samples)
    labels = labels or [f"emg_{i + 1:02d}" for i in range(x.shape[1])]
    finite_fraction = float(np.isfinite(x).mean()) if x.size else 0.0
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    x = signal.detrend(x, axis=0, type="constant")
    x = x - np.median(x, axis=0, keepdims=True)
    bad_channels: set[str] = set()
    for idx in range(x.shape[1]):
        ptp = float(np.ptp(x[:, idx])) if x.shape[0] else 0.0
        std = float(np.std(x[:, idx])) if x.shape[0] else 0.0
        if std < 0.02 or ptp < 0.2:
            bad_channels.add(labels[idx])
        if ptp > 2000.0 or np.nanmax(np.abs(x[:, idx])) > 1000.0:
            bad_channels.add(labels[idx])

    filtered = _safe_bandpass(x, fs_hz, bandpass_hz[0], bandpass_hz[1])
    rectified = np.abs(filtered)
    envelope = _moving_average(rectified, max(1, int(0.1 * fs_hz)))
    emg_valid = finite_fraction >= 0.95 and (len(bad_channels) / max(len(labels), 1)) <= 0.5
    return PreprocessResult(
        samples=envelope,
        validity={
            "emg_valid": bool(emg_valid),
            "finite_fraction": finite_fraction,
            "bad_channels": sorted(bad_channels),
            "bad_channel_count": len(bad_channels),
            "saturated": bool(any(ch in bad_channels for ch in labels)),
        },
    )


def validate_ecg_window(ecg_samples: np.ndarray | None = None, rr_ms: np.ndarray | None = None) -> dict[str, Any]:
    validity: dict[str, Any] = {"ecg_valid": False, "rr_valid_fraction": 0.0}
    if rr_ms is not None:
        rr = np.asarray(rr_ms, dtype=float)
        rr = rr[np.isfinite(rr)]
        if rr.size:
            valid = (rr >= 300.0) & (rr <= 2000.0)
            validity["rr_valid_fraction"] = float(np.mean(valid))
            validity["rr_count"] = int(rr.size)
            validity["ecg_valid"] = bool(validity["rr_valid_fraction"] >= 0.75 and np.sum(valid) >= 3)
    if ecg_samples is not None:
        ecg = np.asarray(ecg_samples, dtype=float)
        finite = np.isfinite(ecg)
        if finite.any():
            x = ecg[finite]
            validity["raw_ecg_finite_fraction"] = float(np.mean(finite))
            validity["raw_ecg_dynamic_range"] = float(np.ptp(x))
            validity["raw_ecg_valid"] = bool(np.mean(finite) >= 0.95 and np.ptp(x) > 10.0)
            validity["ecg_valid"] = bool(validity["ecg_valid"] or validity["raw_ecg_valid"])
    return validity


def robust_z(value: float, center: float, scale: float, *, default: float = 0.0) -> float:
    if not np.isfinite(value) or not np.isfinite(center) or not np.isfinite(scale) or abs(scale) < 1e-9:
        return float(default)
    return float((value - center) / scale)


def robust_stats(values: list[float] | np.ndarray) -> dict[str, float]:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return {"mean": float("nan"), "std": float("nan"), "median": float("nan"), "mad": float("nan")}
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    return {
        "mean": float(np.mean(arr)),
        "std": float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0,
        "median": median,
        "mad": mad,
    }


def _ensure_2d(samples: np.ndarray) -> np.ndarray:
    x = np.asarray(samples, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2:
        raise ValueError("samples must be 1D or 2D")
    return x


def _safe_notch(x: np.ndarray, fs_hz: float, notch_hz: float) -> np.ndarray:
    if x.shape[0] < 12 or notch_hz <= 0 or notch_hz >= fs_hz / 2:
        return x
    try:
        b, a = signal.iirnotch(notch_hz, 30.0, fs=fs_hz)
        return signal.filtfilt(b, a, x, axis=0)
    except ValueError:
        return x


def _safe_bandpass(x: np.ndarray, fs_hz: float, low: float, high: float) -> np.ndarray:
    nyq = fs_hz / 2.0
    low = max(0.01, float(low))
    high = min(float(high), nyq * 0.95)
    if x.shape[0] < 18 or low >= high:
        return x
    try:
        sos = signal.butter(4, [low, high], btype="bandpass", fs=fs_hz, output="sos")
        return signal.sosfiltfilt(sos, x, axis=0)
    except ValueError:
        return x


def _moving_average(x: np.ndarray, width: int) -> np.ndarray:
    if width <= 1 or x.shape[0] < width:
        return x
    kernel = np.ones(width, dtype=float) / float(width)
    return np.vstack([np.convolve(x[:, idx], kernel, mode="same") for idx in range(x.shape[1])]).T


def _line_noise_ratio(x: np.ndarray, fs_hz: float, line_hz: float) -> float:
    if line_hz >= fs_hz / 2:
        return 0.0
    freqs, psd = signal.welch(x, fs=fs_hz, axis=0, nperseg=min(x.shape[0], int(fs_hz * 2)))
    total = np.trapezoid(psd, freqs, axis=0)
    mask = (freqs >= line_hz - 2.0) & (freqs <= line_hz + 2.0)
    if not np.any(mask):
        return 0.0
    line = np.trapezoid(psd[mask], freqs[mask], axis=0)
    ratios = line / np.maximum(total, 1e-12)
    return float(np.nanmax(ratios))
