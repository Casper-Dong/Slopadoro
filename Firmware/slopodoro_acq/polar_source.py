from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from .config import AcquisitionConfig
from .lsl_streams import NoopOutlet, lsl_now

try:
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError
    from bleakheart import HeartRate, PolarMeasurementData
except Exception:  # pragma: no cover - hardware dependency path.
    BleakClient = None  # type: ignore[assignment]
    BleakScanner = None  # type: ignore[assignment]
    BleakError = Exception  # type: ignore[assignment]
    HeartRate = None  # type: ignore[assignment]
    PolarMeasurementData = None  # type: ignore[assignment]

try:
    from pylsl import StreamInfo, StreamOutlet
except Exception:  # pragma: no cover
    StreamInfo = None  # type: ignore[assignment]
    StreamOutlet = None  # type: ignore[assignment]


@dataclass(frozen=True)
class PolarDrain:
    ecg_timestamps: list[float]
    ecg_samples: list[float]
    rr_events: list[dict[str, float]]


class PolarH10Source:
    def __init__(self, cfg: AcquisitionConfig, *, publish_lsl: bool = True) -> None:
        self.cfg = cfg
        self.publish_lsl = publish_lsl
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.ecg_buffer: deque[tuple[float, float]] = deque(maxlen=200000)
        self.rr_buffer: deque[dict[str, float]] = deque(maxlen=20000)
        self.lock = threading.Lock()
        self.ecg_samples_received = 0
        self.hr_rr_events_received = 0
        self.last_ecg_timestamp: float | None = None
        self.last_hr_timestamp: float | None = None
        self.reconnect_count = 0
        self.stream_active = False
        self._started_at: float | None = None

    def start(self) -> None:
        if not self.cfg.polar.device_id:
            raise RuntimeError("polar.device_id must be set for live Polar H10 acquisition")
        if BleakClient is None or BleakScanner is None or HeartRate is None or PolarMeasurementData is None:
            raise RuntimeError("bleak and bleakheart are required for live Polar H10 acquisition")
        self.stop_event.clear()
        self._started_at = lsl_now()
        self.thread = threading.Thread(target=self._thread_main, name="PolarH10Source", daemon=True)
        self.thread.start()

    def drain(self) -> PolarDrain:
        with self.lock:
            ecg = list(self.ecg_buffer)
            rr = list(self.rr_buffer)
            self.ecg_buffer.clear()
            self.rr_buffer.clear()
        return PolarDrain(
            ecg_timestamps=[float(ts) for ts, _sample in ecg],
            ecg_samples=[float(sample) for _ts, sample in ecg],
            rr_events=rr,
        )

    def health(self) -> dict[str, Any]:
        now = lsl_now()
        elapsed = max(1e-6, now - self._started_at) if self._started_at else 0.0
        return {
            "stream_active": self.stream_active,
            "ecg_effective_sample_rate": float(self.ecg_samples_received / elapsed) if elapsed else 0.0,
            "hr_rr_update_rate": float(self.hr_rr_events_received / elapsed) if elapsed else 0.0,
            "last_ecg_timestamp": self.last_ecg_timestamp,
            "last_hr_timestamp": self.last_hr_timestamp,
            "reconnect_count": self.reconnect_count,
        }

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5.0)
            self.thread = None

    def _thread_main(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        ecg_outlet = _build_outlet(
            self.cfg.polar.stream_name_ecg,
            "ECG",
            self.cfg.polar.expected_ecg_sample_rate_hz,
            self.cfg.polar.source_id_ecg,
            "ECG",
            "uV",
        )
        rr_outlet = _build_outlet(
            self.cfg.polar.stream_name_hr,
            "RR",
            0.0,
            self.cfg.polar.source_id_hr,
            "RR",
            "ms",
        )
        mapper = _ClockMapper()
        while not self.stop_event.is_set():
            try:
                device = await _resolve_device(str(self.cfg.polar.device_id), self.cfg.polar.scan_timeout_seconds)
                if device is None:
                    await asyncio.sleep(self.cfg.polar.reconnect_delay_seconds)
                    continue
                mapper.reset()
                disconnected = asyncio.Event()

                def _on_disconnect(_client: Any) -> None:
                    disconnected.set()

                async with BleakClient(device, disconnected_callback=_on_disconnect) as client:  # type: ignore[misc]
                    ecg_queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
                    hr_queue: asyncio.Queue = asyncio.Queue(maxsize=2048)
                    pmd = PolarMeasurementData(client, ecg_queue=ecg_queue)  # type: ignore[operator]
                    hr = HeartRate(client, queue=hr_queue, instant_rate=False, unpack=True)  # type: ignore[operator]
                    await hr.start_notify()
                    if self.cfg.polar.collect_ecg:
                        err_code, err_msg, _payload = await pmd.start_streaming("ECG")
                        if err_code != 0:
                            raise RuntimeError(f"Polar PMD ECG start failed ({err_code}): {err_msg}")
                    self.stream_active = True
                    await self._consume_queues(ecg_queue, hr_queue, ecg_outlet, rr_outlet, mapper, disconnected)
                    self.stream_active = False
                    try:
                        await pmd.stop_streaming("ECG")
                    except Exception:
                        pass
                    try:
                        await hr.stop_notify()
                    except Exception:
                        pass
                    self.reconnect_count += 1
            except BleakError:
                self.stream_active = False
                self.reconnect_count += 1
            except Exception:
                self.stream_active = False
                self.reconnect_count += 1
            if not self.stop_event.is_set():
                await asyncio.sleep(self.cfg.polar.reconnect_delay_seconds)

    async def _consume_queues(
        self,
        ecg_queue: asyncio.Queue,
        hr_queue: asyncio.Queue,
        ecg_outlet: Any,
        rr_outlet: Any,
        mapper: "_ClockMapper",
        disconnected: asyncio.Event,
    ) -> None:
        while not self.stop_event.is_set() and not disconnected.is_set():
            while True:
                try:
                    hr_frame = hr_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if not hr_frame or hr_frame[0] != "HR":
                    continue
                _tag, tstamp_ns, hr_tuple, _energy = hr_frame
                if not isinstance(hr_tuple, tuple) or len(hr_tuple) != 2:
                    continue
                bpm, rr_ms = hr_tuple
                ts = mapper.map(tstamp_ns)
                if rr_ms is not None:
                    event = {"timestamp": float(ts), "rr_ms": float(rr_ms), "heart_rate": float(bpm) if bpm else float("nan")}
                    rr_outlet.push_sample([float(rr_ms)], ts)
                    with self.lock:
                        self.rr_buffer.append(event)
                    self.hr_rr_events_received += 1
                    self.last_hr_timestamp = float(ts)
            try:
                frame = await asyncio.wait_for(ecg_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            if not frame or frame[0] != "ECG":
                continue
            _tag, tstamp_ns, payload = frame
            samples = [float(v) for v in payload]
            if not samples:
                continue
            t_last = mapper.map(tstamp_ns)
            n = len(samples)
            timestamps = [t_last - ((n - 1 - i) / self.cfg.polar.expected_ecg_sample_rate_hz) for i in range(n)]
            for sample, ts in zip(samples, timestamps, strict=False):
                ecg_outlet.push_sample([sample], ts)
            with self.lock:
                self.ecg_buffer.extend(zip(timestamps, samples, strict=False))
            self.ecg_samples_received += n
            self.last_ecg_timestamp = float(timestamps[-1])


class _ClockMapper:
    def __init__(self, warmup_s: float = 30.0, lock_after_n: int = 64) -> None:
        self.warmup_s = float(warmup_s)
        self.lock_after_n = int(lock_after_n)
        self._first_obs: float | None = None
        self._n_obs = 0
        self._running_min_delta: float | None = None
        self._locked_offset: float | None = None

    def reset(self) -> None:
        self._first_obs = None
        self._n_obs = 0
        self._running_min_delta = None
        self._locked_offset = None

    def map(self, t_ns: int | float) -> float:
        t_ble = float(t_ns) / 1_000_000_000.0
        if self._locked_offset is not None:
            return t_ble + self._locked_offset
        now = lsl_now()
        delta = now - t_ble
        self._first_obs = now if self._first_obs is None else self._first_obs
        self._n_obs += 1
        if self._running_min_delta is None or delta < self._running_min_delta:
            self._running_min_delta = delta
        if (now - self._first_obs) >= self.warmup_s and self._n_obs >= self.lock_after_n:
            self._locked_offset = self._running_min_delta
        return t_ble + float(self._running_min_delta)


async def _resolve_device(device_id: str, timeout_s: float) -> Any:
    needle = device_id.lower()

    def _matcher(dev: Any, _adv: Any) -> bool:
        if dev.address and dev.address.lower() == needle:
            return True
        if dev.name and needle in dev.name.lower():
            return True
        return False

    return await BleakScanner.find_device_by_filter(_matcher, timeout=timeout_s)  # type: ignore[union-attr]


def _build_outlet(name: str, stream_type: str, sample_rate: float, source_id: str, label: str, unit: str) -> Any:
    if StreamInfo is None or StreamOutlet is None:
        return NoopOutlet()
    info = StreamInfo(name, stream_type, 1, float(sample_rate), "float32", source_id)
    info.desc().append_child_value("manufacturer", "Polar")
    info.desc().append_child_value("model", "H10")
    channels = info.desc().append_child("channels")
    ch = channels.append_child("channel")
    ch.append_child_value("label", label)
    ch.append_child_value("unit", unit)
    ch.append_child_value("type", stream_type)
    return StreamOutlet(info, chunk_size=0, max_buffered=360)
