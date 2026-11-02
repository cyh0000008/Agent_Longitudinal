from __future__ import annotations

import importlib.util
import time
from bisect import bisect_right
from pathlib import Path
from typing import Any

from signal_lib import FolderSession, mask_to_segments


METRIC_CATEGORY = "MPI"
ACCELERATION_SIGNAL = "IMU_Yaw_Long_Acc_037_M2.IIMULonAccSec"
ACC_ACTIVE_SIGNAL = "Adaptive_Cruise_Disp_Stat.IACCAct376"
PEDAL_OVERRIDE_SIGNAL = "PPEI_Propulsion_General_Data_1_0C2_M.IAccPdlOvrrdAtv"

DEFAULT_START_WINDOW_SECONDS = 1.0
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
]

SIGNAL_KEYS = {
    "acceleration": REQUIRED_SIGNALS[0],
    "acc_active": REQUIRED_SIGNALS[1],
    "pedal_override": REQUIRED_SIGNALS[2],
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

    result = analyze_acc_start_no_override_jerkiness(session)
    all_case_count = int(result["all_case_count"])
    bad_case_count = int(result["bad_case_count"])
    severity_score = float(result["severity_score"])

    stats["All Case Count"] = all_case_count
    stats["Bad Case"] = bad_case_count
    stats["Severity Score"] = severity_score

    result_lines.append(
        f"[ACC Start No Override Jerkiness] file={Path(session.primary_h5_file).name}, "
        f"all_case_count={all_case_count}, bad_case_count={bad_case_count}, "
        f"qualified_sample_count={int(result['qualified_sample_count'])}, severity_score={severity_score:.3f}"
    )
    for scene in result["scenes"]:
        result_lines.append(
            f"[Scene acc_start={scene['acc_start_timestamp']:.3f}, start={scene['start_timestamp']:.3f}, "
            f"end={scene['end_timestamp']:.3f}, duration={scene['duration']:.3f}, "
            f"event_count={scene['event_count']}, severity={scene['severity']:.3f}]"
        )
    for event in result["events"]:
        result_lines.append(
            f"[Event type={event.get('detection_type', 'oscillation')}, start={event['start_timestamp']:.3f}, "
            f"end={event['end_timestamp']:.3f}, duration={event['duration']:.3f}, "
            f"sample_count={event['sample_count']}, amplitude={event['amplitude']:.3f}, "
            f"sign_flips={event['sign_flips']}, severity={event['severity']:.3f}]"
        )

    result_lines.insert(0, f"【运算总耗时】: {time.time() - start_time:.4f} 秒")
    return bad_case_count > 0, result_lines, stats


def analyze_acc_start_no_override_jerkiness(
    session_or_h5_path: FolderSession | str | Path,
    start_window_seconds: float = DEFAULT_START_WINDOW_SECONDS,
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
        return _empty_scene_result(start_timestamp, end_timestamp, start_window_seconds)

    acc_active_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["acc_active"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    pedal_override_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["pedal_override"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )

    scenes = _collect_acc_start_scenes(
        axis=axis,
        acc_active_values=acc_active_values,
        pedal_override_values=pedal_override_values,
        window_seconds=start_window_seconds,
    )
    if not scenes:
        return _empty_scene_result(start_timestamp, end_timestamp, start_window_seconds)

    qualified_mask = [False] * len(axis)
    for scene in scenes:
        for index in range(int(scene["start_index"]), int(scene["end_index"]) + 1):
            qualified_mask[index] = _as_bool(acc_active_values[index]) and (not _as_bool(pedal_override_values[index]))

    smoothed_acceleration = _ACC_JERKINESS._moving_average(accel_values, smoothing_window)
    slope = []
    for index in range(1, len(smoothed_acceleration)):
        dt = axis[index] - axis[index - 1]
        slope.append(0.0 if dt <= 0 else (smoothed_acceleration[index] - smoothed_acceleration[index - 1]) / dt)

    candidate_timestamps = axis[1:]
    candidate_mask = [qualified_mask[index] and qualified_mask[index - 1] for index in range(1, len(axis))]

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

    bad_case_count = 0
    severity_score = 0.0
    scene_results: list[dict[str, float | int]] = []
    for scene in scenes:
        scene_start = float(scene["start_timestamp"])
        scene_end = float(scene["end_timestamp"])
        overlapping_events = [
            event
            for event in events
            if float(event["end_timestamp"]) >= scene_start and float(event["start_timestamp"]) <= scene_end
        ]
        event_count = len(overlapping_events)
        scene_severity = max((float(event["severity"]) for event in overlapping_events), default=0.0)
        if event_count > 0:
            bad_case_count += 1
            severity_score = max(severity_score, scene_severity)
        scene_results.append(
            {
                "acc_start_timestamp": float(scene["acc_start_timestamp"]),
                "start_timestamp": scene_start,
                "end_timestamp": scene_end,
                "duration": float(scene["duration"]),
                "sample_count": int(scene["sample_count"]),
                "event_count": event_count,
                "severity": round(scene_severity, 3),
            }
        )

    return {
        "metric_name": "acc_start_no_override_jerkiness",
        "all_case_count": len(scene_results),
        "bad_case_count": bad_case_count,
        "severity_score": round(severity_score, 3),
        "qualified_sample_count": sum(candidate_mask),
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "parameters": {
            "start_window_seconds": start_window_seconds,
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
        "scenes": scene_results,
        "events": events,
    }


def _collect_acc_start_scenes(
    axis: list[float],
    acc_active_values: list[object],
    pedal_override_values: list[object],
    window_seconds: float,
) -> list[dict[str, float | int]]:
    scenes: list[dict[str, float | int]] = []
    if len(axis) < 2 or window_seconds <= 0:
        return scenes

    for index in range(1, len(axis)):
        previous_acc_active = _as_bool(acc_active_values[index - 1])
        current_acc_active = _as_bool(acc_active_values[index])
        if previous_acc_active or not current_acc_active:
            continue

        acc_start_timestamp = float(axis[index])
        window_end_target = acc_start_timestamp + window_seconds
        end_index = bisect_right(axis, window_end_target) - 1
        if end_index <= index:
            continue
        if axis[end_index] < window_end_target:
            continue
        if not all(_as_bool(value) for value in acc_active_values[index : end_index + 1]):
            continue
        if any(_as_bool(value) for value in pedal_override_values[index : end_index + 1]):
            continue

        scenes.append(
            {
                "acc_start_timestamp": acc_start_timestamp,
                "start_timestamp": float(axis[index]),
                "end_timestamp": float(axis[end_index]),
                "duration": float(axis[end_index] - axis[index]),
                "sample_count": end_index - index + 1,
                "start_index": index,
                "end_index": end_index,
            }
        )
    return scenes


def _empty_scene_result(start_timestamp: float, end_timestamp: float, start_window_seconds: float) -> dict[str, object]:
    return {
        "metric_name": "acc_start_no_override_jerkiness",
        "all_case_count": 0,
        "bad_case_count": 0,
        "severity_score": 0.0,
        "qualified_sample_count": 0,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "parameters": {
            "start_window_seconds": start_window_seconds,
            "smoothing_window": DEFAULT_SMOOTHING_WINDOW,
            "deadband": DEFAULT_DEADBAND,
            "min_oscillation_amplitude": DEFAULT_MIN_OSCILLATION_AMPLITUDE,
            "min_sign_flips": DEFAULT_MIN_SIGN_FLIPS,
            "sign_flip_min_stable_samples": DEFAULT_SIGN_FLIP_MIN_STABLE_SAMPLES,
            "amplitude_window_seconds": DEFAULT_AMPLITUDE_WINDOW_SECONDS,
            "min_event_duration_seconds": DEFAULT_MIN_EVENT_DURATION_SECONDS,
            "max_event_duration_seconds": DEFAULT_MAX_EVENT_DURATION_SECONDS,
            "merge_gap_seconds": DEFAULT_MERGE_GAP_SECONDS,
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
        "scenes": [],
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
