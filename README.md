# Monitor Your Flow State

Chrome MV3 extension that shows a pixel cat along the bottom of normal web pages. The cat responds live to `focus` and `fatigue` samples from a WebSocket stream. It defaults to `ws://localhost:8765/`, and the popup can point it at another `ws://` or `wss://` endpoint.

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
python3 -m pip install -r requirements-dev.txt
```

Run the full fatigue ladder:

```bash
python3 tools/mock_ws.py --scenario sleepy
```

Other scenarios:

```bash
python3 tools/mock_ws.py --scenario scripted
python3 tools/mock_ws.py --scenario random_walk
```

To simulate the headset being off before data starts:

```bash
python3 tools/mock_ws.py --scenario sleepy --headset-delay-seconds 15
```

## Load the Extension

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Click Load unpacked.
4. Select `/Users/casperdong/Slopadoro/extension`.

Open any normal web page after the extension loads. Chrome cannot inject content scripts into pages like `chrome://extensions` or the Chrome Web Store, so test on a regular `https://` page.

To use a non-local stream, open the extension popup, change Stream URL, and click Save. For example, a bridge on another computer on your network might use `ws://192.168.1.50:8765/`; a hosted bridge should use `wss://`.

To let another browser or device connect to the mock server over your local network, start it with:

```bash
python3 tools/mock_ws.py --host 0.0.0.0 --scenario sleepy
```

## Hardware Bridge

Chrome cannot subscribe to LSL directly, so hardware data reaches the extension through a local WebSocket adapter. Start the acquisition side so it publishes the `slopodoro_scores` LSL stream, then run:

```bash
python3 tools/lsl_scores_ws.py
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
- The adaptive distraction gate desaturates blocklisted sites and shows a 5-second confirmation modal only when fatigue is high or focus is low.
- A notification fires after fatigue stays above `0.75` for 30 seconds, then enters a 5-minute cooldown.

## Contract

The frozen WebSocket contract is documented in [docs/websocket-contract.md](docs/websocket-contract.md).
