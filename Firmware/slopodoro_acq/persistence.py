from __future__ import annotations

import csv
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml

from .config import AcquisitionConfig


class SessionWriter:
    def __init__(self, cfg: AcquisitionConfig) -> None:
        self.cfg = cfg
        self.session_dir = (cfg.session.output_dir / cfg.session.session_id).resolve()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, TextIO] = {}
        self._marker_csv: TextIO | None = None
        self._marker_writer: csv.DictWriter | None = None
        self._write_config_snapshot()

    def _write_config_snapshot(self) -> None:
        snapshot_path = self.session_dir / "config_snapshot.yaml"
        with snapshot_path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(self.cfg.to_plain_dict(), fh, sort_keys=False)

    def write_marker(self, marker: dict[str, Any]) -> None:
        self.write_jsonl("markers.jsonl", marker)
        if self._marker_csv is None:
            self._marker_csv = (self.session_dir / "markers.csv").open("a", newline="", encoding="utf-8")
            self._marker_writer = csv.DictWriter(
                self._marker_csv,
                fieldnames=["timestamp", "session_id", "participant_id", "event", "phase"],
                extrasaction="ignore",
            )
            if self._marker_csv.tell() == 0:
                self._marker_writer.writeheader()
        assert self._marker_writer is not None
        self._marker_writer.writerow(marker)
        self._marker_csv.flush()

    def write_features(self, frame: dict[str, Any]) -> None:
        self.write_jsonl("features.jsonl", frame)

    def write_score(self, frame: dict[str, Any]) -> None:
        self.write_jsonl("scores.jsonl", frame)

    def write_health(self, frame: dict[str, Any]) -> None:
        self.write_jsonl("health.jsonl", frame)

    def write_raw_metadata(self, frame: dict[str, Any]) -> None:
        self.write_json("raw_stream_metadata.json", frame)

    def write_calibration(self, model: dict[str, Any]) -> None:
        self.write_json("calibration.json", model)

    def write_raw_openbci_chunk(self, timestamps: list[float], samples: list[list[float]], source_id: str) -> None:
        self.write_jsonl(
            "raw_openbci.jsonl",
            {"stream": "openbci", "source_id": source_id, "timestamps": timestamps, "samples": samples},
        )

    def write_raw_ecg_chunk(self, timestamps: list[float], samples: list[float], source_id: str) -> None:
        self.write_jsonl(
            "raw_polar_ecg.jsonl",
            {"stream": "polar_ecg", "source_id": source_id, "timestamps": timestamps, "samples": samples},
        )

    def write_raw_hr_rr(self, events: list[dict[str, Any]], source_id: str) -> None:
        for event in events:
            payload = {"stream": "polar_hr_rr", "source_id": source_id, **event}
            self.write_jsonl("raw_polar_hr_rr.jsonl", payload)

    def write_json(self, filename: str, payload: dict[str, Any]) -> None:
        path = self.session_dir / filename
        with path.open("w", encoding="utf-8") as fh:
            json.dump(_json_ready(payload), fh, indent=2, sort_keys=True)
            fh.write("\n")

    def write_jsonl(self, filename: str, payload: dict[str, Any]) -> None:
        fh = self._files.get(filename)
        if fh is None:
            fh = (self.session_dir / filename).open("a", encoding="utf-8")
            self._files[filename] = fh
        fh.write(json.dumps(_json_ready(payload), separators=(",", ":")) + "\n")
        fh.flush()

    def flush(self) -> None:
        for fh in self._files.values():
            fh.flush()
        if self._marker_csv is not None:
            self._marker_csv.flush()

    def close(self) -> None:
        self.flush()
        for fh in self._files.values():
            fh.close()
        self._files.clear()
        if self._marker_csv is not None:
            self._marker_csv.close()
            self._marker_csv = None

    def __enter__(self) -> "SessionWriter":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


def _json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return _json_ready(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
    except Exception:
        pass
    return value
