# WebSocket Contract

The browser extension defaults to `ws://localhost:8765/`, and the popup can save another `ws://` or `wss://` endpoint. The server pushes JSON messages at about 4 Hz. The extension sends no messages.

```json
{
  "ts": 1714670000.123,
  "focus": 0.62,
  "fatigue": 0.31,
  "calibrating": false,
  "subscores": {
    "eeg_engagement": 1.4,
    "eeg_drowsiness": 0.7,
    "rmssd_ms": 42.0,
    "lf_hf": 1.8,
    "emg_corrugator": 0.12,
    "emg_zygomaticus": 0.08
  },
  "sources": {
    "eeg": true,
    "ecg": true,
    "emg": true
  }
}
```

Fields:

- `ts`: Float seconds from the producer clock. The phase 2 bridge will use LSL time.
- `focus`: Number in `[0, 1]`, or `null` while calibrating.
- `fatigue`: Number in `[0, 1]`, or `null` while calibrating.
- `calibrating`: `true` until the bridge has enough baseline data.
- `subscores`: Optional diagnostic values for the popup debug view. Sensor-derived values should become `null` when their source is unavailable.
- `sources`: Current source availability for `eeg`, `ecg`, and `emg`.

If one sensor drops, its `sources.*` value becomes `false`. Fused `focus` and `fatigue` should still be emitted using available modalities with weights renormalized by the producer.

For the firmware acquisition path, `tools/lsl_scores_ws.py` translates the `slopodoro_scores` LSL stream into this contract. When `openbci_missing` is true, it emits `sources.eeg=false`, `sources.emg=false`, and null focus/fatigue so the extension parks the cat asleep until the headset stream is available.
