from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from asammdf import MDF
from PIL import Image, ImageDraw, ImageFont

from FileConverter.blf_to_mdf import (
    GroupBuffer,
    build_channel_routes,
    build_unique_signal_ownership,
    channel_group_name,
    extract_signal as extract_can_signal,
    iter_blf_messages,
    load_frames,
    make_payload_array,
    normalize_channel,
    select_frames,
    supports_short_payload,
)
from runtime_paths import (
    ensure_writable_dir,
    ensure_writable_file_path,
    get_runtime_subdir,
    get_runtime_subprocess_env,
    safe_runtime_name,
)


ROOT = Path(__file__).resolve().parent
DATABASE_DIR = ROOT / "database"
CONVERTER_SCRIPT = ROOT / "FileConverter" / "blf_to_mdf.py"
CONVERTER_PROGRESS_PREFIX = "BLF_MDF_PROGRESS "

DEFAULT_ECM_DATABASE_NAME = "Clea_Mode_Year23_ECM_V27.80.1.arxml"
DEFAULT_ABS_ERROR_THRESHOLD_NM = float(os.getenv("AXLE_TORQUE_ABS_ERROR_THRESHOLD_NM", "120"))
DEFAULT_MAE_ERROR_THRESHOLD_NM = float(os.getenv("AXLE_TORQUE_MAE_ERROR_THRESHOLD_NM", "90"))
DEFAULT_P95_ERROR_THRESHOLD_NM = float(os.getenv("AXLE_TORQUE_P95_ERROR_THRESHOLD_NM", "240"))
DEFAULT_PASS_RATE_THRESHOLD = float(os.getenv("AXLE_TORQUE_PASS_RATE_THRESHOLD", "0.78"))
DEFAULT_REL_ERROR_THRESHOLD = float(os.getenv("AXLE_TORQUE_REL_ERROR_THRESHOLD", "0.15"))
DEFAULT_OUTPUT_TORQUE_SCALE = float(os.getenv("AXLE_TORQUE_OUTPUT_SCALE", "1"))
REVERSE_GEAR_CODE = 14
REVERSE_GEAR_PLOT_VALUE = -1
VEHICLE_FINAL_DRIVE_RATIOS = {
    "358-2": 3.47,
    "C1UL": 3.47,
    "E2LB-2": 3.17,
}
VEHICLE_SELECTION_NUMBERS = {
    "1": "358-2",
    "2": "C1UL",
    "3": "E2LB-2",
}
VEHICLE_CONFIG_ALIASES = {
    "358-2": "358-2",
    "358": "358-2",
    "c1ul": "C1UL",
    "c1ul/358-2": "358-2",
    "c1ul 358-2": "358-2",
    "e2lb": "E2LB-2",
    "e2lb-2": "E2LB-2",
    "e2lb/2": "E2LB-2",
}

TARGET_MESSAGES = (
    "PTEI_Brake_Apply_Status_071_M",
    "ICE_PTEI_Brake_Apply_Status_071_M",
    "PPEI_Engine_General_Status_1_073_M1",
    "PTEI_Transmission_General_Status_0CB_M",
    "PPEI_Trans_General_Status_2_TCM_0C8_M2",
    "PPEI_Trans_General_Status_2_ECP_H1_0D7_M",
    "PTEI_Transmission_Torque_Request_076_M",
    "PTEI_Transmission_Torque_Request_078_M",
    "PPEI_Engine_Torque_Status_4_08D_M",
)

TC_RATIO_POINTS = (
    (0.000000, 1.96484),
    (0.062500, 1.89697),
    (0.125000, 1.82324),
    (0.187500, 1.74707),
    (0.250000, 1.67090),
    (0.312500, 1.59717),
    (0.375000, 1.52588),
    (0.437500, 1.45605),
    (0.500000, 1.38721),
    (0.562500, 1.32324),
    (0.625000, 1.26123),
    (0.687500, 1.20313),
    (0.750000, 1.14600),
    (0.812500, 1.07910),
    (0.875000, 1.00977),
    (0.937500, 1.00000),
    (0.999969, 1.00000),
)

GEAR_RATIO = {
    1: 4.6895,
    2: 3.3066,
    3: 3.0117,
    4: 2.4453,
    5: 1.9238,
    6: 1.4453,
    7: 1.0000,
    8: 0.7461,
    9: 0.6172,
    REVERSE_GEAR_CODE: 2.9600,
}

TRANS_EFFICIENCY = {
    1: 0.97961,
    2: 0.97668,
    3: 0.97653,
    4: 0.97961,
    5: 0.98414,
    6: 0.99371,
    7: 1.00000,
    8: 0.99445,
    9: 0.99298,
    REVERSE_GEAR_CODE: 1.00000,
}

MIN_ENGINE_SPEED_RPM = 50.0
MAX_SAMPLE_GAP_SECONDS = 0.25


@dataclass
class SignalSeries:
    name: str
    timestamps: np.ndarray
    values: np.ndarray
    group: str = ""


@dataclass
class DecodedSignalSeries:
    name: str
    timestamps: np.ndarray
    raw_values: np.ndarray
    physical_values: np.ndarray
    group: str = ""


@dataclass
class AxleTorqueMetrics:
    valid_samples: int = 0
    total_axis_samples: int = 0
    coverage: float = 0.0
    mae_nm: float | None = None
    rmse_nm: float | None = None
    p95_abs_error_nm: float | None = None
    max_abs_error_nm: float | None = None
    mean_error_nm: float | None = None
    median_relative_error: float | None = None
    pass_rate: float = 0.0
    zero_lag_mae_nm: float | None = None
    best_lag_seconds: float = 0.0
    best_lag_mae_nm: float | None = None
    output_torque_scale: float = DEFAULT_OUTPUT_TORQUE_SCALE
    final_drive_ratio: float = 0.0
    max_actual_nm: float | None = None
    max_calculated_nm: float | None = None
    min_actual_nm: float | None = None
    min_calculated_nm: float | None = None


@dataclass
class AxleTorqueResult:
    status: str
    verdict: str
    summary: str
    blf_path: str
    arxml_path: str
    output_dir: str
    vehicle_config: str = ""
    final_drive_ratio: float = 0.0
    mdf_path: str = ""
    summary_json_path: str = ""
    report_json_path: str = ""
    plot_path: str = ""
    metrics: AxleTorqueMetrics = field(default_factory=AxleTorqueMetrics)
    signal_sources: dict[str, str] = field(default_factory=dict)
    missing_signals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    abnormal_windows: list[dict[str, float]] = field(default_factory=list)
    conversion_summary: dict[str, Any] = field(default_factory=dict)


def find_default_ecm_arxml() -> Path | None:
    env_path = os.getenv("AXLE_TORQUE_ARXML", "").strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        if candidate.exists() and candidate.suffix.lower() == ".arxml":
            return candidate

    preferred = DATABASE_DIR / DEFAULT_ECM_DATABASE_NAME
    if preferred.exists():
        return preferred

    candidates = list(DATABASE_DIR.glob("*ECM*.arxml")) if DATABASE_DIR.exists() else []
    if candidates:
        return max(candidates, key=_arxml_priority)

    legacy_path = Path(r"D:\PATAC\数据库\27.80.1\27.80.1-CAN\Database\ARXML") / DEFAULT_ECM_DATABASE_NAME
    if os.name == "nt" and legacy_path.exists():
        return legacy_path
    return None


def _arxml_priority(path: Path) -> tuple[tuple[int, ...], str]:
    version = (0,)
    text = path.name
    import re

    match = re.search(r"V(\d+(?:\.\d+)*)", text, re.IGNORECASE)
    if match:
        version = tuple(int(part) for part in match.group(1).split("."))
    return version, text.lower()


def normalize_vehicle_config(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if text in VEHICLE_FINAL_DRIVE_RATIOS:
        return text
    normalized = text.lower().replace("\\", "/").replace("_", "-")
    normalized = " ".join(normalized.split())
    number_text = normalized.strip(".、)）")
    if number_text in VEHICLE_SELECTION_NUMBERS:
        return VEHICLE_SELECTION_NUMBERS[number_text]
    compact = normalized.replace(" ", "")
    for key, label in VEHICLE_CONFIG_ALIASES.items():
        key_norm = key.lower().replace("\\", "/").replace("_", "-")
        if normalized == key_norm or compact == key_norm.replace(" ", ""):
            return label
    return None


def detect_vehicle_config(text: str | None) -> str | None:
    if not text:
        return None
    lowered = str(text).lower().replace("\\", "/").replace("_", "-")
    normalized = " ".join(lowered.split())
    number_text = normalized.strip(".、)）")
    if number_text in VEHICLE_SELECTION_NUMBERS:
        return VEHICLE_SELECTION_NUMBERS[number_text]
    match = re.fullmatch(r"(?:选|选择|车型|车号|编号)?\s*([123])\s*[.、)）]?", normalized)
    if match:
        return VEHICLE_SELECTION_NUMBERS[match.group(1)]
    for key, label in VEHICLE_CONFIG_ALIASES.items():
        if key.lower() in lowered:
            return label
    for label in VEHICLE_FINAL_DRIVE_RATIOS:
        if label.lower() in lowered:
            return label
    return None


def vehicle_final_drive_ratio(vehicle_config: str | None) -> float:
    label = normalize_vehicle_config(vehicle_config)
    if not label:
        raise ValueError("请选择轴端扭矩校验车型：1=358-2，2=C1UL，3=E2LB-2；其他车型请提供 Final Ratio。")
    return float(VEHICLE_FINAL_DRIVE_RATIOS[label])


def _emit_progress(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    payload: dict[str, Any],
) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback(dict(payload))
    except Exception as exc:
        print(f"Axle torque progress callback failed: {exc}", flush=True)


def run_axle_torque_accuracy_analysis(
    blf_path: str | Path,
    arxml_path: str | Path | None = None,
    *,
    output_dir: str | Path | None = None,
    max_frames: int = 0,
    vehicle_config: str | None = None,
    final_drive_ratio: float | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> AxleTorqueResult:
    blf_path = Path(blf_path).expanduser()
    if not blf_path.is_absolute():
        blf_path = blf_path.resolve()
    if not blf_path.exists() or blf_path.stat().st_size <= 0:
        raise FileNotFoundError(f"BLF file not found or empty: {blf_path}")
    if blf_path.suffix.lower() != ".blf":
        raise ValueError("Axle torque accuracy analysis only accepts .blf input.")

    if arxml_path is None:
        resolved_arxml = find_default_ecm_arxml()
        if resolved_arxml is None:
            raise FileNotFoundError(
                "ECM ARXML not found. Put Clea_Mode_Year23_ECM_V27.80.1.arxml "
                "or a newer ECM ARXML in the Agent database directory, or set AXLE_TORQUE_ARXML."
            )
        arxml = resolved_arxml
    else:
        arxml = Path(arxml_path).expanduser()
        if not arxml.is_absolute():
            arxml = arxml.resolve()
    if not arxml.exists() or arxml.stat().st_size <= 0:
        raise FileNotFoundError(f"ARXML file not found or empty: {arxml}")

    resolved_vehicle_config = normalize_vehicle_config(vehicle_config)
    if final_drive_ratio is None:
        final_drive_ratio = vehicle_final_drive_ratio(resolved_vehicle_config)
    else:
        final_drive_ratio = float(final_drive_ratio)
    if not resolved_vehicle_config:
        resolved_vehicle_config = f"自定义主减速比 {final_drive_ratio:g}"

    output_root = Path(output_dir).expanduser() if output_dir else get_runtime_subdir("axle_torque_accuracy")
    if not output_root.is_absolute():
        output_root = output_root.resolve()
    run_dir = ensure_writable_dir(
        output_root
        / f"{safe_runtime_name(blf_path.stem, 'blf')}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    )

    result = AxleTorqueResult(
        status="error",
        verdict="无法判断",
        summary="",
        blf_path=str(blf_path),
        arxml_path=str(arxml),
        output_dir=str(run_dir),
        vehicle_config=resolved_vehicle_config,
        final_drive_ratio=float(final_drive_ratio),
    )

    summary_path, conversion_summary, decoded_signals = _decode_target_messages_from_blf(
        blf_path,
        arxml,
        run_dir,
        max_frames=max_frames,
        progress_callback=progress_callback,
    )
    result.summary_json_path = str(summary_path)
    result.conversion_summary = conversion_summary

    _emit_progress(progress_callback, {"stage": "extract", "message": "reading_decoded_blf_signals"})
    extracted = _extract_required_signals_from_store(decoded_signals)
    result.signal_sources.update(extracted["sources"])
    result.missing_signals.extend(extracted["missing"])
    result.warnings.extend(extracted["warnings"])

    if result.missing_signals:
        result.status = "insufficient_data"
        result.verdict = "证据不足"
        result.summary = "缺少轴端扭矩复算/对比的关键信号，无法判断准确性。"
        _write_report(result, run_dir)
        _emit_progress(progress_callback, {"stage": "done", "percent": 100.0})
        return result

    _emit_progress(progress_callback, {"stage": "compute", "message": "calculating_axle_torque"})
    analysis = _compute_accuracy(extracted["signals"], result.warnings, final_drive_ratio=float(final_drive_ratio))

    result.metrics = analysis["metrics"]
    result.abnormal_windows = analysis["abnormal_windows"]
    result.status = analysis["status"]
    result.verdict = analysis["verdict"]
    result.summary = analysis["summary"]
    _emit_progress(progress_callback, {"stage": "plot", "message": "rendering_plot"})
    result.plot_path = str(
        _render_plot(
            run_dir / "axle_torque_accuracy.png",
            analysis["plot_data"],
            result.verdict,
            result.metrics,
        )
    )
    _write_report(result, run_dir)
    _emit_progress(progress_callback, {"stage": "done", "percent": 100.0})
    return result


def _decode_target_messages_from_blf(
    blf_path: Path,
    arxml_path: Path,
    output_dir: Path,
    *,
    max_frames: int = 0,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[Path, dict[str, Any], dict[str, list[DecodedSignalSeries]]]:
    frames = load_frames(arxml_path)
    selected = select_frames(frames, set(TARGET_MESSAGES), set(), set())
    if not selected:
        raise RuntimeError("No ECM ARXML target messages matched the axle torque filters.")

    progress_interval = float(os.getenv("AXLE_TORQUE_PROGRESS_INTERVAL_SECONDS", "2") or "2")
    last_progress_emit: dict[str, float] = {}

    def emit_progress(
        stage: str,
        *,
        message: Any | None = None,
        total_frames: int = 0,
        candidate_frames: int = 0,
        matched_frames: int = 0,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if not force and now - last_progress_emit.get(stage, 0.0) < max(0.0, progress_interval):
            return
        last_progress_emit[stage] = now
        file_position = int(getattr(message, "file_position", 0) or 0) if message is not None else 0
        file_size = int(getattr(message, "file_size", 0) or 0) if message is not None else 0
        percent = None
        if file_size > 0:
            percent = max(0.0, min(100.0, file_position * 100.0 / file_size))
        _emit_progress(
            progress_callback,
            {
                "stage": stage,
                "percent": None if percent is None else round(percent, 2),
                "file_position": file_position,
                "file_size": file_size,
                "total_frames": int(total_frames or 0),
                "candidate_frames": int(candidate_frames or 0),
                "matched_frames": int(matched_frames or 0),
            },
        )

    observed: Counter = Counter()
    scanned_frames = 0
    last_scanned_msg = None
    emit_progress("scan", force=True)
    for msg in iter_blf_messages(blf_path).messages:
        if max_frames and scanned_frames >= max_frames:
            break
        scanned_frames += 1
        last_scanned_msg = msg
        emit_progress("scan", message=msg, total_frames=scanned_frames)
        observed[
            (
                normalize_channel(msg.channel),
                int(msg.arbitration_id),
                bool(msg.is_extended_id),
                len(bytes(msg.data)),
            )
        ] += 1
    emit_progress("scan", message=last_scanned_msg, total_frames=scanned_frames, force=True)

    routes, route_diagnostics = build_channel_routes(
        selected,
        observed,
        "exact-first",
        True,
        True,
    )

    groups: dict[tuple[object, int], GroupBuffer] = {}
    total_frames = 0
    candidate_frames = 0
    matched_frames = 0
    skipped_short = 0
    skipped_excess = 0
    first_timestamp: float | None = None
    base_offset = 0.0
    last_converted_msg = None

    emit_progress("convert", force=True)
    for msg in iter_blf_messages(blf_path).messages:
        if max_frames and total_frames >= max_frames:
            break
        total_frames += 1
        last_converted_msg = msg
        emit_progress(
            "convert",
            message=msg,
            total_frames=total_frames,
            candidate_frames=candidate_frames,
            matched_frames=matched_frames,
        )
        if first_timestamp is None:
            first_timestamp = float(msg.timestamp)

        payload = bytes(msg.data)
        route_key = (
            normalize_channel(msg.channel),
            int(msg.arbitration_id),
            bool(msg.is_extended_id),
            len(payload),
        )
        frame_def = routes.get(route_key)
        if frame_def is None:
            continue

        candidate_frames += 1
        if len(payload) < frame_def.size and not supports_short_payload(frame_def, len(payload)):
            # Target analysis intentionally mirrors the former converter options:
            # short target frames are zero-padded, not discarded.
            pass
        if len(payload) > frame_def.size:
            # Excess payload is allowed; the ARXML frame width still controls the
            # decoded signal layout.
            pass

        group_channel = normalize_channel(msg.channel)
        key = (group_channel, frame_def.ordinal)
        if key not in groups:
            groups[key] = GroupBuffer(frame_def, group_channel, [], [])
        groups[key].timestamps.append(float(msg.timestamp) - base_offset)
        groups[key].payloads.append(payload)
        matched_frames += 1
    emit_progress(
        "convert",
        message=last_converted_msg,
        total_frames=total_frames,
        candidate_frames=candidate_frames,
        matched_frames=matched_frames,
        force=True,
    )

    if first_timestamp is None:
        raise RuntimeError("The BLF file did not contain any frames.")

    allowed_signals, ownership_diagnostics = build_unique_signal_ownership(
        groups,
        route_diagnostics,
    )
    decoded_signals: dict[str, list[DecodedSignalSeries]] = defaultdict(list)
    skipped_signals: dict[str, list[str]] = {}
    group_summaries: list[dict[str, Any]] = []
    written_frame_samples = 0

    for key in sorted(groups, key=lambda item: (groups[item].frame_def.ordinal, str(groups[item].channel))):
        group = groups[key]
        group_allowed_signals = allowed_signals.get(key, set())
        if not group_allowed_signals:
            continue
        decoded_count, skipped = _decode_group_to_signal_store(
            decoded_signals,
            group,
            group_allowed_signals,
        )
        name = channel_group_name(group.channel, group.frame_def, False)
        if skipped:
            skipped_signals[name] = skipped
        if decoded_count:
            written_frame_samples += len(group.timestamps)
            payload_lengths = {
                str(length): sum(1 for payload in group.payloads if len(payload) == length)
                for length in sorted({len(payload) for payload in group.payloads})
            }
            group_summaries.append(
                {
                    "name": name,
                    "frame_id": f"0x{group.frame_def.frame_id:X}",
                    "extended": group.frame_def.extended,
                    "channel": normalize_channel(group.channel),
                    "arxml_cluster": group.frame_def.cluster,
                    "samples": len(group.timestamps),
                    "payload_min_bytes": min(len(payload) for payload in group.payloads),
                    "payload_max_bytes": max(len(payload) for payload in group.payloads),
                    "payload_lengths": payload_lengths,
                    "signals": decoded_count,
                    "skipped_signals": len(skipped),
                }
            )

    summary = {
        "decode_mode": "direct_blf",
        "input_blf": str(blf_path),
        "input_arxml": str(arxml_path),
        "mdf_written": False,
        "selected_arxml_messages": len(selected),
        "total_blf_frames_read": total_frames,
        "blf_frames_with_matching_id": candidate_frames,
        "matched_frames_routed": matched_frames,
        "matched_frames_written": written_frame_samples,
        "channel_routing": route_diagnostics,
        "signal_ownership": ownership_diagnostics,
        "groups_written": len(group_summaries),
        "skipped_short_frames": skipped_short,
        "skipped_excess_frames": skipped_excess,
        "groups": group_summaries,
        "skipped_signals": skipped_signals,
    }
    summary_path = ensure_writable_file_path(output_dir / "direct_blf_decode_summary.json", "direct_blf_decode_summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary_path, summary, decoded_signals


def _decode_group_to_signal_store(
    signal_store: dict[str, list[DecodedSignalSeries]],
    group: GroupBuffer,
    allowed_signal_names: set[str],
) -> tuple[int, list[str]]:
    frame = group.frame_def.frame
    group_name = channel_group_name(group.channel, group.frame_def, False)
    max_payload_len = max(len(payload) for payload in group.payloads)
    short_payload_compatibility = any(
        supports_short_payload(group.frame_def, len(payload))
        for payload in group.payloads
    )
    width = max(int(frame.size), max_payload_len) if short_payload_compatibility or max_payload_len < int(frame.size) else max_payload_len
    payload = make_payload_array(group.payloads, width)
    timestamps = np.asarray(group.timestamps, dtype=np.float64)

    decoded_count = 0
    skipped: list[str] = []
    for source_signal in frame.signals:
        signal_name = str(source_signal.name)
        if signal_name not in allowed_signal_names:
            continue
        try:
            samples = extract_can_signal(
                source_signal,
                payload,
                raw=True,
                ignore_value2text_conversion=True,
            )
        except Exception:
            skipped.append(signal_name)
            continue

        raw_values = _values_to_float_array(samples)
        physical_values = _physical_values_for_signal(source_signal, raw_values)
        signal_store[signal_name].append(
            DecodedSignalSeries(
                name=signal_name,
                timestamps=timestamps,
                raw_values=raw_values,
                physical_values=physical_values,
                group=group_name,
            )
        )
        decoded_count += 1

    return decoded_count, skipped


def _physical_values_for_signal(source_signal: object, raw_values: np.ndarray) -> np.ndarray:
    values = np.asarray(raw_values, dtype=float)
    try:
        factor = float(getattr(source_signal, "factor", 1) or 1)
        offset = float(getattr(source_signal, "offset", 0) or 0)
    except Exception:
        return values
    return values * factor + offset


def _values_to_float_array(values: Any) -> np.ndarray:
    array = np.asarray(values)
    if array.dtype.kind in {"S", "U", "O"}:
        return np.asarray([_enum_to_float(item) for item in array], dtype=float)
    return array.astype(float)


def _convert_target_messages_to_mdf(
    blf_path: Path,
    arxml_path: Path,
    output_dir: Path,
    *,
    max_frames: int = 0,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    if not CONVERTER_SCRIPT.exists():
        raise FileNotFoundError(f"BLF converter script not found: {CONVERTER_SCRIPT}")

    mdf_path = ensure_writable_file_path(output_dir / f"{safe_runtime_name(blf_path.stem, 'blf')}_axle_torque.mdf", "axle_torque.mdf")
    summary_path = ensure_writable_file_path(output_dir / "conversion_summary.json", "conversion_summary.json")

    cmd = [
        sys.executable,
        str(CONVERTER_SCRIPT),
        "--blf",
        str(blf_path),
        "--arxml",
        str(arxml_path),
        "--output",
        str(mdf_path),
        "--summary",
        str(summary_path),
        "--pad-truncated",
        "--decode-incomplete-signals",
        "--overwrite",
    ]
    for message in TARGET_MESSAGES:
        cmd.extend(["--message", message])
    if max_frames:
        cmd.extend(["--max-frames", str(int(max_frames))])
    cmd.extend(
        [
            "--progress-jsonl",
            "--progress-interval-seconds",
            os.getenv("AXLE_TORQUE_PROGRESS_INTERVAL_SECONDS", "2"),
        ]
    )

    timeout_seconds = int(os.getenv("AXLE_TORQUE_CONVERSION_TIMEOUT_SECONDS", "3600"))
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=get_runtime_subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output_queue: queue.Queue[str] = queue.Queue()
    output_lines: list[str] = []

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            output_queue.put(line)

    def handle_output_line(line: str) -> None:
        text = line.rstrip()
        if text.startswith(CONVERTER_PROGRESS_PREFIX):
            try:
                payload = json.loads(text[len(CONVERTER_PROGRESS_PREFIX) :])
                _emit_progress(progress_callback, payload)
            except Exception as exc:
                output_lines.append(f"progress parse failed: {exc}; raw={text}")
            return
        if text:
            output_lines.append(text)
            if len(output_lines) > 200:
                del output_lines[:-200]

    reader_thread = threading.Thread(target=reader, name="axle-torque-converter-output", daemon=True)
    reader_thread.start()
    deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
    while True:
        try:
            line = output_queue.get(timeout=0.2)
            handle_output_line(line)
        except queue.Empty:
            pass

        if proc.poll() is not None:
            while True:
                try:
                    handle_output_line(output_queue.get_nowait())
                except queue.Empty:
                    break
            break

        if deadline is not None and time.monotonic() > deadline:
            proc.kill()
            reader_thread.join(timeout=2)
            raise RuntimeError(f"BLF target-message conversion timed out after {timeout_seconds}s.")

    reader_thread.join(timeout=2)
    if proc.returncode != 0:
        tail = "\n".join(output_lines).strip()
        raise RuntimeError(f"BLF target-message conversion failed: {tail[-4000:]}")
    if not mdf_path.exists() or mdf_path.stat().st_size <= 0:
        raise RuntimeError("BLF target-message conversion did not create a valid MDF file.")

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        summary = {}
    return mdf_path, summary_path, summary


def _extract_required_signals_from_store(signal_store: dict[str, list[DecodedSignalSeries]]) -> dict[str, Any]:
    return _extract_required_signals_with_getter(
        lambda name, raw=False: _get_decoded_signal(signal_store, name, raw=raw)
    )


def _extract_required_signals(mdf: MDF) -> dict[str, Any]:
    return _extract_required_signals_with_getter(
        lambda name, raw=False: _get_signal(mdf, name, raw=raw)
    )


def _extract_required_signals_with_getter(get_signal: Callable[..., SignalSeries | None]) -> dict[str, Any]:
    signals: dict[str, SignalSeries] = {}
    sources: dict[str, str] = {}
    warnings: list[str] = []

    def first_existing(names: list[str], *, raw: bool = False, role: str) -> SignalSeries | None:
        for name in names:
            series = get_signal(name, raw=raw)
            if series is not None and series.timestamps.size and series.values.size:
                sources[role] = f"{series.group}.{series.name}"
                return series
        return None

    powerflow = first_existing(
        [
            "ITEPI_TrnsEngdPrFlwSt",
            "TEPI_TrnsEngdPrFlwSt",
            "ITEPI_TrnsEngdPrFlwSt_ICE",
            "TEPI_TrnsEngdPrFlwSt_ICE",
        ],
        raw=True,
        role="动力流状态",
    )
    powerflow_valid = first_existing(
        [
            "ITEPI_TrnsEngdPrFlwStV",
            "TEPI_TrnsEngdPrFlwStV",
            "ITEPI_TrnsEngdPrFlwStV_ICE",
            "TEPI_TrnsEngdPrFlwStV_ICE",
        ],
        raw=True,
        role="动力流状态有效性",
    )
    prefer_ice_inputs = bool(
        powerflow is not None
        and (
            powerflow.name.endswith("_ICE")
            or "_ICE" in powerflow.group
            or "_076_" in powerflow.group
        )
    )

    def variant_order(base_names: list[str], ice_names: list[str]) -> list[str]:
        return ice_names + base_names if prefer_ice_inputs else base_names + ice_names

    signals["powerflow"] = powerflow
    signals["powerflow_valid"] = powerflow_valid
    signals["engine_torque"] = first_existing(
        variant_order(["IEngActStdyStTorq"], ["IEngActStdyStTorq_ICE"]),
        role="发动机实际稳态扭矩",
    )
    signals["engine_torque_valid"] = first_existing(
        variant_order(["IEngActStdyStTorqV"], ["IEngActStdyStTorqV_ICE"]),
        raw=True,
        role="发动机扭矩有效性",
    )
    signals["engine_speed"] = first_existing(["IEngSpd"], role="发动机转速")
    signals["engine_speed_status"] = first_existing(["IEngSpdStat"], raw=True, role="发动机转速状态")
    signals["turbine_speed"] = first_existing(variant_order(["ITrnTrbAngVl"], ["ITrnTrbAngVl_ICE"]), role="涡轮转速")
    signals["turbine_speed_valid"] = first_existing(
        variant_order(["ITrnTrbAngVlV"], ["ITrnTrbAngVlV_ICE"]),
        raw=True,
        role="涡轮转速有效性",
    )
    signals["actual_axle_torque"] = first_existing(["IActAxleTrq"], role="实际轴端扭矩")
    signals["actual_axle_torque_valid"] = first_existing(["IActAxleTrqV"], raw=True, role="实际轴端扭矩有效性")

    gear_candidates = []
    for gear_name, valid_name, label, preferred in [
        ("ITransEstGear_ECP_H1", "ITransEstGearV_ECP_H1", "ECP_H1实际档位", True),
        ("ITransEstGear_TCM", "ITransEstGearV_TCM", "TCM实际档位", False),
    ]:
        gear = get_signal(gear_name, raw=True)
        valid = get_signal(valid_name, raw=True)
        if gear is not None:
            gear_candidates.append((label, gear, valid, preferred))

    if not gear_candidates:
        signals["gear"] = None
        signals["gear_valid"] = None
    else:
        axis = signals["actual_axle_torque"].timestamps if signals["actual_axle_torque"] else gear_candidates[0][1].timestamps
        scored_candidates = [
            (_gear_candidate_score(candidate[1], candidate[2], axis), candidate)
            for candidate in gear_candidates
        ]
        preferred_candidates = [
            (score, candidate)
            for score, candidate in scored_candidates
            if candidate[3] and score > 0
        ]
        if preferred_candidates:
            _, best = max(preferred_candidates, key=lambda item: item[0])
        else:
            _, best = max(scored_candidates, key=lambda item: item[0])
        sources["实际档位"] = f"{best[1].group}.{best[1].name}"
        if best[2] is not None:
            sources["实际档位有效性"] = f"{best[2].group}.{best[2].name}"
        signals["gear"] = best[1]
        signals["gear_valid"] = best[2]

    required = {
        "engine_torque": "IEngActStdyStTorq / IEngActStdyStTorq_ICE",
        "engine_torque_valid": "IEngActStdyStTorqV / IEngActStdyStTorqV_ICE",
        "engine_speed": "IEngSpd",
        "engine_speed_status": "IEngSpdStat",
        "turbine_speed": "ITrnTrbAngVl",
        "turbine_speed_valid": "ITrnTrbAngVlV",
        "actual_axle_torque": "IActAxleTrq",
        "actual_axle_torque_valid": "IActAxleTrqV",
        "gear": "ITransEstGear_TCM / ITransEstGear_ECP_H1",
        "gear_valid": "ITransEstGearV_TCM / ITransEstGearV_ECP_H1",
        "powerflow": "ITEPI_TrnsEngdPrFlwSt / TEPI_TrnsEngdPrFlwSt / *_ICE",
        "powerflow_valid": "ITEPI_TrnsEngdPrFlwStV / TEPI_TrnsEngdPrFlwStV / *_ICE",
    }
    missing = [label for key, label in required.items() if signals.get(key) is None]
    return {"signals": signals, "sources": sources, "missing": missing, "warnings": warnings}


def _get_signal(mdf: MDF, name: str, *, raw: bool = False) -> SignalSeries | None:
    for group_index, group in enumerate(mdf.groups):
        group_name = str(getattr(group.channel_group, "acq_name", "") or f"group_{group_index}")
        for channel_index, channel in enumerate(group.channels):
            if str(channel.name) != name:
                continue
            signal = mdf.get(group=group_index, index=channel_index, raw=raw)
            values = np.asarray(signal.samples)
            timestamps = np.asarray(signal.timestamps, dtype=float)
            if values.dtype.kind in {"S", "U", "O"}:
                values = np.asarray([_enum_to_float(item) for item in values], dtype=float)
            else:
                values = values.astype(float)
            return SignalSeries(name=name, timestamps=timestamps, values=values, group=group_name)
    return None


def _get_decoded_signal(
    signal_store: dict[str, list[DecodedSignalSeries]],
    name: str,
    *,
    raw: bool = False,
) -> SignalSeries | None:
    candidates = signal_store.get(name) or []
    for candidate in candidates:
        values = candidate.raw_values if raw else candidate.physical_values
        timestamps = np.asarray(candidate.timestamps, dtype=float)
        values = np.asarray(values, dtype=float)
        if timestamps.size and values.size:
            return SignalSeries(
                name=candidate.name,
                timestamps=timestamps,
                values=values,
                group=candidate.group,
            )
    return None


def _enum_to_float(value: Any) -> float:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    text = str(value)
    mapping = {
        "Valid": 0.0,
        "Invalid": 1.0,
        "Drive_Engaged": 2.0,
        "Reverse_Engaged": 3.0,
        "No_Powerflow": 1.0,
        "Park_Gear": 15.0,
        "Neutral_Gear": 13.0,
        "Reverse_Gear": 14.0,
    }
    for key, mapped in mapping.items():
        if key.lower() in text.lower():
            return mapped
    try:
        return float(text)
    except Exception:
        return float("nan")


def _gear_candidate_score(gear: SignalSeries, valid: SignalSeries | None, axis: np.ndarray) -> int:
    gear_values, gap = _sample_to_axis(gear, axis)
    valid_mask = np.isfinite(gear_values) & (gap <= MAX_SAMPLE_GAP_SECONDS)
    if valid is not None:
        valid_values, valid_gap = _sample_to_axis(valid, axis)
        valid_mask &= (valid_gap <= MAX_SAMPLE_GAP_SECONDS) & _validity_is_valid(valid_values)
    drive_mask = np.isin(np.rint(gear_values).astype(int, copy=False), list(GEAR_RATIO))
    return int(np.sum(valid_mask & drive_mask))


def _compute_accuracy(
    signals: dict[str, SignalSeries | None],
    warnings: list[str],
    *,
    final_drive_ratio: float,
) -> dict[str, Any]:
    warnings.append(
        "说明书只定义轴端扭矩复算公式、输入信号和边界条件，未定义较准/不准阈值；"
        f"当前判定阈值来自 Agent 配置：MAE≤{DEFAULT_MAE_ERROR_THRESHOLD_NM:g} Nm，"
        f"P95≤{DEFAULT_P95_ERROR_THRESHOLD_NM:g} Nm，通过率≥{DEFAULT_PASS_RATE_THRESHOLD:.0%}。"
    )
    actual = signals["actual_axle_torque"]
    assert actual is not None
    axis = actual.timestamps
    actual_values = actual.values.astype(float)

    engine_torque, engine_torque_gap = _sample_to_axis(signals["engine_torque"], axis)
    engine_speed, engine_speed_gap = _sample_to_axis(signals["engine_speed"], axis)
    turbine_speed, turbine_speed_gap = _sample_to_axis(signals["turbine_speed"], axis)
    gear_values, gear_gap = _sample_to_axis(signals["gear"], axis)

    actual_valid = _optional_valid_mask(signals.get("actual_axle_torque_valid"), axis)
    engine_torque_valid = _optional_valid_mask(signals.get("engine_torque_valid"), axis)
    engine_speed_valid = _optional_valid_mask(signals.get("engine_speed_status"), axis)
    turbine_valid = _optional_valid_mask(signals.get("turbine_speed_valid"), axis)
    gear_valid = _optional_valid_mask(signals.get("gear_valid"), axis)

    gap_mask = (
        (engine_torque_gap <= MAX_SAMPLE_GAP_SECONDS)
        & (engine_speed_gap <= MAX_SAMPLE_GAP_SECONDS)
        & (turbine_speed_gap <= MAX_SAMPLE_GAP_SECONDS)
        & (gear_gap <= MAX_SAMPLE_GAP_SECONDS)
    )
    gear_int = np.rint(gear_values).astype(int, copy=False)
    forward_gear_mask = np.isin(gear_int, [gear for gear in GEAR_RATIO if gear != REVERSE_GEAR_CODE])
    reverse_gear_mask = gear_int == REVERSE_GEAR_CODE
    supported_gear_mask = forward_gear_mask | reverse_gear_mask

    powerflow_values, powerflow_gap = _sample_to_axis(signals.get("powerflow"), axis)
    powerflow_int = np.rint(powerflow_values).astype(int, copy=False)
    powerflow_valid = _optional_valid_mask(signals.get("powerflow_valid"), axis)
    powerflow_mask = (
        (powerflow_gap <= MAX_SAMPLE_GAP_SECONDS)
        & powerflow_valid
        & (
            (forward_gear_mask & (powerflow_int == 2))
            | (reverse_gear_mask & (powerflow_int == 3))
        )
    )

    finite_mask = (
        np.isfinite(actual_values)
        & np.isfinite(engine_torque)
        & np.isfinite(engine_speed)
        & np.isfinite(turbine_speed)
        & np.isfinite(gear_values)
    )
    valid_mask = (
        finite_mask
        & gap_mask
        & actual_valid
        & engine_torque_valid
        & engine_speed_valid
        & turbine_valid
        & gear_valid
        & supported_gear_mask
        & powerflow_mask
        & (np.abs(engine_speed) > MIN_ENGINE_SPEED_RPM)
    )

    calculated = _calculate_axle_torque(
        engine_torque,
        engine_speed,
        turbine_speed,
        gear_int,
        final_drive_ratio=final_drive_ratio,
    )
    display_substitution_mask = (
        np.isfinite(actual_values)
        & actual_valid
        & (
            ((gear_gap <= MAX_SAMPLE_GAP_SECONDS) & gear_valid & (gear_int == 15))
            | ((powerflow_gap <= MAX_SAMPLE_GAP_SECONDS) & powerflow_valid & (powerflow_int == 1))
        )
    )
    display_calculated = calculated.copy()
    display_calculated[display_substitution_mask] = actual_values[display_substitution_mask]
    pre_calculation_valid_count = int(np.sum(valid_mask))
    calculated_finite = np.isfinite(calculated)
    valid_mask &= calculated_finite
    if pre_calculation_valid_count and int(np.sum(valid_mask)) < pre_calculation_valid_count:
        warnings.append(
            f"有 {pre_calculation_valid_count - int(np.sum(valid_mask))} 个样本通过输入门控，"
            "但复算轴端扭矩为 NaN；通常由挡位速比、转速边界或解码物理值不适配导致。"
        )
    if abs(DEFAULT_OUTPUT_TORQUE_SCALE - 1.0) > 1e-9:
        warnings.append(
            f"当前按 AXLE_TORQUE_OUTPUT_SCALE={DEFAULT_OUTPUT_TORQUE_SCALE:g} 覆盖复算输出口径；"
            "默认口径是 IActAxleTrq 真实轴扭，即系数 1。"
        )
    if int(np.sum(display_substitution_mask)):
        warnings.append("图中 Park_Gear 或 No_Powerflow 区间按 calculated=actual、error=0 展示，不计入驱动档精度指标。")
    valid_count = int(np.sum(valid_mask))
    metrics = AxleTorqueMetrics(total_axis_samples=int(axis.size), valid_samples=valid_count)
    metrics.coverage = float(valid_count / axis.size) if axis.size else 0.0
    metrics.final_drive_ratio = float(final_drive_ratio)

    if valid_count < 30:
        gate_stats = [
            ("实际轴扭有限值", np.isfinite(actual_values)),
            ("输入信号有限值", finite_mask),
            ("时间对齐", gap_mask),
            ("有效性位", actual_valid & engine_torque_valid & engine_speed_valid & turbine_valid & gear_valid),
            ("支持档位", supported_gear_mask),
            ("动力流匹配", powerflow_mask),
            ("发动机转速边界", np.abs(engine_speed) > MIN_ENGINE_SPEED_RPM),
            ("复算结果有限值", calculated_finite),
        ]
        warnings.append(
            "有效样本不足门控统计："
            + "；".join(f"{name} {int(np.sum(mask))}/{axis.size}" for name, mask in gate_stats)
        )
        return {
            "status": "insufficient_data",
            "verdict": "证据不足",
            "summary": f"有效驱动档样本只有 {valid_count} 个，无法稳定判断轴端扭矩准确性。",
            "metrics": metrics,
            "abnormal_windows": [],
            "plot_data": _plot_payload(
                axis,
                actual_values,
                display_calculated,
                np.where(display_substitution_mask, 0.0, np.nan),
                gear_int,
                valid_mask,
                display_substitution_mask,
            ),
        }

    lags = np.round(np.arange(-0.5, 0.5001, 0.02), 3)
    zero_lag_error = calculated - actual_values
    zero_lag_mae = _nanmean_abs(zero_lag_error[valid_mask])
    best_lag = 0.0
    best_mae = zero_lag_mae
    best_calculated = calculated
    best_valid_mask = valid_mask
    for lag in lags:
        shifted_calc, lag_gap = _sample_arrays_to_axis(axis + float(lag), calculated, axis)
        lag_mask = valid_mask & (lag_gap <= MAX_SAMPLE_GAP_SECONDS) & np.isfinite(shifted_calc)
        if int(np.sum(lag_mask)) < max(30, valid_count * 0.8):
            continue
        mae = _nanmean_abs((shifted_calc - actual_values)[lag_mask])
        if best_mae is None or (mae is not None and mae < best_mae):
            best_mae = mae
            best_lag = float(lag)
            best_calculated = shifted_calc
            best_valid_mask = lag_mask

    valid_mask = best_valid_mask
    valid_count = int(np.sum(valid_mask))
    metrics.valid_samples = valid_count
    metrics.coverage = float(valid_count / axis.size) if axis.size else 0.0
    error = best_calculated - actual_values
    display_calculated = best_calculated.copy()
    display_calculated[display_substitution_mask] = actual_values[display_substitution_mask]
    display_error = display_calculated - actual_values
    abs_error = np.abs(error[valid_mask])
    rel_error = abs_error / np.maximum(np.abs(actual_values[valid_mask]), 50.0)
    pass_mask = (np.abs(error) <= DEFAULT_ABS_ERROR_THRESHOLD_NM) | (
        np.abs(error) / np.maximum(np.abs(actual_values), 50.0) <= DEFAULT_REL_ERROR_THRESHOLD
    )
    pass_rate = float(np.sum(pass_mask & valid_mask) / valid_count)

    metrics.mae_nm = _float_or_none(float(np.mean(abs_error)))
    metrics.rmse_nm = _float_or_none(float(np.sqrt(np.mean(np.square(error[valid_mask])))))
    metrics.p95_abs_error_nm = _float_or_none(float(np.percentile(abs_error, 95)))
    metrics.max_abs_error_nm = _float_or_none(float(np.max(abs_error)))
    metrics.mean_error_nm = _float_or_none(float(np.mean(error[valid_mask])))
    metrics.median_relative_error = _float_or_none(float(np.median(rel_error)))
    metrics.pass_rate = pass_rate
    metrics.zero_lag_mae_nm = _float_or_none(zero_lag_mae)
    metrics.best_lag_seconds = best_lag
    metrics.best_lag_mae_nm = _float_or_none(best_mae)
    metrics.max_actual_nm = _float_or_none(float(np.nanmax(actual_values[valid_mask])))
    metrics.max_calculated_nm = _float_or_none(float(np.nanmax(best_calculated[valid_mask])))
    metrics.min_actual_nm = _float_or_none(float(np.nanmin(actual_values[valid_mask])))
    metrics.min_calculated_nm = _float_or_none(float(np.nanmin(best_calculated[valid_mask])))

    abnormal_mask = valid_mask & (
        (np.abs(error) > DEFAULT_ABS_ERROR_THRESHOLD_NM)
        & (np.abs(error) / np.maximum(np.abs(actual_values), 50.0) > DEFAULT_REL_ERROR_THRESHOLD)
    )
    abnormal_windows = _mask_to_windows(axis, abnormal_mask, error, actual_values, best_calculated)

    passed = (
        metrics.mae_nm is not None
        and metrics.p95_abs_error_nm is not None
        and metrics.mae_nm <= DEFAULT_MAE_ERROR_THRESHOLD_NM
        and metrics.p95_abs_error_nm <= DEFAULT_P95_ERROR_THRESHOLD_NM
        and metrics.pass_rate >= DEFAULT_PASS_RATE_THRESHOLD
    )
    status = "ok" if passed else "issue"
    verdict = "轴端扭矩较准" if passed else "轴端扭矩不准"
    if passed:
        summary = (
            f"结论：轴端扭矩较准。MAE={metrics.mae_nm:.1f} Nm，"
            f"P95={metrics.p95_abs_error_nm:.1f} Nm，通过率={metrics.pass_rate:.1%}。"
        )
    else:
        summary = (
            f"结论：轴端扭矩不准。MAE={metrics.mae_nm:.1f} Nm，"
            f"P95={metrics.p95_abs_error_nm:.1f} Nm，通过率={metrics.pass_rate:.1%}，"
            f"异常窗口={len(abnormal_windows)} 个。"
        )
    if abs(best_lag) >= 0.04 and best_mae is not None and zero_lag_mae is not None and best_mae < zero_lag_mae * 0.85:
        warnings.append(f"计算值与实车值存在约 {best_lag:+.2f}s 的最佳对齐偏移，需关注信号延迟/滤波。")

    return {
        "status": status,
        "verdict": verdict,
        "summary": summary,
        "metrics": metrics,
        "abnormal_windows": abnormal_windows,
        "plot_data": _plot_payload(
            axis,
            actual_values,
            display_calculated,
            display_error,
            gear_int,
            valid_mask,
            display_substitution_mask,
        ),
    }


def _calculate_axle_torque(
    engine_torque: np.ndarray,
    engine_speed: np.ndarray,
    turbine_speed: np.ndarray,
    gear: np.ndarray,
    *,
    final_drive_ratio: float,
) -> np.ndarray:
    ratio = np.divide(
        turbine_speed,
        engine_speed,
        out=np.full(engine_speed.shape, np.nan, dtype=float),
        where=np.abs(engine_speed) > MIN_ENGINE_SPEED_RPM,
    )
    ratio = np.clip(ratio, TC_RATIO_POINTS[0][0], TC_RATIO_POINTS[-1][0])
    xp = np.asarray([item[0] for item in TC_RATIO_POINTS], dtype=float)
    fp = np.asarray([item[1] for item in TC_RATIO_POINTS], dtype=float)
    k_tc = np.interp(ratio, xp, fp)
    gear_ratio = np.asarray([GEAR_RATIO.get(int(item), np.nan) for item in gear], dtype=float)
    efficiency = np.asarray([TRANS_EFFICIENCY.get(int(item), np.nan) for item in gear], dtype=float)
    total_axle_torque = engine_torque * k_tc * gear_ratio * float(final_drive_ratio) * efficiency
    return total_axle_torque * DEFAULT_OUTPUT_TORQUE_SCALE


def _sample_to_axis(series: SignalSeries | None, axis: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if series is None:
        return np.full(axis.shape, np.nan), np.full(axis.shape, np.inf)
    return _sample_arrays_to_axis(series.timestamps, series.values, axis)


def _sample_arrays_to_axis(
    source_timestamps: np.ndarray,
    source_values: np.ndarray,
    axis: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    if source_timestamps.size == 0:
        return np.full(axis.shape, np.nan), np.full(axis.shape, np.inf)
    indices = np.searchsorted(source_timestamps, axis)
    left = np.clip(indices - 1, 0, source_timestamps.size - 1)
    right = np.clip(indices, 0, source_timestamps.size - 1)
    left_gap = np.abs(axis - source_timestamps[left])
    right_gap = np.abs(source_timestamps[right] - axis)
    use_right = right_gap < left_gap
    nearest = np.where(use_right, right, left)
    values = source_values[nearest].astype(float)
    gaps = np.minimum(left_gap, right_gap)
    return values, gaps


def _optional_valid_mask(series: SignalSeries | None, axis: np.ndarray) -> np.ndarray:
    if series is None:
        return np.ones(axis.shape, dtype=bool)
    values, gaps = _sample_to_axis(series, axis)
    return (gaps <= MAX_SAMPLE_GAP_SECONDS) & _validity_is_valid(values)


def _validity_is_valid(values: np.ndarray) -> np.ndarray:
    # In the current ARXML value tables, raw 0 maps to "Valid".
    return np.isfinite(values) & (np.rint(values).astype(int, copy=False) == 0)


def _nanmean_abs(values: np.ndarray) -> float | None:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None
    return float(np.mean(np.abs(finite)))


def _float_or_none(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def _mask_to_windows(
    axis: np.ndarray,
    mask: np.ndarray,
    error: np.ndarray,
    actual: np.ndarray,
    calculated: np.ndarray,
    *,
    min_duration: float = 0.2,
) -> list[dict[str, float]]:
    windows: list[dict[str, float]] = []
    start: int | None = None
    for index, active in enumerate(mask):
        if active and start is None:
            start = index
        if (not active or index == len(mask) - 1) and start is not None:
            end = index if active and index == len(mask) - 1 else index - 1
            duration = float(axis[end] - axis[start]) if end >= start else 0.0
            if duration >= min_duration:
                segment = slice(start, end + 1)
                abs_error = np.abs(error[segment])
                peak_idx = int(np.nanargmax(abs_error)) + start
                windows.append(
                    {
                        "start": float(axis[start]),
                        "end": float(axis[end]),
                        "duration": duration,
                        "max_abs_error_nm": float(np.nanmax(abs_error)),
                        "mean_error_nm": float(np.nanmean(error[segment])),
                        "actual_at_peak_nm": float(actual[peak_idx]),
                        "calculated_at_peak_nm": float(calculated[peak_idx]),
                    }
                )
            start = None
    return windows[:20]


def _plot_payload(
    axis: np.ndarray,
    actual: np.ndarray,
    calculated: np.ndarray,
    error: np.ndarray,
    gear: np.ndarray,
    valid_mask: np.ndarray,
    display_substitution_mask: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    gear_display = np.where(gear == REVERSE_GEAR_CODE, REVERSE_GEAR_PLOT_VALUE, gear)
    if display_substitution_mask is None:
        display_substitution_mask = np.zeros(axis.shape, dtype=bool)
    return {
        "axis": axis,
        "actual": actual,
        "calculated": calculated,
        "error": error,
        "gear": gear_display.astype(float),
        "valid_mask": valid_mask.astype(bool),
        "display_substitution_mask": display_substitution_mask.astype(bool),
    }


def _render_plot(
    output_path: Path,
    data: dict[str, np.ndarray],
    verdict: str,
    metrics: AxleTorqueMetrics,
) -> Path:
    output_path = ensure_writable_file_path(output_path, "axle_torque_accuracy.png")
    width, height = 1280, 860
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = _load_plot_font(20)
    font = _load_plot_font(15)
    small_font = _load_plot_font(13)
    title = (
        f"{verdict} | MAE={_fmt(metrics.mae_nm)} Nm | "
        f"P95={_fmt(metrics.p95_abs_error_nm)} Nm | Pass={metrics.pass_rate:.1%}"
    )
    draw.text((36, 22), title, fill=(15, 23, 42), font=title_font)

    panels = [
        (
            70,
            70,
            1210,
            395,
            "Axle torque: actual vs calculated",
            "Torque (Nm)",
            ["actual", "calculated"],
            [(37, 99, 235), (16, 185, 129)],
        ),
        (
            70,
            430,
            1210,
            635,
            "Error = calculated - actual",
            "Error (Nm)",
            ["error"],
            [(220, 38, 38)],
        ),
        (
            70,
            670,
            1210,
            815,
            "Gear (R=-1, D1-D9 valid samples)",
            "Gear",
            ["gear"],
            [(124, 58, 237)],
        ),
    ]
    axis = data["axis"]
    if axis.size == 0:
        image.save(output_path)
        return output_path

    for left, top, right, bottom, label, y_label, keys, colors in panels:
        draw.rectangle((left, top, right, bottom), outline=(203, 213, 225), width=1)
        draw.text((left + 8, top + 8), label, fill=(71, 85, 105), font=font)
        substitution_mask = data.get("display_substitution_mask")
        series_list = [
            np.where(data["valid_mask"], data[key], np.nan) if key == "gear" else data[key]
            for key in keys
        ]
        plot_box = (left + 76, top + 42, right - 18, bottom - 46)
        _draw_series_panel(
            draw,
            plot_box,
            axis,
            series_list,
            colors,
            font=small_font,
            x_label="Time (s)",
            y_label=y_label,
            integer_y_ticks=(y_label == "Gear"),
            highlight_mask=substitution_mask,
        )
        if substitution_mask is not None and np.any(substitution_mask):
            note = "Park/No powerflow: calc=actual"
            note_width, _ = _text_size(draw, note, small_font)
            swatch_left = max(left + 8, right - note_width - 205)
            draw.rectangle((swatch_left, top + 31, swatch_left + 22, top + 41), fill=(254, 240, 138), outline=(234, 179, 8))
            draw.text((swatch_left + 28, top + 27), note, fill=(113, 63, 18), font=small_font)
        for idx, key in enumerate(keys):
            draw.line((right - 180, top + 14 + idx * 14, right - 154, top + 14 + idx * 14), fill=colors[idx], width=3)
            draw.text((right - 148, top + 7 + idx * 15), key, fill=(51, 65, 85), font=small_font)

    image.save(output_path)
    return output_path


def _load_plot_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\simsun.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.otf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        try:
            if path.exists():
                return ImageFont.truetype(str(path), size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_series_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    x: np.ndarray,
    series_list: list[np.ndarray],
    colors: list[tuple[int, int, int]],
    *,
    font: ImageFont.ImageFont,
    x_label: str,
    y_label: str,
    integer_y_ticks: bool = False,
    highlight_mask: np.ndarray | None = None,
) -> None:
    left, top, right, bottom = box
    x0, x1 = float(np.nanmin(x)), float(np.nanmax(x))
    if x1 <= x0:
        x1 = x0 + 1.0
    finite_chunks = [series[np.isfinite(series)] for series in series_list if np.any(np.isfinite(series))]
    if not finite_chunks:
        return
    finite_values = np.concatenate(finite_chunks)
    y0, y1 = float(np.nanmin(finite_values)), float(np.nanmax(finite_values))
    if y1 <= y0:
        y1 = y0 + 1.0
    pad = (y1 - y0) * 0.08
    y0 -= pad
    y1 += pad

    if highlight_mask is not None and np.any(highlight_mask):
        for start, end in _mask_to_spans(x, highlight_mask):
            px0 = int(left + (start - x0) / (x1 - x0) * (right - left))
            px1 = int(left + (end - x0) / (x1 - x0) * (right - left))
            if px1 < left or px0 > right:
                continue
            px0 = max(left, px0)
            px1 = min(right, max(px1, px0 + 1))
            draw.rectangle((px0, top, px1, bottom), fill=(255, 251, 235))

    draw.line((left, bottom, right, bottom), fill=(71, 85, 105), width=1)
    draw.line((left, top, left, bottom), fill=(71, 85, 105), width=1)
    draw.text((left, top - 22), y_label, fill=(51, 65, 85), font=font)
    x_label_width, _ = _text_size(draw, x_label, font)
    draw.text(
        (left + (right - left - x_label_width) / 2, bottom + 25),
        x_label,
        fill=(51, 65, 85),
        font=font,
    )

    x_ticks = np.linspace(x0, x1, 5)
    for value in x_ticks:
        px = int(left + (float(value) - x0) / (x1 - x0) * (right - left))
        draw.line((px, bottom, px, bottom + 4), fill=(71, 85, 105), width=1)
        label = _format_axis_tick(float(value))
        label_width, _ = _text_size(draw, label, font)
        draw.text((px - label_width / 2, bottom + 7), label, fill=(71, 85, 105), font=font)

    y_ticks = _axis_ticks(y0, y1, integer=integer_y_ticks)
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = int(bottom - frac * (bottom - top))
        draw.line((left, y, right, y), fill=(241, 245, 249), width=1)
    for value in y_ticks:
        if not math.isfinite(value):
            continue
        py = int(bottom - (value - y0) / (y1 - y0) * (bottom - top))
        draw.line((left - 4, py, left, py), fill=(71, 85, 105), width=1)
        label = _format_axis_tick(value, integer=integer_y_ticks)
        label_width, label_height = _text_size(draw, label, font)
        draw.text((left - label_width - 8, py - label_height / 2), label, fill=(71, 85, 105), font=font)

    for series, color in zip(series_list, colors):
        for points in _downsample_segments(x, series, max_points=1200):
            xy: list[tuple[int, int]] = []
            for xv, yv in points:
                px = int(left + (xv - x0) / (x1 - x0) * (right - left))
                py = int(bottom - (yv - y0) / (y1 - y0) * (bottom - top))
                xy.append((px, py))
            if len(xy) >= 2:
                draw.line(xy, fill=color, width=2)


def _axis_ticks(y0: float, y1: float, *, integer: bool = False) -> list[float]:
    if integer:
        low = math.ceil(y0)
        high = math.floor(y1)
        values = [float(item) for item in range(low, high + 1)]
        if 1 <= len(values) <= 8:
            return values
    return [float(value) for value in np.linspace(y0, y1, 5)]


def _format_axis_tick(value: float, *, integer: bool = False) -> str:
    if integer:
        return str(int(round(value)))
    if abs(value) >= 1000:
        return f"{value:.0f}"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return int(bbox[2] - bbox[0]), int(bbox[3] - bbox[1])


def _mask_to_spans(x: np.ndarray, mask: np.ndarray, *, min_duration: float = 0.2) -> list[tuple[float, float]]:
    mask = np.asarray(mask, dtype=bool)
    if mask.size != x.size:
        return []
    spans: list[tuple[float, float]] = []
    start: int | None = None
    for index, active in enumerate(mask):
        if active and start is None:
            start = index
        if (not active or index == mask.size - 1) and start is not None:
            end = index if active and index == mask.size - 1 else index - 1
            start_time = float(x[start])
            end_time = float(x[end])
            if end_time - start_time >= min_duration:
                spans.append((start_time, end_time))
            start = None
    return spans


def _downsample_segments(
    x: np.ndarray,
    y: np.ndarray,
    *,
    max_points: int,
) -> list[list[tuple[float, float]]]:
    finite = np.isfinite(x) & np.isfinite(y)
    if not np.any(finite):
        return []
    step = max(1, int(math.ceil(x.size / max_points))) if x.size > max_points else 1
    segments: list[list[tuple[float, float]]] = []
    start: int | None = None
    for index, active in enumerate(finite):
        if active and start is None:
            start = index
        if (not active or index == finite.size - 1) and start is not None:
            end = index if active and index == finite.size - 1 else index - 1
            indices = list(range(start, end + 1, step))
            if indices[-1] != end:
                indices.append(end)
            if len(indices) >= 2:
                segments.append([(float(x[item]), float(y[item])) for item in indices])
            start = None
    return segments


def _fmt(value: float | None) -> str:
    return "NA" if value is None else f"{value:.1f}"


def _write_report(result: AxleTorqueResult, output_dir: Path) -> None:
    report_path = ensure_writable_file_path(output_dir / "axle_torque_report.json", "axle_torque_report.json")
    result.report_json_path = str(report_path)
    payload = asdict(result)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def format_axle_torque_markdown(open_id: str, file_name: str, result: AxleTorqueResult) -> str:
    metrics = result.metrics
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        f"**上传文件**：`{file_name}`",
        f"**解析数据库**：`{Path(result.arxml_path).name}`",
        f"**车型/主减速比**：`{result.vehicle_config or '未指定'}` / `{result.final_drive_ratio:g}`",
        "",
        f"**结论**：{result.summary or result.verdict}",
    ]
    if result.status in {"ok", "issue"}:
        lines.extend(
            [
                "",
                "**核心指标**",
                f"- 有效样本：{metrics.valid_samples} / {metrics.total_axis_samples}，覆盖率 {metrics.coverage:.1%}",
                f"- MAE：{_fmt(metrics.mae_nm)} Nm，P95：{_fmt(metrics.p95_abs_error_nm)} Nm，Max：{_fmt(metrics.max_abs_error_nm)} Nm",
                f"- 通过率：{metrics.pass_rate:.1%}，最佳对齐偏移：{metrics.best_lag_seconds:+.2f}s",
            ]
        )
        if result.abnormal_windows:
            first = result.abnormal_windows[0]
            lines.extend(
                [
                    "",
                    "**首个异常窗口**",
                    f"- {first['start']:.2f}s ~ {first['end']:.2f}s，峰值误差 {first['max_abs_error_nm']:.1f} Nm",
                ]
            )
    if result.missing_signals:
        lines.extend(["", "**缺失信号**", *[f"- {item}" for item in result.missing_signals]])
    if result.warnings:
        lines.extend(["", "**注意事项**", *[f"- {item}" for item in result.warnings[:4]]])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate axle torque accuracy from BLF + ECM ARXML.")
    parser.add_argument("blf", type=Path)
    parser.add_argument("--arxml", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--vehicle-config", choices=sorted(VEHICLE_FINAL_DRIVE_RATIOS), required=True)
    args = parser.parse_args(argv)

    result = run_axle_torque_accuracy_analysis(
        args.blf,
        args.arxml,
        output_dir=args.output_dir,
        max_frames=args.max_frames,
        vehicle_config=args.vehicle_config,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0 if result.status in {"ok", "issue", "insufficient_data"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
