# BCI Focus & Fatigue Indicator

Phase 1 is a Chrome MV3 extension that reads live `focus` and `fatigue` samples from `ws://localhost:8765`. It includes a mock WebSocket server for visual verification before the LSL/DSP bridge is added.

## Run the Mock Server

Install the only mock-server dependency if needed:

```bash
python -m pip install -r requirements-dev.txt
```

Start the scripted ramp:

```bash
python tools/mock_ws.py --scenario scripted
```

Or use bounded smooth noise:

```bash
python tools/mock_ws.py --scenario random_walk
```

## Load the Extension

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Click Load unpacked.
4. Select the `extension/` directory.

The badge shows `...` while disconnected or calibrating. When live samples arrive, the badge tracks focus from `0` to `100`, and the badge color shifts as fatigue rises.

## Contract

The frozen WebSocket contract is documented in [docs/websocket-contract.md](docs/websocket-contract.md).

## Commit Policy

Use plain commit messages. Do not add a `Co-Authored-By: Claude` trailer.
