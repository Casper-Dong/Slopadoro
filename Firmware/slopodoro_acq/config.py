from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when the Slopodoro acquisition config is missing or unsafe."""


@dataclass(frozen=True)
class SessionConfig:
    participant_id: str
    session_id: str
    output_dir: Path


@dataclass(frozen=True)
class ChannelConfig:
    index: int
    label: str
    type: str
    enabled: bool = True
    gain: str | None = None
    notes: str | None = None
    units: str = "uV"


@dataclass(frozen=True)
class OpenBCIConnectionConfig:
    serial_port: str | None = None
    brainflow_board_id: int | None = None
    streamer_params: str = ""
    startup_buffer_samples: int = 45000
    poll_interval_seconds: float = 0.05


@dataclass(frozen=True)
class OpenBCIConfig:
    enabled: bool
    board: str
    expected_sample_rate_hz: float
    stream_name: str
    source_id: str
    connection: OpenBCIConnectionConfig
    channels: tuple[ChannelConfig, ...]
    board_commands: tuple[str, ...] = ()

    @property
    def enabled_channels(self) -> tuple[ChannelConfig, ...]:
        return tuple(ch for ch in self.channels if ch.enabled)

    @property
    def eeg_channels(self) -> tuple[ChannelConfig, ...]:
        return tuple(ch for ch in self.enabled_channels if ch.type == "eeg")

    @property
    def emg_channels(self) -> tuple[ChannelConfig, ...]:
        return tuple(ch for ch in self.enabled_channels if ch.type == "emg")

    @property
    def eeg_indices_zero_based(self) -> list[int]:
        return [ch.index - 1 for ch in self.eeg_channels]

    @property
    def emg_indices_zero_based(self) -> list[int]:
        return [ch.index - 1 for ch in self.emg_channels]

    @property
    def eeg_labels(self) -> list[str]:
        return [ch.label for ch in self.eeg_channels]

    @property
    def emg_labels(self) -> list[str]:
        return [ch.label for ch in self.emg_channels]


@dataclass(frozen=True)
class PolarConfig:
    enabled: bool
    required: bool
    device_id: str | None
    stream_name_ecg: str
    stream_name_hr: str
    source_id_ecg: str
    source_id_hr: str
    expected_ecg_sample_rate_hz: float
    collect_ecg: bool
    collect_hr_rr: bool
    scan_timeout_seconds: float
    reconnect_delay_seconds: float


@dataclass(frozen=True)
class LSLConfig:
    marker_stream_name: str
    feature_stream_name: str
    score_stream_name: str


@dataclass(frozen=True)
class CalibrationConfig:
    eyes_open_seconds: float
    eyes_closed_seconds: float
    focused_task_seconds: float
    strain_neutral_seconds: float
    strain_reference_seconds: float
    min_valid_eeg_windows: int = 8
    min_valid_emg_windows: int = 4
    max_artifact_fraction: float = 0.45


@dataclass(frozen=True)
class FeatureConfig:
    eeg_window_seconds: float
    eeg_step_seconds: float
    emg_window_seconds: float
    emg_step_seconds: float
    ecg_fast_window_seconds: float
    hrv_window_seconds: float
    notch_hz: float
    eeg_bandpass_hz: tuple[float, float]
    emg_bandpass_hz: tuple[float, float]
    eeg_bands: dict[str, tuple[float, float]]


@dataclass(frozen=True)
class ScoringConfig:
    min_valid_eeg_fraction: float
    min_valid_emg_fraction: float
    slop_detection_minutes: float
    strain_detection_seconds: float
    use_polar_context: bool
    signal_quality_bad_threshold: float = 55.0
    focus_break_threshold: float = 45.0
    strain_notice_threshold: float = 70.0
    state_cooldown_seconds: float = 10.0
    emg_affects_focus: bool = False
    hackathon_mode: bool = False
    hackathon_bad_signal_threshold: float = 15.0
    hackathon_min_usable_eeg_channels: int = 4


@dataclass(frozen=True)
class SyntheticConfig:
    calibration_phase_seconds: float = 10.0
    run_seconds: float = 36.0
    chunk_seconds: float = 1.0
    seed: int = 42


@dataclass(frozen=True)
class AcquisitionConfig:
    session: SessionConfig
    openbci: OpenBCIConfig
    polar: PolarConfig
    lsl: LSLConfig
    calibration: CalibrationConfig
    features: FeatureConfig
    scoring: ScoringConfig
    synthetic: SyntheticConfig = field(default_factory=SyntheticConfig)
    raw: dict[str, Any] = field(default_factory=dict)
    config_path: Path | None = None

    def to_plain_dict(self) -> dict[str, Any]:
        return self.raw


def load_config(path: str | Path) -> AcquisitionConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"Config file does not exist: {config_path}")

    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")

    cfg = _parse_config(raw, config_path)
    validate_config(cfg)
    return cfg


def _parse_config(raw: dict[str, Any], config_path: Path) -> AcquisitionConfig:
    _require_sections(raw, ["session", "openbci", "polar", "lsl", "calibration", "features", "scoring"])

    session_raw = raw["session"]
    participant_id = str(_required(session_raw, "participant_id"))
    session_id = str(_required(session_raw, "session_id"))
    if session_id.lower() == "auto":
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_id = f"{participant_id}_{stamp}"
    output_dir = Path(str(_required(session_raw, "output_dir"))).expanduser()
    if not output_dir.is_absolute():
        output_dir = (config_path.parent.parent / output_dir).resolve()

    openbci_raw = raw["openbci"]
    channels = tuple(_parse_channel(ch) for ch in _required(openbci_raw, "channels"))
    conn_raw = openbci_raw.get("connection") or {}
    connection = OpenBCIConnectionConfig(
        serial_port=conn_raw.get("serial_port"),
        brainflow_board_id=conn_raw.get("brainflow_board_id"),
        streamer_params=str(conn_raw.get("streamer_params") or ""),
        startup_buffer_samples=int(conn_raw.get("startup_buffer_samples", 45000)),
        poll_interval_seconds=float(conn_raw.get("poll_interval_seconds", 0.05)),
    )
    openbci = OpenBCIConfig(
        enabled=bool(openbci_raw.get("enabled", True)),
        board=str(_required(openbci_raw, "board")),
        expected_sample_rate_hz=float(_required(openbci_raw, "expected_sample_rate_hz")),
        stream_name=str(_required(openbci_raw, "stream_name")),
        source_id=str(_required(openbci_raw, "source_id")),
        connection=connection,
        channels=channels,
        board_commands=tuple(str(cmd) for cmd in openbci_raw.get("board_commands", []) or []),
    )

    polar_raw = raw["polar"]
    polar = PolarConfig(
        enabled=bool(polar_raw.get("enabled", True)),
        required=bool(polar_raw.get("required", False)),
        device_id=polar_raw.get("device_id"),
        stream_name_ecg=str(_required(polar_raw, "stream_name_ecg")),
        stream_name_hr=str(_required(polar_raw, "stream_name_hr")),
        source_id_ecg=str(polar_raw.get("source_id_ecg", "polar_h10_ecg")),
        source_id_hr=str(polar_raw.get("source_id_hr", "polar_h10_hr_rr")),
        expected_ecg_sample_rate_hz=float(_required(polar_raw, "expected_ecg_sample_rate_hz")),
        collect_ecg=bool(polar_raw.get("collect_ecg", True)),
        collect_hr_rr=bool(polar_raw.get("collect_hr_rr", True)),
        scan_timeout_seconds=float(polar_raw.get("scan_timeout_seconds", 12.0)),
        reconnect_delay_seconds=float(polar_raw.get("reconnect_delay_seconds", 3.0)),
    )

    lsl_raw = raw["lsl"]
    lsl = LSLConfig(
        marker_stream_name=str(_required(lsl_raw, "marker_stream_name")),
        feature_stream_name=str(_required(lsl_raw, "feature_stream_name")),
        score_stream_name=str(_required(lsl_raw, "score_stream_name")),
    )

    cal_raw = raw["calibration"]
    calibration = CalibrationConfig(
        eyes_open_seconds=float(_required(cal_raw, "eyes_open_seconds")),
        eyes_closed_seconds=float(_required(cal_raw, "eyes_closed_seconds")),
        focused_task_seconds=float(_required(cal_raw, "focused_task_seconds")),
        strain_neutral_seconds=float(_required(cal_raw, "strain_neutral_seconds")),
        strain_reference_seconds=float(_required(cal_raw, "strain_reference_seconds")),
        min_valid_eeg_windows=int(cal_raw.get("min_valid_eeg_windows", 8)),
        min_valid_emg_windows=int(cal_raw.get("min_valid_emg_windows", 4)),
        max_artifact_fraction=float(cal_raw.get("max_artifact_fraction", 0.45)),
    )

    features_raw = raw["features"]
    eeg_bands = {
        str(name): _pair(values, f"features.eeg_bands.{name}")
        for name, values in _required(features_raw, "eeg_bands").items()
    }
    features = FeatureConfig(
        eeg_window_seconds=float(_required(features_raw, "eeg_window_seconds")),
        eeg_step_seconds=float(_required(features_raw, "eeg_step_seconds")),
        emg_window_seconds=float(_required(features_raw, "emg_window_seconds")),
        emg_step_seconds=float(_required(features_raw, "emg_step_seconds")),
        ecg_fast_window_seconds=float(_required(features_raw, "ecg_fast_window_seconds")),
        hrv_window_seconds=float(_required(features_raw, "hrv_window_seconds")),
        notch_hz=float(_required(features_raw, "notch_hz")),
        eeg_bandpass_hz=_pair(_required(features_raw, "eeg_bandpass_hz"), "features.eeg_bandpass_hz"),
        emg_bandpass_hz=_pair(_required(features_raw, "emg_bandpass_hz"), "features.emg_bandpass_hz"),
        eeg_bands=eeg_bands,
    )

    scoring_raw = raw["scoring"]
    scoring = ScoringConfig(
        min_valid_eeg_fraction=float(_required(scoring_raw, "min_valid_eeg_fraction")),
        min_valid_emg_fraction=float(_required(scoring_raw, "min_valid_emg_fraction")),
        slop_detection_minutes=float(_required(scoring_raw, "slop_detection_minutes")),
        strain_detection_seconds=float(_required(scoring_raw, "strain_detection_seconds")),
        use_polar_context=bool(scoring_raw.get("use_polar_context", True)),
        signal_quality_bad_threshold=float(scoring_raw.get("signal_quality_bad_threshold", 55.0)),
        focus_break_threshold=float(scoring_raw.get("focus_break_threshold", 45.0)),
        strain_notice_threshold=float(scoring_raw.get("strain_notice_threshold", 70.0)),
        state_cooldown_seconds=float(scoring_raw.get("state_cooldown_seconds", 10.0)),
        emg_affects_focus=bool(scoring_raw.get("emg_affects_focus", False)),
        hackathon_mode=bool(scoring_raw.get("hackathon_mode", False)),
        hackathon_bad_signal_threshold=float(scoring_raw.get("hackathon_bad_signal_threshold", 15.0)),
        hackathon_min_usable_eeg_channels=int(scoring_raw.get("hackathon_min_usable_eeg_channels", 4)),
    )

    syn_raw = raw.get("synthetic") or {}
    synthetic = SyntheticConfig(
        calibration_phase_seconds=float(syn_raw.get("calibration_phase_seconds", 10.0)),
        run_seconds=float(syn_raw.get("run_seconds", 36.0)),
        chunk_seconds=float(syn_raw.get("chunk_seconds", 1.0)),
        seed=int(syn_raw.get("seed", 42)),
    )

    return AcquisitionConfig(
        session=SessionConfig(participant_id=participant_id, session_id=session_id, output_dir=output_dir),
        openbci=openbci,
        polar=polar,
        lsl=lsl,
        calibration=calibration,
        features=features,
        scoring=scoring,
        synthetic=synthetic,
        raw=raw,
        config_path=config_path,
    )


def validate_config(cfg: AcquisitionConfig) -> None:
    if cfg.openbci.enabled:
        validate_openbci_channel_map(cfg.openbci)
    if cfg.polar.enabled and cfg.polar.required and not cfg.polar.device_id:
        raise ConfigError("polar.required=true requires polar.device_id")
    if cfg.features.notch_hz <= 0:
        raise ConfigError("features.notch_hz must be positive")
    if cfg.features.eeg_bandpass_hz[0] >= cfg.features.eeg_bandpass_hz[1]:
        raise ConfigError("features.eeg_bandpass_hz must be [low, high]")
    if cfg.features.emg_bandpass_hz[0] >= cfg.features.emg_bandpass_hz[1]:
        raise ConfigError("features.emg_bandpass_hz must be [low, high]")
    for name, band in cfg.features.eeg_bands.items():
        if band[0] >= band[1]:
            raise ConfigError(f"EEG band {name!r} must be [low, high]")


def validate_openbci_channel_map(openbci: OpenBCIConfig) -> None:
    enabled = openbci.enabled_channels
    indices = [ch.index for ch in openbci.channels]
    if len(set(indices)) != len(indices):
        raise ConfigError("OpenBCI channel map contains duplicated physical channel indices")
    if openbci.board == "cyton_daisy":
        if len(openbci.channels) != 16:
            raise ConfigError("Cyton+Daisy mode requires exactly 16 configured physical channels")
        bad_indices = [idx for idx in indices if idx < 1 or idx > 16]
        if bad_indices:
            raise ConfigError(f"Cyton+Daisy channel indices must be 1..16; got {bad_indices}")

    unknown_types = sorted({ch.type for ch in enabled if ch.type not in {"eeg", "emg"}})
    if unknown_types:
        raise ConfigError(f"Unsupported channel signal type(s): {unknown_types}")

    eeg_count = sum(1 for ch in enabled if ch.type == "eeg")
    emg_count = sum(1 for ch in enabled if ch.type == "emg")
    if eeg_count < 8:
        raise ConfigError(f"OpenBCI channel map requires at least 8 enabled EEG channels; got {eeg_count}")
    if emg_count > 2:
        raise ConfigError(f"Current MVP supports at most 2 enabled EMG channels; got {emg_count}")


def _require_sections(raw: dict[str, Any], names: list[str]) -> None:
    missing = [name for name in names if name not in raw]
    if missing:
        raise ConfigError(f"Missing required config section(s): {', '.join(missing)}")


def _required(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise ConfigError(f"Missing required config field: {key}")
    return mapping[key]


def _pair(values: Any, label: str) -> tuple[float, float]:
    if not isinstance(values, (list, tuple)) or len(values) != 2:
        raise ConfigError(f"{label} must contain exactly two numeric values")
    return float(values[0]), float(values[1])


def _parse_channel(raw: dict[str, Any]) -> ChannelConfig:
    if not isinstance(raw, dict):
        raise ConfigError("Each openbci.channels item must be a mapping")
    ch_type = str(_required(raw, "type")).lower()
    return ChannelConfig(
        index=int(_required(raw, "index")),
        label=str(_required(raw, "label")),
        type=ch_type,
        enabled=bool(raw.get("enabled", True)),
        gain=raw.get("gain"),
        notes=raw.get("notes"),
        units=str(raw.get("units", "uV")),
    )
