from __future__ import annotations

from pathlib import Path

from slopodoro_acq.calibration import build_calibration_model
from slopodoro_acq.config import load_config
from slopodoro_acq.features_emg import EMGFeatureExtractor
from slopodoro_acq.synthetic import SyntheticSignalGenerator


ROOT = Path(__file__).resolve().parents[1]


def test_emg_strain_scores_against_neutral_and_reference() -> None:
    cfg = load_config(ROOT / "config" / "slopodoro_acquisition.yaml")
    generator = SyntheticSignalGenerator(cfg)
    extractor = EMGFeatureExtractor(cfg.features, cfg.openbci.expected_sample_rate_hz, cfg.openbci.emg_labels)

    phase_frames = {"emg_neutral_baseline": [], "emg_strain_reference": []}
    for idx in range(8):
        ts, raw = generator.openbci_window(100.0 + idx, cfg.features.emg_window_seconds, "emg_neutral_baseline")
        phase_frames["emg_neutral_baseline"].append(
            extractor.compute(ts, raw[:, cfg.openbci.emg_indices_zero_based], session_id="test")
        )
    for idx in range(8):
        ts, raw = generator.openbci_window(200.0 + idx, cfg.features.emg_window_seconds, "emg_strain_reference")
        phase_frames["emg_strain_reference"].append(
            extractor.compute(ts, raw[:, cfg.openbci.emg_indices_zero_based], session_id="test")
        )

    model = build_calibration_model(cfg, phase_frames, started_at=90.0, ended_at=220.0)
    ts, raw = generator.openbci_window(300.0, cfg.features.emg_window_seconds, "strain")
    frame = EMGFeatureExtractor(cfg.features, cfg.openbci.expected_sample_rate_hz, cfg.openbci.emg_labels).compute(
        ts,
        raw[:, cfg.openbci.emg_indices_zero_based],
        session_id="test",
        calibration_model=model,
    )
    assert frame["validity"]["emg_valid"] is True
    assert frame["features"]["emg.emg_strain_score_0_100"] > 50.0
    assert frame["features"]["emg.right_strain_score_0_100"] >= frame["features"]["emg.left_strain_score_0_100"]
