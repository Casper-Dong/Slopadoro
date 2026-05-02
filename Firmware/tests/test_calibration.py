from __future__ import annotations

from pathlib import Path

from slopodoro_acq.calibration import build_calibration_model
from slopodoro_acq.config import load_config


ROOT = Path(__file__).resolve().parents[1]


def test_calibration_model_creation_with_stats() -> None:
    cfg = load_config(ROOT / "config" / "slopodoro_acquisition.yaml")
    frames = []
    for idx in range(10):
        frames.append(
            {
                "timestamp": float(idx),
                "window_start": float(idx),
                "window_end": float(idx + 1),
                "features": {
                    "eeg.engagement_index": 1.0 + idx * 0.1,
                    "eeg.theta_beta_ratio": 0.8 + idx * 0.01,
                    "emg.left_rms": 2.0,
                },
                "validity": {"eeg_valid": True, "emg_valid": True, "ecg_valid": False, "artifact_fraction": 0.1},
            }
        )
    model = build_calibration_model(
        cfg,
        {
            "focused_task_baseline": frames,
            "emg_neutral_baseline": frames,
            "emg_strain_reference": frames,
        },
        started_at=0.0,
        ended_at=20.0,
    )
    assert model["status"] == "ok"
    stats = model["phases"]["focused_task_baseline"]["feature_stats"]["eeg.engagement_index"]
    assert stats["valid_window_count"] == 10
    assert stats["std"] > 0
