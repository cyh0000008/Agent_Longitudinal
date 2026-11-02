from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from typing import Any

from signal_lib import FolderSession, mask_to_segments


METRIC_CATEGORY = "MPI"
ACCELERATION_SIGNAL = "IMU_Yaw_Long_Acc_037_M2.IIMULonAccSec"
ACC_ACTIVE_SIGNAL = "Adaptive_Cruise_Disp_Stat.IACCAct376"
PEDAL_OVERRIDE_SIGNAL = "PPEI_Propulsion_General_Data_1_0C2_M.IAccPdlOvrrdAtv"
BRAKE_REQUEST_ACCEL_SIGNAL = "Adaptive_Cruise_Command_Ext_1A3_M.IACCBSCE_ACCAccl"
ACCELERATOR_PEDAL_POS_SIGNAL = "PPEI_Engine_General_Status_1_082_M2.IAccActPos"

DEFAULT_SMOOTHING_WINDOW = 5
DEFAULT_DEADBAND = 0.8
DEFAULT_MIN_OSCILLATION_AMPLITUDE = 0.75
DEFAULT_MIN_SIGN_FLIPS = 1
DEFAULT_SIGN_FLIP_MIN_STABLE_SAMPLES = 3
DEFAULT_AMPLITUDE_WINDOW_SECONDS = 1.00
DEFAULT_MIN_EVENT_DURATION_SECONDS = 1.00
DEFAULT_MAX_EVENT_DURATION_SECONDS = 4.00
DEFAULT_MERGE_GAP_SECONDS = 0.30
DEFAULT_LONG_PATTERN_MIN_PHASE_SECONDS = 1.5
DEFAULT_LONG_PATTERN_MAX_PHASE_SECONDS = 3.5
DEFAULT_LONG_PATTERN_MIN_PHASE_AMPLITUDE = 0.30
DEFAULT_LONG_PATTERN_MIN_PHASE_COUNT = 4
DEFAULT_LONG_PATTERN_MAX_GAP_SECONDS = 0.60
DEFAULT_DECEL_JUMP_WINDOW_SECONDS = 0.80
DEFAULT_DECEL_JUMP_MIN_DROP = 0.50
DEFAULT_DECEL_JUMP_REBOUND_WINDOW_SECONDS = 0.20
DEFAULT_DECEL_JUMP_MIN_REBOUND = 0.50
DEFAULT_DECEL_JUMP_MIN_JUMP_SLOPE = 2.2
DEFAULT_DECEL_JUMP_MIN_REBOUND_SLOPE = 3.0

REQUIRED_SIGNALS = [
    ACCELERATION_SIGNAL,
    ACC_ACTIVE_SIGNAL,
    PEDAL_OVERRIDE_SIGNAL,
    BRAKE_REQUEST_ACCEL_SIGNAL,
    ACCELERATOR_PEDAL_POS_SIGNAL,
]

SIGNAL_KEYS = {
    "acceleration": REQUIRED_SIGNALS[0],
    "acc_active": REQUIRED_SIGNALS[1],
    "pedal_override": REQUIRED_SIGNALS[2],
    "brake_request_accel": REQUIRED_SIGNALS[3],
    "accelerator_pedal_pos": REQUIRED_SIGNALS[4],
}


def _load_acc_jerkiness_module() -> object:
    module_path = Path(__file__).with_name("acc_jerkiness.py")
    spec = importlib.util.spec_from_file_location("_acc_jerkiness_shared", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载共享顿挫算法：{module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ACC_JERKINESS = _load_acc_jerkiness_module()


def __run_haoran_data_run__(folder_context: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    start_time = time.time()
    result_lines: list[str] = []
    stats: dict[str, Any] = {
        "All Case Count": 0,
        "Bad Case": 0,
        "Severity Score": 0.0,
    }

    session = folder_context.get("session")
    if session is None:
        result_lines.append("当前文件夹下未找到可处理的H5文件")
        result_lines.insert(0, f"【运算总耗时】: {time.time() - start_time:.4f} 秒")
        return False, result_lines, stats

    result = count_acc_override_brake_request_jerkiness_events(session)
    event_count = int(result["event_count"])
    valid_sample_count = int(result["valid_sample_count"])
    candidate_sample_count = int(result["candidate_sample_count"])
    max_severity = float(result["max_event_severity"])

    stats["All Case Count"] = event_count
    stats["Bad Case"] = event_count
    stats["Severity Score"] = max_severity

    result_lines.append(
        f"[ACC Override Brake Request Jerkiness] file={Path(session.primary_h5_file).name}, "
        f"event_count={event_count}, valid_sample_count={valid_sample_count}, "
        f"candidate_sample_count={candidate_sample_count}, max_severity={max_severity:.3f}"
    )
    for event in result["events"]:
        result_lines.append(
            f"[Event type={event.get('detection_type', 'oscillation')}, start={event['start_timestamp']:.3f}, "
            f"end={event['end_timestamp']:.3f}, duration={event['duration']:.3f}, "
            f"sample_count={event['sample_count']}, amplitude={event['amplitude']:.3f}, "
            f"sign_flips={event['sign_flips']}, severity={event['severity']:.3f}]"
        )

    result_lines.insert(0, f"【运算总耗时】: {time.time() - start_time:.4f} 秒")
    return event_count > 0, result_lines, stats


def count_acc_override_brake_request_jerkiness_events(
    session_or_h5_path: FolderSession | str | Path,
    smoothing_window: int = DEFAULT_SMOOTHING_WINDOW,
    deadband: float = DEFAULT_DEADBAND,
    min_oscillation_amplitude: float = DEFAULT_MIN_OSCILLATION_AMPLITUDE,
    min_sign_flips: int = DEFAULT_MIN_SIGN_FLIPS,
    sign_flip_min_stable_samples: int = DEFAULT_SIGN_FLIP_MIN_STABLE_SAMPLES,
    amplitude_window_seconds: float = DEFAULT_AMPLITUDE_WINDOW_SECONDS,
    min_event_duration_seconds: float = DEFAULT_MIN_EVENT_DURATION_SECONDS,
    max_event_duration_seconds: float = DEFAULT_MAX_EVENT_DURATION_SECONDS,
    merge_gap_seconds: float = DEFAULT_MERGE_GAP_SECONDS,
    long_pattern_min_phase_seconds: float = DEFAULT_LONG_PATTERN_MIN_PHASE_SECONDS,
    long_pattern_max_phase_seconds: float = DEFAULT_LONG_PATTERN_MAX_PHASE_SECONDS,
    long_pattern_min_phase_amplitude: float = DEFAULT_LONG_PATTERN_MIN_PHASE_AMPLITUDE,
    long_pattern_min_phase_count: int = DEFAULT_LONG_PATTERN_MIN_PHASE_COUNT,
    long_pattern_max_gap_seconds: float = DEFAULT_LONG_PATTERN_MAX_GAP_SECONDS,
    decel_jump_window_seconds: float = DEFAULT_DECEL_JUMP_WINDOW_SECONDS,
    decel_jump_min_drop: float = DEFAULT_DECEL_JUMP_MIN_DROP,
    decel_jump_rebound_window_seconds: float = DEFAULT_DECEL_JUMP_REBOUND_WINDOW_SECONDS,
    decel_jump_min_rebound: float = DEFAULT_DECEL_JUMP_MIN_REBOUND,
    decel_jump_min_jump_slope: float = DEFAULT_DECEL_JUMP_MIN_JUMP_SLOPE,
    decel_jump_min_rebound_slope: float = DEFAULT_DECEL_JUMP_MIN_REBOUND_SLOPE,
) -> dict[str, object]:
    session = session_or_h5_path if isinstance(session_or_h5_path, FolderSession) else FolderSession(session_or_h5_path)
    start_timestamp, end_timestamp = session.common_time_range(REQUIRED_SIGNALS)

    axis, accel_values_raw = session.get_signal_slice(SIGNAL_KEYS["acceleration"], start_timestamp, end_timestamp)
    accel_values = [float(value) for value in accel_values_raw]
    if len(axis) < 3:
        return _empty_result(
            start_timestamp,
            end_timestamp,
            0,
            0,
            smoothing_window,
            deadband,
            min_oscillation_amplitude,
            min_sign_flips,
            sign_flip_min_stable_samples,
            amplitude_window_seconds,
            min_event_duration_seconds,
            max_event_duration_seconds,
            merge_gap_seconds,
        )

    acc_active_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["acc_active"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    pedal_override_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["pedal_override"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    brake_request_accel_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["brake_request_accel"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    accelerator_pedal_pos_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["accelerator_pedal_pos"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )

    valid_mask = [
        _as_bool(acc_on)
        and _as_bool(override)
        and float(brake_request_accel) < 0.0
        and float(vehicle_accel) < 0.0
        and float(accelerator_pedal_pos) > 0.0
        for acc_on, override, brake_request_accel, vehicle_accel, accelerator_pedal_pos in zip(
            acc_active_values,
            pedal_override_values,
            brake_request_accel_values,
            accel_values,
            accelerator_pedal_pos_values,
        )
    ]

    smoothed_acceleration = _ACC_JERKINESS._moving_average(accel_values, smoothing_window)
    slope = []
    for index in range(1, len(smoothed_acceleration)):
        dt = axis[index] - axis[index - 1]
        slope.append(0.0 if dt <= 0 else (smoothed_acceleration[index] - smoothed_acceleration[index - 1]) / dt)

    candidate_timestamps = axis[1:]
    candidate_mask = [valid_mask[index] and valid_mask[index - 1] for index in range(1, len(axis))]
    valid_sample_count = sum(candidate_mask)

    original_candidate_mask = _ACC_JERKINESS._detect_oscillation_candidates(
        candidate_timestamps,
        slope,
        candidate_mask,
        smoothed_acceleration[1:],
        deadband,
        min_oscillation_amplitude,
        min_sign_flips,
        sign_flip_min_stable_samples,
        amplitude_window_seconds,
        min_event_duration_seconds,
        max_event_duration_seconds,
    )
    original_event_mask = _ACC_JERKINESS._merge_short_gaps(candidate_timestamps, original_candidate_mask, merge_gap_seconds)
    original_events = _build_event_details(
        candidate_timestamps,
        original_event_mask,
        smoothed_acceleration[1:],
        slope,
        deadband,
        min_oscillation_amplitude,
        min_sign_flips,
        sign_flip_min_stable_samples,
        amplitude_window_seconds,
        min_event_duration_seconds,
        max_event_duration_seconds,
        detection_type="oscillation",
    )

    long_cycle_events = _ACC_JERKINESS._detect_long_cycle_events(
        timestamps=candidate_timestamps,
        smoothed_acceleration=smoothed_acceleration[1:],
        slope=slope,
        valid_mask=candidate_mask,
        deadband=deadband,
        min_phase_seconds=long_pattern_min_phase_seconds,
        max_phase_seconds=long_pattern_max_phase_seconds,
        min_phase_amplitude=long_pattern_min_phase_amplitude,
        min_phase_count=long_pattern_min_phase_count,
        max_gap_seconds=long_pattern_max_gap_seconds,
        min_oscillation_amplitude=min_oscillation_amplitude,
        min_sign_flips=min_sign_flips,
    )
    decel_jump_events = _ACC_JERKINESS._detect_decel_jump_events(
        timestamps=candidate_timestamps,
        smoothed_acceleration=smoothed_acceleration[1:],
        valid_mask=candidate_mask,
        min_oscillation_amplitude=min_oscillation_amplitude,
        min_sign_flips=min_sign_flips,
        window_seconds=decel_jump_window_seconds,
        min_drop=decel_jump_min_drop,
        rebound_window_seconds=decel_jump_rebound_window_seconds,
        min_rebound=decel_jump_min_rebound,
        min_jump_slope=decel_jump_min_jump_slope,
        min_rebound_slope=decel_jump_min_rebound_slope,
    )

    events = _ACC_JERKINESS._merge_event_details(
        original_events + long_cycle_events + decel_jump_events,
        candidate_timestamps,
        smoothed_acceleration[1:],
        slope,
        deadband,
        min_oscillation_amplitude,
        min_sign_flips,
        sign_flip_min_stable_samples,
        merge_gap_seconds,
    )
    events = _ACC_JERKINESS._filter_events_by_reversal_pattern(
        events=events,
        timestamps=candidate_timestamps,
        smoothed_acceleration=smoothed_acceleration[1:],
        jump_window_seconds=decel_jump_window_seconds,
        min_jump_amplitude=decel_jump_min_drop,
        rebound_window_seconds=decel_jump_rebound_window_seconds,
        min_rebound=decel_jump_min_rebound,
        min_jump_slope=decel_jump_min_jump_slope,
        min_rebound_slope=decel_jump_min_rebound_slope,
    )
    merged_event_mask = _ACC_JERKINESS._events_to_mask(candidate_timestamps, events)
    max_event_severity = max((float(event["severity"]) for event in events), default=0.0)

    return {
        "metric_name": "acc_override_brake_request_jerkiness",
        "method": "acc_on_override_brake_request_decel_and_pedal_pos_jerkiness",
        "event_count": len(events),
        "valid_sample_count": valid_sample_count,
        "candidate_sample_count": sum(merged_event_mask),
        "total_sample_count": len(candidate_timestamps),
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": max_event_severity,
        "parameters": {
            "smoothing_window": smoothing_window,
            "deadband": deadband,
            "min_oscillation_amplitude": min_oscillation_amplitude,
            "min_sign_flips": min_sign_flips,
            "sign_flip_min_stable_samples": sign_flip_min_stable_samples,
            "amplitude_window_seconds": amplitude_window_seconds,
            "min_event_duration_seconds": min_event_duration_seconds,
            "max_event_duration_seconds": max_event_duration_seconds,
            "merge_gap_seconds": merge_gap_seconds,
            "long_pattern_min_phase_seconds": long_pattern_min_phase_seconds,
            "long_pattern_max_phase_seconds": long_pattern_max_phase_seconds,
            "long_pattern_min_phase_amplitude": long_pattern_min_phase_amplitude,
            "long_pattern_min_phase_count": long_pattern_min_phase_count,
            "long_pattern_max_gap_seconds": long_pattern_max_gap_seconds,
            "decel_jump_window_seconds": decel_jump_window_seconds,
            "decel_jump_min_drop": decel_jump_min_drop,
            "decel_jump_rebound_window_seconds": decel_jump_rebound_window_seconds,
            "decel_jump_min_rebound": decel_jump_min_rebound,
            "decel_jump_min_jump_slope": decel_jump_min_jump_slope,
            "decel_jump_min_rebound_slope": decel_jump_min_rebound_slope,
        },
        "events": events,
    }


def _empty_result(
    start_timestamp: float,
    end_timestamp: float,
    valid_sample_count: int,
    candidate_sample_count: int,
    smoothing_window: int,
    deadband: float,
    min_oscillation_amplitude: float,
    min_sign_flips: int,
    sign_flip_min_stable_samples: int,
    amplitude_window_seconds: float,
    min_event_duration_seconds: float,
    max_event_duration_seconds: float,
    merge_gap_seconds: float,
) -> dict[str, object]:
    return {
        "metric_name": "acc_override_brake_request_jerkiness",
        "method": "acc_on_override_brake_request_decel_and_pedal_pos_jerkiness",
        "event_count": 0,
        "valid_sample_count": valid_sample_count,
        "candidate_sample_count": candidate_sample_count,
        "total_sample_count": 0,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": 0.0,
        "parameters": {
            "smoothing_window": smoothing_window,
            "deadband": deadband,
            "min_oscillation_amplitude": min_oscillation_amplitude,
            "min_sign_flips": min_sign_flips,
            "sign_flip_min_stable_samples": sign_flip_min_stable_samples,
            "amplitude_window_seconds": amplitude_window_seconds,
            "min_event_duration_seconds": min_event_duration_seconds,
            "max_event_duration_seconds": max_event_duration_seconds,
            "merge_gap_seconds": merge_gap_seconds,
            "long_pattern_min_phase_seconds": DEFAULT_LONG_PATTERN_MIN_PHASE_SECONDS,
            "long_pattern_max_phase_seconds": DEFAULT_LONG_PATTERN_MAX_PHASE_SECONDS,
            "long_pattern_min_phase_amplitude": DEFAULT_LONG_PATTERN_MIN_PHASE_AMPLITUDE,
            "long_pattern_min_phase_count": DEFAULT_LONG_PATTERN_MIN_PHASE_COUNT,
            "long_pattern_max_gap_seconds": DEFAULT_LONG_PATTERN_MAX_GAP_SECONDS,
            "decel_jump_window_seconds": DEFAULT_DECEL_JUMP_WINDOW_SECONDS,
            "decel_jump_min_drop": DEFAULT_DECEL_JUMP_MIN_DROP,
            "decel_jump_rebound_window_seconds": DEFAULT_DECEL_JUMP_REBOUND_WINDOW_SECONDS,
            "decel_jump_min_rebound": DEFAULT_DECEL_JUMP_MIN_REBOUND,
            "decel_jump_min_jump_slope": DEFAULT_DECEL_JUMP_MIN_JUMP_SLOPE,
            "decel_jump_min_rebound_slope": DEFAULT_DECEL_JUMP_MIN_REBOUND_SLOPE,
        },
        "events": [],
    }


def _build_event_details(
    timestamps: list[float],
    event_mask: list[bool],
    smoothed_acceleration: list[float],
    slope: list[float],
    deadband: float,
    min_oscillation_amplitude: float,
    min_sign_flips: int,
    sign_flip_min_stable_samples: int,
    amplitude_window_seconds: float,
    min_event_duration_seconds: float,
    max_event_duration_seconds: float,
    detection_type: str = "oscillation",
) -> list[dict[str, float | int | str]]:
    events: list[dict[str, float | int | str]] = []
    for segment in mask_to_segments(timestamps, event_mask):
        start_index = _find_timestamp_index(timestamps, segment.start_timestamp)
        end_index = _find_timestamp_index(timestamps, segment.end_timestamp)
        if start_index < 0 or end_index < start_index:
            continue

        acc_window = smoothed_acceleration[start_index : end_index + 1]
        slope_window = slope[start_index : end_index + 1]
        if not acc_window:
            continue
        if segment.duration < min_event_duration_seconds or segment.duration > max_event_duration_seconds:
            continue

        amplitude = _ACC_JERKINESS._max_window_amplitude(
            timestamps,
            smoothed_acceleration,
            start_index,
            end_index,
            amplitude_window_seconds,
        )
        sign_flips = _ACC_JERKINESS._count_sign_flips(
            slope_window,
            deadband,
            sign_flip_min_stable_samples,
        )
        severity = _ACC_JERKINESS._score_event_severity(
            amplitude,
            sign_flips,
            segment.duration,
            min_oscillation_amplitude,
            min_sign_flips,
        )
        events.append(
            {
                "start_timestamp": segment.start_timestamp,
                "end_timestamp": segment.end_timestamp,
                "duration": segment.duration,
                "sample_count": segment.sample_count,
                "amplitude": amplitude,
                "sign_flips": sign_flips,
                "severity": severity,
                "detection_type": detection_type,
            }
        )
    return events


def _find_timestamp_index(timestamps: list[float], target: float) -> int:
    for index, value in enumerate(timestamps):
        if value == target:
            return index
    return -1


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    return text not in {"0", "false", "none", ""}
