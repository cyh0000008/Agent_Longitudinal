from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from signal_lib import FolderSession


METRIC_CATEGORY = "CPI"
PARKING_STATUS_SIGNAL = "Advanced_Park_Assist_Status_190_M.IAPASP_APAStsAuth"
APA_TYPE_SIGNAL = "APA_Brake_Propulsion_Torqu_1F0_M.IAPABT_APAType"

STATUS_READY_VALUE = 5  # apa guidance
STATUS_COMPLETE_VALUE = 6   # apa finish
APA_TYPE_VALUES = {1}
APA_STATUS_VALUES = {5, 6}

REQUIRED_SIGNALS = [
    PARKING_STATUS_SIGNAL,
    APA_TYPE_SIGNAL,
]

SIGNAL_KEYS = {
    "parking_status": REQUIRED_SIGNALS[0],
    "apa_type": REQUIRED_SIGNALS[1],
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

    result = analyze_apa_parking_completion_count(session)
    all_case_count = int(result["all_case_count"])
    bad_case_count = int(result["bad_case_count"])
    severity_score = float(result["severity_score"])

    stats["All Case Count"] = all_case_count
    stats["Bad Case"] = bad_case_count
    stats["Severity Score"] = severity_score

    result_lines.append(
        f"[APA Parking Completion Count] file={Path(session.primary_h5_file).name}, "
        f"all_case_count={all_case_count}, bad_case_count={bad_case_count}, severity_score={severity_score:.3f}"
    )
    for scene in result["scenes"]:
        result_lines.append(
            f"[Event timestamp={scene['timestamp']:.3f}, prev_status={scene['prev_status']}, "
            f"curr_status={scene['curr_status']}]"
        )

    total_time = time.time() - start_time
    result_lines.insert(0, f"【运算总耗时】: {total_time:.4f} 秒")
    return all_case_count > 0, result_lines, stats


def analyze_apa_parking_completion_count(
    session_or_h5_path: FolderSession | str | Path,
) -> dict[str, object]:
    session = session_or_h5_path if isinstance(session_or_h5_path, FolderSession) else FolderSession(session_or_h5_path)
    start_timestamp, end_timestamp = session.common_time_range(REQUIRED_SIGNALS)

    axis, parking_status_raw = session.get_signal_slice(SIGNAL_KEYS["parking_status"], start_timestamp, end_timestamp)
    apa_type = session.sample_signal_to_axis(
        SIGNAL_KEYS["apa_type"], axis, method="nearest", start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    if len(axis) < 2:
        return _empty_result(start_timestamp, end_timestamp)

    events = []
    for index in range(1, len(axis)):
        prev_status = _normalize_enum_value(parking_status_raw[index - 1])
        curr_status = _normalize_enum_value(parking_status_raw[index])
        prev_is_apa = _matches_enum(apa_type[index - 1], APA_TYPE_VALUES, {"1"})
        curr_is_apa = _matches_enum(apa_type[index], APA_TYPE_VALUES, {"1"})
        prev_status_in_apa = prev_status in APA_STATUS_VALUES
        curr_status_in_apa = curr_status in APA_STATUS_VALUES
        if prev_status == STATUS_READY_VALUE and curr_status == STATUS_COMPLETE_VALUE and prev_is_apa and curr_is_apa and prev_status_in_apa and curr_status_in_apa:
            events.append(
                {
                    "timestamp": float(axis[index]),
                    "prev_status": prev_status,
                    "curr_status": curr_status,
                }
            )

    event_count = len(events)
    return {
        "metric_name": "apa_parking_completion_count",
        "all_case_count": event_count,
        "bad_case_count": event_count,
        "severity_score": 0.0,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "parameters": {
            "status_ready_value": STATUS_READY_VALUE,
            "status_complete_value": STATUS_COMPLETE_VALUE,
            "apa_type_values": sorted(APA_TYPE_VALUES),
            "apa_status_values": sorted(APA_STATUS_VALUES),
        },
        "scenes": events,
    }


def _normalize_enum_value(value: object) -> int | None:
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


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
        "metric_name": "apa_parking_completion_count",
        "all_case_count": 0,
        "bad_case_count": 0,
        "severity_score": 0.0,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "scenes": [],
        "parameters": {
            "status_ready_value": STATUS_READY_VALUE,
            "status_complete_value": STATUS_COMPLETE_VALUE,
            "apa_type_values": sorted(APA_TYPE_VALUES),
            "apa_status_values": sorted(APA_STATUS_VALUES),
        },
    }
