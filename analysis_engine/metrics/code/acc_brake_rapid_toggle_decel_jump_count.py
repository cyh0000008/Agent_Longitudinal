from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from signal_lib import FolderSession


METRIC_CATEGORY = "MPI"
ACCELERATION_SIGNAL = "IMU_Yaw_Long_Acc_037_M2.IIMULonAccSec"
AUTO_BRAKE_TYPE_SIGNAL = "Adaptive_Cruise_Command_Ext_1A3_M.IACCBSCE_AutBrkTp"
ACC_ACTIVE_SIGNAL = "Adaptive_Cruise_Disp_Stat.IACCAct376"
PEDAL_OVERRIDE_SIGNAL = "PPEI_Propulsion_General_Data_1_0C2_M.IAccPdlOvrrdAtv"

DEFAULT_MID_STATE_MAX_DURATION_SECONDS = 0.10
DEFAULT_DECEL_WINDOW_SECONDS = 1.00
DEFAULT_DECEL_RANGE_THRESHOLD = 2.0

STATE_1_VALUES = {1}
STATE_2_VALUES = {2}

REQUIRED_SIGNALS = [
    ACCELERATION_SIGNAL,
    AUTO_BRAKE_TYPE_SIGNAL,
    ACC_ACTIVE_SIGNAL,
    PEDAL_OVERRIDE_SIGNAL,
]

SIGNAL_KEYS = {
    "acceleration": REQUIRED_SIGNALS[0],
    "auto_brake_type": REQUIRED_SIGNALS[1],
    "acc_active": REQUIRED_SIGNALS[2],
    "pedal_override": REQUIRED_SIGNALS[3],
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

    result = count_acc_brake_rapid_toggle_decel_jump_events(session)
    event_count = int(result["event_count"])
    max_severity = float(result["max_event_severity"])

    stats["All Case Count"] = event_count
    stats["Bad Case"] = event_count
    stats["Severity Score"] = max_severity

    result_lines.append(
        f"[ACC Brake Rapid Toggle Decel Jump] file={Path(session.primary_h5_file).name}, event_count={event_count}, "
        f"max_severity={max_severity:.3f}"
    )
    for event in result["events"]:
        result_lines.append(
            f"[Event state1_start={event['state1_start_timestamp']:.3f}, state2_return={event['state2_return_timestamp']:.3f}, "
            f"state1_duration={event['state1_duration']:.3f}, decel_window_end={event['decel_window_end_timestamp']:.3f}, "
            f"decel_range={event['decel_range']:.3f}, min_acceleration={event['min_acceleration']:.3f}, "
            f"max_acceleration={event['max_acceleration']:.3f}, severity={event['severity']:.3f}]"
        )

    result_lines.insert(0, f"【运算总耗时】: {time.time() - start_time:.4f} 秒")
    return event_count > 0, result_lines, stats


def count_acc_brake_rapid_toggle_decel_jump_events(
    session_or_h5_path: FolderSession | str | Path,
    mid_state_max_duration_seconds: float = DEFAULT_MID_STATE_MAX_DURATION_SECONDS,
    decel_window_seconds: float = DEFAULT_DECEL_WINDOW_SECONDS,
    decel_range_threshold: float = DEFAULT_DECEL_RANGE_THRESHOLD,
) -> dict[str, object]:
    session = session_or_h5_path if isinstance(session_or_h5_path, FolderSession) else FolderSession(session_or_h5_path)
    start_timestamp, end_timestamp = session.common_time_range(REQUIRED_SIGNALS)

    accel_axis, accel_raw = session.get_signal_slice(SIGNAL_KEYS["acceleration"], start_timestamp, end_timestamp)
    brake_axis, brake_raw = session.get_signal_slice(SIGNAL_KEYS["auto_brake_type"], start_timestamp, end_timestamp)
    if len(accel_axis) < 2 or len(brake_axis) < 3:
        return _empty_result(start_timestamp, end_timestamp, mid_state_max_duration_seconds, decel_window_seconds, decel_range_threshold)

    accel_values = [float(value) for value in accel_raw]
    brake_values = [_normalize_enum(value) for value in brake_raw]
    acc_active_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["acc_active"], brake_axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    pedal_override_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["pedal_override"], brake_axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )

    events: list[dict[str, float | int]] = []
    index = 1
    while index < len(brake_axis) - 1:
        prev_value = brake_values[index - 1]
        curr_value = brake_values[index]
        if prev_value not in STATE_2_VALUES or curr_value not in STATE_1_VALUES:
            index += 1
            continue

        state1_start_timestamp = float(brake_axis[index])
        state1_end_index = index
        while state1_end_index + 1 < len(brake_axis) and brake_values[state1_end_index + 1] in STATE_1_VALUES:
            state1_end_index += 1

        return_index = state1_end_index + 1
        if return_index >= len(brake_axis):
            break
        if brake_values[return_index] not in STATE_2_VALUES:
            index = state1_end_index + 1
            continue

        state2_return_timestamp = float(brake_axis[return_index])
        state1_duration = state2_return_timestamp - state1_start_timestamp
        if state1_duration > mid_state_max_duration_seconds:
            index = return_index
            continue

        control_window_ok = all(
            _as_bool(acc_active_values[i]) and (not _as_bool(pedal_override_values[i]))
            for i in range(index, return_index + 1)
        )
        if not control_window_ok:
            index = return_index
            continue

        decel_window_end_target = state2_return_timestamp + decel_window_seconds
        accel_window_values: list[float] = []
        for timestamp, accel in zip(accel_axis, accel_values):
            current_timestamp = float(timestamp)
            if current_timestamp < state2_return_timestamp or current_timestamp > decel_window_end_target:
                continue
            acc_on = session.sample_signal_to_axis(
                SIGNAL_KEYS["acc_active"], [current_timestamp], method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
            )[0]
            override = session.sample_signal_to_axis(
                SIGNAL_KEYS["pedal_override"], [current_timestamp], method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
            )[0]
            if _as_bool(acc_on) and (not _as_bool(override)):
                accel_window_values.append(accel)
        if len(accel_window_values) < 2:
            index = return_index
            continue

        min_acceleration = min(accel_window_values)
        max_acceleration = max(accel_window_values)
        decel_range = max_acceleration - min_acceleration
        if decel_range > decel_range_threshold:
            severity = round(decel_range - decel_range_threshold, 3)
            events.append(
                {
                    "state1_start_timestamp": state1_start_timestamp,
                    "state2_return_timestamp": state2_return_timestamp,
                    "state1_duration": state1_duration,
                    "decel_window_end_timestamp": min(decel_window_end_target, end_timestamp),
                    "decel_range": decel_range,
                    "min_acceleration": min_acceleration,
                    "max_acceleration": max_acceleration,
                    "severity": severity,
                }
            )

        index = return_index

    max_event_severity = max((float(event["severity"]) for event in events), default=0.0)
    return {
        "metric_name": "acc_brake_rapid_toggle_decel_jump_count",
        "method": "acc_on_and_no_override__auto_brake_type_2_to_1_to_2_then_1s_decel_range",
        "event_count": len(events),
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": max_event_severity,
        "parameters": {
            "mid_state_max_duration_seconds": mid_state_max_duration_seconds,
            "decel_window_seconds": decel_window_seconds,
            "decel_range_threshold": decel_range_threshold,
            "state_1_values": sorted(STATE_1_VALUES),
            "state_2_values": sorted(STATE_2_VALUES),
        },
        "events": events,
    }


def _normalize_enum(value: object) -> int | str:
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return str(value).strip().lower()


def _empty_result(
    start_timestamp: float,
    end_timestamp: float,
    mid_state_max_duration_seconds: float,
    decel_window_seconds: float,
    decel_range_threshold: float,
) -> dict[str, object]:
    return {
        "metric_name": "acc_brake_rapid_toggle_decel_jump_count",
        "method": "acc_on_and_no_override__auto_brake_type_2_to_1_to_2_then_1s_decel_range",
        "event_count": 0,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": 0.0,
        "parameters": {
            "mid_state_max_duration_seconds": mid_state_max_duration_seconds,
            "decel_window_seconds": decel_window_seconds,
            "decel_range_threshold": decel_range_threshold,
            "state_1_values": sorted(STATE_1_VALUES),
            "state_2_values": sorted(STATE_2_VALUES),
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
