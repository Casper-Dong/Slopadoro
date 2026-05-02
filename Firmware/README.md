# Slopodoro Firmware Acquisition MVP

This firmware-side package handles acquisition, calibration, feature extraction, rule scoring, and live file/LSL outputs for Slopodoro. It does not include a frontend, notifications, cloud storage, ML models, or medical interpretation.

## What It Does

- Streams OpenBCI Cyton + Daisy data through BrainFlow/LSL.
- Treats 14 configured channels as EEG and 2 configured channels as posture EMG.
- Streams Polar H10 ECG/RR/HR where available.
- Uses LSL-compatible timestamps for samples, markers, features, and scores.
- Runs a guided calibration protocol.
- Computes rolling EEG focus-drift, EMG strain, ECG/HRV context, and signal-quality features.
- Persists raw streams, calibration metadata, features, scores, markers, and health records as local files.
- Provides `scores.jsonl` and an LSL score stream for a later UI/backend to tail.

## Channel Map

The default map is config-driven in `config/slopodoro_acquisition.yaml`. OpenBCI raw sample order is preserved, then channels are split into EEG vs EMG based on the config. The default layout follows the stock OpenBCI Mark IV Cyton+Daisy montage, except `T7` and `T8` are replaced by posture EMG.

| Logical channel | Board / pin | Config label | Type |
| ---: | --- | --- | --- |
| 1 | Cyton `N1P` | `Fp1` | EEG |
| 2 | Cyton `N2P` | `Fp2` | EEG |
| 3 | Cyton `N3P` | `C3` | EEG |
| 4 | Cyton `N4P` | `C4` | EEG |
| 5 | Cyton `N5P` | `P7` | EEG |
| 6 | Cyton `N6P` | `P8` | EEG |
| 7 | Cyton `N7P` | `O1` | EEG |
| 8 | Cyton `N8P` | `O2` | EEG |
| 9 | Daisy `N1P` | `F7` | EEG |
| 10 | Daisy `N2P` | `F8` | EEG |
| 11 | Daisy `N3P` | `F3` | EEG |
| 12 | Daisy `N4P` | `F4` | EEG |
| 13 | Daisy `N5P` | `left_upper_trapezius_emg` | EMG |
| 14 | Daisy `N6P` | `right_upper_trapezius_emg` | EMG |
| 15 | Daisy `N7P` | `P3` | EEG |
| 16 | Daisy `N8P` | `P4` | EEG |

The two posture EMG nodes are mapped as requested:

| Replace EEG node | Standard board/channel mapping | New signal | Placement |
| --- | --- | --- | --- |
| `T7` | Daisy `N5P`, logical channel `13` if Cyton is `1-8` and Daisy is `9-16` | Left posture EMG | Left upper trapezius |
| `T8` | Daisy `N6P`, logical channel `14` if Cyton is `1-8` and Daisy is `9-16` | Right posture EMG | Right upper trapezius |

In the config this means:

- `index: 13`, `label: left_upper_trapezius_emg`, `type: emg`
- `index: 14`, `label: right_upper_trapezius_emg`, `type: emg`

These EMG channels are excluded from EEG preprocessing and bandpower. They are only used for posture/strain features unless `scoring.emg_affects_focus` is explicitly changed later.

## Requirements

Use the Python environment that has:

```powershell
pip install numpy scipy PyYAML pylsl brainflow bleak bleakheart pytest
```

The current machine already had these installed during implementation.

For live hardware:

- Set `openbci.connection.serial_port` in `config/slopodoro_acquisition.yaml`.
- Set `polar.device_id` if using Polar H10 live mode.
- Leave `polar.required: false` unless the run should fail when Polar is unavailable.

## Common Commands

Validate config only:

```powershell
python -m slopodoro_acq.main --config config/slopodoro_acquisition.yaml --dry-run
```

Run synthetic calibration and scoring without hardware:

```powershell
python -m slopodoro_acq.main --config config/slopodoro_acquisition.yaml --synthetic --mode calibrate-and-run
```

Run synthetic without LSL publishing, useful for tests or file-only debugging:

```powershell
python -m slopodoro_acq.main --config config/slopodoro_acquisition.yaml --synthetic --mode calibrate-and-run --no-lsl
```

Run live calibration and then live scoring:

```powershell
python -m slopodoro_acq.main --config config/slopodoro_acquisition.yaml --mode calibrate-and-run
```

Run live scoring only:

```powershell
python -m slopodoro_acq.main --config config/slopodoro_acquisition.yaml --mode run
```

Stop live acquisition with `Ctrl-C`. The shutdown handler writes `acquisition_stop`, flushes files, releases OpenBCI, and disconnects Polar.

## Live Validation Plot

To visually validate acquisition, start the acquisition process in one terminal, then run the validation plot in a second terminal:

```powershell
python -m slopodoro_acq.validation_plot --config config/slopodoro_acquisition.yaml
```

For a one-command hardware validation that starts the configured sources itself:

```powershell
python -m slopodoro_acq.validation_plot --config config/slopodoro_acquisition.yaml --start-sources
```

The plot opens three live panes:

- EEG: all configured EEG channels from the raw OpenBCI stream, stacked on one graph.
- EMG: the two configured posture channels, `left_upper_trapezius_emg` and `right_upper_trapezius_emg`, stacked on one graph.
- ECG: raw Polar ECG on one graph.

Useful options:

```powershell
python -m slopodoro_acq.validation_plot --window-seconds 20
python -m slopodoro_acq.validation_plot --require-ecg
python -m slopodoro_acq.validation_plot --eeg-scale-uv 75 --emg-scale-uv 25
```

If ECG is not found and `--require-ecg` is not set, the window still opens for OpenBCI validation and the ECG pane reports the missing stream.

## Calibration Phases

`calibrate` and `calibrate-and-run` run these phases:

1. `eyes_open_baseline`
2. `eyes_closed_baseline`
3. `focused_task_baseline`
4. `emg_neutral_baseline`
5. `emg_strain_reference`

Durations come from the `calibration` section of the config. In `--synthetic` mode, phases are shortened by `synthetic.calibration_phase_seconds` so the full path can be tested quickly.

## LSL Streams

Configured stream names:

- Raw OpenBCI: `slopodoro_openbci_raw`
- Raw Polar ECG: `slopodoro_polar_ecg`
- Polar HR/RR: `slopodoro_polar_hr_rr`
- Markers: `slopodoro_markers`
- Features: `slopodoro_features`
- Scores: `slopodoro_scores`

Feature and score LSL payloads are JSON strings. Raw streams are numeric LSL streams.

## Output Directory

Each run creates:

```text
data/sessions/{session_id}/
```

If `session.session_id` is `auto`, the loader creates an ID like:

```text
anon_001_YYYYMMDD_HHMMSS
```

Generated session folders are ignored by git. `data/sessions/.gitkeep` keeps the parent directory present.

## Output Files

- `config_snapshot.yaml`: config used for the run.
- `raw_stream_metadata.json`: stream names, source IDs, sample rates, and channel map.
- `markers.jsonl`: marker events with timestamps.
- `markers.csv`: marker events in CSV form.
- `raw_openbci.jsonl`: OpenBCI chunks with timestamps and raw 16-channel samples.
- `raw_polar_ecg.jsonl`: Polar ECG chunks with timestamps and samples.
- `raw_polar_hr_rr.jsonl`: Polar RR/HR events.
- `features.jsonl`: rolling merged EEG/EMG/ECG feature frames.
- `scores.jsonl`: rolling score/state frames intended for a later UI/backend to tail.
- `calibration.json`: robust per-feature calibration stats by phase.
- `health.jsonl`: source health snapshots.

## Score Output Shape

Each `scores.jsonl` row contains:

```json
{
  "timestamp": 123456.789,
  "session_id": "anon_001_...",
  "scores": {
    "focus_score_0_100": 73.2,
    "fatigue_drift_score_0_100": 28.1,
    "emg_strain_score_0_100": 61.5,
    "signal_quality_score_0_100": 91.0,
    "recovery_context_score_0_100": 55.4
  },
  "state": "focused_work",
  "flags": {
    "break_recommended": false,
    "strain_notice": true,
    "bad_signal": false,
    "polar_missing": false,
    "openbci_missing": false
  },
  "explanation": {
    "primary": "sustained_right_trap_emg",
    "supporting_features": ["emg.left_rms_z", "emg.right_rms_z", "emg.bilateral_strain_score"]
  }
}
```

States include `focused_work`, `break_recommended`, `strain_notice`, `bad_signal`, `polar_missing`, and `openbci_missing`.

## Feature Output Shape

Each `features.jsonl` row contains:

```json
{
  "timestamp": 123456.789,
  "session_id": "anon_001_...",
  "window_start": 123452.0,
  "window_end": 123456.0,
  "features": {
    "eeg.engagement_index_z": -0.42,
    "emg.left_rms_z": 1.22,
    "emg.right_rms_z": 0.48,
    "ecg.heart_rate": 78.0,
    "ecg.rmssd_ms": 34.2
  },
  "validity": {
    "eeg_valid": true,
    "emg_valid": true,
    "ecg_valid": true,
    "artifact_fraction": 0.12,
    "bad_channels": []
  }
}
```

## Tests

Run:

```powershell
python -m pytest -q
```

The tests cover config validation, channel mapping, ring-buffer slicing, EEG/EMG/ECG feature output, calibration model creation, scoring hysteresis, bad-signal behavior, and a synthetic end-to-end smoke test.
