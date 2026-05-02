from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import AcquisitionConfig
from .preprocess import robust_stats


@dataclass(frozen=True)
class CalibrationPhase:
    name: str
    start_marker: str
    end_marker: str
    duration_seconds: float
    description: str


def calibration_phases(cfg: AcquisitionConfig, *, synthetic: bool = False) -> list[CalibrationPhase]:
    fast = cfg.synthetic.calibration_phase_seconds if synthetic else None

    def dur(configured: float) -> float:
        return float(min(configured, fast)) if fast is not None else float(configured)

    return [
        CalibrationPhase(
            "eyes_open_baseline",
            "eyes_open_start",
            "eyes_open_end",
            dur(cfg.calibration.eyes_open_seconds),
            "Sit normally and look at the screen.",
        ),
        CalibrationPhase(
            "eyes_closed_baseline",
            "eyes_closed_start",
            "eyes_closed_end",
            dur(cfg.calibration.eyes_closed_seconds),
            "Close eyes and stay relaxed.",
        ),
        CalibrationPhase(
            "focused_task_baseline",
            "focused_task_start",
            "focused_task_end",
            dur(cfg.calibration.focused_task_seconds),
            "Do representative focused work.",
        ),
        CalibrationPhase(
            "emg_neutral_baseline",
            "strain_neutral_start",
            "strain_neutral_end",
            dur(cfg.calibration.strain_neutral_seconds),
            "Sit in intended working posture with relaxed shoulders and neck.",
        ),
        CalibrationPhase(
            "emg_strain_reference",
            "strain_reference_start",
            "strain_reference_end",
            dur(cfg.calibration.strain_reference_seconds),
            "Create mild shoulder or neck tension without extreme contraction.",
        ),
    ]


def build_calibration_model(
    cfg: AcquisitionConfig,
    phase_frames: dict[str, list[dict[str, Any]]],
    *,
    started_at: float,
    ended_at: float,
) -> dict[str, Any]:
    phases: dict[str, Any] = {}
    warnings: list[str] = []
    rejected = False

    for phase_name, frames in phase_frames.items():
        phase_summary = _summarize_phase(phase_name, frames)
        phases[phase_name] = phase_summary

    focused = phases.get("focused_task_baseline", {})
    focused_valid = int(focused.get("valid_eeg_windows", 0))
    focused_artifact = float(focused.get("artifact_fraction", 1.0))
    if focused_valid < cfg.calibration.min_valid_eeg_windows:
        warnings.append(
            f"focused_task_baseline has too few valid EEG windows: {focused_valid} < {cfg.calibration.min_valid_eeg_windows}"
        )
        rejected = True
    if focused_artifact > cfg.calibration.max_artifact_fraction:
        warnings.append(
            f"focused_task_baseline artifact fraction is high: {focused_artifact:.2f} > {cfg.calibration.max_artifact_fraction:.2f}"
        )
        rejected = True

    emg_neutral = phases.get("emg_neutral_baseline", {})
    valid_emg = int(emg_neutral.get("valid_emg_windows", 0))
    if cfg.openbci.emg_channels and valid_emg < cfg.calibration.min_valid_emg_windows:
        warnings.append(
            f"emg_neutral_baseline has too few valid EMG windows: {valid_emg} < {cfg.calibration.min_valid_emg_windows}"
        )
        rejected = True

    if cfg.polar.enabled and cfg.polar.required:
        ecg_valid = sum(int(summary.get("valid_ecg_windows", 0)) for summary in phases.values())
        if ecg_valid == 0:
            warnings.append("Polar is required but no valid ECG/RR calibration windows were available")
            rejected = True

    return {
        "session_id": cfg.session.session_id,
        "participant_id": cfg.session.participant_id,
        "created_at": ended_at,
        "timestamp_range": {"start": started_at, "end": ended_at},
        "status": "rejected" if rejected else "ok",
        "warnings": warnings,
        "phases": phases,
        "scoring_baselines": {
            "focus": "focused_task_baseline",
            "signal_quality": ["eyes_open_baseline", "eyes_closed_baseline"],
            "emg_neutral": "emg_neutral_baseline",
            "emg_strain_reference": "emg_strain_reference",
        },
    }


def _summarize_phase(phase_name: str, frames: list[dict[str, Any]]) -> dict[str, Any]:
    features_by_name: dict[str, list[float]] = {}
    valid_eeg = 0
    valid_emg = 0
    valid_ecg = 0
    artifact_values: list[float] = []
    bad_channel_counts: list[int] = []
    starts: list[float] = []
    ends: list[float] = []

    for frame in frames:
        starts.append(float(frame.get("window_start", frame.get("timestamp", 0.0))))
        ends.append(float(frame.get("window_end", frame.get("timestamp", 0.0))))
        validity = frame.get("validity", {})
        if validity.get("eeg_valid"):
            valid_eeg += 1
        if validity.get("emg_valid"):
            valid_emg += 1
        if validity.get("ecg_valid") or validity.get("hrv_window_valid"):
            valid_ecg += 1
        if "artifact_fraction" in validity:
            artifact_values.append(float(validity["artifact_fraction"]))
        if "bad_channel_count" in validity:
            bad_channel_counts.append(int(validity["bad_channel_count"]))
        for name, value in frame.get("features", {}).items():
            if isinstance(value, (int, float, np.number)) and np.isfinite(float(value)):
                features_by_name.setdefault(name, []).append(float(value))

    feature_stats = {
        name: {**robust_stats(values), "valid_window_count": len(values), "baseline_phase_source": phase_name}
        for name, values in features_by_name.items()
    }
    return {
        "feature_stats": feature_stats,
        "valid_window_count": len(frames),
        "valid_eeg_windows": valid_eeg,
        "valid_emg_windows": valid_emg,
        "valid_ecg_windows": valid_ecg,
        "artifact_fraction": float(np.mean(artifact_values)) if artifact_values else 0.0,
        "bad_channel_count_median": float(np.median(bad_channel_counts)) if bad_channel_counts else 0.0,
        "timestamp_range": {
            "start": min(starts) if starts else None,
            "end": max(ends) if ends else None,
        },
    }
