from __future__ import annotations

import time
from bisect import bisect_right
from pathlib import Path
from typing import Any

from signal_lib import FolderSession


METRIC_CATEGORY = "MPI"
ACCELERATION_SIGNAL = "IMU_Yaw_Long_Acc_037_M2.IIMULonAccSec"
ACC_ACTIVE_SIGNAL = "Adaptive_Cruise_Disp_Stat.IACCAct376"
PEDAL_OVERRIDE_SIGNAL = "PPEI_Propulsion_General_Data_1_0C2_M.IAccPdlOvrrdAtv"

DEFAULT_POST_OVERRIDE_WINDOW_SECONDS = 5.0
DEFAULT_ACCELERATION_SMOOTHING_WINDOW = 5
DEFAULT_JERK_SMOOTHING_WINDOW = 5
DEFAULT_HEAVY_BRAKE_ACCEL_THRESHOLD = -2.0
DEFAULT_HEAVY_BRAKE_JERK_THRESHOLD = -3.0
DEFAULT_MIN_BRAKE_DURATION_SECONDS = 0.20
DEFAULT_MERGE_GAP_SECONDS = 0.50

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

    result = count_acc_heavy_braking_after_override_release_events(session)
    event_count = int(result["event_count"])
    max_severity = float(result["max_event_severity"])

    stats["All Case Count"] = event_count
    stats["Bad Case"] = event_count
    stats["Severity Score"] = max_severity

    result_lines.append(
        f"[ACC Heavy Braking After Override Release] file={Path(session.primary_h5_file).name}, event_count={event_count}, "
        f"scene_count={int(result['scene_count'])}, qualified_sample_count={int(result['qualified_sample_count'])}, "
        f"max_severity={max_severity:.3f}"
    )
    for scene in result["scenes"]:
        result_lines.append(
            f"[Scene release={scene['release_timestamp']:.3f}, start={scene['start_timestamp']:.3f}, end={scene['end_timestamp']:.3f}, "
            f"duration={scene['duration']:.3f}, event_count={scene['event_count']}, severity={scene['severity']:.3f}]"
        )
    for event in result["events"]:
        result_lines.append(
            f"[Event start={event['start_timestamp']:.3f}, end={event['end_timestamp']:.3f}, duration={event['duration']:.3f}, "
            f"sample_count={event['sample_count']}, min_acceleration={event['min_acceleration']:.3f}, "
            f"min_filtered_jerk={event['min_filtered_jerk']:.3f}, severity={event['severity']:.3f}]"
        )

    result_lines.insert(0, f"【运算总耗时】: {time.time() - start_time:.4f} 秒")
    return event_count > 0, result_lines, stats


def count_acc_heavy_braking_after_override_release_events(
    session_or_h5_path: FolderSession | str | Path,
    post_override_window_seconds: float = DEFAULT_POST_OVERRIDE_WINDOW_SECONDS,
    acceleration_smoothing_window: int = DEFAULT_ACCELERATION_SMOOTHING_WINDOW,
    jerk_smoothing_window: int = DEFAULT_JERK_SMOOTHING_WINDOW,
    heavy_brake_accel_threshold: float = DEFAULT_HEAVY_BRAKE_ACCEL_THRESHOLD,
    heavy_brake_jerk_threshold: float = DEFAULT_HEAVY_BRAKE_JERK_THRESHOLD,
    min_brake_duration_seconds: float = DEFAULT_MIN_BRAKE_DURATION_SECONDS,
    merge_gap_seconds: float = DEFAULT_MERGE_GAP_SECONDS,
) -> dict[str, object]:
    session = session_or_h5_path if isinstance(session_or_h5_path, FolderSession) else FolderSession(session_or_h5_path)
    start_timestamp, end_timestamp = session.common_time_range(REQUIRED_SIGNALS)

    axis, accel_raw = session.get_signal_slice(SIGNAL_KEYS["acceleration"], start_timestamp, end_timestamp)
    accel_values = [float(value) for value in accel_raw]
    if len(axis) < 3:
        return _empty_result(start_timestamp, end_timestamp, post_override_window_seconds, acceleration_smoothing_window, jerk_smoothing_window, heavy_brake_accel_threshold, heavy_brake_jerk_threshold, min_brake_duration_seconds, merge_gap_seconds)

    acc_active_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["acc_active"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    pedal_override_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["pedal_override"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )

    scenes = _collect_post_override_scenes(
        axis=axis,
        acc_active_values=acc_active_values,
        pedal_override_values=pedal_override_values,
        window_seconds=post_override_window_seconds,
    )
    if not scenes:
        return _empty_result(start_timestamp, end_timestamp, post_override_window_seconds, acceleration_smoothing_window, jerk_smoothing_window, heavy_brake_accel_threshold, heavy_brake_jerk_threshold, min_brake_duration_seconds, merge_gap_seconds)

    qualified_mask = [False] * len(axis)
    for scene in scenes:
        for index in range(int(scene["start_index"]), int(scene["end_index"]) + 1):
            qualified_mask[index] = _as_bool(acc_active_values[index]) and (not _as_bool(pedal_override_values[index]))

    smoothed_acc = _moving_average(accel_values, acceleration_smoothing_window)
    jerk_raw: list[float] = []
    for index in range(1, len(smoothed_acc)):
        dt = axis[index] - axis[index - 1]
        jerk_raw.append(0.0 if dt <= 0 else (smoothed_acc[index] - smoothed_acc[index - 1]) / dt)
    filtered_jerk = _moving_average(jerk_raw, jerk_smoothing_window)

    candidate_timestamps = axis[1:]
    candidate_valid_mask = [qualified_mask[index] and qualified_mask[index - 1] for index in range(1, len(axis))]
    accel_candidates = accel_values[1:]
    qualified_sample_count = sum(candidate_valid_mask)

    heavy_brake_mask = [
        is_valid and accel_value < heavy_brake_accel_threshold
        for is_valid, accel_value in zip(candidate_valid_mask, accel_candidates)
    ]

    raw_events = _detect_heavy_braking_events(
        timestamps=candidate_timestamps,
        braking_mask=heavy_brake_mask,
        accel_values=accel_candidates,
        filtered_jerk=filtered_jerk,
        jerk_threshold=heavy_brake_jerk_threshold,
        min_duration_seconds=min_brake_duration_seconds,
        heavy_brake_accel_threshold=heavy_brake_accel_threshold,
    )
    events = _merge_close_events(
        raw_events,
        timestamps=candidate_timestamps,
        accel_values=accel_candidates,
        filtered_jerk=filtered_jerk,
        accel_threshold=heavy_brake_accel_threshold,
        jerk_threshold=heavy_brake_jerk_threshold,
        merge_gap_seconds=merge_gap_seconds,
    )

    scene_results: list[dict[str, float | int]] = []
    for scene in scenes:
        scene_start = float(scene["start_timestamp"])
        scene_end = float(scene["end_timestamp"])
        overlapping_events = [
            event for event in events
            if float(event["end_timestamp"]) >= scene_start and float(event["start_timestamp"]) <= scene_end
        ]
        scene_results.append(
            {
                "release_timestamp": float(scene["release_timestamp"]),
                "start_timestamp": scene_start,
                "end_timestamp": scene_end,
                "duration": float(scene["duration"]),
                "sample_count": int(scene["sample_count"]),
                "event_count": len(overlapping_events),
                "severity": round(max((float(event["severity"]) for event in overlapping_events), default=0.0), 3),
            }
        )

    return {
        "metric_name": "acc_heavy_braking_after_override_release_count",
        "method": "post_override_window_heavy_brake_with_filtered_jerk_gate",
        "event_count": len(events),
        "scene_count": len(scene_results),
        "qualified_sample_count": qualified_sample_count,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": max((float(event["severity"]) for event in events), default=0.0),
        "parameters": {
            "post_override_window_seconds": post_override_window_seconds,
            "acceleration_smoothing_window": acceleration_smoothing_window,
            "jerk_smoothing_window": jerk_smoothing_window,
            "heavy_brake_accel_threshold": heavy_brake_accel_threshold,
            "heavy_brake_jerk_threshold": heavy_brake_jerk_threshold,
            "min_brake_duration_seconds": min_brake_duration_seconds,
            "merge_gap_seconds": merge_gap_seconds,
        },
        "scenes": scene_results,
        "events": events,
    }


def _collect_post_override_scenes(
    axis: list[float],
    acc_active_values: list[object],
    pedal_override_values: list[object],
    window_seconds: float,
) -> list[dict[str, float | int]]:
    scenes: list[dict[str, float | int]] = []
    if len(axis) < 2 or window_seconds <= 0:
        return scenes

    for index in range(1, len(axis)):
        previous_override = _as_bool(pedal_override_values[index - 1])
        current_override = _as_bool(pedal_override_values[index])
        if not previous_override or current_override:
            continue

        release_timestamp = float(axis[index])
        window_end_target = release_timestamp + window_seconds
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
                "release_timestamp": release_timestamp,
                "start_timestamp": float(axis[index]),
                "end_timestamp": float(axis[end_index]),
                "duration": float(axis[end_index] - axis[index]),
                "sample_count": end_index - index + 1,
                "start_index": index,
                "end_index": end_index,
            }
        )
    return scenes


def _detect_heavy_braking_events(
    timestamps: list[float],
    braking_mask: list[bool],
    accel_values: list[float],
    filtered_jerk: list[float],
    jerk_threshold: float,
    min_duration_seconds: float,
    heavy_brake_accel_threshold: float,
) -> list[dict[str, float | int]]:
    events: list[dict[str, float | int]] = []
    for segment in mask_to_segments(timestamps, braking_mask):
        if segment.duration < min_duration_seconds:
            continue
        start_index = segment.start_index
        end_index = segment.end_index
        jerk_window = filtered_jerk[start_index : end_index + 1]
        if not jerk_window or min(jerk_window) >= jerk_threshold:
            continue
        accel_window = accel_values[start_index : end_index + 1]
        if not accel_window:
            continue
        min_acc = min(accel_window)
        min_filtered_jerk = min(jerk_window)
        severity = _score_event_severity(min_acc, min_filtered_jerk, heavy_brake_accel_threshold, jerk_threshold)
        events.append(
            {
                "start_timestamp": segment.start_timestamp,
                "end_timestamp": segment.end_timestamp,
                "duration": segment.duration,
                "sample_count": segment.sample_count,
                "min_acceleration": min_acc,
                "min_filtered_jerk": min_filtered_jerk,
                "severity": severity,
                "start_index": start_index,
                "end_index": end_index,
            }
        )
    return events


def _merge_close_events(
    events: list[dict[str, float | int]],
    timestamps: list[float],
    accel_values: list[float],
    filtered_jerk: list[float],
    accel_threshold: float,
    jerk_threshold: float,
    merge_gap_seconds: float,
) -> list[dict[str, float | int]]:
    if not events:
        return []
    sorted_events = sorted(events, key=lambda item: float(item["start_timestamp"]))
    merged: list[dict[str, float | int]] = [dict(sorted_events[0])]
    for event in sorted_events[1:]:
        last = merged[-1]
        if float(event["start_timestamp"]) - float(last["end_timestamp"]) > merge_gap_seconds:
            merged.append(dict(event))
            continue
        start_index = int(last["start_index"])
        end_index = int(event["end_index"])
        accel_window = accel_values[start_index : end_index + 1]
        jerk_window = filtered_jerk[start_index : end_index + 1]
        min_acc = min(accel_window) if accel_window else float(last["min_acceleration"])
        min_filtered_jerk = min(jerk_window) if jerk_window else float(last["min_filtered_jerk"])
        start_timestamp = float(last["start_timestamp"])
        end_timestamp = float(event["end_timestamp"])
        duration = end_timestamp - start_timestamp if end_index > start_index else 0.0
        merged[-1] = {
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "duration": duration,
            "sample_count": end_index - start_index + 1,
            "min_acceleration": min_acc,
            "min_filtered_jerk": min_filtered_jerk,
            "severity": _score_event_severity(min_acc, min_filtered_jerk, accel_threshold, jerk_threshold),
            "start_index": start_index,
            "end_index": end_index,
        }
    return merged


def _score_event_severity(
    min_acceleration: float,
    min_filtered_jerk: float,
    accel_threshold: float,
    jerk_threshold: float,
) -> float:
    accel_excess = max(abs(min_acceleration) - abs(accel_threshold), 0.0)
    jerk_excess = max(abs(min_filtered_jerk) - abs(jerk_threshold), 0.0)
    return round(accel_excess * 60.0 + jerk_excess * 40.0, 3)


def _moving_average(values: list[float], window: int) -> list[float]:
    if window <= 1 or len(values) <= 2:
        return values[:]
    radius = max(0, window // 2)
    smoothed: list[float] = []
    for index in range(len(values)):
        left = max(0, index - radius)
        right = min(len(values), index + radius + 1)
        chunk = values[left:right]
        smoothed.append(sum(chunk) / len(chunk))
    return smoothed


def _empty_result(
    start_timestamp: float,
    end_timestamp: float,
    acceleration_smoothing_window: int,
    jerk_smoothing_window: int,
    heavy_brake_accel_threshold: float,
    heavy_brake_jerk_threshold: float,
    min_brake_duration_seconds: float,
    merge_gap_seconds: float,
) -> dict[str, object]:
    return {
        "metric_name": "acc_heavy_braking_after_override_release_count",
        "method": "heavy_brake_with_filtered_jerk_gate",
        "event_count": 0,
        "qualified_sample_count": 0,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": 0.0,
        "parameters": {
            "acceleration_smoothing_window": acceleration_smoothing_window,
            "jerk_smoothing_window": jerk_smoothing_window,
            "heavy_brake_accel_threshold": heavy_brake_accel_threshold,
            "heavy_brake_jerk_threshold": heavy_brake_jerk_threshold,
            "min_brake_duration_seconds": min_brake_duration_seconds,
            "merge_gap_seconds": merge_gap_seconds,
        },
        "events": [],
    }


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    return text not in {"0", "false", "none", ""}


def _empty_result(
    start_timestamp: float,
    end_timestamp: float,
    post_override_window_seconds: float,
    acceleration_smoothing_window: int,
    jerk_smoothing_window: int,
    heavy_brake_accel_threshold: float,
    heavy_brake_jerk_threshold: float,
    min_brake_duration_seconds: float,
    merge_gap_seconds: float,
) -> dict[str, object]:
    return {
        "metric_name": "acc_heavy_braking_after_override_release_count",
        "method": "post_override_window_heavy_brake_with_filtered_jerk_gate",
        "event_count": 0,
        "scene_count": 0,
        "qualified_sample_count": 0,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": 0.0,
        "parameters": {
            "post_override_window_seconds": post_override_window_seconds,
            "acceleration_smoothing_window": acceleration_smoothing_window,
            "jerk_smoothing_window": jerk_smoothing_window,
            "heavy_brake_accel_threshold": heavy_brake_accel_threshold,
            "heavy_brake_jerk_threshold": heavy_brake_jerk_threshold,
            "min_brake_duration_seconds": min_brake_duration_seconds,
            "merge_gap_seconds": merge_gap_seconds,
        },
        "scenes": [],
        "events": [],
    }
