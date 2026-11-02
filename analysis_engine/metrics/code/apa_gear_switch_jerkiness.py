from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from signal_lib import FolderSession, mask_to_segments


METRIC_CATEGORY = "CPI"
ACCELERATION_SIGNAL = "IMU_Yaw_Long_Acc_037_M2.IIMULonAccSec"
APA_TYPE_SIGNAL = "APA_Brake_Propulsion_Torqu_1F0_M.IAPABT_APAType"
APA_STATUS_SIGNAL = "Advanced_Park_Assist_Status_190_M.IAPASP_APAStsAuth"
GEAR_SIGNAL = "PPEI_Trans_General_Status_2_ECP_H1_0D7_M.ITransEstGear_ECP_H1"

DEFAULT_SWITCH_WINDOW_SECONDS = 2.0
DRIVE_GEAR_VALUES = {12}
REVERSE_GEAR_VALUES = {14}
APA_TYPE_VALUES = {1}
APA_STATUS_VALUES = {5, 6}

DEFAULT_SMOOTHING_WINDOW = 5
DEFAULT_DEADBAND = 0.8
DEFAULT_MIN_OSCILLATION_AMPLITUDE = 0.75
DEFAULT_MIN_SIGN_FLIPS = 1
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
    APA_TYPE_SIGNAL,
    APA_STATUS_SIGNAL,
    GEAR_SIGNAL,
]

SIGNAL_KEYS = {
    "acceleration": REQUIRED_SIGNALS[0],
    "apa_type": REQUIRED_SIGNALS[1],
    "apa_status": REQUIRED_SIGNALS[2],
    "gear": REQUIRED_SIGNALS[3],
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

    result = analyze_apa_gear_switch_jerkiness(session)
    stats["All Case Count"] = int(result["all_case_count"])
    stats["Bad Case"] = int(result["bad_case_count"])
    stats["Severity Score"] = float(result["severity_score"])

    result_lines.append(
        f"[APA Gear Switch Jerkiness] file={Path(session.primary_h5_file).name}, all_case_count={result['all_case_count']}, "
        f"bad_case_count={result['bad_case_count']}, severity_score={float(result['severity_score']):.3f}"
    )
    for scene in result["scenes"]:
        result_lines.append(
            f"[Scene switch={scene['switch_timestamp']:.3f}, from={scene['from_gear']}, to={scene['to_gear']}, "
            f"window_end={scene['window_end_timestamp']:.3f}, jerk_event_count={scene['jerk_event_count']}, "
            f"severity={scene['severity']:.3f}]"
        )

    result_lines.insert(0, f"【运算总耗时】: {time.time() - start_time:.4f} 秒")
    return int(result["bad_case_count"]) > 0, result_lines, stats


def analyze_apa_gear_switch_jerkiness(
    session_or_h5_path: FolderSession | str | Path,
    switch_window_seconds: float = DEFAULT_SWITCH_WINDOW_SECONDS,
    smoothing_window: int = DEFAULT_SMOOTHING_WINDOW,
    deadband: float = DEFAULT_DEADBAND,
    min_oscillation_amplitude: float = DEFAULT_MIN_OSCILLATION_AMPLITUDE,
    min_sign_flips: int = DEFAULT_MIN_SIGN_FLIPS,
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

    accel_axis, accel_values_raw = session.get_signal_slice(SIGNAL_KEYS["acceleration"], start_timestamp, end_timestamp)
    gear_axis, gear_values_raw = session.get_signal_slice(SIGNAL_KEYS["gear"], start_timestamp, end_timestamp)
    if len(accel_axis) < 3 or len(gear_axis) < 2:
        return _empty_metric_result(start_timestamp, end_timestamp, switch_window_seconds)

    accel_values = [float(value) for value in accel_values_raw]
    smoothed_acceleration = _moving_average(accel_values, smoothing_window)
    apa_type_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["apa_type"], accel_axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    apa_status_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["apa_status"], accel_axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    gear_on_accel_axis = session.sample_signal_to_axis(
        SIGNAL_KEYS["gear"], accel_axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )

    switch_scenes = []
    severity_score = 0.0
    bad_case_count = 0

    normalized_raw_gears = [_normalize_dr_gear(value) for value in gear_values_raw]
    for index in range(1, len(gear_axis)):
        prev_gear = normalized_raw_gears[index - 1]
        curr_gear = normalized_raw_gears[index]
        if prev_gear is None or curr_gear is None or prev_gear == curr_gear:
            continue

        switch_timestamp = gear_axis[index]
        window_end_timestamp = switch_timestamp + switch_window_seconds
        if window_end_timestamp > end_timestamp:
            continue

        if not _gear_stable_in_window(gear_axis, normalized_raw_gears, index, window_end_timestamp, curr_gear):
            continue

        window_indexes = [i for i, ts in enumerate(accel_axis) if switch_timestamp <= ts <= window_end_timestamp]
        if len(window_indexes) < 3:
            continue

        if not all(_matches_enum(apa_type_values[i], APA_TYPE_VALUES, {"1"}) for i in window_indexes):
            continue
        if not all(_matches_enum(apa_status_values[i], APA_STATUS_VALUES, {"5", "6", "apa guidance", "guidance", "active", "apa finish", "finish"}) for i in window_indexes):
            continue
        if not all(_normalize_dr_gear(gear_on_accel_axis[i]) == curr_gear for i in window_indexes):
            continue

        sub_axis = [accel_axis[i] for i in window_indexes]
        sub_acc = [smoothed_acceleration[i] for i in window_indexes]
        sub_events = _detect_jerk_events_on_window(
            timestamps=sub_axis,
            smoothed_acceleration=sub_acc,
            deadband=deadband,
            min_oscillation_amplitude=min_oscillation_amplitude,
            min_sign_flips=min_sign_flips,
            amplitude_window_seconds=amplitude_window_seconds,
            min_event_duration_seconds=min_event_duration_seconds,
            max_event_duration_seconds=max_event_duration_seconds,
            merge_gap_seconds=merge_gap_seconds,
            long_pattern_min_phase_seconds=long_pattern_min_phase_seconds,
            long_pattern_max_phase_seconds=long_pattern_max_phase_seconds,
            long_pattern_min_phase_amplitude=long_pattern_min_phase_amplitude,
            long_pattern_min_phase_count=long_pattern_min_phase_count,
            long_pattern_max_gap_seconds=long_pattern_max_gap_seconds,
            decel_jump_window_seconds=decel_jump_window_seconds,
            decel_jump_min_drop=decel_jump_min_drop,
            decel_jump_rebound_window_seconds=decel_jump_rebound_window_seconds,
            decel_jump_min_rebound=decel_jump_min_rebound,
            decel_jump_min_jump_slope=decel_jump_min_jump_slope,
            decel_jump_min_rebound_slope=decel_jump_min_rebound_slope,
        )
        scene_severity = max((float(event["severity"]) for event in sub_events), default=0.0)
        if sub_events:
            bad_case_count += 1
            severity_score = max(severity_score, scene_severity)

        switch_scenes.append(
            {
                "switch_timestamp": switch_timestamp,
                "window_end_timestamp": window_end_timestamp,
                "from_gear": prev_gear,
                "to_gear": curr_gear,
                "jerk_event_count": len(sub_events),
                "severity": scene_severity,
                "events": sub_events,
            }
        )

    return {
        "metric_name": "apa_gear_switch_jerkiness",
        "all_case_count": bad_case_count,
        "bad_case_count": bad_case_count,
        "severity_score": round(severity_score, 3),
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "parameters": {
            "switch_window_seconds": switch_window_seconds,
            "drive_gear_values": sorted(DRIVE_GEAR_VALUES),
            "reverse_gear_values": sorted(REVERSE_GEAR_VALUES),
            "apa_type_values": sorted(APA_TYPE_VALUES),
            "apa_status_values": sorted(APA_STATUS_VALUES),
        },
        "scenes": switch_scenes,
    }


def _detect_jerk_events_on_window(
    timestamps: list[float],
    smoothed_acceleration: list[float],
    deadband: float,
    min_oscillation_amplitude: float,
    min_sign_flips: int,
    amplitude_window_seconds: float,
    min_event_duration_seconds: float,
    max_event_duration_seconds: float,
    merge_gap_seconds: float,
    long_pattern_min_phase_seconds: float,
    long_pattern_max_phase_seconds: float,
    long_pattern_min_phase_amplitude: float,
    long_pattern_min_phase_count: int,
    long_pattern_max_gap_seconds: float,
    decel_jump_window_seconds: float,
    decel_jump_min_drop: float,
    decel_jump_rebound_window_seconds: float,
    decel_jump_min_rebound: float,
    decel_jump_min_jump_slope: float,
    decel_jump_min_rebound_slope: float,
) -> list[dict[str, float | int | str]]:
    slope = []
    for index in range(1, len(smoothed_acceleration)):
        dt = timestamps[index] - timestamps[index - 1]
        slope.append(0.0 if dt <= 0 else (smoothed_acceleration[index] - smoothed_acceleration[index - 1]) / dt)

    candidate_timestamps = timestamps[1:]
    candidate_mask = [True] * len(candidate_timestamps)
    if len(candidate_timestamps) < 2:
        return []

    original_candidate_mask = _detect_oscillation_candidates(
        candidate_timestamps,
        slope,
        candidate_mask,
        smoothed_acceleration[1:],
        deadband,
        min_oscillation_amplitude,
        min_sign_flips,
        amplitude_window_seconds,
        min_event_duration_seconds,
        max_event_duration_seconds,
    )
    original_events = _build_event_details(
        candidate_timestamps,
        _merge_short_gaps(candidate_timestamps, original_candidate_mask, merge_gap_seconds),
        smoothed_acceleration[1:],
        slope,
        deadband,
        min_oscillation_amplitude,
        min_sign_flips,
        amplitude_window_seconds,
        min_event_duration_seconds,
        max_event_duration_seconds,
        detection_type="oscillation",
    )
    long_cycle_events = _detect_long_cycle_events(
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
    decel_jump_events = _detect_decel_jump_events(
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
    events = _merge_event_details(
        original_events + long_cycle_events + decel_jump_events,
        candidate_timestamps,
        smoothed_acceleration[1:],
        slope,
        deadband,
        min_oscillation_amplitude,
        min_sign_flips,
        merge_gap_seconds,
    )
    return _filter_events_by_reversal_pattern(
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


def _gear_stable_in_window(
    gear_axis: list[float],
    normalized_raw_gears: list[int | None],
    switch_index: int,
    window_end_timestamp: float,
    expected_gear: int,
) -> bool:
    for index in range(switch_index, len(gear_axis)):
        if gear_axis[index] > window_end_timestamp:
            break
        if normalized_raw_gears[index] != expected_gear:
            return False
    return True


def _normalize_dr_gear(value: object) -> int | None:
    if _matches_enum(value, DRIVE_GEAR_VALUES, {"cvt forward gear", "forward gear", "drive", "d"}):
        return 12
    if _matches_enum(value, REVERSE_GEAR_VALUES, {"reverse gear", "reverse", "r"}):
        return 14
    return None


def _empty_metric_result(start_timestamp: float, end_timestamp: float, switch_window_seconds: float) -> dict[str, object]:
    return {
        "metric_name": "apa_gear_switch_jerkiness",
        "all_case_count": 0,
        "bad_case_count": 0,
        "severity_score": 0.0,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "parameters": {
            "switch_window_seconds": switch_window_seconds,
            "drive_gear_values": sorted(DRIVE_GEAR_VALUES),
            "reverse_gear_values": sorted(REVERSE_GEAR_VALUES),
            "apa_type_values": sorted(APA_TYPE_VALUES),
            "apa_status_values": sorted(APA_STATUS_VALUES),
        },
        "scenes": [],
    }


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


def _build_event_details(
    timestamps: list[float],
    event_mask: list[bool],
    smoothed_acceleration: list[float],
    slope: list[float],
    deadband: float,
    min_oscillation_amplitude: float,
    min_sign_flips: int,
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

        amplitude = _max_window_amplitude(timestamps, smoothed_acceleration, start_index, end_index, amplitude_window_seconds)
        sign_flips = _count_sign_flips(slope_window, deadband)
        severity = _score_event_severity(amplitude, sign_flips, segment.duration, min_oscillation_amplitude, min_sign_flips)
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


def _count_sign_flips(values: list[float], deadband: float) -> int:
    flip_count = 0
    previous_sign = 0
    for value in values:
        sign = _signed_state(value, deadband)
        if sign == 0:
            continue
        if previous_sign != 0 and sign != previous_sign:
            flip_count += 1
        previous_sign = sign
    return flip_count


def _max_window_amplitude(
    timestamps: list[float],
    values: list[float],
    start_index: int,
    end_index: int,
    window_seconds: float,
) -> float:
    if start_index < 0 or end_index < start_index:
        return 0.0
    if window_seconds <= 0:
        chunk = values[start_index : end_index + 1]
        return max(chunk) - min(chunk) if chunk else 0.0

    max_amplitude = 0.0
    for left in range(start_index, end_index + 1):
        right = left
        while right + 1 <= end_index and timestamps[right + 1] - timestamps[left] <= window_seconds:
            right += 1
        chunk = values[left : right + 1]
        if not chunk:
            continue
        amplitude = max(chunk) - min(chunk)
        if amplitude > max_amplitude:
            max_amplitude = amplitude
    return max_amplitude


def _score_event_severity(
    amplitude: float,
    sign_flips: int,
    duration: float,
    min_oscillation_amplitude: float,
    min_sign_flips: int,
) -> float:
    if min_oscillation_amplitude <= 0:
        amplitude_ratio = 0.0
    else:
        amplitude_ratio = amplitude / min_oscillation_amplitude
    if min_sign_flips <= 0:
        flip_ratio = 0.0
    else:
        flip_ratio = sign_flips / float(min_sign_flips)
    duration_ratio = duration / 1.0
    return round(max(amplitude_ratio * 50.0 + flip_ratio * 30.0 + duration_ratio * 20.0, 0.0), 3)


def _detect_oscillation_candidates(
    timestamps: list[float],
    slope: list[float],
    valid_mask: list[bool],
    smoothed_acceleration: list[float],
    deadband: float,
    min_oscillation_amplitude: float,
    min_sign_flips: int,
    amplitude_window_seconds: float,
    min_event_duration_seconds: float,
    max_event_duration_seconds: float,
) -> list[bool]:
    signs = [_signed_state(value, deadband) for value in slope]
    event_mask = [False] * len(timestamps)
    run_start = None
    flip_count = 0
    previous_sign = 0
    for index, sign in enumerate(signs):
        is_valid = valid_mask[index]
        if not is_valid:
            _finalize_run(
                event_mask,
                timestamps,
                smoothed_acceleration,
                slope,
                deadband,
                run_start,
                index - 1,
                flip_count,
                min_sign_flips,
                min_oscillation_amplitude,
                amplitude_window_seconds,
                min_event_duration_seconds,
                max_event_duration_seconds,
            )
            run_start = None
            flip_count = 0
            previous_sign = 0
            continue
        if sign == 0:
            continue
        if run_start is None:
            run_start = index
            previous_sign = sign
            continue
        if sign != previous_sign:
            flip_count += 1
            previous_sign = sign

    _finalize_run(
        event_mask,
        timestamps,
        smoothed_acceleration,
        slope,
        deadband,
        run_start,
        len(timestamps) - 1,
        flip_count,
        min_sign_flips,
        min_oscillation_amplitude,
        amplitude_window_seconds,
        min_event_duration_seconds,
        max_event_duration_seconds,
    )
    return event_mask


def _finalize_run(
    event_mask: list[bool],
    timestamps: list[float],
    smoothed_acceleration: list[float],
    slope: list[float],
    deadband: float,
    run_start: int | None,
    run_end: int,
    flip_count: int,
    min_sign_flips: int,
    min_oscillation_amplitude: float,
    amplitude_window_seconds: float,
    min_event_duration_seconds: float,
    max_event_duration_seconds: float,
) -> None:
    if run_start is None or run_end < run_start:
        return
    duration = timestamps[run_end] - timestamps[run_start] if run_end > run_start else 0.0
    amplitude = _max_window_amplitude(timestamps, smoothed_acceleration, run_start, run_end, amplitude_window_seconds)
    if flip_count < min_sign_flips or amplitude < min_oscillation_amplitude or duration < min_event_duration_seconds:
        return
    if duration <= max_event_duration_seconds:
        for index in range(run_start, run_end + 1):
            event_mask[index] = True
        return

    best_window = _find_best_subwindow(
        timestamps=timestamps,
        smoothed_acceleration=smoothed_acceleration,
        slope=slope,
        run_start=run_start,
        run_end=run_end,
        deadband=deadband,
        min_oscillation_amplitude=min_oscillation_amplitude,
        min_sign_flips=min_sign_flips,
        amplitude_window_seconds=amplitude_window_seconds,
        min_event_duration_seconds=min_event_duration_seconds,
        max_event_duration_seconds=max_event_duration_seconds,
    )
    if best_window is None:
        return
    best_start, best_end = best_window
    for index in range(best_start, best_end + 1):
        event_mask[index] = True


def _find_best_subwindow(
    timestamps: list[float],
    smoothed_acceleration: list[float],
    slope: list[float],
    run_start: int,
    run_end: int,
    deadband: float,
    min_oscillation_amplitude: float,
    min_sign_flips: int,
    amplitude_window_seconds: float,
    min_event_duration_seconds: float,
    max_event_duration_seconds: float,
) -> tuple[int, int] | None:
    best_window: tuple[int, int] | None = None
    best_score = -1.0
    left = run_start
    step_seconds = max(min(min_event_duration_seconds / 2.0, 0.5), 0.1)

    while left <= run_end:
        right = left
        while right + 1 <= run_end and timestamps[right + 1] - timestamps[left] <= max_event_duration_seconds:
            right += 1
        duration = timestamps[right] - timestamps[left] if right > left else 0.0
        if duration >= min_event_duration_seconds:
            amplitude = _max_window_amplitude(timestamps, smoothed_acceleration, left, right, amplitude_window_seconds)
            sign_flips = _count_sign_flips(slope[left : right + 1], deadband)
            if amplitude >= min_oscillation_amplitude and sign_flips >= min_sign_flips:
                score = _score_event_severity(amplitude, sign_flips, duration, min_oscillation_amplitude, min_sign_flips)
                if score > best_score:
                    best_score = score
                    best_window = (left, right)

        next_start_time = timestamps[left] + step_seconds
        next_left = left + 1
        while next_left <= run_end and timestamps[next_left] < next_start_time:
            next_left += 1
        if next_left <= left:
            next_left = left + 1
        left = next_left

    return best_window


def _detect_long_cycle_events(
    timestamps: list[float],
    smoothed_acceleration: list[float],
    slope: list[float],
    valid_mask: list[bool],
    deadband: float,
    min_phase_seconds: float,
    max_phase_seconds: float,
    min_phase_amplitude: float,
    min_phase_count: int,
    max_gap_seconds: float,
    min_oscillation_amplitude: float,
    min_sign_flips: int,
) -> list[dict[str, float | int | str]]:
    phases = _collect_signed_phases(
        timestamps,
        smoothed_acceleration,
        slope,
        valid_mask,
        deadband,
        min_phase_seconds,
        max_phase_seconds,
        min_phase_amplitude,
    )
    if len(phases) < min_phase_count:
        return []

    events: list[dict[str, float | int | str]] = []
    index = 0
    while index <= len(phases) - min_phase_count:
        end_index = index
        if not _phases_can_chain(phases[index], phases[index + 1], max_gap_seconds):
            index += 1
            continue
        while end_index + 1 < len(phases):
            if end_index > index and not _phases_can_chain(phases[end_index], phases[end_index + 1], max_gap_seconds):
                break
            if end_index == index:
                end_index += 1
                continue
            end_index += 1
        sequence = phases[index : end_index + 1]
        if len(sequence) < min_phase_count:
            index += 1
            continue
        if not _is_alternating_sequence(sequence, max_gap_seconds):
            index += 1
            continue

        start_idx = int(sequence[0]["start_index"])
        end_idx = int(sequence[-1]["end_index"])
        acc_window = smoothed_acceleration[start_idx : end_idx + 1]
        amplitude = max(acc_window) - min(acc_window) if acc_window else 0.0
        sign_flips = len(sequence) - 1
        duration = timestamps[end_idx] - timestamps[start_idx] if end_idx > start_idx else 0.0
        severity = _score_event_severity(amplitude, sign_flips, duration, min_oscillation_amplitude, min_sign_flips)
        events.append(
            {
                "start_timestamp": timestamps[start_idx],
                "end_timestamp": timestamps[end_idx],
                "duration": duration,
                "sample_count": end_idx - start_idx + 1,
                "amplitude": amplitude,
                "sign_flips": sign_flips,
                "severity": severity,
                "detection_type": "long_cycle",
            }
        )
        index = end_index + 1
    return events


def _collect_signed_phases(
    timestamps: list[float],
    smoothed_acceleration: list[float],
    slope: list[float],
    valid_mask: list[bool],
    deadband: float,
    min_phase_seconds: float,
    max_phase_seconds: float,
    min_phase_amplitude: float,
) -> list[dict[str, float | int]]:
    phases: list[dict[str, float | int]] = []
    run_start: int | None = None
    run_sign = 0
    for index, value in enumerate(slope):
        sign = _signed_state(value, deadband)
        if not valid_mask[index] or sign == 0:
            _append_phase(phases, timestamps, smoothed_acceleration, run_start, index - 1, run_sign, min_phase_seconds, max_phase_seconds, min_phase_amplitude)
            run_start = None
            run_sign = 0
            continue
        if run_start is None:
            run_start = index
            run_sign = sign
            continue
        if sign != run_sign:
            _append_phase(phases, timestamps, smoothed_acceleration, run_start, index - 1, run_sign, min_phase_seconds, max_phase_seconds, min_phase_amplitude)
            run_start = index
            run_sign = sign
    _append_phase(phases, timestamps, smoothed_acceleration, run_start, len(slope) - 1, run_sign, min_phase_seconds, max_phase_seconds, min_phase_amplitude)
    return phases


def _append_phase(
    phases: list[dict[str, float | int]],
    timestamps: list[float],
    smoothed_acceleration: list[float],
    start_index: int | None,
    end_index: int,
    sign: int,
    min_phase_seconds: float,
    max_phase_seconds: float,
    min_phase_amplitude: float,
) -> None:
    if start_index is None or end_index < start_index or sign == 0:
        return
    duration = timestamps[end_index] - timestamps[start_index] if end_index > start_index else 0.0
    if duration < min_phase_seconds or duration > max_phase_seconds:
        return
    acc_window = smoothed_acceleration[start_index : end_index + 1]
    if not acc_window:
        return
    amplitude = max(acc_window) - min(acc_window)
    if amplitude < min_phase_amplitude:
        return
    phases.append(
        {
            "start_index": start_index,
            "end_index": end_index,
            "start_timestamp": timestamps[start_index],
            "end_timestamp": timestamps[end_index],
            "duration": duration,
            "amplitude": amplitude,
            "sign": sign,
        }
    )


def _phases_can_chain(
    left_phase: dict[str, float | int],
    right_phase: dict[str, float | int],
    max_gap_seconds: float,
) -> bool:
    if int(left_phase["sign"]) == int(right_phase["sign"]):
        return False
    gap = float(right_phase["start_timestamp"]) - float(left_phase["end_timestamp"])
    return gap <= max_gap_seconds


def _is_alternating_sequence(phases: list[dict[str, float | int]], max_gap_seconds: float) -> bool:
    if len(phases) < 2:
        return False
    for index in range(len(phases) - 1):
        if not _phases_can_chain(phases[index], phases[index + 1], max_gap_seconds):
            return False
    return True


def _detect_decel_jump_events(
    timestamps: list[float],
    smoothed_acceleration: list[float],
    valid_mask: list[bool],
    min_oscillation_amplitude: float,
    min_sign_flips: int,
    window_seconds: float,
    min_drop: float,
    rebound_window_seconds: float,
    min_rebound: float,
    min_jump_slope: float,
    min_rebound_slope: float,
) -> list[dict[str, float | int | str]]:
    events: list[dict[str, float | int | str]] = []
    index = 0
    while index < len(timestamps) - 1:
        if not valid_mask[index]:
            index += 1
            continue
        left_acc = smoothed_acceleration[index]
        best_end = -1
        best_jump = 0.0
        best_direction = 0
        probe = index + 1
        while probe < len(timestamps) and valid_mask[probe] and timestamps[probe] - timestamps[index] <= window_seconds:
            candidate_acc = smoothed_acceleration[probe]
            negative_jump = left_acc - candidate_acc
            if negative_jump >= min_drop and negative_jump > best_jump:
                best_jump = negative_jump
                best_end = probe
                best_direction = -1
            positive_jump = candidate_acc - left_acc
            if positive_jump >= min_drop and positive_jump > best_jump:
                best_jump = positive_jump
                best_end = probe
                best_direction = 1
            probe += 1
        if best_end < 0:
            index += 1
            continue

        rebound_peak = smoothed_acceleration[best_end]
        rebound_best_timestamp = timestamps[best_end]
        rebound_probe = best_end + 1
        rebound_deadline = timestamps[best_end] + rebound_window_seconds
        while rebound_probe < len(timestamps) and valid_mask[rebound_probe] and timestamps[rebound_probe] <= rebound_deadline:
            current_acc = smoothed_acceleration[rebound_probe]
            if best_direction < 0:
                rebound_peak = max(rebound_peak, current_acc)
                if rebound_peak == current_acc:
                    rebound_best_timestamp = timestamps[rebound_probe]
            else:
                rebound_peak = min(rebound_peak, current_acc)
                if rebound_peak == current_acc:
                    rebound_best_timestamp = timestamps[rebound_probe]
            rebound_probe += 1
        rebound_amount = (
            rebound_peak - smoothed_acceleration[best_end]
            if best_direction < 0
            else smoothed_acceleration[best_end] - rebound_peak
        )
        if rebound_amount < min_rebound:
            index += 1
            continue
        jump_slope = best_jump / max(timestamps[best_end] - timestamps[index], 1e-6)
        rebound_slope = rebound_amount / max(rebound_best_timestamp - timestamps[best_end], 1e-6)
        if jump_slope < min_jump_slope or rebound_slope < min_rebound_slope:
            index += 1
            continue

        duration = timestamps[best_end] - timestamps[index]
        severity = _score_event_severity(best_jump, max(min_sign_flips, 1), max(duration, 0.2), min_oscillation_amplitude, min_sign_flips)
        events.append(
            {
                "start_timestamp": timestamps[index],
                "end_timestamp": timestamps[best_end],
                "duration": duration,
                "sample_count": best_end - index + 1,
                "amplitude": best_jump,
                "sign_flips": max(min_sign_flips, 1),
                "severity": severity,
                "detection_type": "decel_jump",
            }
        )
        index = best_end + 1
    return events


def _merge_event_details(
    events: list[dict[str, float | int | str]],
    timestamps: list[float],
    smoothed_acceleration: list[float],
    slope: list[float],
    deadband: float,
    min_oscillation_amplitude: float,
    min_sign_flips: int,
    merge_gap_seconds: float,
) -> list[dict[str, float | int | str]]:
    if not events:
        return []
    sorted_events = sorted(events, key=lambda item: (float(item["start_timestamp"]), float(item["end_timestamp"])))
    merged: list[dict[str, float | int | str]] = []
    for event in sorted_events:
        if not merged:
            merged.append(dict(event))
            continue
        last = merged[-1]
        if float(event["start_timestamp"]) > float(last["end_timestamp"]) + merge_gap_seconds:
            merged.append(dict(event))
            continue
        merged[-1] = _combine_two_events(
            last,
            event,
            timestamps,
            smoothed_acceleration,
            slope,
            deadband,
            min_oscillation_amplitude,
            min_sign_flips,
        )
    return merged


def _combine_two_events(
    left_event: dict[str, float | int | str],
    right_event: dict[str, float | int | str],
    timestamps: list[float],
    smoothed_acceleration: list[float],
    slope: list[float],
    deadband: float,
    min_oscillation_amplitude: float,
    min_sign_flips: int,
) -> dict[str, float | int | str]:
    start_timestamp = min(float(left_event["start_timestamp"]), float(right_event["start_timestamp"]))
    end_timestamp = max(float(left_event["end_timestamp"]), float(right_event["end_timestamp"]))
    start_index = _find_timestamp_index(timestamps, start_timestamp)
    end_index = _find_timestamp_index(timestamps, end_timestamp)
    if start_index < 0 or end_index < start_index:
        return dict(left_event)

    acc_window = smoothed_acceleration[start_index : end_index + 1]
    slope_window = slope[start_index : end_index + 1]
    amplitude = max(acc_window) - min(acc_window) if acc_window else max(float(left_event["amplitude"]), float(right_event["amplitude"]))
    sign_flips = max(_count_sign_flips(slope_window, deadband), int(left_event["sign_flips"]), int(right_event["sign_flips"]))
    duration = end_timestamp - start_timestamp if end_index > start_index else 0.0
    severity = max(
        float(left_event["severity"]),
        float(right_event["severity"]),
        _score_event_severity(amplitude, sign_flips, max(duration, 0.2), min_oscillation_amplitude, min_sign_flips),
    )
    detection_types = sorted(
        {
            item.strip()
            for event in (left_event, right_event)
            for item in str(event.get("detection_type", "oscillation")).split("+")
            if item.strip()
        }
    )
    return {
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "duration": duration,
        "sample_count": end_index - start_index + 1,
        "amplitude": amplitude,
        "sign_flips": sign_flips,
        "severity": round(severity, 3),
        "detection_type": "+".join(detection_types) if detection_types else "oscillation",
    }


def _filter_events_by_reversal_pattern(
    events: list[dict[str, float | int | str]],
    timestamps: list[float],
    smoothed_acceleration: list[float],
    jump_window_seconds: float,
    min_jump_amplitude: float,
    rebound_window_seconds: float,
    min_rebound: float,
    min_jump_slope: float,
    min_rebound_slope: float,
) -> list[dict[str, float | int | str]]:
    filtered: list[dict[str, float | int | str]] = []
    for event in events:
        start_index = _find_timestamp_index(timestamps, float(event["start_timestamp"]))
        end_index = _find_timestamp_index(timestamps, float(event["end_timestamp"]))
        if start_index < 0 or end_index < start_index:
            continue
        if _has_strong_reversal_pattern(
            timestamps=timestamps,
            smoothed_acceleration=smoothed_acceleration,
            start_index=start_index,
            end_index=end_index,
            jump_window_seconds=jump_window_seconds,
            min_jump_amplitude=min_jump_amplitude,
            rebound_window_seconds=rebound_window_seconds,
            min_rebound=min_rebound,
            min_jump_slope=min_jump_slope,
            min_rebound_slope=min_rebound_slope,
        ):
            filtered.append(event)
    return filtered


def _has_strong_reversal_pattern(
    timestamps: list[float],
    smoothed_acceleration: list[float],
    start_index: int,
    end_index: int,
    jump_window_seconds: float,
    min_jump_amplitude: float,
    rebound_window_seconds: float,
    min_rebound: float,
    min_jump_slope: float,
    min_rebound_slope: float,
) -> bool:
    for left in range(start_index, end_index):
        left_acc = smoothed_acceleration[left]
        for peak_index in range(left + 1, end_index + 1):
            jump_duration = timestamps[peak_index] - timestamps[left]
            if jump_duration <= 0:
                continue
            if jump_duration > jump_window_seconds:
                break
            peak_acc = smoothed_acceleration[peak_index]
            jump_value = peak_acc - left_acc
            if abs(jump_value) < min_jump_amplitude:
                continue
            jump_slope = abs(jump_value) / jump_duration
            if jump_slope < min_jump_slope:
                continue

            direction = 1 if jump_value > 0 else -1
            best_rebound = 0.0
            best_rebound_slope = 0.0
            for rebound_index in range(peak_index + 1, end_index + 1):
                rebound_duration = timestamps[rebound_index] - timestamps[peak_index]
                if rebound_duration <= 0:
                    continue
                if rebound_duration > rebound_window_seconds:
                    break
                rebound_value = (
                    peak_acc - smoothed_acceleration[rebound_index]
                    if direction > 0
                    else smoothed_acceleration[rebound_index] - peak_acc
                )
                if rebound_value <= 0:
                    continue
                rebound_slope = rebound_value / rebound_duration
                if rebound_value > best_rebound:
                    best_rebound = rebound_value
                    best_rebound_slope = rebound_slope
            if best_rebound >= min_rebound and best_rebound_slope >= min_rebound_slope:
                return True
    return False


def _events_to_mask(timestamps: list[float], events: list[dict[str, float | int | str]]) -> list[bool]:
    mask = [False] * len(timestamps)
    for event in events:
        start_index = _find_timestamp_index(timestamps, float(event["start_timestamp"]))
        end_index = _find_timestamp_index(timestamps, float(event["end_timestamp"]))
        if start_index < 0 or end_index < start_index:
            continue
        for index in range(start_index, end_index + 1):
            mask[index] = True
    return mask


def _merge_short_gaps(timestamps: list[float], mask: list[bool], merge_gap_seconds: float) -> list[bool]:
    if not mask:
        return []
    merged = mask[:]
    index = 0
    while index < len(merged):
        if merged[index]:
            index += 1
            continue
        gap_start = index
        while index < len(merged) and not merged[index]:
            index += 1
        gap_end = index - 1
        left_index = gap_start - 1
        right_index = index
        if left_index < 0 or right_index >= len(merged):
            continue
        if not merged[left_index] or not merged[right_index]:
            continue
        if timestamps[right_index] - timestamps[left_index] <= merge_gap_seconds:
            for fill_index in range(gap_start, gap_end + 1):
                merged[fill_index] = True
    return merged


def _signed_state(value: float, deadband: float) -> int:
    if value > deadband:
        return 1
    if value < -deadband:
        return -1
    return 0


def _empty_result(
    start_timestamp: float,
    end_timestamp: float,
    valid_sample_count: int,
    candidate_sample_count: int,
    smoothing_window: int,
    deadband: float,
    min_oscillation_amplitude: float,
    min_sign_flips: int,
    amplitude_window_seconds: float,
    min_event_duration_seconds: float,
    max_event_duration_seconds: float,
    merge_gap_seconds: float,
    decel_jump_rebound_window_seconds: float = DEFAULT_DECEL_JUMP_REBOUND_WINDOW_SECONDS,
    decel_jump_min_rebound: float = DEFAULT_DECEL_JUMP_MIN_REBOUND,
    decel_jump_min_jump_slope: float = DEFAULT_DECEL_JUMP_MIN_JUMP_SLOPE,
    decel_jump_min_rebound_slope: float = DEFAULT_DECEL_JUMP_MIN_REBOUND_SLOPE,
) -> dict[str, object]:
    return {
        "metric_name": "apa_gear_switch_jerkiness",
        "method": "oscillation_based_or_extended",
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
            "decel_jump_rebound_window_seconds": decel_jump_rebound_window_seconds,
            "decel_jump_min_rebound": decel_jump_min_rebound,
            "decel_jump_min_jump_slope": decel_jump_min_jump_slope,
            "decel_jump_min_rebound_slope": decel_jump_min_rebound_slope,
        },
        "events": [],
    }


def _matches_enum(value: object, numeric_values: set[int], text_values: set[str]) -> bool:
    normalized_text_values = {item.lower() for item in text_values}
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    if isinstance(value, str):
        return value.strip().lower() in normalized_text_values
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bool):
        return int(value) in numeric_values
    if isinstance(value, (int, float)):
        return int(value) in numeric_values
    return str(value).strip().lower() in normalized_text_values


def _matches_enum(value: object, numeric_values: set[int], text_values: set[str]) -> bool:
    normalized_text_values = {item.lower() for item in text_values}
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    if isinstance(value, str):
        return value.strip().lower() in normalized_text_values
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bool):
        return int(value) in numeric_values
    if isinstance(value, (int, float)):
        return int(value) in numeric_values
    return str(value).strip().lower() in normalized_text_values
