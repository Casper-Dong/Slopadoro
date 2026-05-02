#!/usr/bin/env python3
"""Bridge Slopodoro LSL score frames into the Chrome extension WebSocket contract."""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import websockets

try:
    from pylsl import StreamInlet, resolve_byprop
except Exception as exc:  # pragma: no cover - depends on local liblsl install.
    StreamInlet = None
    resolve_byprop = None
    _PYLSL_IMPORT_ERROR = exc
else:
    _PYLSL_IMPORT_ERROR = None


def clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def score01(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return clamp01(float(value) / 100.0)


def dormant_sample(reason: str) -> dict[str, Any]:
    return {
        "ts": time.time(),
        "focus": None,
        "fatigue": None,
        "calibrating": True,
        "subscores": {"bridge_state": reason},
        "sources": {"eeg": False, "ecg": False, "emg": False},
    }


def score_frame_to_contract(frame: dict[str, Any], lsl_ts: float | None = None) -> dict[str, Any]:
    scores = frame.get("scores") if isinstance(frame.get("scores"), dict) else {}
    flags = frame.get("flags") if isinstance(frame.get("flags"), dict) else {}
    state = frame.get("state") if isinstance(frame.get("state"), str) else ""
    openbci_missing = bool(flags.get("openbci_missing") or state == "openbci_missing")
    polar_missing = bool(flags.get("polar_missing") or state == "polar_missing")
    calibrating = bool(frame.get("calibrating") or state == "calibrating")

    sources = {
        "eeg": not openbci_missing,
        "ecg": not polar_missing,
        "emg": not openbci_missing,
    }

    return {
        "ts": float(frame.get("timestamp", lsl_ts or time.time())),
        "focus": None if calibrating or openbci_missing else score01(scores.get("focus_score_0_100")),
        "fatigue": None if calibrating or openbci_missing else score01(scores.get("fatigue_drift_score_0_100")),
        "calibrating": calibrating,
        "subscores": {
            "emg_strain_score_0_100": scores.get("emg_strain_score_0_100"),
            "signal_quality_score_0_100": scores.get("signal_quality_score_0_100"),
            "recovery_context_score_0_100": scores.get("recovery_context_score_0_100"),
            "score_state": state,
        },
        "sources": sources,
    }


@dataclass
class BridgeState:
    latest: dict[str, Any] = field(default_factory=lambda: dormant_sample("waiting_for_lsl_scores"))


def resolve_score_stream(args: argparse.Namespace) -> Any | None:
    by_name = resolve_byprop("name", args.stream_name, minimum=1, timeout=args.resolve_timeout)
    if by_name:
        return by_name[0]
    by_type = resolve_byprop("type", args.stream_type, minimum=1, timeout=args.resolve_timeout)
    return by_type[0] if by_type else None


async def read_lsl_scores(args: argparse.Namespace, state: BridgeState) -> None:
    inlet = None
    last_seen = 0.0

    while True:
        if inlet is None:
            state.latest = dormant_sample("waiting_for_lsl_scores")
            info = await asyncio.to_thread(resolve_score_stream, args)
            if info is None:
                await asyncio.sleep(args.resolve_retry_seconds)
                continue
            inlet = StreamInlet(info, max_buflen=5)
            last_seen = time.monotonic()
            print(f"resolved LSL score stream: {info.name()} ({info.type()})")

        sample, lsl_ts = inlet.pull_sample(timeout=0.0)
        if sample:
            try:
                frame = json.loads(sample[0])
                state.latest = score_frame_to_contract(frame, lsl_ts)
                last_seen = time.monotonic()
            except (TypeError, json.JSONDecodeError, ValueError) as exc:
                print(f"ignoring malformed score frame: {exc}")
        elif time.monotonic() - last_seen > args.stale_seconds:
            state.latest = dormant_sample("lsl_scores_stale")
            inlet.close_stream()
            inlet = None

        await asyncio.sleep(args.poll_seconds)


async def serve_client(websocket: Any, state: BridgeState, interval_seconds: float) -> None:
    print("extension connected")
    try:
        while True:
            await websocket.send(json.dumps(state.latest, separators=(",", ":")))
            await asyncio.sleep(interval_seconds)
    except websockets.ConnectionClosed:
        print("extension disconnected")


async def main_async(args: argparse.Namespace) -> None:
    if _PYLSL_IMPORT_ERROR is not None:
        raise SystemExit(f"pylsl import failed: {_PYLSL_IMPORT_ERROR}")

    state = BridgeState()
    lsl_task = asyncio.create_task(read_lsl_scores(args, state))

    async def handler(websocket: Any, *_args: Any) -> None:
        await serve_client(websocket, state, args.output_interval_seconds)

    async with websockets.serve(handler, args.host, args.port):
        print(f"LSL score bridge listening on ws://{args.host}:{args.port}")
        try:
            await asyncio.Future()
        finally:
            lsl_task.cancel()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Expose Slopodoro LSL Scores as the Fatigue Cat WebSocket contract.")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--stream-name", default="slopodoro_scores")
    parser.add_argument("--stream-type", default="Scores")
    parser.add_argument("--resolve-timeout", default=1.0, type=float)
    parser.add_argument("--resolve-retry-seconds", default=1.0, type=float)
    parser.add_argument("--poll-seconds", default=0.05, type=float)
    parser.add_argument("--output-interval-seconds", default=0.25, type=float)
    parser.add_argument("--stale-seconds", default=2.0, type=float)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(main_async(parse_args()))
    except KeyboardInterrupt:
        print("\nLSL score bridge stopped")
