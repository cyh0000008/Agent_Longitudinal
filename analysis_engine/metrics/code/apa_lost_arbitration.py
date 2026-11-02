from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from signal_lib import FolderSession, mask_to_segments


METRIC_CATEGORY = "CPI"
APA_TYPE_SIGNAL = "APA_Brake_Propulsion_Torqu_1F0_M.IAPABT_APAType"
APA_STATUS_SIGNAL = "Advanced_Park_Assist_Status_190_M.IAPASP_APAStsAuth"
PROPULSION_RESPONSE_SIGNAL = "PPEI_Trans_General_Status_2_ECP_H1_0D7_M.ISuprRmtPrkPrplSysResp"

DEFAULT_MIN_BAD_CASE_DURATION_SECONDS = 5.0
APA_TYPE_VALUES = {1}
APA_STATUS_VALUES = {5, 6}
ARBITRATION_FAILED_VALUES = {2}

REQUIRED_SIGNALS = [
    APA_TYPE_SIGNAL,
    APA_STATUS_SIGNAL,
    PROPULSION_RESPONSE_SIGNAL,
]

SIGNAL_KEYS = {
    "apa_type": REQUIRED_SIGNALS[0],
    "apa_status": REQUIRED_SIGNALS[1],
    "propulsion_response": REQUIRED_SIGNALS[2],
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

    result = analyze_apa_lost_arbitration(session)
    all_case_count = int(result["all_case_count"])
    bad_case_count = int(result["bad_case_count"])
    severity_score = float(result["severity_score"])

    stats["All Case Count"] = all_case_count
    stats["Bad Case"] = bad_case_count
    stats["Severity Score"] = severity_score

    result_lines.append(
        f"[APA Lost Arbitration] file={Path(session.primary_h5_file).name}, all_case_count={all_case_count}, "
        f"bad_case_count={bad_case_count}, severity_score={severity_score:.3f}"
    )
    for scene in result["scenes"]:
        result_lines.append(
            f"[Scene start={scene['start_timestamp']:.3f}, end={scene['end_timestamp']:.3f}, "
            f"duration={scene['duration']:.3f}, severity={scene['severity']:.3f}]"
        )

    total_time = time.time() - start_time
    result_lines.insert(0, f"【运算总耗时】: {total_time:.4f} 秒")
    return bad_case_count > 0, result_lines, stats


def analyze_apa_lost_arbitration(
    session_or_h5_path: FolderSession | str | Path,
    min_bad_case_duration_seconds: float = DEFAULT_MIN_BAD_CASE_DURATION_SECONDS,
) -> dict[str, object]:
    session = session_or_h5_path if isinstance(session_or_h5_path, FolderSession) else FolderSession(session_or_h5_path)
    start_timestamp, end_timestamp = session.common_time_range(REQUIRED_SIGNALS)

    axis, apa_status_raw = session.get_signal_slice(SIGNAL_KEYS["apa_status"], start_timestamp, end_timestamp)
    if len(axis) < 2:
        return _empty_result(start_timestamp, end_timestamp)

    propulsion_response = session.sample_signal_to_axis(
        SIGNAL_KEYS["propulsion_response"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    apa_type = session.sample_signal_to_axis(
        SIGNAL_KEYS["apa_type"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )

    lost_arbitration_mask = [
        _matches_enum(apa_type_value, APA_TYPE_VALUES, {"1"})
        and _matches_enum(apa_status, APA_STATUS_VALUES, {"5", "6", "apa guidance", "guidance", "active", "apa finish", "finish"})
        and _matches_enum(response, ARBITRATION_FAILED_VALUES, {"arbitration failed"})
        for apa_status, apa_type_value, response in zip(apa_status_raw, apa_type, propulsion_response)
    ]

    scenes = []
    severity_score = 0.0
    for segment in mask_to_segments(axis, lost_arbitration_mask):
        if segment.duration < min_bad_case_duration_seconds:
            continue
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
        "metric_name": "apa_lost_arbitration",
        "all_case_count": len(scenes),
        "bad_case_count": len(scenes),
        "severity_score": round(severity_score, 3),
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "parameters": {
            "min_bad_case_duration_seconds": min_bad_case_duration_seconds,
            "apa_type_values": sorted(APA_TYPE_VALUES),
            "apa_status_values": sorted(APA_STATUS_VALUES),
            "arbitration_failed_values": sorted(ARBITRATION_FAILED_VALUES),
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
        "metric_name": "apa_lost_arbitration",
        "all_case_count": 0,
        "bad_case_count": 0,
        "severity_score": 0.0,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "scenes": [],
        "parameters": {
            "min_bad_case_duration_seconds": DEFAULT_MIN_BAD_CASE_DURATION_SECONDS,
            "apa_type_values": sorted(APA_TYPE_VALUES),
            "apa_status_values": sorted(APA_STATUS_VALUES),
            "arbitration_failed_values": sorted(ARBITRATION_FAILED_VALUES),
        },
    }
