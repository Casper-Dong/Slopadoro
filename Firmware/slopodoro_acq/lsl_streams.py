from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .config import AcquisitionConfig, ChannelConfig

try:  # pylsl can be absent on CI or fail to load if liblsl is unavailable.
    from pylsl import StreamInfo, StreamOutlet, local_clock as _lsl_local_clock
except Exception:  # pragma: no cover - exercised only on machines without LSL.
    StreamInfo = None  # type: ignore[assignment]
    StreamOutlet = None  # type: ignore[assignment]

    def _lsl_local_clock() -> float:
        return time.monotonic()


MARKER_EVENTS = {
    "calibration_start",
    "eyes_open_start",
    "eyes_open_end",
    "eyes_closed_start",
    "eyes_closed_end",
    "focused_task_start",
    "focused_task_end",
    "strain_neutral_start",
    "strain_neutral_end",
    "strain_reference_start",
    "strain_reference_end",
    "work_start",
    "work_end",
    "acquisition_start",
    "acquisition_stop",
}


def lsl_now() -> float:
    return float(_lsl_local_clock())


class NoopOutlet:
    def push_sample(self, _sample: list[Any], timestamp: float | None = None) -> None:
        return None

    def push_chunk(self, _chunk: list[list[Any]], timestamp: Any = None) -> None:
        return None


@dataclass
class LSLStreamManager:
    cfg: AcquisitionConfig
    marker_outlet: Any
    feature_outlet: Any
    score_outlet: Any
    enabled: bool

    @classmethod
    def create(cls, cfg: AcquisitionConfig, enabled: bool = True) -> "LSLStreamManager":
        if not enabled or StreamInfo is None or StreamOutlet is None:
            return cls(cfg=cfg, marker_outlet=NoopOutlet(), feature_outlet=NoopOutlet(), score_outlet=NoopOutlet(), enabled=False)

        marker_info = StreamInfo(cfg.lsl.marker_stream_name, "Markers", 1, 0.0, "string", f"{cfg.session.session_id}_markers")
        _append_session_metadata(marker_info, cfg)
        marker_info.desc().append_child_value("payload_format", "json")
        marker_outlet = StreamOutlet(marker_info)

        feature_info = StreamInfo(cfg.lsl.feature_stream_name, "Features", 1, 0.0, "string", f"{cfg.session.session_id}_features")
        _append_session_metadata(feature_info, cfg)
        feature_info.desc().append_child_value("payload_format", "json")
        _append_channel_map(feature_info, cfg.openbci.channels)
        feature_outlet = StreamOutlet(feature_info)

        score_info = StreamInfo(cfg.lsl.score_stream_name, "Scores", 1, 0.0, "string", f"{cfg.session.session_id}_scores")
        _append_session_metadata(score_info, cfg)
        score_info.desc().append_child_value("payload_format", "json")
        score_outlet = StreamOutlet(score_info)

        return cls(cfg=cfg, marker_outlet=marker_outlet, feature_outlet=feature_outlet, score_outlet=score_outlet, enabled=True)

    def push_marker(self, event: str, timestamp: float | None = None, **fields: Any) -> dict[str, Any]:
        if event not in MARKER_EVENTS:
            raise ValueError(f"Unsupported marker event: {event}")
        ts = lsl_now() if timestamp is None else float(timestamp)
        payload = {
            "timestamp": ts,
            "session_id": self.cfg.session.session_id,
            "participant_id": self.cfg.session.participant_id,
            "event": event,
            **fields,
        }
        self.marker_outlet.push_sample([json.dumps(payload, separators=(",", ":"))], ts)
        return payload

    def push_features(self, frame: dict[str, Any]) -> None:
        ts = float(frame.get("timestamp", lsl_now()))
        self.feature_outlet.push_sample([json.dumps(frame, separators=(",", ":"))], ts)

    def push_score(self, frame: dict[str, Any]) -> None:
        ts = float(frame.get("timestamp", lsl_now()))
        self.score_outlet.push_sample([json.dumps(frame, separators=(",", ":"))], ts)


@dataclass
class RawLSLPublisher:
    cfg: AcquisitionConfig
    openbci_outlet: Any
    ecg_outlet: Any
    hr_rr_outlet: Any
    enabled: bool

    @classmethod
    def create(cls, cfg: AcquisitionConfig, enabled: bool = True) -> "RawLSLPublisher":
        if not enabled or StreamInfo is None or StreamOutlet is None:
            return cls(cfg, NoopOutlet(), NoopOutlet(), NoopOutlet(), False)

        open_info = StreamInfo(
            cfg.openbci.stream_name,
            "ExG",
            16,
            cfg.openbci.expected_sample_rate_hz,
            "float32",
            cfg.openbci.source_id,
        )
        _append_session_metadata(open_info, cfg)
        _append_channel_map(open_info, cfg.openbci.channels)
        openbci_outlet = StreamOutlet(open_info, chunk_size=0, max_buffered=360)

        ecg_info = StreamInfo(
            cfg.polar.stream_name_ecg,
            "ECG",
            1,
            cfg.polar.expected_ecg_sample_rate_hz,
            "float32",
            cfg.polar.source_id_ecg,
        )
        _append_session_metadata(ecg_info, cfg)
        _append_single_channel(ecg_info, "ECG", "uV", "ECG")
        ecg_outlet = StreamOutlet(ecg_info, chunk_size=0, max_buffered=360)

        rr_info = StreamInfo(cfg.polar.stream_name_hr, "RR", 2, 0.0, "float32", cfg.polar.source_id_hr)
        _append_session_metadata(rr_info, cfg)
        channels = rr_info.desc().append_child("channels")
        for label, unit, typ in [("RR", "ms", "RR"), ("HR", "bpm", "HR")]:
            ch = channels.append_child("channel")
            ch.append_child_value("label", label)
            ch.append_child_value("unit", unit)
            ch.append_child_value("type", typ)
        hr_rr_outlet = StreamOutlet(rr_info, chunk_size=0, max_buffered=360)
        return cls(cfg, openbci_outlet, ecg_outlet, hr_rr_outlet, True)

    def push_openbci(self, timestamps: list[float], samples: list[list[float]]) -> None:
        if samples:
            self.openbci_outlet.push_chunk(samples, timestamps)

    def push_ecg(self, timestamps: list[float], samples: list[float]) -> None:
        if samples:
            self.ecg_outlet.push_chunk([[float(sample)] for sample in samples], timestamps)

    def push_hr_rr(self, events: list[dict[str, float]]) -> None:
        for event in events:
            ts = float(event["timestamp"])
            self.hr_rr_outlet.push_sample([float(event.get("rr_ms", 0.0)), float(event.get("heart_rate", 0.0))], ts)


def _append_session_metadata(info: Any, cfg: AcquisitionConfig) -> None:
    info.desc().append_child_value("session_id", cfg.session.session_id)
    info.desc().append_child_value("participant_id", cfg.session.participant_id)
    info.desc().append_child_value("created_by", "slopodoro_acq")


def _append_channel_map(info: Any, channels: tuple[ChannelConfig, ...]) -> None:
    channel_map = info.desc().append_child("openbci_channel_map")
    for ch in channels:
        item = channel_map.append_child("channel")
        item.append_child_value("index", str(ch.index))
        item.append_child_value("label", ch.label)
        item.append_child_value("signal_type", ch.type)
        item.append_child_value("enabled", "true" if ch.enabled else "false")
        item.append_child_value("unit", ch.units)
        if ch.notes:
            item.append_child_value("notes", ch.notes)


def _append_single_channel(info: Any, label: str, unit: str, stream_type: str) -> None:
    channels = info.desc().append_child("channels")
    ch = channels.append_child("channel")
    ch.append_child_value("label", label)
    ch.append_child_value("unit", unit)
    ch.append_child_value("type", stream_type)
