from __future__ import annotations

from slopodoro_acq.ring_buffer import TimestampedRollingBuffer


def test_ring_buffer_window_slicing_keeps_timestamps_and_samples() -> None:
    buf = TimestampedRollingBuffer(max_seconds=10, expected_rate_hz=10)
    for idx in range(20):
        buf.append(float(idx) * 0.1, [idx, idx + 1])

    window = buf.window(0.5, 1.0)
    assert window.timestamps[0] == 0.5
    assert window.timestamps[-1] == 1.0
    assert window.samples.shape == (6, 2)
    assert window.samples[0].tolist() == [5, 6]


def test_ring_buffer_latest_window() -> None:
    buf = TimestampedRollingBuffer(max_seconds=2, expected_rate_hz=2)
    buf.extend([0.0, 0.5, 1.0, 1.5, 2.0], [[0], [1], [2], [3], [4]])
    window = buf.latest_window(0.6)
    assert window.timestamps.tolist() == [1.5, 2.0]
