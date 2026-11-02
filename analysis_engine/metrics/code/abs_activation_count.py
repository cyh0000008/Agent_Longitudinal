from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from signal_lib import FolderSession, mask_to_segments


METRIC_CATEGORY = "MPI"
ABS_ACTIVE_SIGNAL = "PPEI_Chassis_General_Data_3_0AC_M.IABSAtv"

ABS_ACTIVE_VALUES = {1}

REQUIRED_SIGNALS = [
    ABS_ACTIVE_SIGNAL,
]

SIGNAL_KEYS = {
    "abs_active": REQUIRED_SIGNALS[0],
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

    result = analyze_abs_activation_count(session)
    all_case_count = int(result["all_case_count"])
    bad_case_count = int(result["bad_case_count"])
    severity_score = float(result["severity_score"])

    stats["All Case Count"] = all_case_count
    stats["Bad Case"] = bad_case_count
    stats["Severity Score"] = severity_score

    result_lines.append(
        f"[ABS Activation Count] file={Path(session.primary_h5_file).name}, all_case_count={all_case_count}, "
        f"bad_case_count={bad_case_count}, severity_score={severity_score:.3f}"
    )
    for scene in result["scenes"]:
        result_lines.append(
            f"[Scene start={scene['start_timestamp']:.3f}, end={scene['end_timestamp']:.3f}, "
            f"duration={scene['duration']:.3f}, sample_count={scene['sample_count']}, severity={scene['severity']:.3f}]"
        )

    total_time = time.time() - start_time
    result_lines.insert(0, f"【运算总耗时】: {total_time:.4f} 秒")
    return bad_case_count > 0, result_lines, stats


def analyze_abs_activation_count(
    session_or_h5_path: FolderSession | str | Path,
) -> dict[str, object]:
    session = session_or_h5_path if isinstance(session_or_h5_path, FolderSession) else FolderSession(session_or_h5_path)
    start_timestamp, end_timestamp = session.common_time_range(REQUIRED_SIGNALS)

    axis, abs_active_raw = session.get_signal_slice(SIGNAL_KEYS["abs_active"], start_timestamp, end_timestamp)
    if len(axis) < 2:
        return _empty_result(start_timestamp, end_timestamp)

    abs_active_mask = [
        _matches_enum(value, ABS_ACTIVE_VALUES, {"1", "true", "active"})
        for value in abs_active_raw
    ]

    scenes = []
    severity_score = 0.0
    for segment in mask_to_segments(axis, abs_active_mask):
        severity = segment.duration
        severity_score = max(severity_score, severity)
        scenes.append(
            {
                "start_timestamp": segment.start_timestamp,
                "end_timestamp": segment.end_timestamp,
                "duration": segment.duration,
                "sample_count": segment.sample_count,
                "severity": severity,
            }
        )

    return {
        "metric_name": "abs_activation_count",
        "all_case_count": len(scenes),
        "bad_case_count": len(scenes),
        "severity_score": round(severity_score, 3),
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "parameters": {
            "abs_active_values": sorted(ABS_ACTIVE_VALUES),
        },
        "scenes": scenes,
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


def _empty_result(start_timestamp: float, end_timestamp: float) -> dict[str, object]:
    return {
        "metric_name": "abs_activation_count",
        "all_case_count": 0,
        "bad_case_count": 0,
        "severity_score": 0.0,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "scenes": [],
        "parameters": {
            "abs_active_values": sorted(ABS_ACTIVE_VALUES),
        },
    }
