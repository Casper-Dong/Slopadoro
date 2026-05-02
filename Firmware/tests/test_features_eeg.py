from __future__ import annotations

from pathlib import Path

from slopodoro_acq.config import load_config
from slopodoro_acq.features_eeg import EEGFeatureExtractor
from slopodoro_acq.synthetic import SyntheticSignalGenerator


ROOT = Path(__file__).resolve().parents[1]


def test_eeg_feature_dict_shape_excludes_emg_channels() -> None:
    cfg = load_config(ROOT / "config" / "slopodoro_acquisition.yaml")
    generator = SyntheticSignalGenerator(cfg)
    timestamps, raw = generator.openbci_window(100.0, cfg.features.eeg_window_seconds, "focused_task_baseline")
    extractor = EEGFeatureExtractor(cfg.features, cfg.openbci.expected_sample_rate_hz, cfg.openbci.eeg_labels)
    frame = extractor.compute(timestamps, raw[:, cfg.openbci.eeg_indices_zero_based], session_id="test")
    assert frame["validity"]["eeg_valid"] is True
    assert "eeg.engagement_index" in frame["features"]
    assert "eeg.global_theta" in frame["features"]
    assert not any(key.startswith("emg.") for key in frame["features"])
