#!/usr/bin/env python3
"""Synthetic BCI WebSocket server for extension development."""

import argparse
import asyncio
import json
import math
import random
import time

import websockets


def clamp01(value):
    return min(1.0, max(0.0, value))


def scripted(elapsed):
    phase = (elapsed % 60.0) / 30.0
    rising = phase <= 1.0
    t = phase if rising else phase - 1.0
    eased = 0.5 - 0.5 * math.cos(math.pi * t)
    focus = 0.18 + 0.68 * (eased if rising else 1.0 - eased)
    fatigue = 0.82 - 0.64 * (eased if rising else 1.0 - eased)
    return focus, fatigue


def random_walk(state):
    for key in ("focus", "fatigue"):
        state[key] = clamp01(state[key] + random.uniform(-0.055, 0.055))
        state[key] = 0.85 * state[key] + 0.15 * 0.5
    return state["focus"], state["fatigue"]


def subscores(focus, fatigue):
    return {
        "eeg_engagement": round(0.6 + focus * 1.8, 3),
        "eeg_drowsiness": round(0.45 + fatigue * 1.5, 3),
        "rmssd_ms": round(30.0 + fatigue * 35.0, 3),
        "lf_hf": round(0.9 + (1.0 - fatigue) * 1.7, 3),
        "emg_corrugator": round(0.04 + focus * 0.18, 3),
        "emg_zygomaticus": round(0.03 + focus * 0.12, 3),
    }


async def stream(websocket, scenario, calibration_seconds):
    start = time.monotonic()
    state = {"focus": 0.5, "fatigue": 0.4}
    while True:
        elapsed = time.monotonic() - start
        focus, fatigue = scripted(elapsed) if scenario == "scripted" else random_walk(state)
        calibrating = elapsed < calibration_seconds
        message = {
            "ts": time.time(),
            "focus": None if calibrating else round(focus, 3),
            "fatigue": None if calibrating else round(fatigue, 3),
            "calibrating": calibrating,
            "subscores": subscores(focus, fatigue),
            "sources": {"eeg": True, "ecg": True, "emg": True},
        }
        await websocket.send(json.dumps(message))
        await asyncio.sleep(0.25)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--scenario", choices=("random_walk", "scripted"), default="random_walk")
    parser.add_argument("--calibration-seconds", default=0.0, type=float)
    args = parser.parse_args()

    async def handler(websocket, *_):
        print("client connected")
        try:
            await stream(websocket, args.scenario, args.calibration_seconds)
        except websockets.ConnectionClosed:
            print("client disconnected")

    async with websockets.serve(handler, args.host, args.port):
        print(f"mock server listening on ws://{args.host}:{args.port} ({args.scenario})")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nmock server stopped")
