from __future__ import annotations

import json
from pathlib import Path

import yaml

from slopodoro_acq.main import main


ROOT = Path(__file__).resolve().parents[1]


def test_synthetic_calibrate_and_run_smoke(tmp_path: Path) -> None:
    raw = yaml.safe_load((ROOT / "config" / "slopodoro_acquisition.yaml").read_text(encoding="utf-8"))
    raw["session"]["session_id"] = "test_synthetic_e2e"
    raw["session"]["output_dir"] = str(tmp_path)
    raw["synthetic"]["calibration_phase_seconds"] = 12
    raw["synthetic"]["run_seconds"] = 8
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    result = main(["--config", str(path), "--synthetic", "--mode", "calibrate-and-run", "--no-lsl"])
    assert result == 0

    session_dir = tmp_path / "test_synthetic_e2e"
    assert (session_dir / "calibration.json").exists()
    assert (session_dir / "features.jsonl").exists()
    assert (session_dir / "scores.jsonl").exists()
    assert (session_dir / "markers.jsonl").exists()
    assert (session_dir / "raw_stream_metadata.json").exists()

    calibration = json.loads((session_dir / "calibration.json").read_text(encoding="utf-8"))
    assert calibration["status"] == "ok"
    scores = [json.loads(line) for line in (session_dir / "scores.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(scores) > 0
    focus_scores = {round(row["scores"]["focus_score_0_100"], 1) for row in scores}
    assert len(focus_scores) > 1
