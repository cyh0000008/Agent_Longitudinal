from __future__ import annotations

import time
import math
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from signal_lib import FolderSession, mask_to_segments


METRIC_CATEGORY = "MPI"
SPEED_SIGNAL = "PPEI_Vehicle_Speed_and_Distance_209_M1.IVehSpdAvgDrvn_1"
SET_SPEED_SIGNAL = "Adaptive_Cruise_Disp_Stat.IACCDrvrSeltdSpd"
ACC_ACTIVE_SIGNAL = "Adaptive_Cruise_Disp_Stat.IACCAct376"
PEDAL_OVERRIDE_SIGNAL = "PPEI_Propulsion_General_Data_1_0C2_M.IAccPdlOvrrdAtv"

DEFAULT_TARGET_BAND_KPH = 1.5
DEFAULT_MIN_SCENE_DURATION_SECONDS = 8.0
DEFAULT_STABLE_RANGE_KPH = 2.0
DEFAULT_REACH_MARGIN_KPH = 0.0
DEFAULT_UNDER_TARGET_GAP_KPH = 5.0
DEFAULT_UNDER_TARGET_MIN_SCENE_DURATION_SECONDS = 1.5

BIAS_X = [0.0, 40.0, 80.0, 120.0, 200.0, 300.0]
BIAS_Y = [1.0, 1.075, 1.052, 1.045, 1.039, 1.036]

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

    result = analyze_acc_set_speed_reach(session)
    all_case_count = int(result["all_case_count"])
    bad_case_count = int(result["bad_case_count"])
    severity_score = float(result["severity_score"])

    stats["All Case Count"] = all_case_count
    stats["Bad Case"] = bad_case_count
    stats["Severity Score"] = severity_score
    stats["Under Target Candidate Count"] = int(result.get("under_target_candidate_count", 0))

    result_lines.append(
        f"[ACC Set Speed Reach] file={Path(session.primary_h5_file).name}, all_case_count={all_case_count}, "
        f"bad_case_count={bad_case_count}, under_target_candidate_count={stats['Under Target Candidate Count']}, "
        f"severity_score={severity_score:.3f}"
    )
    for scene in result["scenes"]:
        prefix = "Candidate" if scene.get("scene_type") == "under_target_candidate" else "Scene"
        result_lines.append(
            f"[{prefix} start={scene['start_timestamp']:.3f}, end={scene['end_timestamp']:.3f}, "
            f"duration={scene['duration']:.3f}, set_speed={scene['set_speed']:.1f}, "
            f"max_display_speed={scene['max_display_speed']:.1f}, display_range={scene['display_range']:.1f}, "
            f"speed_gap={scene.get('speed_gap', 0.0):.1f}, reached={scene['reached']}, "
            f"scene_type={scene.get('scene_type')}, severity={scene['severity']:.3f}]"
        )

    total_time = time.time() - start_time
    result_lines.insert(0, f"【运算总耗时】: {total_time:.4f} 秒")
    return bad_case_count > 0, result_lines, stats


def analyze_acc_set_speed_reach(
    session_or_h5_path: FolderSession | str | Path,
    target_band_kph: float = DEFAULT_TARGET_BAND_KPH,
    min_scene_duration_seconds: float = DEFAULT_MIN_SCENE_DURATION_SECONDS,
    stable_range_kph: float = DEFAULT_STABLE_RANGE_KPH,
    reach_margin_kph: float = DEFAULT_REACH_MARGIN_KPH,
    under_target_gap_kph: float = DEFAULT_UNDER_TARGET_GAP_KPH,
    under_target_min_scene_duration_seconds: float = DEFAULT_UNDER_TARGET_MIN_SCENE_DURATION_SECONDS,
) -> dict[str, object]:
    session = session_or_h5_path if isinstance(session_or_h5_path, FolderSession) else FolderSession(session_or_h5_path)
    start_timestamp, end_timestamp = session.common_time_range(REQUIRED_SIGNALS)

    axis, speed_raw = session.get_signal_slice(SIGNAL_KEYS["speed"], start_timestamp, end_timestamp)
    if len(axis) < 2:
        return _empty_result(start_timestamp, end_timestamp)

    actual_speed = [float(value) for value in speed_raw]
    display_speed = [_display_speed_from_actual(speed) for speed in actual_speed]
    set_speed_raw = [float(value) for value in session.sample_signal_to_axis(SIGNAL_KEYS["set_speed"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp)]
    set_speed = [_display_speed_from_actual(speed) for speed in set_speed_raw]
    acc_active = session.sample_signal_to_axis(SIGNAL_KEYS["acc_active"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp)
    pedal_override = session.sample_signal_to_axis(SIGNAL_KEYS["pedal_override"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp)

    base_mask = [
        _as_bool(acc_on) and (not _as_bool(override)) and float(selected_speed_raw) > 0.0
        for acc_on, override, selected_speed_raw in zip(acc_active, pedal_override, set_speed_raw)
    ]
    near_mask = [
        is_base and abs(disp_speed - selected_speed) <= target_band_kph
        for is_base, disp_speed, selected_speed in zip(base_mask, display_speed, set_speed)
    ]
    under_target_mask = [
        is_base and math_is_finite_pair(disp_speed, selected_speed) and selected_speed - disp_speed >= under_target_gap_kph
        for is_base, disp_speed, selected_speed in zip(base_mask, display_speed, set_speed)
    ]

    scenes = []
    bad_case_count = 0
    under_target_candidate_count = 0
    severity_score = 0.0
    for segment in mask_to_segments(axis, near_mask):
        if segment.duration < min_scene_duration_seconds:
            continue
        start_index = segment.start_index
        end_index = segment.end_index
        display_window = display_speed[start_index : end_index + 1]
        set_speed_window = set_speed[start_index : end_index + 1]
        if not display_window or not set_speed_window:
            continue

        display_range = max(display_window) - min(display_window)
        set_speed_range = max(set_speed_window) - min(set_speed_window)
        if display_range > stable_range_kph or set_speed_range > 1e-6:
            continue

        scene_set_speed = sum(set_speed_window) / len(set_speed_window)
        max_display_speed = max(display_window)
        reached = any(speed_value >= scene_set_speed - reach_margin_kph for speed_value in display_window)
        severity = 0.0 if reached else max(scene_set_speed - max_display_speed, 0.0)
        if not reached:
            bad_case_count += 1
            severity_score = max(severity_score, severity)

        scenes.append(
            {
                "scene_type": "near_set_speed_stability",
                "start_timestamp": segment.start_timestamp,
                "end_timestamp": segment.end_timestamp,
                "duration": segment.duration,
                "sample_count": segment.sample_count,
                "set_speed": scene_set_speed,
                "max_display_speed": max_display_speed,
                "min_display_speed": min(display_window),
                "display_range": display_range,
                "speed_gap": max(scene_set_speed - max_display_speed, 0.0),
                "reached": reached,
                "severity": severity,
            }
        )

    for segment in mask_to_segments(axis, under_target_mask):
        if segment.duration < under_target_min_scene_duration_seconds:
            continue
        start_index = segment.start_index
        end_index = segment.end_index
        display_window = display_speed[start_index : end_index + 1]
        set_speed_window = set_speed[start_index : end_index + 1]
        if not display_window or not set_speed_window:
            continue

        scene_set_speed = sum(set_speed_window) / len(set_speed_window)
        max_display_speed = max(display_window)
        min_display_speed = min(display_window)
        max_gap = max(
            selected_speed - disp_speed
            for disp_speed, selected_speed in zip(display_window, set_speed_window)
            if math_is_finite_pair(disp_speed, selected_speed)
        )
        severity = max(max_gap - under_target_gap_kph, 0.0)
        under_target_candidate_count += 1
        severity_score = max(severity_score, severity)
        scenes.append(
            {
                "scene_type": "under_target_candidate",
                "start_timestamp": segment.start_timestamp,
                "end_timestamp": segment.end_timestamp,
                "duration": segment.duration,
                "sample_count": segment.sample_count,
                "set_speed": scene_set_speed,
                "max_display_speed": max_display_speed,
                "min_display_speed": min_display_speed,
                "display_range": max_display_speed - min_display_speed,
                "speed_gap": max_gap,
                "reached": False,
                "severity": severity,
            }
        )

    return {
        "metric_name": "acc_set_speed_reach",
        "all_case_count": len(scenes),
        "bad_case_count": bad_case_count,
        "under_target_candidate_count": under_target_candidate_count,
        "severity_score": round(severity_score, 3),
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "parameters": {
            "target_band_kph": target_band_kph,
            "min_scene_duration_seconds": min_scene_duration_seconds,
            "stable_range_kph": stable_range_kph,
            "reach_margin_kph": reach_margin_kph,
            "under_target_gap_kph": under_target_gap_kph,
            "under_target_min_scene_duration_seconds": under_target_min_scene_duration_seconds,
        },
        "scenes": scenes,
    }


def _display_speed_from_actual(actual_speed: float) -> int:
    bias = _speed_bias_factor(actual_speed)
    display_speed = actual_speed * bias
    return _round_half_up(display_speed, 0)


def _speed_bias_factor(actual_speed: float) -> float:
    speed = max(float(actual_speed), 0.0)
    if speed <= BIAS_X[0]:
        return BIAS_Y[0]
    if speed >= BIAS_X[-1]:
        return BIAS_Y[-1]
    for index in range(1, len(BIAS_X)):
        if speed <= BIAS_X[index]:
            x0 = BIAS_X[index - 1]
            x1 = BIAS_X[index]
            y0 = BIAS_Y[index - 1]
            y1 = BIAS_Y[index]
            slope = (y1 - y0) / (x1 - x0)
            value = slope * (speed - x0) + y0
            return _round_half_up(value, 3)
    return BIAS_Y[-1]


def _round_half_up(value: float, digits: int) -> float:
    quantize_str = '1' if digits == 0 else '1.' + ('0' * digits)
    decimal_value = Decimal(str(value)).quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP)
    rounded = float(decimal_value)
    return int(rounded) if digits == 0 else rounded


def _empty_result(start_timestamp: float, end_timestamp: float) -> dict[str, object]:
    return {
        "metric_name": "acc_set_speed_reach",
        "all_case_count": 0,
        "bad_case_count": 0,
        "under_target_candidate_count": 0,
        "severity_score": 0.0,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "scenes": [],
        "parameters": {
            "target_band_kph": DEFAULT_TARGET_BAND_KPH,
            "min_scene_duration_seconds": DEFAULT_MIN_SCENE_DURATION_SECONDS,
            "stable_range_kph": DEFAULT_STABLE_RANGE_KPH,
            "reach_margin_kph": DEFAULT_REACH_MARGIN_KPH,
            "under_target_gap_kph": DEFAULT_UNDER_TARGET_GAP_KPH,
            "under_target_min_scene_duration_seconds": DEFAULT_UNDER_TARGET_MIN_SCENE_DURATION_SECONDS,
        },
    }


def math_is_finite_pair(first: float, second: float) -> bool:
    return math.isfinite(first) and math.isfinite(second)


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    return text not in {"0", "false", "none", ""}
