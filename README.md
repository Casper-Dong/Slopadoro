# Fatigue Cat Chrome Extension

Chrome MV3 extension that shows a pixel cat along the bottom of normal web pages. The cat responds live to `focus` and `fatigue` samples from a WebSocket stream. It defaults to `ws://localhost:8765/`, and the popup can point it at another `ws://` or `wss://` endpoint.

## End-to-End Firmware Demo

The browser extension does not read LSL directly. The live hardware path is:

```text
OpenBCI + Polar -> Firmware/slopodoro_acq -> LSL slopodoro_scores -> tools/lsl_scores_ws.py -> ws://localhost:8765/ -> extension/
```

Start the firmware scorer from the `Firmware` directory:

```powershell
cd C:\Users\hocke\OneDrive\Documents\GitHub\Slopadoro\Firmware
python -m slopodoro_acq.main --config config/slopodoro_acquisition.yaml --mode calibrate-and-run
```

In a second terminal, start the WebSocket bridge from the repository root:

```powershell
cd C:\Users\hocke\OneDrive\Documents\GitHub\Slopadoro
python tools/lsl_scores_ws.py
```

Load `extension/` as an unpacked Chrome extension, then open a normal web page. The popup should show `Live from ws://localhost:8765`, focus/fatigue numbers, and active EEG/EMG source dots once OpenBCI scores are flowing. ECG is active when the Polar stream is available.

To test the full path without hardware, run synthetic firmware scoring in the first terminal instead:

```powershell
cd C:\Users\hocke\OneDrive\Documents\GitHub\Slopadoro\Firmware
python -m slopodoro_acq.main --config config/slopodoro_acquisition.yaml --synthetic --mode calibrate-and-run
```

Firmware setup, channel mapping, calibration phases, and output files are documented in [Firmware/README.md](Firmware/README.md).

## Dev Setup

Install the bridge and smoke-test dependencies:

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
```

Run the automated integration smoke test:

```bash
python tools/playwright_extension_smoke.py
```

The smoke test publishes a temporary LSL `Scores` stream, starts `tools/lsl_scores_ws.py`, loads `extension/` as an unpacked Chromium extension, and asserts that the popup plus page overlay receive live score data.

## Hackathon Demo Mode

If calibration is painful or gets rejected by strict EEG validity checks, keep the firmware acquisition running and replace the normal score bridge with the relaxed feature bridge:

```powershell
cd C:\Users\hocke\OneDrive\Documents\GitHub\Slopadoro
python tools/hackathon_live_bridge.py
```

This reads `slopodoro_features` directly, computes rough delta/theta/alpha/beta bandpower scores without requiring accepted calibration, serves the extension contract at `ws://127.0.0.1:8765/`, and opens a live dashboard at `http://127.0.0.1:8766/`.

Use this for demos when the data is flowing but the regular scorer is stuck in `bad_signal`. It still shows signal quality and bad channels, but it will keep focus/fatigue moving from the usable bandpower features unless the stream is completely unusable. The dashboard also streams raw EEG, the two posture EMG channels, and raw ECG from LSL so the signal can be inspected live.

In this bridge, posture strain is intentionally spike-based: it is computed from the quick time derivative of the two raw trapezius EMG channels over a short live window. Steady EMG amplitude alone should not raise the extension strain score unless there is a fast left or right EMG change.

## Hosted Live Stream

Vercel can host a static dashboard or frontend, but the hardware bridge still needs to run on the machine connected to OpenBCI and Polar. Keep `tools/hackathon_live_bridge.py` or `tools/lsl_scores_ws.py` local, then expose its WebSocket through a tunnel or a realtime relay. For a quick demo, start the local bridge on all interfaces:

```powershell
python tools/hackathon_live_bridge.py --host 0.0.0.0 --no-open-dashboard
```

Then publish the needed WebSocket port with a tunneling tool or realtime provider, and point the extension popup at the resulting `wss://` URL. Vercel should host only the browser-facing page; it should not be the process that talks to LSL or holds the live WebSocket connection.

The deployable hosted dashboard lives in `vercel_site/`. Deploy that directory directly on Vercel, then open it with the stream URL as a query parameter:

```text
https://your-project.vercel.app/?ws=wss%3A%2F%2Fyour-live-stream.example
```

Use the same `wss://` stream URL in the extension popup, or paste the hosted dashboard URL if it contains the `?ws=...` parameter. `tools/hackathon_live_bridge.py` serves dashboard frames on port `8767` as a superset of the extension contract, so the hosted dashboard can show raw EEG/EMG/ECG while the extension can read focus/fatigue from the same stream.

## Sprite Sheet

The provided sprite sheet is copied byte-for-byte into:

```text
extension/cat sprite sheet.png
```

Chrome did not paint the URL-encoded filename from page CSS in local testing, so the extension also includes a byte-identical runtime copy:

```text
extension/cat-sprite-sheet.png
```

Measured layout:

```text
image: 256 x 320
frame: 32 x 32
grid: 8 columns x 10 rows
row frame counts: 4, 4, 4, 4, 8, 8, 4, 6, 7, 8
```

`cat.js` maps focus and fatigue to three visible moods:

- Focused: high focus with low fatigue parks the cat sleeping/dozing in the bottom-right corner.
- Drifting: medium focus or mild fatigue makes the cat wander slowly along the bottom edge.
- Break needed: high fatigue or very low focus makes the cat run fast and jump while bouncing between window edges.

## Run the Mock Server

Install the mock-server dependency if needed:

```bash
python -m pip install -r requirements-dev.txt
```

Run the full fatigue ladder:

```bash
python tools/mock_ws.py --scenario sleepy
```

Other scenarios:

```bash
python tools/mock_ws.py --scenario scripted
python tools/mock_ws.py --scenario random_walk
```

To simulate the headset being off before data starts:

```bash
python tools/mock_ws.py --scenario sleepy --headset-delay-seconds 15
```

## Load the Extension

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Click Load unpacked.
4. Select this repository's `extension` directory, for example `C:\Users\hocke\OneDrive\Documents\GitHub\Slopadoro\extension`.

Open any normal web page after the extension loads. Chrome cannot inject content scripts into pages like `chrome://extensions` or the Chrome Web Store, so test on a regular `https://` page.

To use a non-local stream, open the extension popup, change Stream URL, and click Save. For example, a bridge on another computer on your network might use `ws://192.168.1.50:8765/`; a hosted bridge should use `wss://`.

To let another browser or device connect to the mock server over your local network, start it with:

```bash
python tools/mock_ws.py --host 0.0.0.0 --scenario sleepy
```

## Hardware Bridge

Chrome cannot subscribe to LSL directly, so hardware data reaches the extension through a local WebSocket adapter. Start the acquisition side so it publishes the `slopodoro_scores` LSL stream, then run:

```bash
python tools/lsl_scores_ws.py
```

That bridge reads the firmware `Scores` frames, maps `focus_score_0_100` and `fatigue_drift_score_0_100` into the extension's `0..1` WebSocket contract, and reports OpenBCI/Polar availability through `sources`.

## Behavior

- Badge shows `...` while disconnected or calibrating.
- Badge shows `0` to `99` from focus when live.
- Badge color becomes redder as fatigue rises.
- Before the headset/OpenBCI stream is available, the cat sleeps in the bottom-right corner.
- During calibration or a disconnected WebSocket, the cat also stays asleep instead of disappearing.
- High focus plus low fatigue makes the cat sleep in the corner.
- Medium focus/fatigue makes the cat wander.
- High fatigue or very low focus makes the cat jump and run around.
- The popup keeps bounded session-only metric and flow logs, rendering focus/fatigue as line charts and recent states as a heat map.
- A notification fires after fatigue stays above `0.75` for 30 seconds, then enters a 5-minute cooldown.

## Contract

The frozen WebSocket contract is documented in [docs/websocket-contract.md](docs/websocket-contract.md).
