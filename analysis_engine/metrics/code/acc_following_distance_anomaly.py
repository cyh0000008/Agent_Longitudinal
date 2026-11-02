from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Iterable

from signal_lib import FolderSession, mask_to_segments


METRIC_CATEGORY = "MPI"

SPEED_SIGNAL = "PPEI_Vehicle_Speed_and_Distance_209_M1.IVehSpdAvgDrvn_1"
ACC_ACTIVE_SIGNAL = "Adaptive_Cruise_Disp_Stat.IACCAct376"
PEDAL_OVERRIDE_SIGNAL = "PPEI_Propulsion_General_Data_1_0C2_M.IAccPdlOvrrdAtv"

DEFAULT_MIN_EVENT_SECONDS = 1.0
DEFAULT_DISTANCE_MARGIN_M = 3.0
DEFAULT_DISTANCE_MARGIN_RATIO = 0.15
DEFAULT_FLUCTUATION_WINDOW_SECONDS = 3.0
DEFAULT_FLUCTUATION_MIN_RANGE_M = 6.0
DEFAULT_FLUCTUATION_RATIO = 0.20
DEFAULT_CAUSE_PADDING_SECONDS = 1.0

REQUIRED_SIGNALS = [
    SPEED_SIGNAL,
    ACC_ACTIVE_SIGNAL,
    PEDAL_OVERRIDE_SIGNAL,
]

SIGNAL_CANDIDATES = {
    "ego_speed": (
        "PPEI_Vehicle_Speed_and_Distance_209_M1.IVehSpdAvgDrvn_1",
        "VehSpdAvgDrvn_1",
        "IVehSpdAvgDrvn_1",
    ),
    "acc_active": (
        "Adaptive_Cruise_Disp_Stat.IACCAct376",
        "IACCAct376",
        "ACCAct376",
    ),
    "pedal_override": (
        "PPEI_Propulsion_General_Data_1_0C2_M.IAccPdlOvrrdAtv",
        "IAccPdlOvrrdAtv",
        "AccPdlOvrrdAtv",
    ),
    "headway_setting": ("Adaptive_Cruise_Disp_Stat.IACCHdwyStg", "IACCHdwyStg", "ACCHdwyStg"),
    "lead_target_id": ("FLT1_Global_ID", "LeadTargetId", "ACC_Target_Info.LeadTargetId"),
    "lead_target_distance": ("FLT1_Long_Range", "LeadTargetDistance", "ACC_Target_Info.LeadTargetDistance"),
    "lead_target_velocity": ("FLT1_Long_Velocity", "LeadTargetVelocity", "ACC_Target_Info.LeadTargetVelocity"),
    "lead_target_accel": ("FLT1_Long_Accel", "LeadTargetAccel", "ACC_Target_Info.LeadTargetAccel"),
    "lead_target_ttc": ("LeadTargetTTC", "ACC_Target_Info.LeadTargetTTC"),
    "acc_axle_torque_request": (
        "PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M.IACCATC_AxlTrqRq",
        "PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M.ACCATC_AxlTrqRq",
        "IACCATC_AxlTrqRq",
        "ACCATC_AxlTrqRq",
    ),
    "acc_brake_accel_request": (
        "ADAS_Command_Ext1.IACCBSCE_ACCAccl1",
        "ADAS_Command_Ext1.ACCBSCE_ACCAccl1",
        "IACCBSCE_ACCAccl1",
        "ACCBSCE_ACCAccl1",
    ),
    "acc_brake_torque_request": (
        "ADAS_Command_Ext1.IACCBSCE_ACCTrq1",
        "ADAS_Command_Ext1.ACCBSCE_ACCTrq1",
        "IACCBSCE_ACCTrq1",
        "ACCBSCE_ACCTrq1",
    ),
    "propulsion_response": (
        "PPEI_Propulsion_General_Data_1_0C2_M.IACCATCS_RqStat",
        "PPEI_Propulsion_General_Data_1_0C2_M.ACCATCS_RqStat",
        "IACCATCS_RqStat",
        "ACCATCS_RqStat",
    ),
    "brake_response": (
        "PPEI_Chassis_General_Data_3_0AC_M.IACCBrkngAct",
        "PPEI_Chassis_General_Data_3_0AC_M.ACCBrkngAct",
        "Automatic_Braking_Status_044_M.IBrkSysAutBrkStat",
        "Automatic_Braking_Status_044_M.BrkSysAutBrkStat",
        "IACCBrkngAct",
        "ACCBrkngAct",
        "IBrkSysAutBrkStat",
        "BrkSysAutBrkStat",
    ),
}
ESSENTIAL_SIGNAL_KEYS = ("ego_speed", "acc_active", "pedal_override", "headway_setting", "lead_target_distance")
SIGNAL_DISPLAY_NAMES = {
    "ego_speed": "实车车速",
    "acc_active": "ACC 激活状态",
    "pedal_override": "驾驶员踏板接管",
    "headway_setting": "跟车时距设置 ACCHdwyStg",
    "lead_target_distance": "前车距离",
}

OPTIONAL_SIGNALS = sorted({item for values in SIGNAL_CANDIDATES.values() for item in values})
PLOT_SIGNALS = [
    SPEED_SIGNAL,
    ACC_ACTIVE_SIGNAL,
    PEDAL_OVERRIDE_SIGNAL,
    SIGNAL_CANDIDATES["headway_setting"][0],
    SIGNAL_CANDIDATES["lead_target_distance"][0],
    SIGNAL_CANDIDATES["lead_target_velocity"][0],
    SIGNAL_CANDIDATES["lead_target_accel"][0],
    SIGNAL_CANDIDATES["lead_target_id"][0],
    SIGNAL_CANDIDATES["acc_axle_torque_request"][0],
    SIGNAL_CANDIDATES["acc_brake_accel_request"][0],
    SIGNAL_CANDIDATES["acc_brake_torque_request"][0],
    SIGNAL_CANDIDATES["propulsion_response"][0],
    SIGNAL_CANDIDATES["brake_response"][0],
]

HEADWAY_SECONDS = {1: 1.2, 2: 1.8, 3: 2.4}
EVENT_TYPE_LABELS = {
    "too_close": "跟车过近",
    "too_far": "跟车过远",
    "distance_fluctuation": "跟车距离波动",
}

FOLLOW_DISTANCE_TABLE = {
    1: (
        (0.0, 5.7), (0.45, 6.04), (1.0, 6.46), (2.5, 7.6), (3.0, 8.4),
        (7.5, 16.11), (10.0, 20.03), (13.89, 26.13), (15.0, 27.87),
        (19.44, 34.05), (20.0, 34.75), (25.0, 41.0), (30.0, 46.5),
        (33.33, 50.16), (41.67, 56.8), (45.0, 59.2), (55.56, 66.8), (60.0, 70.0),
    ),
    2: (
        (0.0, 5.9), (0.45, 6.33), (1.0, 6.85), (2.5, 8.27), (3.0, 9.44),
        (7.5, 20.18), (10.0, 24.6), (13.89, 31.49), (15.0, 33.46),
        (19.44, 41.58), (20.0, 42.62), (25.0, 52.0), (30.0, 58.0),
        (33.33, 62.0), (41.67, 68.27), (45.0, 70.4), (55.56, 77.16), (60.0, 80.0),
    ),
    3: (
        (0.0, 6.1), (0.45, 6.61), (1.0, 7.23), (2.5, 8.93), (3.0, 10.48),
        (7.5, 24.24), (10.0, 29.18), (13.89, 36.86), (15.0, 39.05),
        (19.44, 49.1), (20.0, 50.5), (25.0, 63.0), (30.0, 69.5),
        (33.33, 73.83), (41.67, 79.74), (45.0, 81.6), (55.56, 87.51), (60.0, 90.0),
    ),
}


def __run_haoran_data_run__(folder_context: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    start_time = time.time()
    result_lines: list[str] = []
    stats: dict[str, Any] = {"All Case Count": 0, "Bad Case": 0, "Severity Score": 0.0}

    session = folder_context.get("session")
    if session is None:
        result_lines.append("No H5 session found under current folder.")
        result_lines.insert(0, f"[Total Time] {time.time() - start_time:.4f}s")
        return False, result_lines, stats

    result = count_acc_following_distance_anomaly_events(session)
    events = list(result.get("events", []))
    stats["All Case Count"] = int(result.get("candidate_count", 0))
    stats["Bad Case"] = len(events)
    stats["Severity Score"] = float(result.get("max_event_severity", 0.0))
    stats["Too Close Count"] = int(result.get("too_close_count", 0))
    stats["Too Far Count"] = int(result.get("too_far_count", 0))
    stats["Distance Fluctuation Count"] = int(result.get("fluctuation_count", 0))
    stats["Missing Essential Signal Count"] = len(result.get("missing_essential_signals", []))

    result_lines.append(
        f"[ACC Following Distance] file={Path(session.primary_h5_file).name}, "
        f"candidate_count={stats['All Case Count']}, event_count={stats['Bad Case']}, "
        f"max_severity={stats['Severity Score']:.3f}"
    )
    if result.get("missing_essential_signals"):
        result_lines.append("[Coverage Missing] " + ", ".join(str(item) for item in result["missing_essential_signals"]))

    for event in events:
        result_lines.append(
            f"[Event start={event['start_timestamp']:.3f}, end={event['end_timestamp']:.3f}, "
            f"duration={event['duration']:.3f}, type={event['event_type']}, "
            f"headway={event['headway_seconds']:.1f}s, expected={event['expected_distance']:.2f}, "
            f"actual={event['actual_distance']:.2f}, error={event['distance_error']:.2f}, "
            f"cause={event['cause_summary']}, severity={event['severity']:.3f}]"
        )

    result_lines.insert(0, f"[Total Time] {time.time() - start_time:.4f}s")
    return len(events) > 0, result_lines, stats


def count_acc_following_distance_anomaly_events(
    session_or_h5_path: FolderSession | str | Path,
    min_event_seconds: float = DEFAULT_MIN_EVENT_SECONDS,
    distance_margin_m: float = DEFAULT_DISTANCE_MARGIN_M,
    distance_margin_ratio: float = DEFAULT_DISTANCE_MARGIN_RATIO,
    fluctuation_window_seconds: float = DEFAULT_FLUCTUATION_WINDOW_SECONDS,
    fluctuation_min_range_m: float = DEFAULT_FLUCTUATION_MIN_RANGE_M,
    fluctuation_ratio: float = DEFAULT_FLUCTUATION_RATIO,
    cause_padding_seconds: float = DEFAULT_CAUSE_PADDING_SECONDS,
) -> dict[str, object]:
    session = session_or_h5_path if isinstance(session_or_h5_path, FolderSession) else FolderSession(session_or_h5_path)
    resolved = _resolve_signals(session)
    missing_essential_keys = [key for key in ESSENTIAL_SIGNAL_KEYS if not resolved.get(key)]
    missing_essential = [SIGNAL_DISPLAY_NAMES.get(key, key) for key in missing_essential_keys]
    if missing_essential:
        return _empty_result(0.0, 0.0, missing_essential)

    required_resolved = [str(resolved[key]) for key in ("ego_speed", "acc_active", "pedal_override")]
    start_timestamp, end_timestamp = session.common_time_range(required_resolved)
    axis, speed_raw = session.get_signal_slice(str(resolved["ego_speed"]), start_timestamp, end_timestamp)
    if len(axis) < 3:
        return _empty_result(start_timestamp, end_timestamp, missing_essential)

    speed_mps = [_speed_mps(_float_or_nan(value)) for value in speed_raw]
    acc_active = session.sample_signal_to_axis(
        str(resolved["acc_active"]), axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    pedal_override = session.sample_signal_to_axis(
        str(resolved["pedal_override"]), axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    headway_values = _sample_resolved(session, resolved["headway_setting"], axis, start_timestamp, end_timestamp)
    distance_values = _sample_resolved(session, resolved["lead_target_distance"], axis, start_timestamp, end_timestamp)
    lead_ids = _sample_resolved(session, resolved.get("lead_target_id"), axis, start_timestamp, end_timestamp)

    actual_distance = [_float_or_nan(value) for value in distance_values]
    headway_setting = [_headway_index(value) for value in headway_values]
    expected_distance = [
        _expected_distance(speed, headway) if math.isfinite(speed) and headway is not None else math.nan
        for speed, headway in zip(speed_mps, headway_setting)
    ]
    distance_error = [
        actual - expected if math.isfinite(actual) and math.isfinite(expected) else math.nan
        for actual, expected in zip(actual_distance, expected_distance)
    ]
    valid_mask = [
        _as_bool(acc_on)
        and not _as_bool(override)
        and headway is not None
        and math.isfinite(expected)
        and math.isfinite(actual)
        and 0.0 < actual < 250.0
        for acc_on, override, headway, expected, actual in zip(
            acc_active, pedal_override, headway_setting, expected_distance, actual_distance
        )
    ]

    too_close_mask: list[bool] = []
    too_far_mask: list[bool] = []
    for valid, error, expected in zip(valid_mask, distance_error, expected_distance):
        margin = _distance_margin(expected, distance_margin_m, distance_margin_ratio)
        too_close_mask.append(valid and math.isfinite(error) and error <= -margin)
        too_far_mask.append(valid and math.isfinite(error) and error >= margin)

    fluctuation_score = _rolling_range(axis, distance_error, valid_mask, fluctuation_window_seconds)
    fluctuation_mask = [
        valid
        and score >= (max(fluctuation_min_range_m, expected * fluctuation_ratio) if math.isfinite(expected) else math.inf)
        for valid, score, expected in zip(valid_mask, fluctuation_score, expected_distance)
    ]

    event_args = {
        "session": session,
        "axis": axis,
        "resolved": resolved,
        "speed_mps": speed_mps,
        "expected_distance": expected_distance,
        "actual_distance": actual_distance,
        "distance_error": distance_error,
        "headway_setting": headway_setting,
        "lead_ids": lead_ids,
        "min_event_seconds": min_event_seconds,
        "cause_padding_seconds": cause_padding_seconds,
    }
    events: list[dict[str, float | str]] = []
    events.extend(_events_from_mask(mask=too_close_mask, event_type="too_close", **event_args))
    events.extend(_events_from_mask(mask=too_far_mask, event_type="too_far", **event_args))
    events.extend(_events_from_mask(mask=fluctuation_mask, event_type="distance_fluctuation", **event_args))
    events.sort(key=lambda item: float(item["start_timestamp"]))

    return {
        "metric_name": "acc_following_distance_anomaly",
        "method": "expected_follow_distance_from_speed_and_acchdwystg_then_distance_error_context_check",
        "event_count": len(events),
        "candidate_count": len(events),
        "too_close_count": sum(1 for event in events if event["event_type"] == "too_close"),
        "too_far_count": sum(1 for event in events if event["event_type"] == "too_far"),
        "fluctuation_count": sum(1 for event in events if event["event_type"] == "distance_fluctuation"),
        "missing_essential_signals": missing_essential,
        "resolved_signals": resolved,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": max((float(event["severity"]) for event in events), default=0.0),
        "parameters": {
            "distance_margin_m": distance_margin_m,
            "distance_margin_ratio": distance_margin_ratio,
            "fluctuation_window_seconds": fluctuation_window_seconds,
            "fluctuation_min_range_m": fluctuation_min_range_m,
            "fluctuation_ratio": fluctuation_ratio,
            "min_event_seconds": min_event_seconds,
        },
        "events": events,
    }


def _events_from_mask(
    *,
    session: FolderSession,
    axis: list[float],
    mask: list[bool],
    event_type: str,
    resolved: dict[str, str | None],
    speed_mps: list[float],
    expected_distance: list[float],
    actual_distance: list[float],
    distance_error: list[float],
    headway_setting: list[int | None],
    lead_ids: list[object | None],
    min_event_seconds: float,
    cause_padding_seconds: float,
) -> list[dict[str, float | str]]:
    events: list[dict[str, float | str]] = []
    for segment in mask_to_segments(axis, mask):
        if segment.duration < min_event_seconds:
            continue
        indices = range(segment.start_index, segment.end_index + 1)
        ref_index = max(indices, key=lambda index: abs(distance_error[index]) if math.isfinite(distance_error[index]) else 0.0)
        start = max(float(segment.start_timestamp) - cause_padding_seconds, axis[0])
        end = min(float(segment.end_timestamp) + cause_padding_seconds, axis[-1])
        cause = _cause_summary(
            session=session,
            resolved=resolved,
            start=start,
            end=end,
            ref_time=float(axis[ref_index]),
            event_type=event_type,
            lead_ids=[lead_ids[index] for index in indices],
            headway_values=[headway_setting[index] for index in indices],
        )
        error = distance_error[ref_index]
        events.append(
            {
                "start_timestamp": float(segment.start_timestamp),
                "end_timestamp": float(segment.end_timestamp),
                "duration": float(segment.duration),
                "event_type": event_type,
                "event_label": EVENT_TYPE_LABELS.get(event_type, event_type),
                "headway_seconds": HEADWAY_SECONDS.get(headway_setting[ref_index], math.nan),
                "speed_mps": speed_mps[ref_index],
                "expected_distance": expected_distance[ref_index],
                "actual_distance": actual_distance[ref_index],
                "distance_error": error,
                "cause": cause,
                "cause_summary": cause,
                "severity": round(abs(error) if math.isfinite(error) else 0.0, 3),
            }
        )
    return events


def _cause_summary(
    *,
    session: FolderSession,
    resolved: dict[str, str | None],
    start: float,
    end: float,
    ref_time: float,
    event_type: str,
    lead_ids: list[object | None],
    headway_values: list[int | None],
) -> str:
    reasons: list[str] = []
    if _has_value_change(lead_ids):
        reasons.append("问题窗口内跟车目标发生切换")
    if _has_value_change(headway_values):
        reasons.append("问题窗口内 ACCHdwyStg 时距档位发生变化")

    lead_velocity_range = _range_for_signal(session, resolved.get("lead_target_velocity"), start, end)
    lead_accel_range = _range_for_signal(session, resolved.get("lead_target_accel"), start, end)
    if lead_velocity_range is not None and lead_velocity_range >= 2.0:
        reasons.append(f"前车相对速度波动 {lead_velocity_range:.2f}m/s")
    if lead_accel_range is not None and lead_accel_range >= 1.0:
        reasons.append(f"前车相对加速度波动 {lead_accel_range:.2f}m/s2")

    request_snapshot = _request_response_evidence(session, resolved, ref_time, event_type)
    if request_snapshot:
        reasons.append(request_snapshot)
    if reasons:
        return "; ".join(reasons)

    missing = [key for key in ("lead_target_id", "lead_target_velocity", "lead_target_accel") if not resolved.get(key)]
    if missing:
        return "已经识别到跟车距离偏差，但目标上下文信号不完整：" + "、".join(missing)
    return "已经识别到跟车距离偏差，当前窗口未匹配到明确的目标、请求或响应突变"


def _request_response_evidence(
    session: FolderSession,
    resolved: dict[str, str | None],
    timestamp: float,
    event_type: str,
) -> str:
    axle_request = _float_or_none(_sample_one(session, resolved.get("acc_axle_torque_request"), timestamp))
    brake_accel_request = _float_or_none(_sample_one(session, resolved.get("acc_brake_accel_request"), timestamp))
    brake_torque_request = _float_or_none(_sample_one(session, resolved.get("acc_brake_torque_request"), timestamp))
    propulsion_response = _sample_one(session, resolved.get("propulsion_response"), timestamp)
    brake_response = _sample_one(session, resolved.get("brake_response"), timestamp)

    request_parts: list[str] = []
    if brake_accel_request is not None and brake_accel_request < -0.2:
        request_parts.append(f"制动减速度请求={brake_accel_request:.2f}")
    if brake_torque_request is not None and abs(brake_torque_request) > 1e-6:
        request_parts.append(f"制动扭矩请求={brake_torque_request:.2f}")
    if axle_request is not None:
        request_parts.append(f"驱动扭矩请求={axle_request:.2f}")

    response_parts: list[str] = []
    if propulsion_response is not None:
        response_parts.append(f"驱动响应={propulsion_response}")
    if brake_response is not None:
        response_parts.append(f"制动响应={brake_response}")

    if event_type == "too_close" and request_parts:
        return "车距偏近时算法已有修正请求：" + "，".join(request_parts + response_parts)
    if event_type == "too_far" and not request_parts:
        return "车距偏远，但当前窗口没有匹配到明显 ACC 修正请求"
    if request_parts:
        return "请求/响应快照：" + "，".join(request_parts + response_parts)
    return ""


def _expected_distance(speed_mps: float, headway_index: int) -> float:
    points = FOLLOW_DISTANCE_TABLE.get(headway_index)
    if not points:
        return math.nan
    if speed_mps <= points[0][0]:
        return points[0][1]
    if speed_mps >= points[-1][0]:
        return points[-1][1]
    for index in range(1, len(points)):
        right_speed, right_distance = points[index]
        if speed_mps <= right_speed:
            left_speed, left_distance = points[index - 1]
            ratio = (speed_mps - left_speed) / (right_speed - left_speed)
            return left_distance + ratio * (right_distance - left_distance)
    return points[-1][1]


def _resolve_signals(session: FolderSession) -> dict[str, str | None]:
    signal_lookup = {_short_signal_name(signal_name).lower(): signal_name for signal_name in session.get_store().list_signals()}
    resolved: dict[str, str | None] = {}
    for key, candidates in SIGNAL_CANDIDATES.items():
        found = None
        for candidate in candidates:
            if session.has_signal(candidate):
                found = candidate
                break
            short_name = _short_signal_name(candidate).lower()
            if short_name in signal_lookup:
                found = signal_lookup[short_name]
                break
        resolved[key] = found
    return resolved


def _sample_resolved(
    session: FolderSession,
    signal_name: str | None,
    axis: list[float],
    start_timestamp: float,
    end_timestamp: float,
) -> list[object | None]:
    if not signal_name:
        return [None for _ in axis]
    try:
        return session.sample_signal_to_axis(
            signal_name, axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
        )
    except Exception:
        return [None for _ in axis]


def _sample_one(session: FolderSession, signal_name: str | None, timestamp: float) -> object | None:
    if not signal_name:
        return None
    try:
        return session.sample_signal_to_axis(signal_name, [timestamp], method="nearest")[0]
    except Exception:
        return None


def _range_for_signal(session: FolderSession, signal_name: str | None, start: float, end: float) -> float | None:
    if not signal_name:
        return None
    try:
        _, values = session.get_signal_slice(signal_name, start, end)
    except Exception:
        return None
    numeric = [_float_or_nan(value) for value in values]
    numeric = [value for value in numeric if math.isfinite(value)]
    if len(numeric) < 2:
        return None
    return max(numeric) - min(numeric)


def _rolling_range(axis: list[float], values: list[float], valid_mask: list[bool], window_seconds: float) -> list[float]:
    ranges: list[float] = []
    left = 0
    for right, timestamp in enumerate(axis):
        while left < right and float(axis[left]) < float(timestamp) - window_seconds:
            left += 1
        window_values = [
            values[index]
            for index in range(left, right + 1)
            if valid_mask[index] and math.isfinite(values[index])
        ]
        ranges.append((max(window_values) - min(window_values)) if len(window_values) >= 2 else 0.0)
    return ranges


def _distance_margin(expected_distance: float, margin_m: float, margin_ratio: float) -> float:
    if not math.isfinite(expected_distance):
        return math.inf
    return max(float(margin_m), abs(expected_distance) * float(margin_ratio))


def _empty_result(start_timestamp: float, end_timestamp: float, missing_essential: list[str]) -> dict[str, object]:
    return {
        "metric_name": "acc_following_distance_anomaly",
        "method": "expected_follow_distance_from_speed_and_acchdwystg_then_distance_error_context_check",
        "event_count": 0,
        "candidate_count": 0,
        "too_close_count": 0,
        "too_far_count": 0,
        "fluctuation_count": 0,
        "missing_essential_signals": missing_essential,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": 0.0,
        "events": [],
    }


def _headway_index(value: object) -> int | None:
    number = _float_or_none(value)
    if number is None:
        return None
    index = int(round(number))
    if index not in HEADWAY_SECONDS:
        return None
    return index


def _speed_mps(value: float) -> float:
    if not math.isfinite(value):
        return math.nan
    if value > 70.0:
        return value / 3.6
    return value


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "active", "on"}


def _float_or_none(value: object) -> float | None:
    result = _float_or_nan(value)
    return result if math.isfinite(result) else None


def _float_or_nan(value: object) -> float:
    try:
        if value is None:
            return math.nan
        return float(value)
    except Exception:
        return math.nan


def _short_signal_name(signal_name: str) -> str:
    short_name = str(signal_name or "").rsplit(".", 1)[-1]
    if len(short_name) > 1 and short_name.startswith("I") and short_name[1].isupper():
        return short_name[1:]
    return short_name


def _has_value_change(values: Iterable[object]) -> bool:
    normalized = [str(value) for value in values if value is not None]
    return len(normalized) >= 2 and len(set(normalized)) > 1
