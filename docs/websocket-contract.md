# WebSocket Contract

The browser extension defaults to `ws://localhost:8765/`, and the popup can save another `ws://` or `wss://` endpoint. The server pushes JSON messages at about 4 Hz. The extension sends no messages.

```json
{
  "ts": 1714670000.123,
  "focus": 0.62,
  "fatigue": 0.31,
  "calibrating": false,
  "subscores": {
    "emg_strain_score_0_100": 31.0,
    "emg_strain_mode": "derivative_spike",
    "emg_derivative_left_ratio": 1.4,
    "emg_derivative_right_ratio": 0.7,
    "signal_quality_score_0_100": 93.0,
    "recovery_context_score_0_100": 57.0,
    "score_state": "focused_work"
  },
  "sources": {
    "eeg": true,
    "ecg": true,
    "emg": true
  }
}
```

Fields:

- `ts`: Float seconds from the producer clock. The firmware bridge uses the score-frame timestamp when present, otherwise the LSL timestamp.
- `focus`: Number in `[0, 1]`, or `null` while calibrating.
- `fatigue`: Number in `[0, 1]`, or `null` while calibrating.
- `calibrating`: `true` until the bridge has enough baseline data.
- `subscores`: Optional diagnostic values for the popup debug view. Sensor-derived values should become `null` when their source is unavailable.
- `sources`: Current source availability for `eeg`, `ecg`, and `emg`.

The extension uses `subscores.signal_quality_score_0_100` as the primary value for the badge and on-page cat animation when it is present. `focus` and `fatigue` are still used by the popup charts, the distraction gate, focus-test logging, and as fallback cat inputs for older streams.

In `tools/hackathon_live_bridge.py`, `emg_strain_score_0_100` is a quick-derivative spike score from the two raw posture EMG channels. `emg_derivative_left_ratio` and `emg_derivative_right_ratio` report recent derivative magnitude relative to the local derivative noise floor; steady EMG amplitude alone should not raise strain.

If one sensor drops, its `sources.*` value becomes `false`. Fused `focus` and `fatigue` should still be emitted using available modalities with weights renormalized by the producer.

For the firmware acquisition path, `tools/lsl_scores_ws.py` translates the `slopodoro_scores` LSL stream into this contract. When `openbci_missing` is true, it emits `sources.eeg=false`, `sources.emg=false`, and null focus/fatigue so the extension parks the cat asleep until the headset stream is available.

For the hosted Vercel dashboard, `tools/hackathon_live_bridge.py` serves the dashboard WebSocket on port `8767` as an extension-compatible superset. It includes the top-level extension fields above plus dashboard fields such as `scores`, `eeg_bands`, `emg`, `ecg`, `validity`, and `raw`. That means the same `wss://` URL can be used by `vercel_site/` and by the extension popup.

## Local Integration Smoke Test

Run this from the repository root to verify the extension, bridge, and WebSocket contract without live hardware:

```bash
python tools/playwright_extension_smoke.py
```

The smoke test publishes a temporary LSL `Scores` stream, starts `tools/lsl_scores_ws.py`, loads `extension/` as an unpacked extension in Chromium, and checks that the popup plus content-script overlay receive live data.
