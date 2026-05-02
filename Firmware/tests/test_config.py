from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from slopodoro_acq.config import ConfigError, load_config


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "slopodoro_acquisition.yaml"


def test_config_validation_default_channel_map() -> None:
    cfg = load_config(CONFIG)
    assert cfg.openbci.board == "cyton_daisy"
    assert len(cfg.openbci.channels) == 16
    assert len(cfg.openbci.eeg_channels) == 14
    assert len(cfg.openbci.emg_channels) == 2
    assert cfg.openbci.eeg_indices_zero_based == list(range(12)) + [14, 15]
    assert cfg.openbci.emg_indices_zero_based == [12, 13]
    assert cfg.openbci.eeg_labels == ["Fp1", "Fp2", "C3", "C4", "P7", "P8", "O1", "O2", "F7", "F8", "F3", "F4", "P3", "P4"]
    assert cfg.openbci.emg_labels == ["left_upper_trapezius_emg", "right_upper_trapezius_emg"]


def test_config_rejects_duplicate_physical_channel(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    bad = copy.deepcopy(raw)
    bad["session"]["output_dir"] = str(tmp_path)
    bad["openbci"]["channels"][1]["index"] = bad["openbci"]["channels"][0]["index"]
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicated"):
        load_config(path)
