#!/usr/bin/env python3
"""Smoke-test the Slopodoro Chrome extension against the LSL score bridge.

The test creates a temporary LSL Scores stream, starts tools/lsl_scores_ws.py
against that stream, loads extension/ as an unpacked Chrome extension, and
asserts that the popup plus content-script overlay receive live scores.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except Exception as exc:  # pragma: no cover - import failure path is environment-specific.
    raise SystemExit(
        "Playwright is required. Run `python -m pip install -r requirements-dev.txt` "
        "and `python -m playwright install chromium`."
    ) from exc

try:
    from pylsl import StreamInfo, StreamOutlet
except Exception as exc:  # pragma: no cover - import failure path is environment-specific.
    raise SystemExit("pylsl is required. Run `python -m pip install -r requirements-dev.txt`.") from exc


ROOT = Path(__file__).resolve().parents[1]


def free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


async def http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await reader.readuntil(b"\r\n\r\n")
    except Exception:
        pass

    body = (
        b"<!doctype html><html><head><title>Slopodoro smoke</title></head>"
        b"<body><main><h1>Slopodoro extension smoke test</h1></main></body></html>"
    )
    writer.write(
        b"HTTP/1.1 200 OK\r\n"
        + b"Content-Type: text/html; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + body
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def publish_lsl_scores(stream_name: str, stop_event: asyncio.Event) -> None:
    source_id = f"{stream_name}_source"
    info = StreamInfo(stream_name, "Scores", 1, 0, "string", source_id)
    info.desc().append_child_value("session_id", "playwright_extension_smoke")
    outlet = StreamOutlet(info)

    tick = 0
    while not stop_event.is_set():
        focus = 68 + (tick % 12)
        fatigue = 22 + (tick % 8)
        frame = {
            "timestamp": time.time(),
            "session_id": "playwright_extension_smoke",
            "scores": {
                "focus_score_0_100": focus,
                "fatigue_drift_score_0_100": fatigue,
                "emg_strain_score_0_100": 31,
                "signal_quality_score_0_100": 93,
                "recovery_context_score_0_100": 57,
            },
            "state": "focused_work",
            "flags": {
                "break_recommended": False,
                "strain_notice": False,
                "bad_signal": False,
                "polar_missing": False,
                "openbci_missing": False,
            },
        }
        outlet.push_sample([json.dumps(frame, separators=(",", ":"))])
        tick += 1
        await asyncio.sleep(0.25)


async def read_process_output(process: asyncio.subprocess.Process, lines: list[str]) -> None:
    if process.stdout is None:
        return

    while True:
        line = await process.stdout.readline()
        if not line:
            break
        lines.append(line.decode(errors="replace").rstrip())


async def chrome_storage_get(page: Any, area: str, keys: list[str]) -> dict[str, Any]:
    return await page.evaluate(
        """([area, keys]) => new Promise(resolve => chrome.storage[area].get(keys, resolve))""",
        [area, keys],
    )


async def chrome_storage_set(page: Any, area: str, values: dict[str, Any]) -> None:
    await page.evaluate(
        """([area, values]) => new Promise(resolve => chrome.storage[area].set(values, resolve))""",
        [area, values],
    )


async def wait_for_extension_id(context: Any, timeout_ms: int) -> str:
    worker = context.service_workers[0] if context.service_workers else await context.wait_for_event(
        "serviceworker",
        timeout=timeout_ms,
    )
    return worker.url.split("/")[2]


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return

    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    extension_dir = Path(args.extension_dir).resolve()
    bridge_script = Path(args.bridge_script).resolve()
    if not extension_dir.exists():
        raise FileNotFoundError(f"Extension directory not found: {extension_dir}")
    if not bridge_script.exists():
        raise FileNotFoundError(f"Bridge script not found: {bridge_script}")

    host = args.host
    port = args.port or free_port(host)
    stream_name = args.stream_name or f"slopodoro_scores_smoke_{uuid.uuid4().hex[:8]}"
    timeout_ms = int(args.timeout_seconds * 1000)
    ws_url = f"ws://{host}:{port}/"

    http_server = await asyncio.start_server(http_handler, "127.0.0.1", 0)
    http_port = int(http_server.sockets[0].getsockname()[1])
    stop_publisher = asyncio.Event()
    publisher_task = asyncio.create_task(publish_lsl_scores(stream_name, stop_publisher))

    bridge_lines: list[str] = []
    bridge_output_snapshot: list[str] = []
    bridge_process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(bridge_script),
        "--host",
        host,
        "--port",
        str(port),
        "--stream-name",
        stream_name,
        "--resolve-retry-seconds",
        "0.25",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(ROOT),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    bridge_reader_task = asyncio.create_task(read_process_output(bridge_process, bridge_lines))

    console_messages: list[str] = []
    page_errors: list[str] = []
    user_data_dir = tempfile.mkdtemp(prefix="slopodoro-extension-smoke-")

    try:
        await asyncio.sleep(args.bridge_warmup_seconds)
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir,
                headless=args.headless,
                args=[
                    f"--disable-extensions-except={extension_dir}",
                    f"--load-extension={extension_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            context.on("console", lambda msg: console_messages.append(f"{msg.type}: {msg.text}"))
            context.on("pageerror", lambda exc: page_errors.append(str(exc)))

            extension_id = await wait_for_extension_id(context, timeout_ms)
            popup = await context.new_page()
            await popup.goto(f"chrome-extension://{extension_id}/popup.html")
            await chrome_storage_set(popup, "local", {"wsUrl": ws_url})
            await popup.wait_for_function(
                """() => document.querySelector('#statusText')?.textContent?.startsWith('Live from')
                    && document.querySelector('#focusValue')?.textContent !== '--'
                    && document.querySelector('#fatigueValue')?.textContent !== '--'
                    && document.querySelector('#eegDot')?.classList.contains('active')
                    && document.querySelector('#emgDot')?.classList.contains('active')""",
                timeout=timeout_ms,
            )

            popup_state = await popup.evaluate(
                """() => ({
                    status: document.querySelector('#statusText')?.textContent,
                    focus: document.querySelector('#focusValue')?.textContent,
                    fatigue: document.querySelector('#fatigueValue')?.textContent,
                    flow: document.querySelector('#flowState')?.textContent,
                    eegActive: document.querySelector('#eegDot')?.classList.contains('active'),
                    ecgActive: document.querySelector('#ecgDot')?.classList.contains('active'),
                    emgActive: document.querySelector('#emgDot')?.classList.contains('active')
                })"""
            )
            storage = await chrome_storage_get(popup, "session", ["latestSample", "connectionStatus"])

            page = await context.new_page()
            await page.goto(f"http://127.0.0.1:{http_port}/", wait_until="domcontentloaded")
            await page.wait_for_function(
                """() => {
                    const el = document.getElementById('slopadoro-fatigue-cat');
                    return el && getComputedStyle(el).display !== 'none';
                }""",
                timeout=timeout_ms,
            )
            overlay_state = await page.evaluate(
                """() => {
                    const el = document.getElementById('slopadoro-fatigue-cat');
                    const style = getComputedStyle(el);
                    return {
                        exists: Boolean(el),
                        display: style.display,
                        width: style.width,
                        height: style.height,
                        backgroundImageLoaded: style.backgroundImage.includes('chrome-extension://')
                    };
                }"""
            )
            bridge_output_snapshot = list(bridge_lines)

            if args.keep_open:
                print("Smoke test passed; keeping browser open. Press Ctrl-C to close.")
                while True:
                    await asyncio.sleep(1)

            await context.close()
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("Timed out waiting for the extension to show live bridge data.") from exc
    finally:
        stop_publisher.set()
        await publisher_task
        await terminate_process(bridge_process)
        bridge_reader_task.cancel()
        http_server.close()
        await http_server.wait_closed()

    warning_or_error_console = [
        line for line in console_messages if line.startswith(("warning:", "error:"))
    ]
    bridge_events = [
        line
        for line in bridge_output_snapshot
        if line.startswith(("LSL score bridge", "resolved LSL score", "extension "))
    ]
    return {
        "status": "passed",
        "extension_dir": str(extension_dir),
        "bridge_script": str(bridge_script),
        "stream_name": stream_name,
        "ws_url": ws_url,
        "popup": popup_state,
        "latest_sample": storage.get("latestSample"),
        "connection_status": storage.get("connectionStatus"),
        "overlay": overlay_state,
        "bridge_events": bridge_events,
        "console_warnings_or_errors": warning_or_error_console,
        "page_errors": page_errors,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load the unpacked extension and verify the LSL bridge path.")
    parser.add_argument("--extension-dir", default=str(ROOT / "extension"))
    parser.add_argument("--bridge-script", default=str(ROOT / "tools" / "lsl_scores_ws.py"))
    parser.add_argument("--stream-name", default=None, help="Temporary LSL Scores stream name. Defaults to a unique name.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="WebSocket bridge port. Defaults to a free local port.")
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--bridge-warmup-seconds", type=float, default=1.0)
    parser.add_argument("--headless", action="store_true", help="Try headless Chromium. Headed mode is more reliable for extensions.")
    parser.add_argument("--keep-open", action="store_true", help="Leave the browser open after assertions pass.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(run_smoke(args))
    except KeyboardInterrupt:
        print("\nSmoke test interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
