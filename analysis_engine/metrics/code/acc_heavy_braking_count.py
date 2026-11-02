from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from signal_lib import FolderSession


METRIC_CATEGORY = "MPI"
ACCELERATION_SIGNAL = "IMU_Yaw_Long_Acc_037_M2.IIMULonAccSec"
ACC_ACTIVE_SIGNAL = "Adaptive_Cruise_Disp_Stat.IACCAct376"
PEDAL_OVERRIDE_SIGNAL = "PPEI_Propulsion_General_Data_1_0C2_M.IAccPdlOvrrdAtv"

DEFAULT_ACCELERATION_SMOOTHING_WINDOW = 5
DEFAULT_JERK_SMOOTHING_WINDOW = 5
DEFAULT_HEAVY_BRAKE_ACCEL_THRESHOLD = -1.8
DEFAULT_HEAVY_BRAKE_JERK_THRESHOLD = -2.0
DEFAULT_DECEL_JUMP_WINDOW_SECONDS = 1.60
DEFAULT_DECEL_JUMP_MIN_DROP = 2.00
DEFAULT_DECEL_JUMP_MIN_SLOPE = 1.0
DEFAULT_HEAVY_BRAKE_MIN_DWELL_SECONDS = 0.20
DEFAULT_DEEP_SPIKE_ACCEL_THRESHOLD = -2.7
DEFAULT_DEEP_SPIKE_MIN_DROP = 2.6
DEFAULT_DEEP_SPIKE_MAX_DWELL_SECONDS = 0.12
DEFAULT_DEEP_SPIKE_MIN_NEGATIVE_JERK = -10.0
DEFAULT_DEEP_SUSTAINED_ACCEL_THRESHOLD = -2.7
DEFAULT_DEEP_SUSTAINED_MIN_DROP = 1.3
DEFAULT_DEEP_SUSTAINED_MIN_DWELL_SECONDS = 0.35
DEFAULT_DEEP_SUSTAINED_MIN_NEGATIVE_JERK = -5.0
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

    result = count_acc_heavy_braking_events(session)
    event_count = int(result["event_count"])
    max_severity = float(result["max_event_severity"])

    stats["All Case Count"] = event_count
    stats["Bad Case"] = event_count
    stats["Severity Score"] = max_severity

    result_lines.append(
        f"[ACC Heavy Braking] file={Path(session.primary_h5_file).name}, event_count={event_count}, "
        f"qualified_sample_count={int(result['qualified_sample_count'])}, max_severity={max_severity:.3f}"
    )
    for event in result["events"]:
        result_lines.append(
            f"[Event start={event['start_timestamp']:.3f}, end={event['end_timestamp']:.3f}, "
            f"duration={event['duration']:.3f}, sample_count={event['sample_count']}, "
            f"start_acceleration={event['start_acceleration']:.3f}, min_acceleration={event['min_acceleration']:.3f}, "
            f"decel_drop={event['decel_drop']:.3f}, decel_slope={event['decel_slope']:.3f}, "
            f"heavy_brake_dwell_seconds={event['heavy_brake_dwell_seconds']:.3f}, "
            f"min_filtered_jerk={event['min_filtered_jerk']:.3f}, "
            f"type={event.get('detection_type', 'primary_decel_jump')}, severity={event['severity']:.3f}]"
        )

    result_lines.insert(0, f"【运算总耗时】: {time.time() - start_time:.4f} 秒")
    return event_count > 0, result_lines, stats


def count_acc_heavy_braking_events(
    session_or_h5_path: FolderSession | str | Path,
    acceleration_smoothing_window: int = DEFAULT_ACCELERATION_SMOOTHING_WINDOW,
    jerk_smoothing_window: int = DEFAULT_JERK_SMOOTHING_WINDOW,
    heavy_brake_accel_threshold: float = DEFAULT_HEAVY_BRAKE_ACCEL_THRESHOLD,
    heavy_brake_jerk_threshold: float = DEFAULT_HEAVY_BRAKE_JERK_THRESHOLD,
    decel_jump_window_seconds: float = DEFAULT_DECEL_JUMP_WINDOW_SECONDS,
    decel_jump_min_drop: float = DEFAULT_DECEL_JUMP_MIN_DROP,
    decel_jump_min_slope: float = DEFAULT_DECEL_JUMP_MIN_SLOPE,
    heavy_brake_min_dwell_seconds: float = DEFAULT_HEAVY_BRAKE_MIN_DWELL_SECONDS,
    merge_gap_seconds: float = DEFAULT_MERGE_GAP_SECONDS,
) -> dict[str, object]:
    session = session_or_h5_path if isinstance(session_or_h5_path, FolderSession) else FolderSession(session_or_h5_path)
    start_timestamp, end_timestamp = session.common_time_range(REQUIRED_SIGNALS)

    axis, accel_raw = session.get_signal_slice(SIGNAL_KEYS["acceleration"], start_timestamp, end_timestamp)
    accel_values = [float(value) for value in accel_raw]
    if len(axis) < 3:
        return _empty_result(
            start_timestamp,
            end_timestamp,
            acceleration_smoothing_window,
            jerk_smoothing_window,
            heavy_brake_accel_threshold,
            heavy_brake_jerk_threshold,
            decel_jump_window_seconds,
            decel_jump_min_drop,
            decel_jump_min_slope,
            heavy_brake_min_dwell_seconds,
            merge_gap_seconds,
        )

    acc_active_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["acc_active"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    pedal_override_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["pedal_override"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )

    valid_mask = [
        _as_bool(acc_on) and (not _as_bool(override))
        for acc_on, override in zip(acc_active_values, pedal_override_values)
    ]

    smoothed_acc = _moving_average(accel_values, acceleration_smoothing_window)
    jerk_raw: list[float] = []
    for index in range(1, len(smoothed_acc)):
        dt = axis[index] - axis[index - 1]
        jerk_raw.append(0.0 if dt <= 0 else (smoothed_acc[index] - smoothed_acc[index - 1]) / dt)
    filtered_jerk = _moving_average(jerk_raw, jerk_smoothing_window)

    candidate_timestamps = axis[1:]
    candidate_valid_mask = [valid_mask[index] and valid_mask[index - 1] for index in range(1, len(axis))]
    accel_candidates = smoothed_acc[1:]
    qualified_sample_count = sum(candidate_valid_mask)

    raw_events = _detect_heavy_braking_events(
        timestamps=candidate_timestamps,
        valid_mask=candidate_valid_mask,
        accel_values=accel_candidates,
        filtered_jerk=filtered_jerk,
        heavy_brake_accel_threshold=heavy_brake_accel_threshold,
        heavy_brake_jerk_threshold=heavy_brake_jerk_threshold,
        decel_jump_window_seconds=decel_jump_window_seconds,
        decel_jump_min_drop=decel_jump_min_drop,
        decel_jump_min_slope=decel_jump_min_slope,
        heavy_brake_min_dwell_seconds=heavy_brake_min_dwell_seconds,
    )
    events = _merge_close_events(
        raw_events,
        accel_values=accel_candidates,
        filtered_jerk=filtered_jerk,
        accel_threshold=heavy_brake_accel_threshold,
        jerk_threshold=heavy_brake_jerk_threshold,
        drop_threshold=decel_jump_min_drop,
        slope_threshold=decel_jump_min_slope,
        dwell_threshold=heavy_brake_min_dwell_seconds,
        merge_gap_seconds=merge_gap_seconds,
    )
    max_event_severity = max((float(event["severity"]) for event in events), default=0.0)

    return {
        "metric_name": "acc_heavy_braking_count",
        "method": "heavy_brake_decel_jump_detection",
        "event_count": len(events),
        "qualified_sample_count": qualified_sample_count,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": max_event_severity,
        "parameters": {
            "acceleration_smoothing_window": acceleration_smoothing_window,
            "jerk_smoothing_window": jerk_smoothing_window,
            "heavy_brake_accel_threshold": heavy_brake_accel_threshold,
            "heavy_brake_jerk_threshold": heavy_brake_jerk_threshold,
            "decel_jump_window_seconds": decel_jump_window_seconds,
            "decel_jump_min_drop": decel_jump_min_drop,
            "decel_jump_min_slope": decel_jump_min_slope,
            "heavy_brake_min_dwell_seconds": heavy_brake_min_dwell_seconds,
            "deep_spike_accel_threshold": DEFAULT_DEEP_SPIKE_ACCEL_THRESHOLD,
            "deep_spike_min_drop": DEFAULT_DEEP_SPIKE_MIN_DROP,
            "deep_spike_max_dwell_seconds": DEFAULT_DEEP_SPIKE_MAX_DWELL_SECONDS,
            "deep_spike_min_negative_jerk": DEFAULT_DEEP_SPIKE_MIN_NEGATIVE_JERK,
            "deep_sustained_accel_threshold": DEFAULT_DEEP_SUSTAINED_ACCEL_THRESHOLD,
            "deep_sustained_min_drop": DEFAULT_DEEP_SUSTAINED_MIN_DROP,
            "deep_sustained_min_dwell_seconds": DEFAULT_DEEP_SUSTAINED_MIN_DWELL_SECONDS,
            "deep_sustained_min_negative_jerk": DEFAULT_DEEP_SUSTAINED_MIN_NEGATIVE_JERK,
            "merge_gap_seconds": merge_gap_seconds,
        },
        "events": events,
    }


def _detect_heavy_braking_events(
    timestamps: list[float],
    valid_mask: list[bool],
    accel_values: list[float],
    filtered_jerk: list[float],
    heavy_brake_accel_threshold: float,
    heavy_brake_jerk_threshold: float,
    decel_jump_window_seconds: float,
    decel_jump_min_drop: float,
    decel_jump_min_slope: float,
    heavy_brake_min_dwell_seconds: float,
) -> list[dict[str, float | int]]:
    events: list[dict[str, float | int]] = []
    index = 0
    while index < len(timestamps) - 1:
        if not valid_mask[index]:
            index += 1
            continue

        start_acc = accel_values[index]
        best_end_index = -1
        best_drop = 0.0
        best_min_acc = start_acc
        best_dwell = 0.0
        heavy_zone_start_index = -1
        probe = index + 1
        while probe < len(timestamps) and valid_mask[probe] and timestamps[probe] - timestamps[index] <= decel_jump_window_seconds:
            end_acc = accel_values[probe]
            if end_acc <= heavy_brake_accel_threshold:
                if heavy_zone_start_index < 0:
                    heavy_zone_start_index = probe
            else:
                heavy_zone_start_index = -1
            drop = start_acc - end_acc
            if drop < decel_jump_min_drop or end_acc > heavy_brake_accel_threshold:
                probe += 1
                continue
            duration = timestamps[probe] - timestamps[index]
            if duration <= 0:
                probe += 1
                continue
            heavy_brake_dwell = (
                timestamps[probe] - timestamps[heavy_zone_start_index]
                if heavy_zone_start_index >= 0
                else 0.0
            )
            if heavy_brake_dwell < heavy_brake_min_dwell_seconds:
                probe += 1
                continue
            jump_slope = drop / duration
            if jump_slope < decel_jump_min_slope:
                probe += 1
                continue
            if filtered_jerk[probe] > heavy_brake_jerk_threshold:
                probe += 1
                continue
            if (
                drop > best_drop
                or (drop == best_drop and heavy_brake_dwell > best_dwell)
                or (drop == best_drop and heavy_brake_dwell == best_dwell and end_acc < best_min_acc)
            ):
                best_drop = drop
                best_end_index = probe
                best_min_acc = end_acc
                best_dwell = heavy_brake_dwell
            probe += 1

        if best_end_index < 0:
            supplemental_event = _build_supplemental_event(
                index=index,
                timestamps=timestamps,
                valid_mask=valid_mask,
                accel_values=accel_values,
                filtered_jerk=filtered_jerk,
                window_seconds=decel_jump_window_seconds,
            )
            if supplemental_event is not None:
                events.append(supplemental_event)
                index = int(supplemental_event["end_index"]) + 1
                continue
            index += 1
            continue

        jerk_window = filtered_jerk[index : best_end_index + 1]
        min_filtered_jerk = min(jerk_window) if jerk_window else filtered_jerk[best_end_index]
        jump_duration = timestamps[best_end_index] - timestamps[index]
        jump_slope = best_drop / max(jump_duration, 1e-6)
        severity = _score_event_severity(
            min_acceleration=best_min_acc,
            min_filtered_jerk=min_filtered_jerk,
            accel_threshold=heavy_brake_accel_threshold,
            jerk_threshold=heavy_brake_jerk_threshold,
            decel_drop=best_drop,
            decel_drop_threshold=decel_jump_min_drop,
            decel_slope=jump_slope,
            decel_slope_threshold=decel_jump_min_slope,
        )
        events.append(
            {
                "start_timestamp": timestamps[index],
                "end_timestamp": timestamps[best_end_index],
                "duration": jump_duration,
                "sample_count": best_end_index - index + 1,
                "start_acceleration": start_acc,
                "end_acceleration": accel_values[best_end_index],
                "min_acceleration": best_min_acc,
                "min_filtered_jerk": min_filtered_jerk,
                "decel_drop": best_drop,
                "decel_slope": jump_slope,
                "heavy_brake_dwell_seconds": best_dwell,
                "detection_type": "primary_decel_jump",
                "severity": severity,
                "start_index": index,
                "end_index": best_end_index,
            }
        )
        index = best_end_index + 1
    return events


def _merge_close_events(
    events: list[dict[str, float | int]],
    accel_values: list[float],
    filtered_jerk: list[float],
    accel_threshold: float,
    jerk_threshold: float,
    drop_threshold: float,
    slope_threshold: float,
    dwell_threshold: float,
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
        start_acc = float(last["start_acceleration"])
        end_acc = accel_values[end_index] if accel_window else float(event["end_acceleration"])
        start_timestamp = float(last["start_timestamp"])
        end_timestamp = float(event["end_timestamp"])
        duration = end_timestamp - start_timestamp if end_index > start_index else 0.0
        decel_drop = start_acc - min_acc
        decel_slope = decel_drop / max(duration, 1e-6)
        merged[-1] = {
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "duration": duration,
            "sample_count": end_index - start_index + 1,
            "start_acceleration": start_acc,
            "end_acceleration": end_acc,
            "min_acceleration": min_acc,
            "min_filtered_jerk": min_filtered_jerk,
            "decel_drop": decel_drop,
            "decel_slope": decel_slope,
            "heavy_brake_dwell_seconds": max(
                float(last.get("heavy_brake_dwell_seconds", 0.0)),
                float(event.get("heavy_brake_dwell_seconds", 0.0)),
                duration if min_acc <= accel_threshold else 0.0,
            ),
            "detection_type": _merge_detection_type(last, event),
            "severity": _score_event_severity(
                min_acceleration=min_acc,
                min_filtered_jerk=min_filtered_jerk,
                accel_threshold=accel_threshold,
                jerk_threshold=jerk_threshold,
                decel_drop=decel_drop,
                decel_drop_threshold=drop_threshold,
                decel_slope=decel_slope,
                decel_slope_threshold=slope_threshold,
            ),
            "start_index": start_index,
            "end_index": end_index,
        }
    return merged


def _build_supplemental_event(
    index: int,
    timestamps: list[float],
    valid_mask: list[bool],
    accel_values: list[float],
    filtered_jerk: list[float],
    window_seconds: float,
) -> dict[str, float | int | str] | None:
    start_acc = accel_values[index]
    heavy_zone_start_index = -1
    best_event: dict[str, float | int | str] | None = None
    probe = index + 1
    while probe < len(timestamps) and valid_mask[probe] and timestamps[probe] - timestamps[index] <= window_seconds:
        end_acc = accel_values[probe]
        if end_acc <= -1.8:
            if heavy_zone_start_index < 0:
                heavy_zone_start_index = probe
        else:
            heavy_zone_start_index = -1
        duration = timestamps[probe] - timestamps[index]
        if duration <= 0:
            probe += 1
            continue
        drop = start_acc - end_acc
        heavy_brake_dwell = (
            timestamps[probe] - timestamps[heavy_zone_start_index]
            if heavy_zone_start_index >= 0
            else 0.0
        )
        jerk_window = filtered_jerk[index : probe + 1]
        min_filtered_jerk = min(jerk_window) if jerk_window else filtered_jerk[probe]

        supplemental_type = None
        if (
            end_acc <= DEFAULT_DEEP_SPIKE_ACCEL_THRESHOLD
            and drop >= DEFAULT_DEEP_SPIKE_MIN_DROP
            and heavy_brake_dwell <= DEFAULT_DEEP_SPIKE_MAX_DWELL_SECONDS
            and min_filtered_jerk <= DEFAULT_DEEP_SPIKE_MIN_NEGATIVE_JERK
        ):
            supplemental_type = "deep_short_spike"
        elif (
            end_acc <= DEFAULT_DEEP_SUSTAINED_ACCEL_THRESHOLD
            and drop >= DEFAULT_DEEP_SUSTAINED_MIN_DROP
            and heavy_brake_dwell >= DEFAULT_DEEP_SUSTAINED_MIN_DWELL_SECONDS
            and min_filtered_jerk <= DEFAULT_DEEP_SUSTAINED_MIN_NEGATIVE_JERK
        ):
            supplemental_type = "deep_sustained_brake"
        if supplemental_type is None:
            probe += 1
            continue

        severity = _score_event_severity(
            min_acceleration=min(accel_values[index : probe + 1]),
            min_filtered_jerk=min_filtered_jerk,
            accel_threshold=DEFAULT_DEEP_SUSTAINED_ACCEL_THRESHOLD,
            jerk_threshold=DEFAULT_HEAVY_BRAKE_JERK_THRESHOLD,
            decel_drop=drop,
            decel_drop_threshold=DEFAULT_DEEP_SUSTAINED_MIN_DROP,
            decel_slope=drop / duration,
            decel_slope_threshold=DEFAULT_DECEL_JUMP_MIN_SLOPE,
        )
        candidate = {
            "start_timestamp": timestamps[index],
            "end_timestamp": timestamps[probe],
            "duration": duration,
            "sample_count": probe - index + 1,
            "start_acceleration": start_acc,
            "end_acceleration": end_acc,
            "min_acceleration": min(accel_values[index : probe + 1]),
            "min_filtered_jerk": min_filtered_jerk,
            "decel_drop": drop,
            "decel_slope": drop / duration,
            "heavy_brake_dwell_seconds": heavy_brake_dwell,
            "detection_type": supplemental_type,
            "severity": severity,
            "start_index": index,
            "end_index": probe,
        }
        if best_event is None or float(candidate["severity"]) > float(best_event["severity"]):
            best_event = candidate
        probe += 1
    return best_event


def _merge_detection_type(
    left: dict[str, float | int],
    right: dict[str, float | int],
) -> str:
    left_type = str(left.get("detection_type", "primary_decel_jump"))
    right_type = str(right.get("detection_type", "primary_decel_jump"))
    if left_type == right_type:
        return left_type
    if "primary_decel_jump" in {left_type, right_type}:
        return "mixed_primary_and_supplemental"
    return "mixed_supplemental"


def _score_event_severity(
    min_acceleration: float,
    min_filtered_jerk: float,
    accel_threshold: float,
    jerk_threshold: float,
    decel_drop: float,
    decel_drop_threshold: float,
    decel_slope: float,
    decel_slope_threshold: float,
) -> float:
    accel_excess = max(abs(min_acceleration) - abs(accel_threshold), 0.0)
    jerk_excess = max(abs(min_filtered_jerk) - abs(jerk_threshold), 0.0)
    drop_excess = max(decel_drop - decel_drop_threshold, 0.0)
    slope_excess = max(decel_slope - decel_slope_threshold, 0.0)
    return round(accel_excess * 30.0 + jerk_excess * 20.0 + drop_excess * 30.0 + slope_excess * 20.0, 3)


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
    decel_jump_window_seconds: float,
    decel_jump_min_drop: float,
    decel_jump_min_slope: float,
    heavy_brake_min_dwell_seconds: float,
    merge_gap_seconds: float,
) -> dict[str, object]:
    return {
        "metric_name": "acc_heavy_braking_count",
        "method": "heavy_brake_decel_jump_detection",
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
            "decel_jump_window_seconds": decel_jump_window_seconds,
            "decel_jump_min_drop": decel_jump_min_drop,
            "decel_jump_min_slope": decel_jump_min_slope,
            "heavy_brake_min_dwell_seconds": heavy_brake_min_dwell_seconds,
            "deep_spike_accel_threshold": DEFAULT_DEEP_SPIKE_ACCEL_THRESHOLD,
            "deep_spike_min_drop": DEFAULT_DEEP_SPIKE_MIN_DROP,
            "deep_spike_max_dwell_seconds": DEFAULT_DEEP_SPIKE_MAX_DWELL_SECONDS,
            "deep_spike_min_negative_jerk": DEFAULT_DEEP_SPIKE_MIN_NEGATIVE_JERK,
            "deep_sustained_accel_threshold": DEFAULT_DEEP_SUSTAINED_ACCEL_THRESHOLD,
            "deep_sustained_min_drop": DEFAULT_DEEP_SUSTAINED_MIN_DROP,
            "deep_sustained_min_dwell_seconds": DEFAULT_DEEP_SUSTAINED_MIN_DWELL_SECONDS,
            "deep_sustained_min_negative_jerk": DEFAULT_DEEP_SUSTAINED_MIN_NEGATIVE_JERK,
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
