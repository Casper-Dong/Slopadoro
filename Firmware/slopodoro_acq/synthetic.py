from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import AcquisitionConfig


@dataclass(frozen=True)
class SyntheticChunk:
    openbci_timestamps: np.ndarray
    openbci_samples: np.ndarray
    ecg_timestamps: np.ndarray
    ecg_samples: np.ndarray
    rr_events: list[dict[str, float]]


class SyntheticSignalGenerator:
    def __init__(self, cfg: AcquisitionConfig) -> None:
        self.cfg = cfg
        self.rng = np.random.default_rng(cfg.synthetic.seed)
        self.openbci_fs = float(cfg.openbci.expected_sample_rate_hz)
        self.ecg_fs = float(cfg.polar.expected_ecg_sample_rate_hz)

    def chunk(self, end_time: float, duration_seconds: float, profile: str) -> SyntheticChunk:
        open_t, open_samples = self.openbci_window(end_time, duration_seconds, profile)
        ecg_t, ecg_samples, rr_events = self.polar_window(end_time, duration_seconds, profile)
        return SyntheticChunk(open_t, open_samples, ecg_t, ecg_samples, rr_events)

    def openbci_window(self, end_time: float, duration_seconds: float, profile: str) -> tuple[np.ndarray, np.ndarray]:
        n = max(1, int(round(duration_seconds * self.openbci_fs)))
        timestamps = end_time - (np.arange(n - 1, -1, -1, dtype=float) / self.openbci_fs)
        rel_t = timestamps - timestamps[0]
        samples = np.zeros((n, 16), dtype=float)
        eeg_indices = self.cfg.openbci.eeg_indices_zero_based
        emg_indices = self.cfg.openbci.emg_indices_zero_based

        for order, phys_idx in enumerate(eeg_indices):
            label = self.cfg.openbci.eeg_channels[order].label
            samples[:, phys_idx] = self._eeg_channel(rel_t, profile, label, order)
        for order, phys_idx in enumerate(emg_indices):
            label = self.cfg.openbci.emg_channels[order].label
            samples[:, phys_idx] = self._emg_channel(rel_t, profile, label, order)

        if profile == "bad_contact":
            if eeg_indices:
                samples[:, eeg_indices[0]] = 0.0
            if len(eeg_indices) > 1:
                samples[:, eeg_indices[1]] += 500.0 * signal_square(rel_t, 2.0)
        return timestamps, samples

    def polar_window(self, end_time: float, duration_seconds: float, profile: str) -> tuple[np.ndarray, np.ndarray, list[dict[str, float]]]:
        n = max(1, int(round(duration_seconds * self.ecg_fs)))
        timestamps = end_time - (np.arange(n - 1, -1, -1, dtype=float) / self.ecg_fs)
        rel_t = timestamps - timestamps[0]
        base_rr = 790.0
        if profile == "fatigue_drift":
            base_rr = 830.0
        elif profile == "strain":
            base_rr = 720.0
        elif profile == "polar_disconnect":
            return np.asarray([], dtype=float), np.asarray([], dtype=float), []
        rr_jitter = 35.0 * np.sin(2 * np.pi * rel_t / 8.0)
        rr_ms = base_rr + rr_jitter
        beat_times = [timestamps[0]]
        while beat_times[-1] < end_time + 0.1:
            idx = min(len(rel_t) - 1, max(0, int((beat_times[-1] - timestamps[0]) * self.ecg_fs)))
            beat_times.append(beat_times[-1] + float(rr_ms[idx]) / 1000.0)
        beat_times = [bt for bt in beat_times if timestamps[0] <= bt <= end_time]

        ecg = 18.0 * self.rng.normal(size=n)
        for beat in beat_times:
            center = int(round((beat - timestamps[0]) * self.ecg_fs))
            for offset, amp in [(-2, -80.0), (-1, 180.0), (0, 750.0), (1, 180.0), (2, -70.0)]:
                idx = center + offset
                if 0 <= idx < n:
                    ecg[idx] += amp
        rr_events: list[dict[str, float]] = []
        for prev, curr in zip(beat_times, beat_times[1:], strict=False):
            rr_events.append({"timestamp": float(curr), "rr_ms": float((curr - prev) * 1000.0), "heart_rate": float(60.0 / (curr - prev))})
        return timestamps, ecg, rr_events

    def profile_for_elapsed(self, elapsed_seconds: float) -> str:
        if elapsed_seconds < 10.0:
            return "focused_task_baseline"
        if elapsed_seconds < 20.0:
            return "fatigue_drift"
        if elapsed_seconds < 30.0:
            return "strain"
        return "bad_contact"

    def _eeg_channel(self, t: np.ndarray, profile: str, label: str, idx: int) -> np.ndarray:
        lower = label.lower()
        posterior = lower.startswith(("p", "o"))
        frontal = lower.startswith(("fp", "f"))
        theta_amp = 4.0
        alpha_amp = 5.0 + (3.0 if posterior else 0.0)
        beta_amp = 7.0
        if profile == "eyes_closed_baseline":
            alpha_amp = 18.0 if posterior else 10.0
            beta_amp = 4.0
        elif profile == "focused_task_baseline":
            beta_amp = 11.0
            theta_amp = 3.5
            alpha_amp = 5.0 + (2.0 if posterior else 0.0)
        elif profile == "fatigue_drift":
            theta_amp = 11.0 + (2.0 if frontal else 0.0)
            beta_amp = 4.0
            alpha_amp = 6.0 + (2.0 if posterior else 0.0)
        elif profile == "strain":
            beta_amp = 8.0
            theta_amp = 5.0

        phase = idx * 0.31
        signal = (
            2.0 * np.sin(2 * np.pi * 2.0 * t + phase)
            + theta_amp * np.sin(2 * np.pi * 6.0 * t + phase)
            + alpha_amp * np.sin(2 * np.pi * 10.0 * t + phase)
            + beta_amp * np.sin(2 * np.pi * 20.0 * t + phase)
            + 4.0 * self.rng.normal(size=t.size)
        )
        if profile == "bad_contact":
            signal += 90.0 * self.rng.normal(size=t.size)
        return signal

    def _emg_channel(self, t: np.ndarray, profile: str, label: str, idx: int) -> np.ndarray:
        lower = label.lower()
        amp = 3.0
        if profile == "emg_strain_reference":
            amp = 28.0
        elif profile == "strain":
            amp = 30.0 if ("right" in lower or idx == 1) else 18.0
        elif profile == "fatigue_drift":
            amp = 7.0
        elif profile == "bad_contact":
            amp = 4.0
        return amp * np.sin(2 * np.pi * 32.0 * t + idx) + amp * 0.35 * self.rng.normal(size=t.size)


def signal_square(t: np.ndarray, hz: float) -> np.ndarray:
    return np.where(np.sin(2 * np.pi * hz * t) >= 0.0, 1.0, -1.0)
