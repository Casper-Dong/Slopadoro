from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np


@dataclass(frozen=True)
class TimestampedWindow:
    timestamps: np.ndarray
    samples: np.ndarray

    @property
    def empty(self) -> bool:
        return self.timestamps.size == 0

    @property
    def start(self) -> float | None:
        return None if self.empty else float(self.timestamps[0])

    @property
    def end(self) -> float | None:
        return None if self.empty else float(self.timestamps[-1])


class TimestampedRollingBuffer:
    def __init__(self, max_seconds: float, expected_rate_hz: float | None = None, max_samples: int | None = None) -> None:
        if max_samples is None:
            if expected_rate_hz is None:
                max_samples = 10000
            else:
                max_samples = max(1, int(max_seconds * expected_rate_hz * 1.5) + 16)
        self.max_seconds = float(max_seconds)
        self._timestamps: deque[float] = deque(maxlen=max_samples)
        self._samples: deque[Any] = deque(maxlen=max_samples)

    def append(self, timestamp: float, sample: Any) -> None:
        self._timestamps.append(float(timestamp))
        self._samples.append(sample)
        self._trim_old()

    def extend(self, timestamps: Iterable[float], samples: Iterable[Any]) -> None:
        for ts, sample in zip(timestamps, samples, strict=False):
            self.append(float(ts), sample)

    def latest_timestamp(self) -> float | None:
        return None if not self._timestamps else self._timestamps[-1]

    def __len__(self) -> int:
        return len(self._timestamps)

    def window(self, start: float, end: float) -> TimestampedWindow:
        if end < start:
            raise ValueError("window end must be >= start")
        timestamps = np.asarray(self._timestamps, dtype=float)
        if timestamps.size == 0:
            return TimestampedWindow(np.asarray([], dtype=float), np.asarray([]))
        mask = (timestamps >= float(start)) & (timestamps <= float(end))
        selected_ts = timestamps[mask]
        if selected_ts.size == 0:
            return TimestampedWindow(selected_ts, np.asarray([]))
        sample_list = list(self._samples)
        selected_samples = [sample_list[i] for i, keep in enumerate(mask) if keep]
        try:
            samples = np.asarray(selected_samples, dtype=float)
        except (TypeError, ValueError):
            samples = np.asarray(selected_samples, dtype=object)
        return TimestampedWindow(selected_ts, samples)

    def latest_window(self, duration_seconds: float, end: float | None = None) -> TimestampedWindow:
        if end is None:
            end = self.latest_timestamp()
        if end is None:
            return TimestampedWindow(np.asarray([], dtype=float), np.asarray([]))
        return self.window(float(end) - float(duration_seconds), float(end))

    def clear(self) -> None:
        self._timestamps.clear()
        self._samples.clear()

    def _trim_old(self) -> None:
        if not self._timestamps:
            return
        newest = self._timestamps[-1]
        cutoff = newest - self.max_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
            self._samples.popleft()


class MultiStreamBuffers:
    def __init__(
        self,
        eeg_rate_hz: float,
        emg_rate_hz: float,
        ecg_rate_hz: float,
        max_seconds: float = 600.0,
    ) -> None:
        self.eeg = TimestampedRollingBuffer(max_seconds, eeg_rate_hz)
        self.emg = TimestampedRollingBuffer(max_seconds, emg_rate_hz)
        self.ecg = TimestampedRollingBuffer(max_seconds, ecg_rate_hz)
        self.hr_rr = TimestampedRollingBuffer(max_seconds, 2.0)
        self.markers = TimestampedRollingBuffer(max_seconds, 1.0)

    def add_marker(self, marker: dict[str, Any]) -> None:
        self.markers.append(float(marker["timestamp"]), marker)
