from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

from signal_lib import FolderSession, mask_to_segments


METRIC_CATEGORY = "MPI"

ACCELERATION_SIGNAL = "IMU_Yaw_Long_Acc_037_M2.IIMULonAccSec"
SPEED_SIGNAL = "PPEI_Vehicle_Speed_and_Distance_209_M1.IVehSpdAvgDrvn_1"
ACC_ACTIVE_SIGNAL = "Adaptive_Cruise_Disp_Stat.IACCAct376"
PEDAL_OVERRIDE_SIGNAL = "PPEI_Propulsion_General_Data_1_0C2_M.IAccPdlOvrrdAtv"

DEFAULT_MIN_SPEED_KPH = 1.0
DEFAULT_MIN_EVENT_SECONDS = 0.5
DEFAULT_RISK_DISTANCE_M = 55.0
DEFAULT_CLOSE_DISTANCE_M = 18.0
DEFAULT_SHORT_TTC_SECONDS = 3.5
DEFAULT_CLOSING_SPEED_MPS = 2.0
DEFAULT_TARGET_DECEL_MPS2 = -1.5
DEFAULT_RESPONSE_DECEL_MPS2 = -0.35
DEFAULT_BRAKE_ACCEL_REQUEST_MPS2 = -0.25
DEFAULT_BRAKE_TORQUE_REQUEST = 1.0
DEFAULT_CONTEXT_SIGNAL_VALUES = {
    "target_kind_flag": 0.0,
}

REQUIRED_SIGNALS = [
    ACCELERATION_SIGNAL,
    SPEED_SIGNAL,
    ACC_ACTIVE_SIGNAL,
    PEDAL_OVERRIDE_SIGNAL,
]

SIGNAL_KEYS = {
    "acceleration": REQUIRED_SIGNALS[0],
    "speed": REQUIRED_SIGNALS[1],
    "acc_active": REQUIRED_SIGNALS[2],
    "pedal_override": REQUIRED_SIGNALS[3],
}

CONTEXT_SIGNAL_CANDIDATES = {
    "target_kind_flag": ("FLT1_CUTIN_Flag",),
    "lead_target_id": ("FLT1_Global_ID", "LeadTargetId", "ACC_Target_Info.LeadTargetId"),
    "lead_target_distance": ("FLT1_Long_Range", "LeadTargetDistance", "ACC_Target_Info.LeadTargetDistance"),
    "lead_target_velocity": ("FLT1_Long_Velocity", "LeadTargetVelocity", "ACC_Target_Info.LeadTargetVelocity"),
    "lead_target_accel": ("FLT1_Long_Accel", "LeadTargetAccel", "ACC_Target_Info.LeadTargetAccel"),
    "lead_target_ttc": ("LeadTargetTTC", "ACC_Target_Info.LeadTargetTTC"),
    "cutin_target_id": ("FLT1_Global_ID", "CutInTargetId", "ACC_CutIn_Target_Info.CutInTargetId"),
    "cutin_target_distance": ("FLT1_Long_Range", "CutInTargetDistance", "ACC_CutIn_Target_Info.CutInTargetDistance"),
    "cutin_target_velocity": ("FLT1_Long_Velocity", "CutInTargetVelocity", "ACC_CutIn_Target_Info.CutInTargetVelocity"),
    "cutin_target_accel": ("FLT1_Long_Accel", "CutInTargetAccel", "ACC_CutIn_Target_Info.CutInTargetAccel"),
    "cutin_target_ttc": ("CutInTargetTTC", "ACC_CutIn_Target_Info.CutInTargetTTC"),
    "acc_brake_active_request": (
        "ADAS_Command_Ext1.IACCBSCE_ACCAct1",
        "ADAS_Command_Ext1.ACCBSCE_ACCAct1",
        "IACCBSCE_ACCAct1",
        "ACCBSCE_ACCAct1",
    ),
    "acc_brake_type_request": (
        "ADAS_Command_Ext1.IACCBSCE_AutBrkTp1",
        "ADAS_Command_Ext1.ACCBSCE_AutBrkTp1",
        "ADAS_Command_Ext1.IFCACBSC_AutBrkRqTp1",
        "IACCBSCE_AutBrkTp1",
        "ACCBSCE_AutBrkTp1",
        "IFCACBSC_AutBrkRqTp1",
        "FCACBSC_AutBrkRqTp1",
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
        "ADAS_Command_Ext1.IFCACBSC_CtrlTrq1",
        "IACCBSCE_ACCTrq1",
        "ACCBSCE_ACCTrq1",
        "IFCACBSC_CtrlTrq1",
        "FCACBSC_CtrlTrq1",
    ),
    "acc_axle_torque_request": (
        "PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M.IACCATC_AxlTrqRq",
        "PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M.ACCATC_AxlTrqRq",
        "IACCATC_AxlTrqRq",
        "ACCATC_AxlTrqRq",
    ),
}

OPTIONAL_SIGNALS = sorted(
    {
        candidate
        for candidates in CONTEXT_SIGNAL_CANDIDATES.values()
        for candidate in candidates
    }
)

PLOT_SIGNALS = REQUIRED_SIGNALS + [
    CONTEXT_SIGNAL_CANDIDATES["target_kind_flag"][0],
    CONTEXT_SIGNAL_CANDIDATES["lead_target_distance"][0],
    CONTEXT_SIGNAL_CANDIDATES["lead_target_velocity"][0],
    CONTEXT_SIGNAL_CANDIDATES["lead_target_accel"][0],
    CONTEXT_SIGNAL_CANDIDATES["acc_brake_active_request"][0],
    CONTEXT_SIGNAL_CANDIDATES["acc_brake_type_request"][0],
    CONTEXT_SIGNAL_CANDIDATES["acc_brake_accel_request"][0],
    CONTEXT_SIGNAL_CANDIDATES["acc_brake_torque_request"][0],
    CONTEXT_SIGNAL_CANDIDATES["acc_axle_torque_request"][0],
]


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
        result_lines.append("No H5 session found under current folder.")
        result_lines.insert(0, f"[Total Time] {time.time() - start_time:.4f}s")
        return False, result_lines, stats

    result = count_acc_missed_braking_events(session)
    event_count = int(result["event_count"])
    candidate_count = int(result["candidate_count"])
    max_severity = float(result["max_event_severity"])

    stats["All Case Count"] = candidate_count
    stats["Bad Case"] = event_count
    stats["Severity Score"] = max_severity
    stats["Request Missing Count"] = int(result.get("request_missing_count", 0))
    stats["Response Missing Count"] = int(result.get("response_missing_count", 0))
    stats["Missing Context Signal Count"] = len(result.get("missing_context_signals", []))

    result_lines.append(
        f"[ACC Missed Braking] file={Path(session.primary_h5_file).name}, "
        f"candidate_count={candidate_count}, missed_count={event_count}, "
        f"request_missing_count={stats['Request Missing Count']}, "
        f"response_missing_count={stats['Response Missing Count']}, "
        f"max_severity={max_severity:.3f}"
    )
    if result.get("missing_context_signals"):
        result_lines.append(
            "[Context Missing] "
            + ", ".join(str(item) for item in list(result["missing_context_signals"])[:12])
        )
    for event in result["events"]:
        result_lines.append(
            f"[Event start={event['start_timestamp']:.3f}, end={event['end_timestamp']:.3f}, "
            f"duration={event['duration']:.3f}, risk_start={event['risk_start_timestamp']:.3f}, "
            f"min_distance={event['min_distance']:.3f}, min_ttc={event['min_ttc']:.3f}, "
            f"max_closing_speed={event['max_closing_speed']:.3f}, min_acceleration={event['min_acceleration']:.3f}, "
            f"request_state={event['request_state']}, response_state={event['response_state']}, "
            f"context={event['context_summary']}, severity={event['severity']:.3f}]"
        )

    result_lines.insert(0, f"[Total Time] {time.time() - start_time:.4f}s")
    return event_count > 0, result_lines, stats


def count_acc_missed_braking_events(
    session_or_h5_path: FolderSession | str | Path,
    min_speed_kph: float = DEFAULT_MIN_SPEED_KPH,
    min_event_seconds: float = DEFAULT_MIN_EVENT_SECONDS,
    risk_distance_m: float = DEFAULT_RISK_DISTANCE_M,
    close_distance_m: float = DEFAULT_CLOSE_DISTANCE_M,
    short_ttc_seconds: float = DEFAULT_SHORT_TTC_SECONDS,
    closing_speed_mps: float = DEFAULT_CLOSING_SPEED_MPS,
    target_decel_mps2: float = DEFAULT_TARGET_DECEL_MPS2,
    response_decel_mps2: float = DEFAULT_RESPONSE_DECEL_MPS2,
    brake_accel_request_mps2: float = DEFAULT_BRAKE_ACCEL_REQUEST_MPS2,
    brake_torque_request: float = DEFAULT_BRAKE_TORQUE_REQUEST,
) -> dict[str, object]:
    session = session_or_h5_path if isinstance(session_or_h5_path, FolderSession) else FolderSession(session_or_h5_path)
    start_timestamp, end_timestamp = session.common_time_range(REQUIRED_SIGNALS)
    axis, acceleration_raw = session.get_signal_slice(SIGNAL_KEYS["acceleration"], start_timestamp, end_timestamp)
    if len(axis) < 2:
        return _empty_result(start_timestamp, end_timestamp)

    acceleration = [_float_or_nan(value) for value in acceleration_raw]
    speed = _sample_required(session, SIGNAL_KEYS["speed"], axis, start_timestamp, end_timestamp)
    acc_active = session.sample_signal_to_axis(
        SIGNAL_KEYS["acc_active"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    pedal_override = session.sample_signal_to_axis(
        SIGNAL_KEYS["pedal_override"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )

    resolved = _resolve_context_signals(session)
    sampled = {
        key: _sample_optional(session, signal_name, axis, start_timestamp, end_timestamp)
        for key, signal_name in resolved.items()
        if signal_name
    }
    for key, value in DEFAULT_CONTEXT_SIGNAL_VALUES.items():
        if key not in sampled:
            sampled[key] = [value for _ in axis]

    candidate_mask: list[bool] = []
    missed_mask: list[bool] = []
    per_sample: list[dict[str, object]] = []
    for index, timestamp in enumerate(axis):
        base_valid = _as_bool(acc_active[index]) and not _as_bool(pedal_override[index]) and speed[index] >= min_speed_kph
        sample = _evaluate_sample(
            sampled=sampled,
            index=index,
            vehicle_speed=speed[index],
            acceleration=acceleration[index],
            risk_distance_m=risk_distance_m,
            close_distance_m=close_distance_m,
            short_ttc_seconds=short_ttc_seconds,
            closing_speed_mps=closing_speed_mps,
            target_decel_mps2=target_decel_mps2,
            response_decel_mps2=response_decel_mps2,
            brake_accel_request_mps2=brake_accel_request_mps2,
            brake_torque_request=brake_torque_request,
        )
        candidate = base_valid and bool(sample["risk"])
        missed = candidate and bool(sample["request_missing"]) and bool(sample["response_missing"])
        candidate_mask.append(candidate)
        missed_mask.append(missed)
        sample["timestamp"] = float(timestamp)
        per_sample.append(sample)

    candidate_segments = [segment for segment in mask_to_segments(axis, candidate_mask) if segment.duration >= min_event_seconds]
    events = []
    request_missing_count = 0
    response_missing_count = 0
    for segment in mask_to_segments(axis, missed_mask):
        if segment.duration < min_event_seconds:
            continue
        samples = per_sample[segment.start_index : segment.end_index + 1]
        request_missing_count += 1
        response_missing_count += 1
        distances = [_float_or_nan(sample.get("distance")) for sample in samples]
        ttcs = [_float_or_nan(sample.get("ttc")) for sample in samples]
        closing_speeds = [_float_or_nan(sample.get("closing_speed")) for sample in samples]
        accelerations = acceleration[segment.start_index : segment.end_index + 1]
        min_distance = _finite_min(distances, default=math.nan)
        min_ttc = _finite_min(ttcs, default=math.nan)
        max_closing = _finite_max(closing_speeds, default=0.0)
        min_accel = _finite_min(accelerations, default=math.nan)
        severity = _event_severity(min_distance, min_ttc, max_closing, min_accel)
        events.append(
            {
                "start_timestamp": segment.start_timestamp,
                "end_timestamp": segment.end_timestamp,
                "duration": segment.duration,
                "risk_start_timestamp": float(samples[0]["timestamp"]),
                "min_distance": min_distance,
                "min_ttc": min_ttc,
                "max_closing_speed": max_closing,
                "min_acceleration": min_accel,
                "request_state": "request_missing",
                "response_state": "vehicle_decel_missing",
                "context_summary": _context_summary(samples),
                "severity": severity,
            }
        )

    return {
        "metric_name": "acc_missed_braking_count",
        "method": "target_risk_then_request_and_response_missing_check",
        "event_count": len(events),
        "candidate_count": len(candidate_segments),
        "request_missing_count": request_missing_count,
        "response_missing_count": response_missing_count,
        "missing_context_signals": _missing_context_signal_keys(resolved),
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": max((float(event["severity"]) for event in events), default=0.0),
        "parameters": {
            "min_event_seconds": min_event_seconds,
            "risk_distance_m": risk_distance_m,
            "close_distance_m": close_distance_m,
            "short_ttc_seconds": short_ttc_seconds,
            "closing_speed_mps": closing_speed_mps,
            "target_decel_mps2": target_decel_mps2,
            "response_decel_mps2": response_decel_mps2,
            "brake_accel_request_mps2": brake_accel_request_mps2,
            "brake_torque_request": brake_torque_request,
        },
        "events": events,
    }


def _empty_result(start_timestamp: float, end_timestamp: float) -> dict[str, object]:
    return {
        "metric_name": "acc_missed_braking_count",
        "method": "target_risk_then_request_and_response_missing_check",
        "event_count": 0,
        "candidate_count": 0,
        "request_missing_count": 0,
        "response_missing_count": 0,
        "missing_context_signals": [
            key for key in CONTEXT_SIGNAL_CANDIDATES if key not in DEFAULT_CONTEXT_SIGNAL_VALUES
        ],
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": 0.0,
        "parameters": {},
        "events": [],
    }


def _evaluate_sample(
    *,
    sampled: dict[str, list[object]],
    index: int,
    vehicle_speed: float,
    acceleration: float,
    risk_distance_m: float,
    close_distance_m: float,
    short_ttc_seconds: float,
    closing_speed_mps: float,
    target_decel_mps2: float,
    response_decel_mps2: float,
    brake_accel_request_mps2: float,
    brake_torque_request: float,
) -> dict[str, object]:
    target_role = _flt1_target_role(_value_at(sampled, "target_kind_flag", index))
    prefix = "cutin_target" if target_role == "cut-in" else "lead_target"
    target_id = _value_at(sampled, f"{prefix}_id", index)
    distance = _float_or_none(_value_at(sampled, f"{prefix}_distance", index))
    velocity = _float_or_none(_value_at(sampled, f"{prefix}_velocity", index))
    target_accel = _float_or_none(_value_at(sampled, f"{prefix}_accel", index))
    ttc = _float_or_none(_value_at(sampled, f"{prefix}_ttc", index))
    if ttc is None and distance is not None and velocity is not None and velocity < -1e-6:
        ttc = distance / max(-velocity, 1e-6)

    has_target = _is_present_id(target_id) or distance is not None
    valid_distance = distance is not None and 0.0 < distance <= risk_distance_m
    short_ttc = ttc is not None and 0.0 < ttc <= short_ttc_seconds
    closing_speed = max(-velocity, 0.0) if velocity is not None else 0.0
    closing = closing_speed >= closing_speed_mps
    close = distance is not None and 0.0 < distance <= close_distance_m
    target_decel = target_accel is not None and target_accel <= target_decel_mps2
    risk = has_target and valid_distance and (short_ttc or closing or close or target_decel)

    brake_active = _as_bool(_value_at(sampled, "acc_brake_active_request", index))
    brake_type = _float_or_none(_value_at(sampled, "acc_brake_type_request", index))
    brake_accel = _float_or_none(_value_at(sampled, "acc_brake_accel_request", index))
    brake_torque = _float_or_none(_value_at(sampled, "acc_brake_torque_request", index))
    axle_torque = _float_or_none(_value_at(sampled, "acc_axle_torque_request", index))
    request_present = (
        brake_active
        or (brake_type is not None and abs(brake_type) > 0.5)
        or (brake_accel is not None and brake_accel <= brake_accel_request_mps2)
        or (brake_torque is not None and abs(brake_torque) >= brake_torque_request)
        or (axle_torque is not None and axle_torque < -brake_torque_request)
    )
    response_present = math.isfinite(acceleration) and acceleration <= response_decel_mps2

    return {
        "risk": risk,
        "target_role": target_role or "lead",
        "distance": distance,
        "velocity": velocity,
        "target_accel": target_accel,
        "ttc": ttc,
        "closing_speed": closing_speed,
        "request_missing": not request_present,
        "response_missing": not response_present,
    }


def _resolve_context_signals(session: FolderSession) -> dict[str, str | None]:
    signal_lookup = {
        _short_signal_name(signal_name).lower(): signal_name
        for signal_name in session.get_store().list_signals()
    }
    resolved: dict[str, str | None] = {}
    for key, candidates in CONTEXT_SIGNAL_CANDIDATES.items():
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


def _missing_context_signal_keys(resolved_context: dict[str, str | None]) -> list[str]:
    missing: list[str] = []
    for key, signal_name in resolved_context.items():
        if signal_name or key in DEFAULT_CONTEXT_SIGNAL_VALUES:
            continue
        if key == "lead_target_ttc" and resolved_context.get("lead_target_distance") and resolved_context.get("lead_target_velocity"):
            continue
        if key == "cutin_target_ttc" and resolved_context.get("cutin_target_distance") and resolved_context.get("cutin_target_velocity"):
            continue
        missing.append(key)
    return missing


def _sample_required(
    session: FolderSession,
    signal_name: str,
    axis: list[float],
    start_timestamp: float,
    end_timestamp: float,
) -> list[float]:
    return [
        _float_or_nan(value)
        for value in session.sample_signal_to_axis(
            signal_name,
            axis,
            method="nearest",
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
    ]


def _sample_optional(
    session: FolderSession,
    signal_name: str | None,
    axis: list[float],
    start_timestamp: float,
    end_timestamp: float,
) -> list[object]:
    if not signal_name:
        return []
    return session.sample_signal_to_axis(
        signal_name,
        axis,
        method="nearest",
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )


def _context_summary(samples: list[dict[str, object]]) -> str:
    if not samples:
        return "target risk detected"
    roles = {str(sample.get("target_role", "lead")) for sample in samples}
    distances = [_float_or_nan(sample.get("distance")) for sample in samples]
    ttcs = [_float_or_nan(sample.get("ttc")) for sample in samples]
    closing = [_float_or_nan(sample.get("closing_speed")) for sample in samples]
    return (
        f"target_role={','.join(sorted(roles))}; "
        f"min_distance={_finite_min(distances, math.nan):.3f}; "
        f"min_ttc={_finite_min(ttcs, math.nan):.3f}; "
        f"max_closing_speed={_finite_max(closing, 0.0):.3f}"
    )


def _event_severity(min_distance: float, min_ttc: float, max_closing: float, min_accel: float) -> float:
    distance_score = 0.0 if not math.isfinite(min_distance) else max(0.0, (DEFAULT_RISK_DISTANCE_M - min_distance) / 10.0)
    ttc_score = 0.0 if not math.isfinite(min_ttc) else max(0.0, DEFAULT_SHORT_TTC_SECONDS - min_ttc)
    closing_score = max(0.0, max_closing - DEFAULT_CLOSING_SPEED_MPS)
    response_score = 0.0 if not math.isfinite(min_accel) else max(0.0, min_accel - DEFAULT_RESPONSE_DECEL_MPS2)
    return round(distance_score + ttc_score + closing_score + response_score, 3)


def _value_at(sampled: dict[str, list[object]], key: str, index: int) -> object | None:
    values = sampled.get(key)
    if not values or index >= len(values):
        return None
    return values[index]


def _flt1_target_role(flag_value: object | None) -> str:
    flag = _float_or_none(flag_value)
    return "cut-in" if flag is not None and int(round(flag)) == 1 else "lead"


def _is_present_id(value: object | None) -> bool:
    if value is None:
        return False
    number = _float_or_none(value)
    if number is None:
        return True
    return math.isfinite(number) and abs(number) > 1e-9


def _finite_min(values: list[float], default: float) -> float:
    finite = [value for value in values if math.isfinite(value)]
    return min(finite) if finite else default


def _finite_max(values: list[float], default: float) -> float:
    finite = [value for value in values if math.isfinite(value)]
    return max(finite) if finite else default


def _short_signal_name(signal_name: str) -> str:
    return signal_name.rsplit(".", 1)[-1]


def _float_or_nan(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _float_or_none(value: object | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_bool(value: object) -> bool:
    try:
        return bool(int(round(float(value))))
    except (TypeError, ValueError, OverflowError):
        return bool(value)
