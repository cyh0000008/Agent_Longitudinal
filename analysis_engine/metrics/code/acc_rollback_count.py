from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from signal_lib import FolderSession, mask_to_segments


METRIC_CATEGORY = "MPI"
WHEEL_DIR_LF_SIGNAL = "Wheel_Distance_Direction_Info_High_Frequency.IWhlRotDirStatLFHigFreq"
WHEEL_DIR_RF_SIGNAL = "Wheel_Distance_Direction_Info_High_Frequency.IWhlRotDirStatRFHigFreq"
WHEEL_DIR_LR_SIGNAL = "Wheel_Distance_Direction_Info_High_Frequency.IWhlRotDirStatLRHigFreq"
WHEEL_DIR_RR_SIGNAL = "Wheel_Distance_Direction_Info_High_Frequency.IWhlRotDirStatRRHigFreq"
VEHICLE_SPEED_SIGNAL = "PPEI_Vehicle_Speed_and_Distance_208_M2.IVehSpdAvgNDrvn"
ACC_ACTIVE_SIGNAL = "Adaptive_Cruise_Disp_Stat.IACCAct376"
AUTO_BRAKE_TYPE_SIGNAL = "Adaptive_Cruise_Command_Ext_1A3_M.IACCBSCE_AutBrkTp"
WHEEL_SPEED_LF_SIGNAL = "PPEI_Chassis_General_Data_1_031_M.IWhlAngVelLFrtAuth"
WHEEL_SPEED_RF_SIGNAL = "PPEI_Chassis_General_Data_1_031_M.IWhlAngVelRFrtAuth"
WHEEL_SPEED_LR_SIGNAL = "PPEI_Chassis_General_Data_1_031_M.IWhlAngVelLRrAuth"
WHEEL_SPEED_RR_SIGNAL = "PPEI_Chassis_General_Data_1_031_M.IWhlAngVelRRrAuth"
GEAR_SIGNAL = "PPEI_Trans_General_Status_2_ECP_H1_0D7_M.ITransEstGear_ECP_H1"

DEFAULT_MAX_VEHICLE_SPEED_KPH = 0.5
DEFAULT_MIN_SCENE_DURATION_SECONDS = 0.20
DEFAULT_MIN_WHEEL_SPEED_ABS = 0.01

REVERSE_DIRECTION_VALUES = {4}
ACC_ACTIVE_VALUES = {1}
HOLD_BRAKE_TYPE_VALUES = {5}
FORWARD_GEAR_VALUES = {12}

REQUIRED_SIGNALS = [
    WHEEL_DIR_LF_SIGNAL,
    WHEEL_DIR_RF_SIGNAL,
    WHEEL_DIR_LR_SIGNAL,
    WHEEL_DIR_RR_SIGNAL,
    VEHICLE_SPEED_SIGNAL,
    ACC_ACTIVE_SIGNAL,
    AUTO_BRAKE_TYPE_SIGNAL,
    WHEEL_SPEED_LF_SIGNAL,
    WHEEL_SPEED_RF_SIGNAL,
    WHEEL_SPEED_LR_SIGNAL,
    WHEEL_SPEED_RR_SIGNAL,
    GEAR_SIGNAL,
]

SIGNAL_KEYS = {
    "wheel_dir_lf": REQUIRED_SIGNALS[0],
    "wheel_dir_rf": REQUIRED_SIGNALS[1],
    "wheel_dir_lr": REQUIRED_SIGNALS[2],
    "wheel_dir_rr": REQUIRED_SIGNALS[3],
    "vehicle_speed": REQUIRED_SIGNALS[4],
    "acc_active": REQUIRED_SIGNALS[5],
    "auto_brake_type": REQUIRED_SIGNALS[6],
    "wheel_speed_lf": REQUIRED_SIGNALS[7],
    "wheel_speed_rf": REQUIRED_SIGNALS[8],
    "wheel_speed_lr": REQUIRED_SIGNALS[9],
    "wheel_speed_rr": REQUIRED_SIGNALS[10],
    "gear": REQUIRED_SIGNALS[11],
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

    result = analyze_acc_rollback_count(session)
    all_case_count = int(result["all_case_count"])
    bad_case_count = int(result["bad_case_count"])
    severity_score = float(result["severity_score"])

    stats["All Case Count"] = all_case_count
    stats["Bad Case"] = bad_case_count
    stats["Severity Score"] = severity_score

    result_lines.append(
        f"[ACC Rollback Count] file={Path(session.primary_h5_file).name}, all_case_count={all_case_count}, "
        f"bad_case_count={bad_case_count}, severity_score={severity_score:.3f}"
    )
    for scene in result["scenes"]:
        result_lines.append(
            f"[Scene start={scene['start_timestamp']:.3f}, end={scene['end_timestamp']:.3f}, "
            f"duration={scene['duration']:.3f}, vehicle_speed={scene['max_vehicle_speed']:.3f}, "
            f"wheel_speed_mean={scene['mean_abs_wheel_speed']:.3f}, severity={scene['severity']:.3f}]"
        )

    total_time = time.time() - start_time
    result_lines.insert(0, f"【运算总耗时】: {total_time:.4f} 秒")
    return bad_case_count > 0, result_lines, stats


def analyze_acc_rollback_count(
    session_or_h5_path: FolderSession | str | Path,
    max_vehicle_speed_kph: float = DEFAULT_MAX_VEHICLE_SPEED_KPH,
    min_scene_duration_seconds: float = DEFAULT_MIN_SCENE_DURATION_SECONDS,
    min_wheel_speed_abs: float = DEFAULT_MIN_WHEEL_SPEED_ABS,
) -> dict[str, object]:
    session = session_or_h5_path if isinstance(session_or_h5_path, FolderSession) else FolderSession(session_or_h5_path)
    start_timestamp, end_timestamp = session.common_time_range(REQUIRED_SIGNALS)

    axis, wheel_speed_lf_raw = session.get_signal_slice(SIGNAL_KEYS["wheel_speed_lf"], start_timestamp, end_timestamp)
    if len(axis) < 2:
        return _empty_result(start_timestamp, end_timestamp)

    wheel_dir_lf = session.sample_signal_to_axis(SIGNAL_KEYS["wheel_dir_lf"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp)
    wheel_dir_rf = session.sample_signal_to_axis(SIGNAL_KEYS["wheel_dir_rf"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp)
    wheel_dir_lr = session.sample_signal_to_axis(SIGNAL_KEYS["wheel_dir_lr"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp)
    wheel_dir_rr = session.sample_signal_to_axis(SIGNAL_KEYS["wheel_dir_rr"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp)
    vehicle_speed = [float(value) for value in session.sample_signal_to_axis(SIGNAL_KEYS["vehicle_speed"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp)]
    acc_active = session.sample_signal_to_axis(SIGNAL_KEYS["acc_active"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp)
    auto_brake_type = session.sample_signal_to_axis(SIGNAL_KEYS["auto_brake_type"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp)
    gear = session.sample_signal_to_axis(SIGNAL_KEYS["gear"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp)
    wheel_speed_lf = [float(value) for value in wheel_speed_lf_raw]
    wheel_speed_rf = [float(value) for value in session.sample_signal_to_axis(SIGNAL_KEYS["wheel_speed_rf"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp)]
    wheel_speed_lr = [float(value) for value in session.sample_signal_to_axis(SIGNAL_KEYS["wheel_speed_lr"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp)]
    wheel_speed_rr = [float(value) for value in session.sample_signal_to_axis(SIGNAL_KEYS["wheel_speed_rr"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp)]

    rollback_mask = []
    for index in range(len(axis)):
        is_reverse = (
            _matches_enum(wheel_dir_lf[index], REVERSE_DIRECTION_VALUES, {"reverse"})
            and _matches_enum(wheel_dir_rf[index], REVERSE_DIRECTION_VALUES, {"reverse"})
            and _matches_enum(wheel_dir_lr[index], REVERSE_DIRECTION_VALUES, {"reverse"})
            and _matches_enum(wheel_dir_rr[index], REVERSE_DIRECTION_VALUES, {"reverse"})
        )
        is_acc_active = _matches_enum(acc_active[index], ACC_ACTIVE_VALUES, {"1", "true", "active"})
        is_hold = _matches_enum(auto_brake_type[index], HOLD_BRAKE_TYPE_VALUES, {"hold"})
        is_forward_gear = _matches_enum(gear[index], FORWARD_GEAR_VALUES, {"cvt forward gear", "forward gear", "drive"})
        low_vehicle_speed = float(vehicle_speed[index]) < max_vehicle_speed_kph
        wheel_speed_non_zero = (
            abs(wheel_speed_lf[index]) > min_wheel_speed_abs
            and abs(wheel_speed_rf[index]) > min_wheel_speed_abs
            and abs(wheel_speed_lr[index]) > min_wheel_speed_abs
            and abs(wheel_speed_rr[index]) > min_wheel_speed_abs
        )
        rollback_mask.append(is_reverse and low_vehicle_speed and is_acc_active and is_hold and wheel_speed_non_zero and is_forward_gear)

    scenes = []
    severity_score = 0.0
    for segment in mask_to_segments(axis, rollback_mask):
        if segment.duration < min_scene_duration_seconds:
            continue
        start_index = segment.start_index
        end_index = segment.end_index
        vehicle_speed_window = vehicle_speed[start_index : end_index + 1]
        wheel_speed_window = [
            (abs(wheel_speed_lf[i]) + abs(wheel_speed_rf[i]) + abs(wheel_speed_lr[i]) + abs(wheel_speed_rr[i])) / 4.0
            for i in range(start_index, end_index + 1)
        ]
        max_vehicle_speed = max(vehicle_speed_window) if vehicle_speed_window else 0.0
        mean_abs_wheel_speed = sum(wheel_speed_window) / len(wheel_speed_window) if wheel_speed_window else 0.0
        severity = mean_abs_wheel_speed
        severity_score = max(severity_score, severity)
        scenes.append(
            {
                "start_timestamp": segment.start_timestamp,
                "end_timestamp": segment.end_timestamp,
                "duration": segment.duration,
                "sample_count": segment.sample_count,
                "max_vehicle_speed": max_vehicle_speed,
                "mean_abs_wheel_speed": mean_abs_wheel_speed,
                "severity": severity,
            }
        )

    return {
        "metric_name": "acc_rollback_count",
        "all_case_count": len(scenes),
        "bad_case_count": len(scenes),
        "severity_score": round(severity_score, 3),
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "parameters": {
            "max_vehicle_speed_kph": max_vehicle_speed_kph,
            "min_scene_duration_seconds": min_scene_duration_seconds,
            "min_wheel_speed_abs": min_wheel_speed_abs,
            "reverse_direction_values": sorted(REVERSE_DIRECTION_VALUES),
            "acc_active_values": sorted(ACC_ACTIVE_VALUES),
            "hold_brake_type_values": sorted(HOLD_BRAKE_TYPE_VALUES),
            "forward_gear_values": sorted(FORWARD_GEAR_VALUES),
        },
        "scenes": scenes,
    }


def _matches_enum(value: object, numeric_values: set[int], text_values: set[str]) -> bool:
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    if isinstance(value, str):
        return value.strip().lower() in {item.lower() for item in text_values}
    if hasattr(value, 'item'):
        value = value.item()
    if isinstance(value, bool):
        return int(value) in numeric_values
    if isinstance(value, (int, float)):
        return int(value) in numeric_values
    text = str(value).strip().lower()
    return text in {item.lower() for item in text_values}


def _empty_result(start_timestamp: float, end_timestamp: float) -> dict[str, object]:
    return {
        "metric_name": "acc_rollback_count",
        "all_case_count": 0,
        "bad_case_count": 0,
        "severity_score": 0.0,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "scenes": [],
        "parameters": {
            "max_vehicle_speed_kph": DEFAULT_MAX_VEHICLE_SPEED_KPH,
            "min_scene_duration_seconds": DEFAULT_MIN_SCENE_DURATION_SECONDS,
            "min_wheel_speed_abs": DEFAULT_MIN_WHEEL_SPEED_ABS,
            "reverse_direction_values": sorted(REVERSE_DIRECTION_VALUES),
            "acc_active_values": sorted(ACC_ACTIVE_VALUES),
            "hold_brake_type_values": sorted(HOLD_BRAKE_TYPE_VALUES),
            "forward_gear_values": sorted(FORWARD_GEAR_VALUES),
        },
    }
