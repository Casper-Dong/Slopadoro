from __future__ import annotations

import argparse
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import AcquisitionConfig, load_config

try:
    from pylsl import StreamInlet, local_clock, resolve_byprop
except Exception as exc:  # pragma: no cover - depends on local liblsl install.
    StreamInlet = None  # type: ignore[assignment]
    local_clock = None  # type: ignore[assignment]
    resolve_byprop = None  # type: ignore[assignment]
    _PYLSL_IMPORT_ERROR = exc
else:
    _PYLSL_IMPORT_ERROR = None


@dataclass
class LiveBuffer:
    max_samples: int
    timestamps: deque[float]
    samples: deque[list[float]]

    @classmethod
    def create(cls, max_samples: int) -> "LiveBuffer":
        return cls(max_samples=max_samples, timestamps=deque(maxlen=max_samples), samples=deque(maxlen=max_samples))

    def extend(self, timestamps: list[float], samples: list[list[float]]) -> None:
        for ts, row in zip(timestamps, samples, strict=False):
            self.timestamps.append(float(ts))
            self.samples.append([float(v) for v in row])

    def window(self, start: float, end: float) -> tuple[np.ndarray, np.ndarray]:
        if not self.timestamps:
            return np.asarray([], dtype=float), np.empty((0, 0), dtype=float)
        ts = np.asarray(self.timestamps, dtype=float)
        vals = np.asarray(self.samples, dtype=float)
        mask = (ts >= start) & (ts <= end)
        if not np.any(mask):
            return np.asarray([], dtype=float), np.empty((0, vals.shape[1] if vals.ndim == 2 else 0), dtype=float)
        return ts[mask], vals[mask]


class InletReader:
    def __init__(self, inlet: Any, *, max_samples: int) -> None:
        self.inlet = inlet
        self.buffer = LiveBuffer.create(max_samples=max_samples)
        self.time_correction = 0.0
        self._last_correction_poll = 0.0
        self.total_samples = 0
        self.last_sample_time: float | None = None

    def poll(self, max_samples: int = 1024) -> None:
        chunk, timestamps = self.inlet.pull_chunk(timeout=0.0, max_samples=max_samples)
        if not timestamps:
            return
        now = float(local_clock())
        if now - self._last_correction_poll >= 1.0:
            try:
                self.time_correction = float(self.inlet.time_correction(timeout=0.05))
            except Exception:
                self.time_correction = 0.0
            self._last_correction_poll = now
        corrected = [float(ts) + self.time_correction for ts in timestamps]
        self.buffer.extend(corrected, chunk)
        self.total_samples += len(corrected)
        self.last_sample_time = corrected[-1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live validation plot for Slopodoro raw LSL acquisition streams.")
    parser.add_argument("--config", default="config/slopodoro_acquisition.yaml", help="Path to acquisition config")
    parser.add_argument("--window-seconds", type=float, default=10.0, help="Visible time window")
    parser.add_argument("--display-delay-ms", type=float, default=150.0, help="Right-edge delay for BLE/LSL jitter")
    parser.add_argument("--refresh-ms", type=float, default=33.0, help="Plot refresh interval")
    parser.add_argument("--resolve-timeout-seconds", type=float, default=20.0, help="Initial LSL resolve timeout per stream")
    parser.add_argument("--openbci-stream", default=None, help="Override raw OpenBCI LSL stream name")
    parser.add_argument("--ecg-stream", default=None, help="Override raw Polar ECG LSL stream name")
    parser.add_argument("--require-ecg", action="store_true", help="Exit if the ECG stream is not found at startup")
    parser.add_argument(
        "--start-sources",
        action="store_true",
        help="Connect to configured OpenBCI/Polar devices directly, publish raw LSL, and plot in this process.",
    )
    parser.add_argument("--eeg-scale-uv", type=float, default=0.0, help="Fixed per-channel EEG vertical spacing; 0 = auto")
    parser.add_argument("--emg-scale-uv", type=float, default=0.0, help="Fixed per-channel EMG vertical spacing; 0 = auto")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if _PYLSL_IMPORT_ERROR is not None:
        print(f"pylsl import failed: {_PYLSL_IMPORT_ERROR}", file=sys.stderr)
        return 2

    args = parse_args(argv)
    cfg = load_config(args.config)

    try:
        import matplotlib.pyplot as plt
        from matplotlib.animation import FuncAnimation
    except Exception as exc:
        print(f"matplotlib import failed: {exc}", file=sys.stderr)
        return 2

    openbci_name = args.openbci_stream or cfg.openbci.stream_name
    ecg_name = args.ecg_stream or cfg.polar.stream_name_ecg

    source_handles: list[Any] = []
    if args.start_sources:
        openbci_reader, ecg_reader, source_handles = _start_direct_sources(cfg, args)
        if openbci_reader is None:
            return 1
        if args.require_ecg and ecg_reader is None:
            for handle in source_handles:
                _safe_stop(handle)
            return 1
    else:
        print(f"Resolving OpenBCI stream '{openbci_name}'...", flush=True)
        openbci_inlet = _resolve_inlet(openbci_name, args.resolve_timeout_seconds, required=True)
        if openbci_inlet is None:
            return 1

        print(f"Resolving ECG stream '{ecg_name}'...", flush=True)
        ecg_inlet = _resolve_inlet(ecg_name, args.resolve_timeout_seconds, required=args.require_ecg)
        if args.require_ecg and ecg_inlet is None:
            return 1

        openbci_reader = InletReader(openbci_inlet, max_samples=int(max(1000, args.window_seconds * cfg.openbci.expected_sample_rate_hz * 4)))
        ecg_reader = (
            InletReader(ecg_inlet, max_samples=int(max(1000, args.window_seconds * cfg.polar.expected_ecg_sample_rate_hz * 4)))
            if ecg_inlet is not None
            else None
        )

    eeg_indices = cfg.openbci.eeg_indices_zero_based
    emg_indices = cfg.openbci.emg_indices_zero_based
    eeg_labels = cfg.openbci.eeg_labels
    emg_labels = cfg.openbci.emg_labels

    fig, (ax_eeg, ax_emg, ax_ecg) = plt.subplots(3, 1, figsize=(14, 9), sharex=True)
    fig.canvas.manager.set_window_title("Slopodoro Acquisition Validation")
    fig.suptitle("Slopodoro Raw LSL Validation")

    status_text = ax_eeg.text(0.01, 0.98, "", transform=ax_eeg.transAxes, va="top", ha="left")
    eeg_lines = [ax_eeg.plot([], [], linewidth=0.8)[0] for _ in eeg_labels]
    emg_lines = [ax_emg.plot([], [], linewidth=1.0)[0] for _ in emg_labels]
    ecg_line = ax_ecg.plot([], [], linewidth=0.9, color="tab:red")[0]

    ax_eeg.set_title(f"EEG channels from {openbci_name}")
    ax_emg.set_title("Posture EMG channels")
    ax_ecg.set_title(f"ECG from {ecg_name}")
    ax_ecg.set_xlabel("Seconds from display edge")
    ax_eeg.set_ylabel("EEG stacked uV")
    ax_emg.set_ylabel("EMG stacked uV")
    ax_ecg.set_ylabel("ECG uV")
    for ax in (ax_eeg, ax_emg, ax_ecg):
        ax.grid(True, alpha=0.25)

    stopped = False

    def _on_close(_event: object) -> None:
        nonlocal stopped
        stopped = True

    fig.canvas.mpl_connect("close_event", _on_close)

    def _update(_frame_idx: int) -> list[Any]:
        if stopped:
            return [*eeg_lines, *emg_lines, ecg_line, status_text]

        openbci_reader.poll(max_samples=1024)
        if ecg_reader is not None:
            ecg_reader.poll(max_samples=1024)

        edge = float(local_clock()) - (float(args.display_delay_ms) / 1000.0)
        start = edge - float(args.window_seconds)

        open_ts, open_vals = openbci_reader.buffer.window(start, edge)
        if open_vals.size and open_vals.ndim == 2 and open_vals.shape[1] >= max(eeg_indices + emg_indices) + 1:
            x = open_ts - edge
            eeg_vals = open_vals[:, eeg_indices]
            emg_vals = open_vals[:, emg_indices]
            _set_stacked_lines(ax_eeg, eeg_lines, x, eeg_vals, eeg_labels, fixed_spacing=args.eeg_scale_uv)
            _set_stacked_lines(ax_emg, emg_lines, x, emg_vals, emg_labels, fixed_spacing=args.emg_scale_uv)

        if ecg_reader is not None:
            ecg_ts, ecg_vals = ecg_reader.buffer.window(start, edge)
            if ecg_vals.size:
                ecg_x = ecg_ts - edge
                ecg_y = ecg_vals[:, 0]
                ecg_line.set_data(ecg_x, ecg_y)
                _autoscale_single_axis(ax_ecg, ecg_x, ecg_y)
            else:
                ax_ecg.set_xlim(-args.window_seconds, 0.0)
        else:
            ax_ecg.text(0.5, 0.5, "ECG stream not resolved", transform=ax_ecg.transAxes, ha="center", va="center")

        ax_eeg.set_xlim(-args.window_seconds, 0.0)
        ax_emg.set_xlim(-args.window_seconds, 0.0)
        ax_ecg.set_xlim(-args.window_seconds, 0.0)

        status_text.set_text(_status_text(openbci_reader, ecg_reader, eeg_labels, emg_labels))
        return [*eeg_lines, *emg_lines, ecg_line, status_text]

    try:
        _animation = FuncAnimation(
            fig,
            _update,
            interval=max(10, int(args.refresh_ms)),
            blit=False,
            cache_frame_data=False,
        )
        plt.tight_layout()
        plt.show()
        return 0
    finally:
        for handle in source_handles:
            _safe_stop(handle)


def _resolve_inlet(stream_name: str, timeout_seconds: float, *, required: bool) -> Any | None:
    assert resolve_byprop is not None
    deadline = time.time() + max(0.1, float(timeout_seconds))
    while time.time() < deadline:
        streams = resolve_byprop("name", stream_name, timeout=0.5)
        if streams:
            print(f"Resolved '{stream_name}'.", flush=True)
            return StreamInlet(streams[0], max_buflen=60)
    message = f"Could not resolve LSL stream '{stream_name}' within {timeout_seconds:.1f}s."
    if required:
        print(message, file=sys.stderr)
    else:
        print(message + " Continuing without it.", flush=True)
    return None


class DirectOpenBCIReader:
    def __init__(self, source: Any, max_samples: int) -> None:
        self.source = source
        self.buffer = LiveBuffer.create(max_samples=max_samples)
        self.total_samples = 0
        self.last_sample_time: float | None = None
        self.mode = getattr(source, "mode", "brainflow")

    def poll(self, max_samples: int = 1024) -> None:
        chunk = self.source.poll()
        if chunk.timestamps.size == 0:
            return
        timestamps = chunk.timestamps.tolist()
        samples = chunk.samples.tolist()
        self.buffer.extend(timestamps, samples)
        self.total_samples += len(timestamps)
        self.last_sample_time = float(timestamps[-1])


class DirectPolarECGReader:
    def __init__(self, source: Any, max_samples: int) -> None:
        self.source = source
        self.buffer = LiveBuffer.create(max_samples=max_samples)
        self.total_samples = 0
        self.last_sample_time: float | None = None

    def poll(self, max_samples: int = 1024) -> None:
        drained = self.source.drain()
        if not drained.ecg_timestamps:
            return
        self.buffer.extend(drained.ecg_timestamps, [[sample] for sample in drained.ecg_samples])
        self.total_samples += len(drained.ecg_timestamps)
        self.last_sample_time = float(drained.ecg_timestamps[-1])


def _start_direct_sources(cfg: AcquisitionConfig, args: argparse.Namespace) -> tuple[Any | None, Any | None, list[Any]]:
    from .openbci_source import OpenBCISource
    from .polar_source import PolarH10Source

    handles: list[Any] = []
    if not cfg.openbci.connection.serial_port:
        print("Config field openbci.connection.serial_port is required for --start-sources.", file=sys.stderr)
        return None, None, handles

    print(f"Starting OpenBCI source on {cfg.openbci.connection.serial_port}; publishing '{cfg.openbci.stream_name}'...", flush=True)
    openbci_source = OpenBCISource(cfg, publish_lsl=True)
    try:
        openbci_source.start()
    except Exception as exc:
        print(f"OpenBCI BrainFlow start failed: {exc}", file=sys.stderr)
        _safe_stop(openbci_source)
        print("Trying raw serial fallback because COM10 appears to already be streaming Cyton+Daisy packets...", flush=True)
        try:
            openbci_source = RawCytonDaisySerialSource(cfg)
            openbci_source.start()
        except Exception as fallback_exc:
            print(f"OpenBCI raw serial fallback failed: {fallback_exc}", file=sys.stderr)
            _safe_stop(openbci_source)
            return None, None, handles
    handles.append(openbci_source)
    openbci_reader = DirectOpenBCIReader(
        openbci_source,
        max_samples=int(max(1000, args.window_seconds * cfg.openbci.expected_sample_rate_hz * 4)),
    )

    ecg_reader = None
    if cfg.polar.enabled:
        if not cfg.polar.device_id:
            message = "Config field polar.device_id is required to start Polar from the validation plot."
            if args.require_ecg:
                print(message, file=sys.stderr)
            else:
                print(message + " Continuing without ECG.", flush=True)
        else:
            print(f"Starting Polar source '{cfg.polar.device_id}'; publishing '{cfg.polar.stream_name_ecg}'...", flush=True)
            polar_source = PolarH10Source(cfg, publish_lsl=True)
            try:
                polar_source.start()
                handles.append(polar_source)
                ecg_reader = DirectPolarECGReader(
                    polar_source,
                    max_samples=int(max(1000, args.window_seconds * cfg.polar.expected_ecg_sample_rate_hz * 4)),
                )
            except Exception as exc:
                print(f"Polar start failed: {exc}", file=sys.stderr)
                _safe_stop(polar_source)
                if args.require_ecg:
                    return openbci_reader, None, handles
                print("Continuing without ECG.", flush=True)

    return openbci_reader, ecg_reader, handles


class RawCytonDaisySerialSource:
    """Best-effort validation-only reader for a Cyton+Daisy already streaming raw packets.

    BrainFlow normally owns acquisition. This fallback is only for the recovery
    case where the board is already emitting binary packets and will not answer
    BrainFlow's prepare-session query.
    """

    mode = "raw-serial-fallback"
    _PACKET_LEN = 33
    _ADS1299_SCALE_UV = 4.5 / 24.0 / ((2**23) - 1) * 1_000_000.0

    def __init__(self, cfg: AcquisitionConfig) -> None:
        self.cfg = cfg
        self.serial: Any = None
        self._buffer = bytearray()
        self._pending_cyton: list[float] | None = None
        self._sample_rate = float(cfg.openbci.expected_sample_rate_hz)

    def start(self) -> None:
        import serial

        port = self.cfg.openbci.connection.serial_port
        if not port:
            raise RuntimeError("openbci.connection.serial_port is required for raw serial fallback")
        self.serial = serial.Serial(port, 115200, timeout=0.0, write_timeout=0.1)
        self.serial.reset_input_buffer()

        deadline = time.time() + 3.0
        while time.time() < deadline:
            chunk = self.poll()
            if chunk.timestamps.size:
                self._replay_chunk = chunk
                print("OpenBCI raw serial fallback is receiving Cyton+Daisy packets.", flush=True)
                return
            time.sleep(0.05)
        raise RuntimeError("COM port opened, but no parseable Cyton+Daisy packets arrived")

    def poll(self) -> Any:
        from .openbci_source import OpenBCIChunk

        replay = getattr(self, "_replay_chunk", None)
        if replay is not None:
            self._replay_chunk = None
            return replay
        if self.serial is None:
            return OpenBCIChunk(np.asarray([], dtype=float), np.empty((0, 16), dtype=float))

        waiting = getattr(self.serial, "in_waiting", 0)
        data = self.serial.read(max(4096, waiting or 4096))
        if data:
            self._buffer.extend(data)

        samples: list[list[float]] = []
        while True:
            packet = self._next_packet()
            if packet is None:
                break
            sample_id = int(packet[1])
            channels = [_decode_openbci_24bit(packet[2 + 3 * idx : 5 + 3 * idx]) * self._ADS1299_SCALE_UV for idx in range(8)]
            if sample_id % 2 == 1:
                self._pending_cyton = channels
            elif self._pending_cyton is not None:
                samples.append(self._pending_cyton + channels)
                self._pending_cyton = None

        if not samples:
            return OpenBCIChunk(np.asarray([], dtype=float), np.empty((0, 16), dtype=float))
        end = float(local_clock())
        timestamps = end - (np.arange(len(samples) - 1, -1, -1, dtype=float) / self._sample_rate)
        return OpenBCIChunk(timestamps, np.asarray(samples, dtype=float))

    def stop(self) -> None:
        if self.serial is None:
            return
        try:
            for _ in range(3):
                self.serial.write(b"s")
                self.serial.flush()
                time.sleep(0.05)
        except Exception:
            pass
        try:
            self.serial.close()
        except Exception:
            pass
        self.serial = None

    def _next_packet(self) -> bytes | None:
        while True:
            start = self._buffer.find(b"\xA0")
            if start < 0:
                if len(self._buffer) > self._PACKET_LEN:
                    del self._buffer[:-self._PACKET_LEN]
                return None
            if start > 0:
                del self._buffer[:start]
            if len(self._buffer) < self._PACKET_LEN:
                return None
            packet = bytes(self._buffer[: self._PACKET_LEN])
            if 0xC0 <= packet[-1] <= 0xCF:
                del self._buffer[: self._PACKET_LEN]
                return packet
            del self._buffer[0]


def _decode_openbci_24bit(raw: bytes | bytearray) -> int:
    value = (int(raw[0]) << 16) | (int(raw[1]) << 8) | int(raw[2])
    if value & 0x800000:
        value -= 0x1000000
    return value


def _safe_stop(handle: Any) -> None:
    try:
        handle.stop()
    except Exception:
        pass


def _set_stacked_lines(
    ax: Any,
    lines: list[Any],
    x: np.ndarray,
    values: np.ndarray,
    labels: list[str],
    *,
    fixed_spacing: float,
) -> None:
    centered = values - np.nanmedian(values, axis=0, keepdims=True)
    spacing = float(fixed_spacing) if fixed_spacing > 0 else _auto_spacing(centered)
    offsets = np.arange(len(labels), dtype=float)[::-1] * spacing
    for idx, line in enumerate(lines):
        line.set_data(x, centered[:, idx] + offsets[idx])
    ax.set_yticks(offsets)
    ax.set_yticklabels(labels)
    ax.set_ylim(-spacing, offsets[0] + spacing)


def _auto_spacing(values: np.ndarray) -> float:
    if values.size == 0:
        return 1.0
    span = np.nanpercentile(values, 95, axis=0) - np.nanpercentile(values, 5, axis=0)
    span = span[np.isfinite(span)]
    if span.size == 0:
        return 1.0
    return float(max(1.0, np.nanmedian(span) * 1.4))


def _autoscale_single_axis(ax: Any, x: np.ndarray, y: np.ndarray) -> None:
    if x.size == 0 or y.size == 0:
        return
    finite = np.isfinite(y)
    if not np.any(finite):
        return
    low = float(np.nanpercentile(y[finite], 2))
    high = float(np.nanpercentile(y[finite], 98))
    if abs(high - low) < 1e-9:
        pad = max(1.0, abs(low) * 0.1)
        ax.set_ylim(low - pad, high + pad)
    else:
        pad = 0.2 * (high - low)
        ax.set_ylim(low - pad, high + pad)


def _status_text(openbci_reader: InletReader, ecg_reader: InletReader | None, eeg_labels: list[str], emg_labels: list[str]) -> str:
    now = float(local_clock())
    open_age = _age_ms(now, openbci_reader.last_sample_time)
    ecg_age = _age_ms(now, ecg_reader.last_sample_time) if ecg_reader is not None else None
    ecg_status = "missing" if ecg_reader is None else f"{ecg_reader.total_samples} samples, age {ecg_age:.0f} ms"
    mode = getattr(openbci_reader, "mode", "unknown")
    return (
        f"OpenBCI({mode}): {openbci_reader.total_samples} samples, age {open_age:.0f} ms | "
        f"EEG {len(eeg_labels)} ch | EMG {', '.join(emg_labels)} | ECG: {ecg_status}"
    )


def _age_ms(now: float, timestamp: float | None) -> float:
    if timestamp is None:
        return float("nan")
    return max(0.0, (now - timestamp) * 1000.0)


if __name__ == "__main__":
    raise SystemExit(main())
