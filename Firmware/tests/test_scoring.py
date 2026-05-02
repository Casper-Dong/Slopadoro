from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from slopodoro_acq.config import load_config
from slopodoro_acq.scoring import RuleBasedScorer


ROOT = Path(__file__).resolve().parents[1]


def test_scoring_hysteresis_requires_sustained_focus_drift() -> None:
    cfg = load_config(ROOT / "config" / "slopodoro_acquisition.yaml")
    cfg = replace(
        cfg,
        scoring=replace(cfg.scoring, slop_detection_minutes=0.05, state_cooldown_seconds=0.0),
    )
    scorer = RuleBasedScorer(cfg)
    last = None
    for idx in range(5):
        frame = {
            "timestamp": float(idx),
            "session_id": "test",
            "features": {"eeg.engagement_index_z": -3.0, "eeg.theta_beta_ratio_z": 3.0},
            "validity": {"eeg_valid": True, "emg_valid": True, "ecg_valid": True, "artifact_fraction": 0.0, "bad_channel_count": 0},
        }
        last = scorer.score(frame, current_mode="run")
    assert last is not None
    assert last["flags"]["break_recommended"] is True
    assert last["state"] == "break_recommended"


def test_bad_signal_behavior_overrides_confident_scores() -> None:
    cfg = load_config(ROOT / "config" / "slopodoro_acquisition.yaml")
    cfg = replace(cfg, scoring=replace(cfg.scoring, state_cooldown_seconds=0.0))
    scorer = RuleBasedScorer(cfg)
    last = None
    for idx in range(6):
        frame = {
            "timestamp": float(idx),
            "session_id": "test",
            "features": {"eeg.engagement_index_z": 2.0, "eeg.theta_beta_ratio_z": -2.0},
            "validity": {"eeg_valid": False, "emg_valid": True, "ecg_valid": True, "artifact_fraction": 0.7, "bad_channel_count": 4},
        }
        last = scorer.score(frame, current_mode="run")
    assert last is not None
    assert last["flags"]["bad_signal"] is True
    assert last["state"] == "bad_signal"
    assert last["scores"]["signal_quality_score_0_100"] < cfg.scoring.signal_quality_bad_threshold
