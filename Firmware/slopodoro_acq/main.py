from __future__ import annotations

import argparse
import signal
import sys
import time
from typing import Any

import numpy as np

from .calibration import build_calibration_model, calibration_phases
from .config import AcquisitionConfig, ConfigError, load_config
from .features_ecg import compute_ecg_features
from .features_eeg import EEGFeatureExtractor
from .features_emg import EMGFeatureExtractor
from .lsl_streams import LSLStreamManager, RawLSLPublisher, lsl_now
from .openbci_source import OpenBCISource
from .persistence import SessionWriter
from .polar_source import PolarH10Source
from .scoring import RuleBasedScorer
from .synthetic import SyntheticSignalGenerator


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slopodoro acquisition, calibration, features, and rule scoring.")
    parser.add_argument("--config", default="config/slopodoro_acquisition.yaml", help="Path to acquisition YAML config")
    parser.add_argument("--mode", choices=["calibrate", "run", "calibrate-and-run"], default="calibrate-and-run")
    parser.add_argument("--dry-run", action="store_true", help="Load and validate config, then exit")
    parser.add_argument("--synthetic", action="store_true", help="Use deterministic synthetic signals instead of hardware")
    parser.add_argument("--duration-seconds", type=float, default=None, help="Run duration for run mode. Hardware default is until Ctrl-C.")
    parser.add_argument("--no-lsl", action="store_true", help="Disable local LSL output while still writing files")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"Config OK: session_id={cfg.session.session_id} output_dir={cfg.session.output_dir}")
        return 0

    if args.synthetic:
        return _run_synthetic(cfg, args)
    return _run_hardware(cfg, args)


def _run_synthetic(cfg: AcquisitionConfig, args: argparse.Namespace) -> int:
    lsl = LSLStreamManager.create(cfg, enabled=not args.no_lsl)
    raw_lsl = RawLSLPublisher.create(cfg, enabled=not args.no_lsl)
    generator = SyntheticSignalGenerator(cfg)
    scorer = RuleBasedScorer(cfg)
    started_at = lsl_now()

    with SessionWriter(cfg) as writer:
        writer.write_raw_metadata(_raw_metadata(cfg, synthetic=True))

        marker = lsl.push_marker("acquisition_start", timestamp=started_at, mode=args.mode, synthetic=True)
        writer.write_marker(marker)

        calibration_model: dict[str, Any] | None = None
        try:
            if args.mode in {"calibrate", "calibrate-and-run"}:
                calibration_model = _synthetic_calibration(cfg, generator, lsl, raw_lsl, writer, started_at)
            if args.mode in {"run", "calibrate-and-run"}:
                if calibration_model is None:
                    calibration_model = _load_existing_calibration(writer)
                duration = float(args.duration_seconds if args.duration_seconds is not None else cfg.synthetic.run_seconds)
                _synthetic_run(cfg, generator, lsl, raw_lsl, writer, scorer, calibration_model, started_at + 120.0, duration)
            marker = lsl.push_marker("acquisition_stop", timestamp=lsl_now(), mode=args.mode, synthetic=True)
            writer.write_marker(marker)
            return 0
        except KeyboardInterrupt:
            marker = lsl.push_marker("acquisition_stop", timestamp=lsl_now(), reason="keyboard_interrupt", synthetic=True)
            writer.write_marker(marker)
            return 130


def _synthetic_calibration(
    cfg: AcquisitionConfig,
    generator: SyntheticSignalGenerator,
    lsl: LSLStreamManager,
    raw_lsl: RawLSLPublisher,
    writer: SessionWriter,
    start_ts: float,
) -> dict[str, Any]:
    phase_frames: dict[str, list[dict[str, Any]]] = {}
    eeg_extractor = EEGFeatureExtractor(cfg.features, cfg.openbci.expected_sample_rate_hz, cfg.openbci.eeg_labels)
    emg_extractor = EMGFeatureExtractor(cfg.features, cfg.openbci.expected_sample_rate_hz, cfg.openbci.emg_labels)
    scorer = RuleBasedScorer(cfg)

    marker = lsl.push_marker("calibration_start", timestamp=start_ts, synthetic=True)
    writer.write_marker(marker)
    cursor = start_ts + 1.0
    for phase in calibration_phases(cfg, synthetic=True):
        marker = lsl.push_marker(phase.start_marker, timestamp=cursor, phase=phase.name, synthetic=True)
        writer.write_marker(marker)
        phase_frames[phase.name] = []
        frame_end = cursor + max(cfg.features.eeg_window_seconds, cfg.features.emg_window_seconds)
        phase_end = cursor + phase.duration_seconds
        while frame_end <= phase_end + 1e-6:
            merged = _synthetic_feature_frame(cfg, generator, eeg_extractor, emg_extractor, frame_end, phase.name, None)
            phase_frames[phase.name].append(merged)
            lsl.push_features(merged)
            writer.write_features(merged)
            score = scorer.score(merged, current_mode="calibration")
            lsl.push_score(score)
            writer.write_score(score)
            _write_synthetic_raw(cfg, generator, writer, raw_lsl, frame_end, cfg.synthetic.chunk_seconds, phase.name)
            frame_end += cfg.features.eeg_step_seconds
        cursor = phase_end
        marker = lsl.push_marker(phase.end_marker, timestamp=cursor, phase=phase.name, synthetic=True)
        writer.write_marker(marker)
        cursor += 1.0

    model = build_calibration_model(cfg, phase_frames, started_at=start_ts, ended_at=cursor)
    writer.write_calibration(model)
    return model


def _synthetic_run(
    cfg: AcquisitionConfig,
    generator: SyntheticSignalGenerator,
    lsl: LSLStreamManager,
    raw_lsl: RawLSLPublisher,
    writer: SessionWriter,
    scorer: RuleBasedScorer,
    calibration_model: dict[str, Any],
    start_ts: float,
    duration_seconds: float,
) -> None:
    eeg_extractor = EEGFeatureExtractor(cfg.features, cfg.openbci.expected_sample_rate_hz, cfg.openbci.eeg_labels)
    emg_extractor = EMGFeatureExtractor(cfg.features, cfg.openbci.expected_sample_rate_hz, cfg.openbci.emg_labels)
    marker = lsl.push_marker("work_start", timestamp=start_ts, synthetic=True)
    writer.write_marker(marker)
    step = max(0.1, float(cfg.synthetic.chunk_seconds))
    elapsed = 0.0
    while elapsed <= duration_seconds + 1e-9:
        end_ts = start_ts + elapsed + max(cfg.features.eeg_window_seconds, cfg.features.emg_window_seconds)
        profile = generator.profile_for_elapsed(elapsed)
        frame = _synthetic_feature_frame(cfg, generator, eeg_extractor, emg_extractor, end_ts, profile, calibration_model)
        _write_synthetic_raw(cfg, generator, writer, raw_lsl, end_ts, step, profile)
        writer.write_features(frame)
        lsl.push_features(frame)
        score = scorer.score(frame, source_health={"polar_missing": profile == "polar_disconnect"}, current_mode="run")
        writer.write_score(score)
        lsl.push_score(score)
        writer.write_health(
            {
                "timestamp": end_ts,
                "session_id": cfg.session.session_id,
                "synthetic_profile": profile,
                "openbci": {"stream_active": True, "effective_sample_rate": cfg.openbci.expected_sample_rate_hz},
                "polar": {"stream_active": profile != "polar_disconnect", "ecg_effective_sample_rate": cfg.polar.expected_ecg_sample_rate_hz},
            }
        )
        elapsed += step
    marker = lsl.push_marker("work_end", timestamp=start_ts + duration_seconds, synthetic=True)
    writer.write_marker(marker)


def _run_hardware(cfg: AcquisitionConfig, args: argparse.Namespace) -> int:
    lsl = LSLStreamManager.create(cfg, enabled=not args.no_lsl)
    scorer = RuleBasedScorer(cfg)
    stop_requested = False

    def _stop_handler(_sig: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _stop_handler)
    signal.signal(signal.SIGTERM, _stop_handler)

    openbci = OpenBCISource(cfg, publish_lsl=True) if cfg.openbci.enabled else None
    polar = PolarH10Source(cfg, publish_lsl=True) if cfg.polar.enabled else None

    with SessionWriter(cfg) as writer:
        writer.write_raw_metadata(_raw_metadata(cfg, synthetic=False))
        marker = lsl.push_marker("acquisition_start", mode=args.mode, synthetic=False)
        writer.write_marker(marker)
        try:
            if openbci is not None:
                openbci.start()
            if polar is not None:
                try:
                    polar.start()
                except Exception as exc:
                    if cfg.polar.required:
                        raise
                    writer.write_health({"timestamp": lsl_now(), "session_id": cfg.session.session_id, "polar_warning": str(exc)})
                    polar = None

            calibration_model: dict[str, Any] | None = None
            if args.mode in {"calibrate", "calibrate-and-run"}:
                calibration_model = _hardware_calibration(cfg, lsl, writer, openbci, polar, lambda: stop_requested)
            if args.mode in {"run", "calibrate-and-run"} and not stop_requested:
                duration = args.duration_seconds
                _hardware_run(cfg, lsl, writer, scorer, calibration_model, openbci, polar, duration, lambda: stop_requested)
            marker = lsl.push_marker("acquisition_stop", synthetic=False)
            writer.write_marker(marker)
            return 0
        except KeyboardInterrupt:
            marker = lsl.push_marker("acquisition_stop", reason="keyboard_interrupt", synthetic=False)
            writer.write_marker(marker)
            return 130
        finally:
            if polar is not None:
                polar.stop()
            if openbci is not None:
                openbci.stop()


def _hardware_calibration(
    cfg: AcquisitionConfig,
    lsl: LSLStreamManager,
    writer: SessionWriter,
    openbci: OpenBCISource | None,
    polar: PolarH10Source | None,
    should_stop: Any,
) -> dict[str, Any]:
    if openbci is None:
        raise RuntimeError("OpenBCI is required for calibration")
    phase_frames: dict[str, list[dict[str, Any]]] = {}
    eeg_extractor = EEGFeatureExtractor(cfg.features, cfg.openbci.expected_sample_rate_hz, cfg.openbci.eeg_labels)
    emg_extractor = EMGFeatureExtractor(cfg.features, cfg.openbci.expected_sample_rate_hz, cfg.openbci.emg_labels)
    marker = lsl.push_marker("calibration_start")
    writer.write_marker(marker)
    started_at = marker["timestamp"]
    for phase in calibration_phases(cfg, synthetic=False):
        print(f"[calibration] {phase.name}: {phase.description} ({phase.duration_seconds:.0f}s)", flush=True)
        marker = lsl.push_marker(phase.start_marker, phase=phase.name)
        writer.write_marker(marker)
        phase_frames[phase.name] = []
        phase_start = time.monotonic()
        next_feature = time.monotonic() + cfg.features.eeg_window_seconds
        raw_cache_ts: list[float] = []
        raw_cache_samples: list[list[float]] = []
        rr_events: list[dict[str, float]] = []
        ecg_ts: list[float] = []
        ecg_samples: list[float] = []
        while not should_stop() and (time.monotonic() - phase_start) < phase.duration_seconds:
            chunk = openbci.poll()
            if chunk.timestamps.size:
                raw_cache_ts.extend(chunk.timestamps.tolist())
                raw_cache_samples.extend(chunk.samples.tolist())
                writer.write_raw_openbci_chunk(chunk.timestamps.tolist(), chunk.samples.tolist(), cfg.openbci.source_id)
            if polar is not None:
                drained = polar.drain()
                ecg_ts.extend(drained.ecg_timestamps)
                ecg_samples.extend(drained.ecg_samples)
                rr_events.extend(drained.rr_events)
                if drained.ecg_timestamps:
                    writer.write_raw_ecg_chunk(drained.ecg_timestamps, drained.ecg_samples, cfg.polar.source_id_ecg)
                if drained.rr_events:
                    writer.write_raw_hr_rr(drained.rr_events, cfg.polar.source_id_hr)
            if time.monotonic() >= next_feature and raw_cache_ts:
                frame = _feature_from_raw_cache(cfg, eeg_extractor, emg_extractor, raw_cache_ts, raw_cache_samples, ecg_ts, ecg_samples, rr_events, None)
                phase_frames[phase.name].append(frame)
                writer.write_features(frame)
                lsl.push_features(frame)
                next_feature += cfg.features.eeg_step_seconds
            time.sleep(cfg.openbci.connection.poll_interval_seconds)
        marker = lsl.push_marker(phase.end_marker, phase=phase.name)
        writer.write_marker(marker)
    ended_at = lsl_now()
    model = build_calibration_model(cfg, phase_frames, started_at=started_at, ended_at=ended_at)
    writer.write_calibration(model)
    return model


def _hardware_run(
    cfg: AcquisitionConfig,
    lsl: LSLStreamManager,
    writer: SessionWriter,
    scorer: RuleBasedScorer,
    calibration_model: dict[str, Any] | None,
    openbci: OpenBCISource | None,
    polar: PolarH10Source | None,
    duration_seconds: float | None,
    should_stop: Any,
) -> None:
    if openbci is None:
        raise RuntimeError("OpenBCI source is required for run mode")
    eeg_extractor = EEGFeatureExtractor(cfg.features, cfg.openbci.expected_sample_rate_hz, cfg.openbci.eeg_labels)
    emg_extractor = EMGFeatureExtractor(cfg.features, cfg.openbci.expected_sample_rate_hz, cfg.openbci.emg_labels)
    marker = lsl.push_marker("work_start")
    writer.write_marker(marker)
    started = time.monotonic()
    next_feature = time.monotonic() + cfg.features.eeg_window_seconds
    raw_cache_ts: list[float] = []
    raw_cache_samples: list[list[float]] = []
    ecg_ts: list[float] = []
    ecg_samples: list[float] = []
    rr_events: list[dict[str, float]] = []
    while not should_stop():
        if duration_seconds is not None and (time.monotonic() - started) >= duration_seconds:
            break
        chunk = openbci.poll()
        if chunk.timestamps.size:
            raw_cache_ts.extend(chunk.timestamps.tolist())
            raw_cache_samples.extend(chunk.samples.tolist())
            writer.write_raw_openbci_chunk(chunk.timestamps.tolist(), chunk.samples.tolist(), cfg.openbci.source_id)
        if polar is not None:
            drained = polar.drain()
            ecg_ts.extend(drained.ecg_timestamps)
            ecg_samples.extend(drained.ecg_samples)
            rr_events.extend(drained.rr_events)
            if drained.ecg_timestamps:
                writer.write_raw_ecg_chunk(drained.ecg_timestamps, drained.ecg_samples, cfg.polar.source_id_ecg)
            if drained.rr_events:
                writer.write_raw_hr_rr(drained.rr_events, cfg.polar.source_id_hr)
        if time.monotonic() >= next_feature and raw_cache_ts:
            frame = _feature_from_raw_cache(cfg, eeg_extractor, emg_extractor, raw_cache_ts, raw_cache_samples, ecg_ts, ecg_samples, rr_events, calibration_model)
            writer.write_features(frame)
            lsl.push_features(frame)
            source_health = {
                "openbci_missing": not openbci.health().get("stream_active", False),
                "polar_missing": cfg.polar.enabled and polar is None,
            }
            score = scorer.score(frame, source_health=source_health, current_mode="run")
            writer.write_score(score)
            lsl.push_score(score)
            writer.write_health({"timestamp": frame["timestamp"], "session_id": cfg.session.session_id, "openbci": openbci.health(), "polar": polar.health() if polar else None})
            next_feature += cfg.features.eeg_step_seconds
        time.sleep(cfg.openbci.connection.poll_interval_seconds)
    marker = lsl.push_marker("work_end")
    writer.write_marker(marker)


def _synthetic_feature_frame(
    cfg: AcquisitionConfig,
    generator: SyntheticSignalGenerator,
    eeg_extractor: EEGFeatureExtractor,
    emg_extractor: EMGFeatureExtractor,
    end_ts: float,
    profile: str,
    calibration_model: dict[str, Any] | None,
) -> dict[str, Any]:
    eeg_ts, eeg_raw = generator.openbci_window(end_ts, cfg.features.eeg_window_seconds, profile)
    emg_ts, emg_raw = generator.openbci_window(end_ts, cfg.features.emg_window_seconds, profile)
    ecg_duration = max(8.0, min(cfg.features.hrv_window_seconds, 30.0))
    ecg_ts, ecg_samples, rr_events = generator.polar_window(end_ts, ecg_duration, profile)
    eeg_frame = eeg_extractor.compute(
        eeg_ts,
        _select_eeg(cfg, eeg_raw),
        session_id=cfg.session.session_id,
        calibration_model=calibration_model,
    )
    emg_frame = emg_extractor.compute(
        emg_ts,
        _select_emg(cfg, emg_raw),
        session_id=cfg.session.session_id,
        calibration_model=calibration_model,
    )
    ecg_frame = compute_ecg_features(
        timestamp=end_ts,
        session_id=cfg.session.session_id,
        rr_ms=[event["rr_ms"] for event in rr_events],
        ecg_samples=ecg_samples,
        ecg_timestamps=ecg_ts,
        sample_rate_hz=cfg.polar.expected_ecg_sample_rate_hz,
    )
    return _merge_feature_frames(cfg.session.session_id, [eeg_frame, emg_frame, ecg_frame])


def _feature_from_raw_cache(
    cfg: AcquisitionConfig,
    eeg_extractor: EEGFeatureExtractor,
    emg_extractor: EMGFeatureExtractor,
    raw_ts: list[float],
    raw_samples: list[list[float]],
    ecg_ts: list[float],
    ecg_samples: list[float],
    rr_events: list[dict[str, float]],
    calibration_model: dict[str, Any] | None,
) -> dict[str, Any]:
    timestamps = np.asarray(raw_ts, dtype=float)
    samples = np.asarray(raw_samples, dtype=float)
    end = float(timestamps[-1])
    eeg_mask = timestamps >= end - cfg.features.eeg_window_seconds
    emg_mask = timestamps >= end - cfg.features.emg_window_seconds
    eeg_frame = eeg_extractor.compute(
        timestamps[eeg_mask],
        _select_eeg(cfg, samples[eeg_mask]),
        session_id=cfg.session.session_id,
        calibration_model=calibration_model,
    )
    emg_frame = emg_extractor.compute(
        timestamps[emg_mask],
        _select_emg(cfg, samples[emg_mask]),
        session_id=cfg.session.session_id,
        calibration_model=calibration_model,
    )
    rr_window = [event["rr_ms"] for event in rr_events if float(event.get("timestamp", 0.0)) >= end - cfg.features.hrv_window_seconds]
    ecg_arr_t = np.asarray(ecg_ts, dtype=float)
    ecg_arr = np.asarray(ecg_samples, dtype=float)
    ecg_mask = ecg_arr_t >= end - cfg.features.ecg_fast_window_seconds if ecg_arr_t.size else np.asarray([], dtype=bool)
    ecg_frame = compute_ecg_features(
        timestamp=end,
        session_id=cfg.session.session_id,
        rr_ms=rr_window,
        ecg_samples=ecg_arr[ecg_mask] if ecg_arr.size and ecg_mask.size else None,
        ecg_timestamps=ecg_arr_t[ecg_mask] if ecg_arr_t.size and ecg_mask.size else None,
        sample_rate_hz=cfg.polar.expected_ecg_sample_rate_hz,
    )
    _trim_cache(raw_ts, raw_samples, end - max(cfg.features.hrv_window_seconds, cfg.features.eeg_window_seconds) - 1.0)
    _trim_cache(ecg_ts, ecg_samples, end - cfg.features.hrv_window_seconds - 1.0)
    rr_events[:] = [event for event in rr_events if float(event.get("timestamp", 0.0)) >= end - cfg.features.hrv_window_seconds - 1.0]
    return _merge_feature_frames(cfg.session.session_id, [eeg_frame, emg_frame, ecg_frame])


def _merge_feature_frames(session_id: str, frames: list[dict[str, Any]]) -> dict[str, Any]:
    features: dict[str, Any] = {}
    validity: dict[str, Any] = {"bad_channels": []}
    starts: list[float] = []
    ends: list[float] = []
    for frame in frames:
        features.update(frame.get("features", {}))
        for key, value in frame.get("validity", {}).items():
            if key == "bad_channels":
                validity["bad_channels"] = sorted(set(validity.get("bad_channels", []) + list(value)))
            elif key == "artifact_fraction" and key in validity:
                validity[key] = max(float(validity[key]), float(value))
            elif key == "bad_channel_count" and key in validity:
                validity[key] = max(int(validity[key]), int(value))
            else:
                validity[key] = value
        starts.append(float(frame.get("window_start", frame.get("timestamp", 0.0))))
        ends.append(float(frame.get("window_end", frame.get("timestamp", 0.0))))
    validity["bad_channel_count"] = int(validity.get("bad_channel_count", len(validity.get("bad_channels", []))))
    return {
        "timestamp": max(ends) if ends else lsl_now(),
        "session_id": session_id,
        "window_start": min(starts) if starts else 0.0,
        "window_end": max(ends) if ends else 0.0,
        "features": features,
        "validity": validity,
    }


def _select_eeg(cfg: AcquisitionConfig, raw_samples: np.ndarray) -> np.ndarray:
    return raw_samples[:, cfg.openbci.eeg_indices_zero_based]


def _select_emg(cfg: AcquisitionConfig, raw_samples: np.ndarray) -> np.ndarray:
    if not cfg.openbci.emg_indices_zero_based:
        return np.empty((raw_samples.shape[0], 0), dtype=float)
    return raw_samples[:, cfg.openbci.emg_indices_zero_based]


def _write_synthetic_raw(
    cfg: AcquisitionConfig,
    generator: SyntheticSignalGenerator,
    writer: SessionWriter,
    raw_lsl: RawLSLPublisher,
    end_ts: float,
    duration_seconds: float,
    profile: str,
) -> None:
    chunk = generator.chunk(end_ts, duration_seconds, profile)
    open_ts = chunk.openbci_timestamps.tolist()
    open_samples = chunk.openbci_samples.tolist()
    writer.write_raw_openbci_chunk(open_ts, open_samples, cfg.openbci.source_id)
    raw_lsl.push_openbci(open_ts, open_samples)
    if chunk.ecg_timestamps.size:
        ecg_ts = chunk.ecg_timestamps.tolist()
        ecg_samples = chunk.ecg_samples.tolist()
        writer.write_raw_ecg_chunk(ecg_ts, ecg_samples, cfg.polar.source_id_ecg)
        raw_lsl.push_ecg(ecg_ts, ecg_samples)
    writer.write_raw_hr_rr(chunk.rr_events, cfg.polar.source_id_hr)
    raw_lsl.push_hr_rr(chunk.rr_events)


def _trim_cache(timestamps: list[float], samples: list[Any], cutoff: float) -> None:
    drop = 0
    while drop < len(timestamps) and timestamps[drop] < cutoff:
        drop += 1
    if drop:
        del timestamps[:drop]
        del samples[:drop]


def _raw_metadata(cfg: AcquisitionConfig, *, synthetic: bool) -> dict[str, Any]:
    return {
        "session_id": cfg.session.session_id,
        "participant_id": cfg.session.participant_id,
        "synthetic": synthetic,
        "openbci": {
            "stream_name": cfg.openbci.stream_name,
            "source_id": cfg.openbci.source_id,
            "board": cfg.openbci.board,
            "expected_sample_rate_hz": cfg.openbci.expected_sample_rate_hz,
            "channels": [
                {"index": ch.index, "label": ch.label, "type": ch.type, "enabled": ch.enabled, "units": ch.units}
                for ch in cfg.openbci.channels
            ],
        },
        "polar": {
            "stream_name_ecg": cfg.polar.stream_name_ecg,
            "stream_name_hr": cfg.polar.stream_name_hr,
            "source_id_ecg": cfg.polar.source_id_ecg,
            "source_id_hr": cfg.polar.source_id_hr,
            "expected_ecg_sample_rate_hz": cfg.polar.expected_ecg_sample_rate_hz,
            "collect_ecg": cfg.polar.collect_ecg,
            "collect_hr_rr": cfg.polar.collect_hr_rr,
        },
    }


def _load_existing_calibration(writer: SessionWriter) -> dict[str, Any]:
    path = writer.session_dir / "calibration.json"
    if not path.exists():
        return {}
    import json

    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


if __name__ == "__main__":
    raise SystemExit(main())
