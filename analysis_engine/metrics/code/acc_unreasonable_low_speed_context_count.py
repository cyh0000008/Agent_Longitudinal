from __future__ import annotations

import math
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from signal_lib import FolderSession, mask_to_segments


METRIC_CATEGORY = "MPI"

SPEED_SIGNAL = "PPEI_Vehicle_Speed_and_Distance_209_M1.IVehSpdAvgDrvn_1"
SET_SPEED_SIGNAL = "Adaptive_Cruise_Disp_Stat.IACCDrvrSeltdSpd"
ACC_ACTIVE_SIGNAL = "Adaptive_Cruise_Disp_Stat.IACCAct376"
PEDAL_OVERRIDE_SIGNAL = "PPEI_Propulsion_General_Data_1_0C2_M.IAccPdlOvrrdAtv"

DEFAULT_SET_SPEED_GAP_KPH = 5.0
DEFAULT_SPEED_DROP_KPH = 6.0
DEFAULT_SPEED_DROP_LOOKBACK_SECONDS = 5.0
DEFAULT_MIN_SCENE_DURATION_SECONDS = 1.5
DEFAULT_CONTEXT_SAMPLE_PADDING_SECONDS = 1.0
DEFAULT_SET_SPEED_DROP_KPH = 3.0
DEFAULT_TARGET_MAX_DISTANCE_M = 80.0
DEFAULT_TARGET_TTC_SECONDS = 5.0
DEFAULT_CURVE_SPEED_MARGIN_KPH = 3.0
DEFAULT_CONTEXT_SIGNAL_VALUES = {
    "target_kind_flag": 0.0,
}

REQUIRED_SIGNALS = [
    SPEED_SIGNAL,
    SET_SPEED_SIGNAL,
    ACC_ACTIVE_SIGNAL,
    PEDAL_OVERRIDE_SIGNAL,
]

SIGNAL_KEYS = {
    "speed": REQUIRED_SIGNALS[0],
    "set_speed": REQUIRED_SIGNALS[1],
    "acc_active": REQUIRED_SIGNALS[2],
    "pedal_override": REQUIRED_SIGNALS[3],
}

# Temporary names until the real bus signals are confirmed.
# Keep these aligned with config/vehicle_response_signal_catalog.xlsx.
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
    CONTEXT_SIGNAL_CANDIDATES["cutin_target_ttc"][0],
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

    result = count_acc_unreasonable_low_speed_context_events(session)
    candidate_count = int(result["candidate_count"])
    event_count = int(result["event_count"])
    explained_count = int(result["context_explained_count"])
    max_severity = float(result["max_event_severity"])

    stats["All Case Count"] = candidate_count
    stats["Bad Case"] = event_count
    stats["Severity Score"] = max_severity
    stats["Context Explained Count"] = explained_count
    stats["Candidate Event Count"] = len(result.get("candidate_events", []))
    stats["Missing Context Signal Count"] = len(result.get("missing_context_signals", []))

    result_lines.append(
        f"[ACC Unreasonable Low Speed Context] file={Path(session.primary_h5_file).name}, "
        f"candidate_count={candidate_count}, unexplained_count={event_count}, "
        f"context_explained_count={explained_count}, max_severity={max_severity:.3f}"
    )
    if result.get("missing_context_signals"):
        result_lines.append(
            "[Context Missing] "
            + ", ".join(str(item) for item in list(result["missing_context_signals"])[:12])
        )
    for event in result.get("candidate_events", []):
        prefix = "Event" if event.get("is_bad_case") else "Candidate"
        result_lines.append(
            f"[{prefix} start={event['start_timestamp']:.3f}, end={event['end_timestamp']:.3f}, "
            f"duration={event['duration']:.3f}, set_speed={event['set_speed']:.1f}, "
            f"min_speed={event['min_speed']:.1f}, set_speed_gap={event['max_set_speed_gap']:.1f}, "
            f"speed_drop={event['max_speed_drop']:.1f}, context={event['context_summary']}, "
            f"explained={event.get('is_explained')}, severity={event['severity']:.3f}]"
        )

    result_lines.insert(0, f"[Total Time] {time.time() - start_time:.4f}s")
    return event_count > 0, result_lines, stats


def count_acc_unreasonable_low_speed_context_events(
    session_or_h5_path: FolderSession | str | Path,
    set_speed_gap_kph: float = DEFAULT_SET_SPEED_GAP_KPH,
    speed_drop_kph: float = DEFAULT_SPEED_DROP_KPH,
    speed_drop_lookback_seconds: float = DEFAULT_SPEED_DROP_LOOKBACK_SECONDS,
    min_scene_duration_seconds: float = DEFAULT_MIN_SCENE_DURATION_SECONDS,
    context_sample_padding_seconds: float = DEFAULT_CONTEXT_SAMPLE_PADDING_SECONDS,
    set_speed_drop_kph: float = DEFAULT_SET_SPEED_DROP_KPH,
    target_max_distance_m: float = DEFAULT_TARGET_MAX_DISTANCE_M,
    target_ttc_seconds: float = DEFAULT_TARGET_TTC_SECONDS,
    curve_speed_margin_kph: float = DEFAULT_CURVE_SPEED_MARGIN_KPH,
) -> dict[str, object]:
    session = session_or_h5_path if isinstance(session_or_h5_path, FolderSession) else FolderSession(session_or_h5_path)
    start_timestamp, end_timestamp = session.common_time_range(REQUIRED_SIGNALS)

    axis, speed_raw = session.get_signal_slice(SIGNAL_KEYS["speed"], start_timestamp, end_timestamp)
    if len(axis) < 3:
        return _empty_result(start_timestamp, end_timestamp)

    actual_speed = [_float_or_nan(value) for value in speed_raw]
    display_speed = [_display_speed_from_actual(speed) for speed in actual_speed]
    set_speed_raw = [
        _float_or_nan(value)
        for value in session.sample_signal_to_axis(
            SIGNAL_KEYS["set_speed"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
        )
    ]
    set_speed = [_display_speed_from_actual(speed) for speed in set_speed_raw]
    acc_active = session.sample_signal_to_axis(
        SIGNAL_KEYS["acc_active"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    pedal_override = session.sample_signal_to_axis(
        SIGNAL_KEYS["pedal_override"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )

    valid_mask = [
        _as_bool(acc_on) and not _as_bool(override) and math.isfinite(selected_speed) and selected_speed > 0.0
        for acc_on, override, selected_speed in zip(acc_active, pedal_override, set_speed)
    ]
    set_speed_gap = [
        max(selected_speed - speed, 0.0) if valid and math.isfinite(speed) and math.isfinite(selected_speed) else 0.0
        for valid, speed, selected_speed in zip(valid_mask, display_speed, set_speed)
    ]
    speed_drop = _rolling_speed_drop(axis, display_speed, valid_mask, speed_drop_lookback_seconds)
    candidate_mask = [
        valid and (gap >= set_speed_gap_kph or drop >= speed_drop_kph)
        for valid, gap, drop in zip(valid_mask, set_speed_gap, speed_drop)
    ]

    resolved_context = _resolve_context_signals(session)
    missing_context_signals = _missing_context_signal_keys(resolved_context)
    candidate_count = 0
    context_explained_count = 0
    events: list[dict[str, float | str | bool]] = []
    candidate_events: list[dict[str, float | str | bool]] = []

    for segment in mask_to_segments(axis, candidate_mask):
        if segment.duration < min_scene_duration_seconds:
            continue

        indices = range(segment.start_index, segment.end_index + 1)
        ref_index = max(
            indices,
            key=lambda index: (
                max(set_speed_gap[index] - set_speed_gap_kph, 0.0)
                + max(speed_drop[index] - speed_drop_kph, 0.0)
            ),
        )
        scene_start = max(segment.start_timestamp - context_sample_padding_seconds, start_timestamp)
        scene_end = min(segment.end_timestamp + context_sample_padding_seconds, end_timestamp)
        context = _evaluate_context(
            session=session,
            resolved_context=resolved_context,
            timestamp=float(axis[ref_index]),
            scene_start=scene_start,
            scene_end=scene_end,
            vehicle_speed=display_speed[ref_index],
            set_speed=set_speed[ref_index],
            set_speed_drop_kph=set_speed_drop_kph,
            target_max_distance_m=target_max_distance_m,
            target_ttc_seconds=target_ttc_seconds,
            curve_speed_margin_kph=curve_speed_margin_kph,
        )

        max_gap = max(set_speed_gap[index] for index in indices)
        max_drop = max(speed_drop[index] for index in indices)
        min_speed = min(display_speed[index] for index in indices if math.isfinite(display_speed[index]))
        avg_set_speed = _safe_average(set_speed[index] for index in indices if math.isfinite(set_speed[index]))
        severity = round(max(max_gap - set_speed_gap_kph, 0.0) + max(max_drop - speed_drop_kph, 0.0), 3)
        is_explained = bool(context["has_plausible_cause"])
        candidate_count += 1
        if is_explained:
            context_explained_count += 1

        event = {
            "start_timestamp": segment.start_timestamp,
            "end_timestamp": segment.end_timestamp,
            "duration": segment.duration,
            "set_speed": avg_set_speed,
            "min_speed": min_speed,
            "max_set_speed_gap": max_gap,
            "max_speed_drop": max_drop,
            "context_summary": str(context["summary"]),
            "is_explained": is_explained,
            "is_bad_case": not is_explained,
            "severity": severity,
        }
        candidate_events.append(event)
        if not is_explained:
            events.append(event)

    max_event_severity = max((float(event["severity"]) for event in candidate_events), default=0.0)
    return {
        "metric_name": "acc_unreasonable_low_speed_context_count",
        "method": "acc_active_low_speed_or_speed_drop_then_target_curve_setspeed_context_check",
        "event_count": len(events),
        "candidate_count": candidate_count,
        "context_explained_count": context_explained_count,
        "missing_context_signals": missing_context_signals,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": max_event_severity,
        "parameters": {
            "set_speed_gap_kph": set_speed_gap_kph,
            "speed_drop_kph": speed_drop_kph,
            "speed_drop_lookback_seconds": speed_drop_lookback_seconds,
            "min_scene_duration_seconds": min_scene_duration_seconds,
            "context_sample_padding_seconds": context_sample_padding_seconds,
            "set_speed_drop_kph": set_speed_drop_kph,
            "target_max_distance_m": target_max_distance_m,
            "target_ttc_seconds": target_ttc_seconds,
            "curve_speed_margin_kph": curve_speed_margin_kph,
        },
        "events": events,
        "candidate_events": candidate_events,
    }


def _empty_result(start_timestamp: float, end_timestamp: float) -> dict[str, object]:
    return {
        "metric_name": "acc_unreasonable_low_speed_context_count",
        "method": "acc_active_low_speed_or_speed_drop_then_target_curve_setspeed_context_check",
        "event_count": 0,
        "candidate_count": 0,
        "context_explained_count": 0,
        "missing_context_signals": [
            key for key in CONTEXT_SIGNAL_CANDIDATES if key not in DEFAULT_CONTEXT_SIGNAL_VALUES
        ],
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_event_severity": 0.0,
        "parameters": {
            "set_speed_gap_kph": DEFAULT_SET_SPEED_GAP_KPH,
            "speed_drop_kph": DEFAULT_SPEED_DROP_KPH,
            "speed_drop_lookback_seconds": DEFAULT_SPEED_DROP_LOOKBACK_SECONDS,
            "min_scene_duration_seconds": DEFAULT_MIN_SCENE_DURATION_SECONDS,
        },
        "events": [],
        "candidate_events": [],
    }


def _rolling_speed_drop(
    axis: list[float],
    speed_values: list[float],
    valid_mask: list[bool],
    lookback_seconds: float,
) -> list[float]:
    drops: list[float] = []
    for index, timestamp in enumerate(axis):
        if not valid_mask[index] or not math.isfinite(speed_values[index]):
            drops.append(0.0)
            continue
        candidates = [
            speed_values[left]
            for left in range(index + 1)
            if valid_mask[left]
            and math.isfinite(speed_values[left])
            and float(timestamp) - lookback_seconds <= float(axis[left]) <= float(timestamp)
        ]
        if not candidates:
            drops.append(0.0)
            continue
        drops.append(max(max(candidates) - speed_values[index], 0.0))
    return drops


def _evaluate_context(
    *,
    session: FolderSession,
    resolved_context: dict[str, str | None],
    timestamp: float,
    scene_start: float,
    scene_end: float,
    vehicle_speed: float,
    set_speed: float,
    set_speed_drop_kph: float,
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
    set_speed_reason = _set_speed_drop_reason(session, scene_start, scene_end, set_speed_drop_kph)
    if set_speed_reason:
        reasons.append(set_speed_reason)

    target_role = _flt1_target_role(values.get("target_kind_flag"))
    if target_role in {None, "lead"}:
        lead_reason = _target_reason("lead", values, "lead_target", target_max_distance_m, target_ttc_seconds)
        if lead_reason:
            reasons.append(lead_reason)
    if target_role in {None, "cut-in"}:
        cutin_reason = _target_reason("cut-in", values, "cutin_target", target_max_distance_m, target_ttc_seconds)
        if cutin_reason:
            reasons.append(cutin_reason)

    curve_reason = _curve_limit_reason(values, vehicle_speed, set_speed, curve_speed_margin_kph)
    if curve_reason:
        reasons.append(curve_reason)

    if reasons:
        return {"has_plausible_cause": True, "summary": "; ".join(reasons)}

    available = len(values)
    missing = len(resolved_context) - available
    if available == 0:
        summary = "no lead/cut-in/curve context signals available and no set speed drop matched"
    else:
        summary = f"no lead/cut-in/curve/set-speed-drop cause matched; context_available={available}; context_missing={missing}"
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


def _set_speed_drop_reason(
    session: FolderSession,
    scene_start: float,
    scene_end: float,
    set_speed_drop_kph: float,
) -> str:
    pre_start = max(scene_start - DEFAULT_SPEED_DROP_LOOKBACK_SECONDS, 0.0)
    try:
        axis, values = session.get_signal_slice(SIGNAL_KEYS["set_speed"], pre_start, scene_end)
    except Exception:
        return ""
    if len(axis) < 2:
        return ""
    before_values = [
        _display_speed_from_actual(_float_or_nan(value))
        for timestamp, value in zip(axis, values)
        if float(timestamp) < scene_start
    ]
    scene_values = [
        _display_speed_from_actual(_float_or_nan(value))
        for timestamp, value in zip(axis, values)
        if scene_start <= float(timestamp) <= scene_end
    ]
    before_values = [value for value in before_values if math.isfinite(value)]
    scene_values = [value for value in scene_values if math.isfinite(value)]
    if not before_values or not scene_values:
        return ""
    previous_set_speed = max(before_values)
    current_set_speed = min(scene_values)
    drop = previous_set_speed - current_set_speed
    if drop >= set_speed_drop_kph:
        return f"set speed reduced {previous_set_speed:.1f}->{current_set_speed:.1f}kph"
    return ""


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


def _curve_limit_reason(
    values: dict[str, object],
    vehicle_speed: float,
    set_speed: float,
    speed_margin_kph: float,
) -> str:
    enabled = values.get("curvature_limit_enable")
    limit_speed = _float_or_none(values.get("curvature_limit_speed"))
    curvature = _float_or_none(values.get("curvature"))
    if _as_bool(enabled) and limit_speed is not None:
        if math.isfinite(set_speed) and limit_speed + speed_margin_kph < set_speed:
            return f"curve speed limit active limit={limit_speed:.1f}kph set_speed={set_speed:.1f}kph"
        if math.isfinite(vehicle_speed) and limit_speed + speed_margin_kph < vehicle_speed:
            return f"curve speed limit active limit={limit_speed:.1f}kph speed={vehicle_speed:.1f}kph"
    if _as_bool(enabled) and curvature is not None and abs(curvature) > 0.003:
        return f"curve limit active curvature={curvature:.5f}"
    return ""


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


def _sample_optional(session: FolderSession, signal_name: str | None, timestamp: float) -> object | None:
    if not signal_name:
        return None
    try:
        return session.sample_signal_to_axis(signal_name, [timestamp], method="nearest")[0]
    except Exception:
        return None


def _display_speed_from_actual(actual_speed: float) -> int:
    if not math.isfinite(actual_speed):
        return math.nan
    bias = _speed_bias_factor(actual_speed)
    display_speed = actual_speed * bias
    return _round_half_up(display_speed, 0)


def _speed_bias_factor(actual_speed: float) -> float:
    speed = max(float(actual_speed), 0.0)
    bias_x = [0.0, 40.0, 80.0, 120.0, 200.0, 300.0]
    bias_y = [1.0, 1.075, 1.052, 1.045, 1.039, 1.036]
    if speed <= bias_x[0]:
        return bias_y[0]
    if speed >= bias_x[-1]:
        return bias_y[-1]
    for index in range(1, len(bias_x)):
        if speed <= bias_x[index]:
            x0 = bias_x[index - 1]
            x1 = bias_x[index]
            y0 = bias_y[index - 1]
            y1 = bias_y[index]
            slope = (y1 - y0) / (x1 - x0)
            value = slope * (speed - x0) + y0
            return _round_half_up(value, 3)
    return bias_y[-1]


def _round_half_up(value: float, digits: int) -> float:
    quantize_str = "1" if digits == 0 else "1." + ("0" * digits)
    decimal_value = Decimal(str(value)).quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP)
    rounded = float(decimal_value)
    return int(rounded) if digits == 0 else rounded


def _safe_average(values: Any) -> float:
    collected = [float(value) for value in values if math.isfinite(float(value))]
    if not collected:
        return math.nan
    return sum(collected) / len(collected)


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
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


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
