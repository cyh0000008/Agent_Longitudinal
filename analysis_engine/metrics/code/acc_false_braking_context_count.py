from __future__ import annotations

import time
import math
from pathlib import Path
from typing import Any

from signal_lib import FolderSession


METRIC_CATEGORY = "MPI"

ACCELERATION_SIGNAL = "IMU_Yaw_Long_Acc_037_M2.IIMULonAccSec"
SPEED_SIGNAL = "PPEI_Vehicle_Speed_and_Distance_209_M1.IVehSpdAvgDrvn_1"
ACC_ACTIVE_SIGNAL = "Adaptive_Cruise_Disp_Stat.IACCAct376"
PEDAL_OVERRIDE_SIGNAL = "PPEI_Propulsion_General_Data_1_0C2_M.IAccPdlOvrrdAtv"

DEFAULT_SMOOTHING_WINDOW = 5
DEFAULT_PRE_TROUGH_WINDOW_SECONDS = 0.80
DEFAULT_POST_TROUGH_WINDOW_SECONDS = 0.80
DEFAULT_TROUGH_ACCEL_THRESHOLD = -1.20
DEFAULT_TROUGH_MIN_DROP = 1.00
DEFAULT_TROUGH_MIN_REBOUND = 0.40
DEFAULT_MIN_SPEED_KPH = 1.00
DEFAULT_TARGET_MAX_DISTANCE_M = 80.00
DEFAULT_TARGET_TTC_SECONDS = 4.00
DEFAULT_CURVE_SPEED_MARGIN_KPH = 3.00
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

# These names are temporary assumptions until the real bus signals are confirmed.
# Keep them aligned with config/vehicle_response_signal_catalog.xlsx.
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
    "curvature_limit_speed": ("Debug_uint_Reserved_48", "CurvatureLimitSpeed", "Road_Curvature_Info.CurvatureLimitSpeed"),
    "curvature_left_c2": ("F_V_Left_Lane_ModelC2", "Left_Lane_Track_Coeff.LaneModelC2"),
    "curvature_right_c2": ("F_V_Right_Lane_ModelC2", "Right_Lane_Track_Coeff.LaneModelC2"),
    "curvature": ("Curvature", "Road_Curvature_Info.Curvature"),
    "curvature_limit_enable": ("Debug_uint_Reserved_49", "CurvatureLimitEnable", "Road_Curvature_Info.CurvatureLimitEnable"),
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
    CONTEXT_SIGNAL_CANDIDATES["lead_target_ttc"][0],
    CONTEXT_SIGNAL_CANDIDATES["cutin_target_distance"][0],
    CONTEXT_SIGNAL_CANDIDATES["curvature_limit_speed"][0],
    CONTEXT_SIGNAL_CANDIDATES["curvature_left_c2"][1],
    CONTEXT_SIGNAL_CANDIDATES["curvature_right_c2"][1],
    CONTEXT_SIGNAL_CANDIDATES["curvature_limit_enable"][0],
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

    result = count_acc_false_braking_context_events(session)
    event_count = int(result["event_count"])
    candidate_count = int(result["candidate_count"])
    explained_count = int(result["context_explained_count"])
    max_severity = float(result["max_event_severity"])

    stats["All Case Count"] = candidate_count
    stats["Bad Case"] = event_count
    stats["Severity Score"] = max_severity
    stats["Context Explained Count"] = explained_count
    stats["Missing Context Signal Count"] = len(result.get("missing_context_signals", []))

    result_lines.append(
        f"[ACC False Braking Context] file={Path(session.primary_h5_file).name}, "
        f"candidate_count={candidate_count}, unexplained_count={event_count}, "
        f"context_explained_count={explained_count}, max_severity={max_severity:.3f}"
    )
    if result.get("missing_context_signals"):
        result_lines.append(
            "[Context Missing] "
            + ", ".join(str(item) for item in list(result["missing_context_signals"])[:12])
        )
    for event in result["events"]:
        result_lines.append(
            f"[Event start={event['start_timestamp']:.3f}, end={event['end_timestamp']:.3f}, "
            f"duration={event['duration']:.3f}, trough={event['trough_timestamp']:.3f}, "
            f"min_acceleration={event['min_acceleration']:.3f}, decel_drop={event['decel_drop']:.3f}, "
            f"rebound={event['rebound']:.3f}, vehicle_speed={event['vehicle_speed']:.3f}, "
            f"context={event['context_summary']}, severity={event['severity']:.3f}]"
        )

    result_lines.insert(0, f"[Total Time] {time.time() - start_time:.4f}s")
    return event_count > 0, result_lines, stats


def count_acc_false_braking_context_events(
    session_or_h5_path: FolderSession | str | Path,
    smoothing_window: int = DEFAULT_SMOOTHING_WINDOW,
    pre_trough_window_seconds: float = DEFAULT_PRE_TROUGH_WINDOW_SECONDS,
    post_trough_window_seconds: float = DEFAULT_POST_TROUGH_WINDOW_SECONDS,
    trough_accel_threshold: float = DEFAULT_TROUGH_ACCEL_THRESHOLD,
    trough_min_drop: float = DEFAULT_TROUGH_MIN_DROP,
    trough_min_rebound: float = DEFAULT_TROUGH_MIN_REBOUND,
    min_speed_kph: float = DEFAULT_MIN_SPEED_KPH,
    target_max_distance_m: float = DEFAULT_TARGET_MAX_DISTANCE_M,
    target_ttc_seconds: float = DEFAULT_TARGET_TTC_SECONDS,
    curve_speed_margin_kph: float = DEFAULT_CURVE_SPEED_MARGIN_KPH,
) -> dict[str, object]:
    session = session_or_h5_path if isinstance(session_or_h5_path, FolderSession) else FolderSession(session_or_h5_path)
    start_timestamp, end_timestamp = session.common_time_range(REQUIRED_SIGNALS)

    axis, accel_raw = session.get_signal_slice(SIGNAL_KEYS["acceleration"], start_timestamp, end_timestamp)
    accel_values = [float(value) for value in accel_raw]
    if len(axis) < 5:
        return _empty_result(start_timestamp, end_timestamp, smoothing_window)

    speed_values = [
        _float_or_nan(value)
        for value in session.sample_signal_to_axis(
            SIGNAL_KEYS["speed"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
        )
    ]
    acc_active_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["acc_active"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    pedal_override_values = session.sample_signal_to_axis(
        SIGNAL_KEYS["pedal_override"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    valid_mask = [
        _as_bool(acc_on) and not _as_bool(override) and speed >= min_speed_kph
        for speed, acc_on, override in zip(speed_values, acc_active_values, pedal_override_values)
    ]
    smoothed_accel = _moving_average(accel_values, smoothing_window)

    resolved_context = _resolve_context_signals(session)
    missing_context_signals = _missing_context_signal_keys(resolved_context)
    candidate_count = 0
    context_explained_count = 0
    events: list[dict[str, float | str]] = []

    index = 1
    while index < len(axis) - 1:
        timestamp = float(axis[index])
        if not valid_mask[index]:
            index += 1
            continue

        pre_indices = [
            left
            for left in range(index)
            if timestamp - pre_trough_window_seconds <= float(axis[left]) < timestamp and valid_mask[left]
        ]
        post_indices = [
            right
            for right in range(index + 1, len(axis))
            if timestamp < float(axis[right]) <= timestamp + post_trough_window_seconds and valid_mask[right]
        ]
        if not pre_indices or not post_indices:
            index += 1
            continue

        trough_accel = float(smoothed_accel[index])
        before_ref = max(float(smoothed_accel[left]) for left in pre_indices)
        after_ref = max(float(smoothed_accel[right]) for right in post_indices)
        decel_drop = before_ref - trough_accel
        rebound = after_ref - trough_accel
        if (
            trough_accel > trough_accel_threshold
            or decel_drop < trough_min_drop
            or rebound < trough_min_rebound
        ):
            index += 1
            continue

        event_start = max(float(axis[pre_indices[0]]), start_timestamp)
        event_end = min(float(axis[post_indices[-1]]), end_timestamp)
        context = _evaluate_context(
            session=session,
            resolved_context=resolved_context,
            timestamp=timestamp,
            vehicle_speed=speed_values[index],
            target_max_distance_m=target_max_distance_m,
            target_ttc_seconds=target_ttc_seconds,
            curve_speed_margin_kph=curve_speed_margin_kph,
        )
        candidate_count += 1
        if context["has_plausible_cause"]:
            context_explained_count += 1
        else:
            severity = round(max(decel_drop - trough_min_drop, 0.0) + max(abs(trough_accel) - abs(trough_accel_threshold), 0.0), 3)
            events.append(
                {
                    "start_timestamp": event_start,
                    "end_timestamp": event_end,
                    "duration": max(0.0, event_end - event_start),
                    "trough_timestamp": timestamp,
                    "min_acceleration": trough_accel,
                    "before_acceleration": before_ref,
                    "after_acceleration": after_ref,
                    "decel_drop": decel_drop,
                    "rebound": rebound,
                    "vehicle_speed": speed_values[index],
                    "context_summary": context["summary"],
                    "severity": severity,
                }
            )

        while index < len(axis) and float(axis[index]) <= event_end:
            index += 1

    max_event_severity = max((float(event["severity"]) for event in events), default=0.0)
    return {
        "metric_name": "acc_false_braking_context_count",
        "method": "acc_active_decel_trough_then_target_curve_context_check",
        "event_count": len(events),
        "candidate_count": candidate_count,
        "context_explained_count": context_explained_count,
        "missing_context_signals": missing_context_signals,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": max_event_severity,
        "parameters": {
            "smoothing_window": smoothing_window,
            "pre_trough_window_seconds": pre_trough_window_seconds,
            "post_trough_window_seconds": post_trough_window_seconds,
            "trough_accel_threshold": trough_accel_threshold,
            "trough_min_drop": trough_min_drop,
            "trough_min_rebound": trough_min_rebound,
            "min_speed_kph": min_speed_kph,
            "target_max_distance_m": target_max_distance_m,
            "target_ttc_seconds": target_ttc_seconds,
            "curve_speed_margin_kph": curve_speed_margin_kph,
        },
        "events": events,
    }


def _empty_result(start_timestamp: float, end_timestamp: float, smoothing_window: int) -> dict[str, object]:
    return {
        "metric_name": "acc_false_braking_context_count",
        "method": "acc_active_decel_trough_then_target_curve_context_check",
        "event_count": 0,
        "candidate_count": 0,
        "context_explained_count": 0,
        "missing_context_signals": [
            key for key in CONTEXT_SIGNAL_CANDIDATES if key not in DEFAULT_CONTEXT_SIGNAL_VALUES
        ],
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": 0.0,
        "parameters": {"smoothing_window": smoothing_window},
        "events": [],
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
        if signal_name:
            continue
        if key in DEFAULT_CONTEXT_SIGNAL_VALUES:
            continue
        if key == "lead_target_ttc" and resolved_context.get("lead_target_distance") and resolved_context.get("lead_target_velocity"):
            continue
        if key == "cutin_target_ttc" and resolved_context.get("cutin_target_distance") and resolved_context.get("cutin_target_velocity"):
            continue
        if key == "curvature" and resolved_context.get("curvature_left_c2") and resolved_context.get("curvature_right_c2"):
            continue
        missing.append(key)
    return missing


def _evaluate_context(
    *,
    session: FolderSession,
    resolved_context: dict[str, str | None],
    timestamp: float,
    vehicle_speed: float,
    target_max_distance_m: float,
    target_ttc_seconds: float,
    curve_speed_margin_kph: float,
) -> dict[str, object]:
    values = {
        key: _sample_optional(session, signal_name, timestamp)
        for key, signal_name in resolved_context.items()
        if signal_name
    }
    for key, value in DEFAULT_CONTEXT_SIGNAL_VALUES.items():
        if not resolved_context.get(key):
            values.setdefault(key, value)
    _apply_derived_context_values(values)

    reasons: list[str] = []
    target_role = _flt1_target_role(values.get("target_kind_flag"))
    if target_role in {None, "lead"}:
        lead_reason = _target_reason("lead", values, "lead_target", target_max_distance_m, target_ttc_seconds)
        if lead_reason:
            reasons.append(lead_reason)
    if target_role in {None, "cut-in"}:
        cutin_reason = _target_reason("cut-in", values, "cutin_target", target_max_distance_m, target_ttc_seconds)
        if cutin_reason:
            reasons.append(cutin_reason)

    curve_reason = _curve_limit_reason(values, vehicle_speed, curve_speed_margin_kph)
    if curve_reason:
        reasons.append(curve_reason)

    if reasons:
        return {"has_plausible_cause": True, "summary": "; ".join(reasons)}

    available = len(values)
    missing = len(resolved_context) - available
    if available == 0:
        summary = "no target/curve context signals available"
    else:
        summary = f"no target/curve cause matched; context_available={available}; context_missing={missing}"
    return {"has_plausible_cause": False, "summary": summary}


def _apply_derived_context_values(values: dict[str, object]) -> None:
    if "curvature" not in values:
        left = _float_or_none(values.get("curvature_left_c2"))
        right = _float_or_none(values.get("curvature_right_c2"))
        if left is not None and right is not None:
            denominator = left + right
            if math.isfinite(denominator) and abs(denominator) > 1e-12:
                values["curvature"] = 1.0 / denominator


def _flt1_target_role(flag_value: object | None) -> str | None:
    flag = _float_or_none(flag_value)
    if flag is None:
        return None
    return "cut-in" if int(round(flag)) == 1 else "lead"


def _target_reason(
    label: str,
    values: dict[str, object],
    prefix: str,
    max_distance_m: float,
    ttc_seconds: float,
) -> str:
    target_id = values.get(f"{prefix}_id")
    distance = _float_or_none(values.get(f"{prefix}_distance"))
    ttc = _float_or_none(values.get(f"{prefix}_ttc"))
    velocity = _float_or_none(values.get(f"{prefix}_velocity"))

    has_id = target_id is not None and _is_present_id(target_id)
    close_distance = distance is not None and 0.0 <= distance <= max_distance_m
    short_ttc = ttc is not None and 0.0 < ttc <= ttc_seconds
    closing_speed = velocity is not None and velocity < -1.0
    if has_id and (close_distance or short_ttc or closing_speed):
        parts = [f"{label} target"]
        if distance is not None:
            parts.append(f"distance={distance:.1f}m")
        if ttc is not None:
            parts.append(f"TTC={ttc:.1f}s")
        if velocity is not None:
            parts.append(f"velocity={velocity:.1f}")
        return " ".join(parts)
    return ""


def _curve_limit_reason(values: dict[str, object], vehicle_speed: float, speed_margin_kph: float) -> str:
    enabled = values.get("curvature_limit_enable")
    limit_speed = _float_or_none(values.get("curvature_limit_speed"))
    curvature = _float_or_none(values.get("curvature"))
    if _as_bool(enabled) and limit_speed is not None and limit_speed + speed_margin_kph < vehicle_speed:
        return f"curve speed limit active limit={limit_speed:.1f}kph speed={vehicle_speed:.1f}kph"
    if _as_bool(enabled) and curvature is not None and abs(curvature) > 0.003:
        return f"curve limit active curvature={curvature:.5f}"
    return ""


def _sample_optional(session: FolderSession, signal_name: str | None, timestamp: float) -> object | None:
    if not signal_name:
        return None
    try:
        return session.sample_signal_to_axis(signal_name, [timestamp], method="nearest")[0]
    except Exception:
        return None


def _moving_average(values: list[float], window: int) -> list[float]:
    if window <= 1 or len(values) <= 2:
        return values[:]
    smoothed: list[float] = []
    half = max(1, window // 2)
    for index in range(len(values)):
        start = max(0, index - half)
        end = min(len(values), index + half + 1)
        segment = values[start:end]
        smoothed.append(sum(segment) / len(segment))
    return smoothed


def _short_signal_name(signal_name: str) -> str:
    return str(signal_name or "").rsplit(".", 1)[-1].strip()


def _float_or_nan(value: object) -> float:
    result = _float_or_none(value)
    return result if result is not None else float("nan")


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        numeric = float(value)
        return math.isfinite(numeric) and numeric != 0
    text = str(value).strip().lower()
    return text not in {"0", "0.0", "false", "none", "nan", ""}


def _is_present_id(value: object) -> bool:
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    text = str(value).strip().lower()
    if text in {"", "0", "0.0", "-1", "none", "nan", "invalid", "no target", "no_target"}:
        return False
    return True
