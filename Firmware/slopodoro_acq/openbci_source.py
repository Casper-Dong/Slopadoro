from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import AcquisitionConfig
from .lsl_streams import NoopOutlet, lsl_now

try:
    from brainflow.board_shim import BoardIds, BoardShim, BrainFlowInputParams
except Exception:  # pragma: no cover - hardware dependency path.
    BoardIds = None  # type: ignore[assignment]
    BoardShim = None  # type: ignore[assignment]
    BrainFlowInputParams = None  # type: ignore[assignment]

try:
    from pylsl import StreamInfo, StreamOutlet
except Exception:  # pragma: no cover
    StreamInfo = None  # type: ignore[assignment]
    StreamOutlet = None  # type: ignore[assignment]


@dataclass(frozen=True)
class OpenBCIChunk:
    timestamps: np.ndarray
    samples: np.ndarray


class OpenBCISource:
    def __init__(self, cfg: AcquisitionConfig, *, publish_lsl: bool = True) -> None:
        self.cfg = cfg
        self.publish_lsl = publish_lsl
        self.board: Any = None
        self.outlet: Any = NoopOutlet()
        self.started_at: float | None = None
        self.samples_received = 0
        self.last_sample_timestamp: float | None = None
        self.dropped_packet_estimate = 0
        self._last_package_num: int | None = None
        self._packet_step: int | None = None
        self._exg_channels: list[int] = []
        self._timestamp_channel: int | None = None
        self._package_channel: int | None = None
        self._sample_rate = float(cfg.openbci.expected_sample_rate_hz)

    def start(self) -> None:
        if BoardShim is None or BrainFlowInputParams is None:
            raise RuntimeError("brainflow is not importable; install brainflow to use live OpenBCI acquisition")
        board_id = self.cfg.openbci.connection.brainflow_board_id
        if board_id is None:
            board_id = _default_board_id(self.cfg.openbci.board)
        params = BrainFlowInputParams()
        params.serial_port = self.cfg.openbci.connection.serial_port or ""
        BoardShim.enable_dev_board_logger()
        self.board = BoardShim(int(board_id), params)
        self.board.prepare_session()
        for cmd in self.cfg.openbci.board_commands:
            self.board.config_board(cmd)

        self._sample_rate = float(BoardShim.get_sampling_rate(int(board_id)))
        self._exg_channels = list(BoardShim.get_exg_channels(int(board_id)))[:16]
        self._timestamp_channel = int(BoardShim.get_timestamp_channel(int(board_id)))
        try:
            self._package_channel = int(BoardShim.get_package_num_channel(int(board_id)))
        except Exception:
            self._package_channel = None

        if self.publish_lsl:
            self.outlet = _build_outlet(self.cfg, self._sample_rate)
        self.board.start_stream(
            int(self.cfg.openbci.connection.startup_buffer_samples),
            self.cfg.openbci.connection.streamer_params,
        )
        self.started_at = lsl_now()

    def poll(self) -> OpenBCIChunk:
        if self.board is None:
            return OpenBCIChunk(np.asarray([], dtype=float), np.empty((0, 16), dtype=float))
        data = self.board.get_board_data()
        if data.size == 0 or data.shape[1] == 0:
            return OpenBCIChunk(np.asarray([], dtype=float), np.empty((0, 16), dtype=float))
        n_samples = int(data.shape[1])
        rows = data[np.asarray(self._exg_channels, dtype=int), :].T.astype(float, copy=False)
        if rows.shape[1] < 16:
            pad = np.full((rows.shape[0], 16 - rows.shape[1]), np.nan, dtype=float)
            rows = np.hstack([rows, pad])
        elif rows.shape[1] > 16:
            rows = rows[:, :16]
        timestamps = self._timestamps_from_data(data, n_samples)
        self._update_packet_health(data)
        self.outlet.push_chunk(rows.tolist(), timestamps.tolist())
        self.samples_received += n_samples
        self.last_sample_timestamp = float(timestamps[-1])
        return OpenBCIChunk(timestamps, rows)

    def health(self) -> dict[str, Any]:
        now = lsl_now()
        elapsed = max(1e-6, (now - self.started_at)) if self.started_at else 0.0
        return {
            "stream_active": self.board is not None,
            "effective_sample_rate": float(self.samples_received / elapsed) if elapsed else 0.0,
            "samples_received": self.samples_received,
            "channels_received": 16,
            "last_sample_timestamp": self.last_sample_timestamp,
            "dropped_packet_estimate": self.dropped_packet_estimate,
            "bad_channel_flags": [],
        }

    def stop(self) -> None:
        if self.board is None:
            return
        try:
            self.board.stop_stream()
        except Exception:
            pass
        try:
            self.board.release_session()
        except Exception:
            pass
        self.board = None

    def _timestamps_from_data(self, data: np.ndarray, n_samples: int) -> np.ndarray:
        end = lsl_now()
        return end - (np.arange(n_samples - 1, -1, -1, dtype=float) / self._sample_rate)

    def _update_packet_health(self, data: np.ndarray) -> None:
        if self._package_channel is None:
            return
        try:
            packages = np.asarray(data[self._package_channel, :], dtype=int)
        except Exception:
            return
        for package in packages:
            if self._last_package_num is not None:
                diff = int((int(package) - self._last_package_num) % 256)
                if diff > 0 and self._packet_step is None:
                    self._packet_step = diff
                elif diff > 0 and self._packet_step is not None and diff != self._packet_step:
                    self.dropped_packet_estimate += max(0, int(round(diff / max(self._packet_step, 1))) - 1)
            self._last_package_num = int(package)


def _default_board_id(board: str) -> int:
    if BoardIds is None:
        return 2
    if board == "cyton_daisy":
        return int(BoardIds.CYTON_DAISY_BOARD.value)
    if board == "cyton":
        return int(BoardIds.CYTON_BOARD.value)
    raise ValueError(f"Unsupported OpenBCI board: {board}")


def _build_outlet(cfg: AcquisitionConfig, sample_rate_hz: float) -> Any:
    if StreamInfo is None or StreamOutlet is None:
        return NoopOutlet()
    info = StreamInfo(
        cfg.openbci.stream_name,
        "ExG",
        16,
        float(sample_rate_hz),
        "float32",
        cfg.openbci.source_id,
    )
    info.desc().append_child_value("manufacturer", "OpenBCI")
    info.desc().append_child_value("model", cfg.openbci.board)
    info.desc().append_child_value("session_id", cfg.session.session_id)
    channels = info.desc().append_child("channels")
    for ch in sorted(cfg.openbci.channels, key=lambda item: item.index):
        node = channels.append_child("channel")
        node.append_child_value("index", str(ch.index))
        node.append_child_value("label", ch.label)
        node.append_child_value("unit", ch.units)
        node.append_child_value("type", ch.type.upper())
        if ch.notes:
            node.append_child_value("notes", ch.notes)
    return StreamOutlet(info, chunk_size=0, max_buffered=360)
