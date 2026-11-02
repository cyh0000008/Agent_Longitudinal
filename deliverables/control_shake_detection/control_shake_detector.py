#!/usr/bin/env python
"""Detect longitudinal control shake from BLF logs decoded with ARXML.

Brake-side evidence intentionally uses only:
  - IACCBSCE_ACCTrq1: ACC brake request torque
  - ICSTBATS_TrqVl: brake response torque

The script reuses the project's proven BLF/ARXML routing helpers from
FileConverter/blf_to_mdf.py, but it does not create or read MDF files.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from asammdf.blocks.bus_logging_utils import extract_signal

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover - plotting is optional at runtime
    Image = None
    ImageDraw = None
    ImageFont = None


def _add_project_root_to_path() -> None:
    candidates = [Path.cwd(), Path(__file__).resolve().parent]
    candidates.extend(Path(__file__).resolve().parents)
    for candidate in candidates:
        if (candidate / "FileConverter" / "blf_to_mdf.py").exists():
            sys.path.insert(0, str(candidate))
            return


_add_project_root_to_path()

from FileConverter.blf_to_mdf import (  # noqa: E402
    GroupBuffer,
    build_channel_routes,
    build_unique_signal_ownership,
    iter_blf_messages,
    load_frames,
    make_payload_array,
    normalize_channel,
    supports_short_payload,
)


TARGET_SIGNALS = [
    "IIMULonAccPri",
    "IActVehAccel",
    "IVehSpdAvgDrvn_1",
    "IACCBSCE_ACCAct1",
    "IACCATC_ACCAct",
    "IACCATC_AxlTrqRq",
    "IActAxleTrq",
    "IACCBSCE_ACCTrq1",
    "ICSTBATS_TrqVl",
    "IACCBrkngAct",
    "IBrkSysAutBrkStat",
    "IACCBSCE_AutBrkTp1",
    "IACCBSCE_ACCAccl1",
]

STATUS_SIGNALS = {
    "IACCBSCE_ACCAct1",
    "IACCATC_ACCAct",
    "IACCBrkngAct",
    "IBrkSysAutBrkStat",
    "IACCBSCE_AutBrkTp1",
}

COLORS = {
    "IIMULonAccPri": (25, 95, 220),
    "IActVehAccel": (43, 153, 88),
    "IACCBSCE_ACCAccl1": (229, 126, 35),
    "IACCATC_AxlTrqRq": (203, 67, 53),
    "IActAxleTrq": (142, 68, 173),
    "IACCBSCE_ACCTrq1": (22, 160, 133),
    "ICSTBATS_TrqVl": (192, 57, 43),
    "IACCBSCE_ACCAct1": (39, 174, 96),
    "IACCATC_ACCAct": (46, 134, 193),
    "IACCBrkngAct": (231, 76, 60),
    "IBrkSysAutBrkStat": (155, 89, 182),
    "IACCBSCE_AutBrkTp1": (243, 156, 18),
}


@dataclass
class DetectorConfig:
    sample_period_s: float = 0.05
    window_s: float = 8.0
    stride_s: float = 0.5
    active_ratio_min: float = 0.85
    min_speed_kph: float = 5.0
    drive_acc_amp_min: float = 0.58
    drive_zc_min: int = 6
    drive_request_amp_min_nm: float = 280.0
    drive_actual_amp_min_nm: float = 250.0
    drive_brake_quiet_amp_max_nm: float = 200.0
    drive_brake_active_ratio_max: float = 0.10
    brake_acc_amp_min: float = 1.45
    brake_zc_min: int = 5
    brake_request_or_response_amp_min_nm: float = 500.0
    brake_pulse_prominence_nm: float = 500.0
    max_selected_windows_per_file: int = 3
    min_window_separation_s: float = 4.0


@dataclass
class WindowResult:
    start_s: float
    end_s: float
    kind: str
    score: float
    acc_amp: float
    acc_zero_crossings: int
    acc_turns: int
    drive_request_amp_nm: float
    drive_actual_amp_nm: float
    brake_request_amp_nm: float
    brake_response_amp_nm: float
    brake_request_pulses: int
    brake_response_pulses: int
    brake_active_ratio: float
    plot: str = ""


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect control shake directly from BLF + ARXML.")
    parser.add_argument("--arxml", required=True, type=Path, help="ARXML database path")
    parser.add_argument("--blf", action="append", default=[], type=Path, help="BLF file path; can repeat")
    parser.add_argument("--blf-dir", type=Path, help="Directory containing .blf files")
    parser.add_argument("--out-dir", required=True, type=Path, help="Output directory")
    parser.add_argument("--max-plots-per-file", type=int, default=3, help="Max selected windows per BLF")
    parser.add_argument("--no-plots", action="store_true", help="Only write JSON summary")
    parser.add_argument("--progress", action="store_true", help="Print per-file progress")
    return parser.parse_args(argv)


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def frame_signal_names(frame_def: object) -> set:
    return {str(signal.name) for signal in frame_def.frame.signals}


def decode_signal_values(signal: object, payloads: Sequence[bytes], raw: bool) -> np.ndarray:
    width = max(max(len(payload) for payload in payloads), int(signal.size + signal.start_bit + 7) // 8)
    payload = make_payload_array(payloads, width)
    values = extract_signal(signal, payload, raw=raw, ignore_value2text_conversion=True)
    samples = np.asarray(values)
    if samples.dtype.kind not in "SUO":
        return samples.astype(float, copy=False)

    converted = []
    for item in samples:
        text = item.decode(errors="ignore") if isinstance(item, bytes) else str(item)
        numbers = re.findall(r"-?\d+(?:\.\d+)?", text)
        converted.append(float(numbers[0]) if numbers else np.nan)
    return np.asarray(converted, dtype=float)


def decode_blf_signals(blf_path: Path, arxml_frames: Sequence[object], progress: bool = False) -> Tuple[Dict[str, Tuple[np.ndarray, np.ndarray]], Dict[str, object]]:
    observed: Counter = Counter()
    scanned = 0
    for message in iter_blf_messages(blf_path).messages:
        scanned += 1
        payload = bytes(message.data)
        observed[
            (
                normalize_channel(message.channel),
                int(message.arbitration_id),
                bool(message.is_extended_id),
                len(payload),
            )
        ] += 1

    routes, route_diagnostics = build_channel_routes(
        arxml_frames,
        observed,
        "exact-first",
        True,
        False,
    )

    wanted_signals = set(TARGET_SIGNALS)
    target_ordinals = {
        frame_def.ordinal for frame_def in arxml_frames if frame_signal_names(frame_def) & wanted_signals
    }
    groups: Dict[Tuple[str, int], GroupBuffer] = {}
    matched = 0

    for message in iter_blf_messages(blf_path).messages:
        payload = bytes(message.data)
        route_key = (
            normalize_channel(message.channel),
            int(message.arbitration_id),
            bool(message.is_extended_id),
            len(payload),
        )
        frame_def = routes.get(route_key)
        if frame_def is None or frame_def.ordinal not in target_ordinals:
            continue
        if len(payload) < frame_def.size and not supports_short_payload(frame_def, len(payload)):
            continue

        key = (normalize_channel(message.channel), frame_def.ordinal)
        if key not in groups:
            groups[key] = GroupBuffer(frame_def, normalize_channel(message.channel), [], [])
        groups[key].timestamps.append(float(message.timestamp))
        groups[key].payloads.append(payload)
        matched += 1

    allowed_by_group, ownership = build_unique_signal_ownership(groups, route_diagnostics)
    decoded: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
    sources: Dict[str, object] = {}

    for key, group in groups.items():
        allowed = allowed_by_group.get(key, set())
        available = frame_signal_names(group.frame_def)
        selected = (available & wanted_signals) & allowed
        if not selected:
            continue

        for signal in group.frame_def.frame.signals:
            name = str(signal.name)
            if name not in selected:
                continue
            try:
                samples = decode_signal_values(signal, group.payloads, name in STATUS_SIGNALS)
            except Exception as exc:
                if progress:
                    print(f"[WARN] skip signal {name}: {exc}", file=sys.stderr)
                continue

            timestamps = np.asarray(group.timestamps, dtype=float)
            valid = np.isfinite(timestamps) & np.isfinite(samples)
            if valid.sum() < 2:
                continue
            timestamps = timestamps[valid]
            samples = samples[valid]
            order = np.argsort(timestamps)
            timestamps = timestamps[order]
            samples = samples[order]
            keep = np.r_[True, np.diff(timestamps) > 1e-9]
            timestamps = timestamps[keep]
            samples = samples[keep]

            if name not in decoded or len(timestamps) > len(decoded[name][0]):
                decoded[name] = (timestamps, samples)
                sources[name] = {
                    "frame": group.frame_def.name,
                    "frame_id": f"0x{group.frame_def.frame_id:X}",
                    "cluster": group.frame_def.cluster,
                    "channel": normalize_channel(group.channel),
                    "samples": int(len(timestamps)),
                }

    diagnostics = {
        "scanned_frames": scanned,
        "matched_target_frames": matched,
        "decoded_signals": sorted(decoded),
        "signal_sources": sources,
        "ownership": ownership,
    }
    return decoded, diagnostics


def interpolate_signal(grid: np.ndarray, signal: Optional[Tuple[np.ndarray, np.ndarray]], hold: bool = False) -> np.ndarray:
    if signal is None:
        return np.full(grid.shape, np.nan)
    timestamps, samples = signal
    if hold:
        indices = np.searchsorted(timestamps, grid, side="right") - 1
        out = np.full(grid.shape, np.nan)
        valid = (indices >= 0) & (indices < len(samples))
        out[valid] = samples[indices[valid]]
        return out
    return np.interp(grid, timestamps, samples, left=np.nan, right=np.nan)


def resample(decoded: Dict[str, Tuple[np.ndarray, np.ndarray]], config: DetectorConfig) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
    if "IIMULonAccPri" not in decoded:
        raise RuntimeError("Missing required acceleration signal: IIMULonAccPri")
    if "IVehSpdAvgDrvn_1" not in decoded:
        raise RuntimeError("Missing required speed signal: IVehSpdAvgDrvn_1")

    starts = [signal[0][0] for signal in decoded.values()]
    ends = [signal[0][-1] for signal in decoded.values()]
    grid = np.arange(min(starts), max(ends), config.sample_period_s)
    arrays = {
        name: interpolate_signal(grid, decoded.get(name), name in STATUS_SIGNALS)
        for name in TARGET_SIGNALS
    }
    return grid, arrays


def p95_p5(values: np.ndarray) -> float:
    if values is None or not np.isfinite(values).any():
        return float("nan")
    return float(np.nanpercentile(values, 95) - np.nanpercentile(values, 5))


def smooth(values: np.ndarray, width: int = 7) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if len(values) < width:
        return values.copy()
    pad = width // 2
    padded = np.pad(values, (pad, pad), mode="edge")
    return np.convolve(padded, np.ones(width) / width, mode="valid")[: len(values)]


def detrend(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    finite = np.isfinite(values)
    if finite.sum() < 3:
        return values - np.nanmean(values)
    x = np.arange(len(values))
    slope, offset = np.polyfit(x[finite], values[finite], 1)
    return values - (slope * x + offset)


def zero_crossings(values: np.ndarray, epsilon: float = 0.04) -> int:
    signs = np.zeros_like(values, dtype=int)
    signs[values > epsilon] = 1
    signs[values < -epsilon] = -1
    non_zero = signs[signs != 0]
    if len(non_zero) < 2:
        return 0
    return int(np.sum(non_zero[1:] != non_zero[:-1]))


def turn_count(values: np.ndarray, prominence: float) -> int:
    y = smooth(np.asarray(values, dtype=float), 7)
    finite = np.isfinite(y)
    if finite.sum() < 8:
        return 0
    y = np.where(finite, y, np.nanmedian(y[finite]))
    dy = np.diff(y)
    deadband = max(prominence / 12.0, 1e-6)
    signs = np.zeros_like(dy, dtype=int)
    signs[dy > deadband] = 1
    signs[dy < -deadband] = -1

    last = 0
    for index, value in enumerate(signs):
        if value == 0:
            signs[index] = last
        else:
            last = value

    extrema: List[int] = []
    for index in range(1, len(signs)):
        if signs[index] and signs[index - 1] and signs[index] != signs[index - 1]:
            extrema.append(index)
    if not extrema:
        return 0

    filtered = [extrema[0]]
    center = float(np.nanmedian(y))
    for index in extrema[1:]:
        if abs(y[index] - y[filtered[-1]]) >= prominence:
            filtered.append(index)
        elif abs(y[index] - center) > abs(y[filtered[-1]] - center):
            filtered[-1] = index
    return len(filtered)


def pulse_count(values: np.ndarray, prominence_nm: float) -> int:
    y = np.asarray(values, dtype=float)
    if not np.isfinite(y).any():
        return 0
    baseline = float(np.nanpercentile(y, 10))
    high = float(np.nanpercentile(y, 90))
    threshold = baseline + max((high - baseline) * 0.35, prominence_nm * 0.25)
    above = y > threshold

    for index in range(1, len(above) - 1):
        if not above[index] and above[index - 1] and above[index + 1]:
            above[index] = True

    count = 0
    index = 0
    while index < len(above):
        if not above[index]:
            index += 1
            continue
        end = index
        while end < len(above) and above[end]:
            end += 1
        if float(np.nanmax(y[index:end]) - baseline) >= prominence_nm:
            count += 1
        index = end
    return count


def detect_windows(grid: np.ndarray, arrays: Dict[str, np.ndarray], config: DetectorConfig) -> List[WindowResult]:
    active = (np.nan_to_num(arrays["IACCBSCE_ACCAct1"]) >= 0.5) | (
        np.nan_to_num(arrays["IACCATC_ACCAct"]) >= 0.5
    )
    speed_ok = np.nan_to_num(arrays["IVehSpdAvgDrvn_1"]) > config.min_speed_kph
    valid = active & speed_ok & np.isfinite(arrays["IIMULonAccPri"])

    window_samples = max(2, int(round(config.window_s / config.sample_period_s)))
    stride_samples = max(1, int(round(config.stride_s / config.sample_period_s)))

    candidates: List[WindowResult] = []
    for start in range(0, len(grid) - window_samples, stride_samples):
        end = start + window_samples
        if float(valid[start:end].mean()) < config.active_ratio_min:
            continue

        acc = detrend(arrays["IIMULonAccPri"][start:end])
        acc_amp = p95_p5(acc)
        zc = zero_crossings(acc)
        acc_turns = turn_count(acc, 0.14)

        drive_request = arrays["IACCATC_AxlTrqRq"][start:end]
        drive_actual = arrays["IActAxleTrq"][start:end]
        brake_request = arrays["IACCBSCE_ACCTrq1"][start:end]
        brake_response = arrays["ICSTBATS_TrqVl"][start:end]

        drive_request_amp = p95_p5(drive_request)
        drive_actual_amp = p95_p5(drive_actual)
        brake_request_amp = p95_p5(brake_request)
        brake_response_amp = p95_p5(brake_response)
        brake_request_pulses = pulse_count(brake_request, config.brake_pulse_prominence_nm)
        brake_response_pulses = pulse_count(brake_response, config.brake_pulse_prominence_nm)
        brake_active_ratio = float(
            (
                (np.nan_to_num(brake_request) > config.drive_brake_quiet_amp_max_nm)
                | (np.nan_to_num(brake_response) > config.drive_brake_quiet_amp_max_nm)
            ).mean()
        )

        drive_like = (
            acc_amp >= config.drive_acc_amp_min
            and zc >= config.drive_zc_min
            and drive_request_amp >= config.drive_request_amp_min_nm
            and drive_actual_amp >= config.drive_actual_amp_min_nm
            and brake_request_amp < config.drive_brake_quiet_amp_max_nm
            and brake_response_amp < config.drive_brake_quiet_amp_max_nm
            and brake_active_ratio <= config.drive_brake_active_ratio_max
        )

        brake_like = (
            acc_amp >= config.brake_acc_amp_min
            and zc >= config.brake_zc_min
            and (
                brake_request_amp >= config.brake_request_or_response_amp_min_nm
                or brake_response_amp >= config.brake_request_or_response_amp_min_nm
                or brake_request_pulses >= 1
                or brake_response_pulses >= 1
            )
            and (brake_request_pulses + brake_response_pulses >= 1 or acc_turns >= 4)
        )

        if not (drive_like or brake_like):
            continue

        kind = "drive torque oscillation" if drive_like else "brake request/response oscillation"
        score = (
            acc_amp
            + 0.08 * min(acc_turns, 8)
            + 0.10 * min(brake_request_pulses + brake_response_pulses, 4)
        )
        candidates.append(
            WindowResult(
                start_s=float(grid[start]),
                end_s=float(grid[end - 1]),
                kind=kind,
                score=float(score),
                acc_amp=float(acc_amp),
                acc_zero_crossings=int(zc),
                acc_turns=int(acc_turns),
                drive_request_amp_nm=float(drive_request_amp),
                drive_actual_amp_nm=float(drive_actual_amp),
                brake_request_amp_nm=float(brake_request_amp),
                brake_response_amp_nm=float(brake_response_amp),
                brake_request_pulses=int(brake_request_pulses),
                brake_response_pulses=int(brake_response_pulses),
                brake_active_ratio=float(brake_active_ratio),
            )
        )

    candidates.sort(key=lambda item: item.score, reverse=True)
    selected: List[WindowResult] = []
    for item in candidates:
        overlaps = any(
            not (
                item.end_s < previous.start_s - config.min_window_separation_s
                or item.start_s > previous.end_s + config.min_window_separation_s
            )
            for previous in selected
        )
        if overlaps:
            continue
        selected.append(item)
        if len(selected) >= config.max_selected_windows_per_file:
            break
    return sorted(selected, key=lambda item: item.start_s)


def load_font(size: int, bold: bool = False):
    if ImageFont is None:
        return None
    candidates = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def y_range(series: Sequence[Tuple[str, np.ndarray, Tuple[int, int, int]]]) -> Tuple[float, float]:
    values: List[float] = []
    for _, samples, _ in series:
        finite = np.asarray(samples)[np.isfinite(samples)]
        values.extend(finite.tolist())
    if not values:
        return -1.0, 1.0
    low = float(np.percentile(values, 2))
    high = float(np.percentile(values, 98))
    if math.isclose(low, high):
        low -= 1.0
        high += 1.0
    span = high - low
    return low - span * 0.08, high + span * 0.08


def draw_panel(draw, box, title, series, start_s, end_s, unit, fonts, fixed_range=None, step=False) -> None:
    title_font, font, small_font = fonts
    x0, y0, x1, y1 = box
    draw.rectangle(box, fill=(255, 255, 255), outline=(210, 218, 230))
    draw.text((x0 + 10, y0 + 8), title, fill=(20, 28, 43), font=title_font)

    px0, py0, px1, py1 = x0 + 78, y0 + 43, x1 - 20, y1 - 34
    ymin, ymax = fixed_range or y_range(series)
    if math.isclose(ymin, ymax):
        ymax = ymin + 1.0

    draw.line((px0, py1, px1, py1), fill=(160, 170, 185))
    draw.line((px0, py0, px0, py1), fill=(160, 170, 185))
    draw.text((x0 + 12, (py0 + py1) // 2 - 8), unit, fill=(75, 85, 100), font=small_font)

    for index in range(5):
        y = py1 - (py1 - py0) * index / 4
        value = ymin + (ymax - ymin) * index / 4
        draw.line((px0, y, px1, y), fill=(238, 242, 248))
        draw.text((x0 + 8, y - 8), f"{value:.1f}", fill=(90, 98, 112), font=small_font)

    for index in range(5):
        x = px0 + (px1 - px0) * index / 4
        t = start_s + (end_s - start_s) * index / 4
        draw.line((x, py1, x, py1 + 4), fill=(160, 170, 185))
        draw.text((x - 22, py1 + 8), f"{t:.1f}s", fill=(90, 98, 112), font=small_font)

    for name, samples, color in series:
        samples = np.asarray(samples, dtype=float)
        xs = np.linspace(px0, px1, len(samples))
        ys = py1 - (np.clip(samples, ymin, ymax) - ymin) / (ymax - ymin) * (py1 - py0)
        points = [(float(xs[i]), float(ys[i])) for i in range(len(samples)) if np.isfinite(samples[i])]
        if len(points) < 2:
            continue
        if step:
            stepped = []
            for index in range(len(points) - 1):
                stepped.append(points[index])
                stepped.append((points[index + 1][0], points[index][1]))
            stepped.append(points[-1])
            points = stepped
        draw.line(points, fill=color, width=3)

    legend_x, legend_y = px0, y0 + 11
    for name, _, color in series:
        draw.line((legend_x, legend_y + 8, legend_x + 22, legend_y + 8), fill=color, width=4)
        draw.text((legend_x + 28, legend_y), name, fill=(40, 48, 62), font=small_font)
        legend_x += 28 + int(draw.textlength(name, font=small_font)) + 22
        if legend_x > x1 - 180:
            legend_x = px0
            legend_y += 16


def plot_window(
    blf_name: str,
    result: WindowResult,
    grid: np.ndarray,
    arrays: Dict[str, np.ndarray],
    output_dir: Path,
    index: int,
) -> str:
    if Image is None or ImageDraw is None:
        return ""

    mask = (grid >= result.start_s - 3.0) & (grid <= result.end_s + 3.0)
    t = grid[mask]
    sliced = {name: samples[mask] for name, samples in arrays.items()}
    if len(t) < 2:
        return ""

    title_font = load_font(28, True)
    panel_font = load_font(19, True)
    font = load_font(16)
    small_font = load_font(13)

    width, height = 1500, 1120
    image = Image.new("RGB", (width, height), (246, 248, 252))
    draw = ImageDraw.Draw(image)
    draw.text((36, 26), f"{blf_name} | ACCTrq1 + CSTBATS only candidate {index}", fill=(15, 23, 42), font=title_font)
    draw.text(
        (36, 62),
        f"{result.start_s:.1f}-{result.end_s:.1f}s | {result.kind} | score={result.score:.2f}",
        fill=(55, 70, 92),
        font=font,
    )
    draw.text(
        (36, 90),
        (
            f"accAmp={result.acc_amp:.2f}, zc={result.acc_zero_crossings}, "
            f"reqAmp={result.brake_request_amp_nm:.0f}, respAmp={result.brake_response_amp_nm:.0f}, "
            f"reqPulse={result.brake_request_pulses}, respPulse={result.brake_response_pulses}"
        ),
        fill=(70, 82, 100),
        font=small_font,
    )

    panels = [
        (36, 128, width - 36, 365),
        (36, 390, width - 36, 627),
        (36, 652, width - 36, 889),
        (36, 914, width - 36, 1085),
    ]
    fonts = (panel_font, font, small_font)
    draw_panel(
        draw,
        panels[0],
        "Acceleration / Control Decel",
        [(name, sliced[name], COLORS[name]) for name in ["IIMULonAccPri", "IActVehAccel", "IACCBSCE_ACCAccl1"] if np.isfinite(sliced[name]).any()],
        float(t[0]),
        float(t[-1]),
        "m/s^2",
        fonts,
    )
    draw_panel(
        draw,
        panels[1],
        "Drive / Axle Torque",
        [(name, sliced[name], COLORS[name]) for name in ["IACCATC_AxlTrqRq", "IActAxleTrq"] if np.isfinite(sliced[name]).any()],
        float(t[0]),
        float(t[-1]),
        "Nm",
        fonts,
    )
    draw_panel(
        draw,
        panels[2],
        "Brake Request / Brake Response Torque",
        [(name, sliced[name], COLORS[name]) for name in ["IACCBSCE_ACCTrq1", "ICSTBATS_TrqVl"] if np.isfinite(sliced[name]).any()],
        float(t[0]),
        float(t[-1]),
        "Nm",
        fonts,
    )
    draw_panel(
        draw,
        panels[3],
        "State Signals (context only)",
        [(name, sliced[name], COLORS[name]) for name in ["IACCBSCE_ACCAct1", "IACCATC_ACCAct", "IACCBrkngAct", "IBrkSysAutBrkStat", "IACCBSCE_AutBrkTp1"] if np.isfinite(sliced[name]).any()],
        float(t[0]),
        float(t[-1]),
        "enum",
        fonts,
        fixed_range=(-0.3, 6.3),
        step=True,
    )

    output = output_dir / f"{safe_name(blf_name)}__control_shake_{index}_{result.start_s:.0f}_{result.end_s:.0f}.png"
    image.save(output)
    return str(output)


def write_index_plot(items: Sequence[Dict[str, object]], output_dir: Path) -> str:
    if Image is None or ImageDraw is None:
        return ""
    title_font = load_font(28, True)
    header_font = load_font(19, True)
    font = load_font(13)
    width = 1760
    row_height = 104
    height = 116 + row_height * len(items) + 50
    image = Image.new("RGB", (width, height), (246, 248, 252))
    draw = ImageDraw.Draw(image)
    draw.text((36, 26), "Control Shake BLF Candidates - ACCTrq1 + CSTBATS only", fill=(15, 23, 42), font=title_font)
    draw.text(
        (36, 64),
        "Brake-side judging and plots use only IACCBSCE_ACCTrq1 and ICSTBATS_TrqVl.",
        fill=(55, 70, 92),
        font=load_font(16),
    )
    y = 104
    columns = [
        (36, "file"),
        (520, "window"),
        (650, "score"),
        (750, "acc"),
        (850, "zc"),
        (970, "reqAmp"),
        (1090, "respAmp"),
        (1240, "class"),
        (1430, "plot"),
    ]
    for x, label in columns:
        draw.text((x, y), label, fill=(55, 70, 92), font=header_font)
    y += 34
    for item in items:
        draw.rectangle((28, y - 8, width - 28, y + row_height - 14), fill=(255, 255, 255), outline=(220, 226, 236))
        draw.text((36, y), str(item["file"])[:53], fill=(20, 28, 43), font=font)
        draw.text((520, y), f"{item['start_s']:.1f}-{item['end_s']:.1f}s", fill=(20, 28, 43), font=font)
        draw.text((650, y), f"{item['score']:.2f}", fill=(20, 28, 43), font=font)
        draw.text((750, y), f"{item['acc_amp']:.2f}", fill=(20, 28, 43), font=font)
        draw.text((850, y), str(item["acc_zero_crossings"]), fill=(20, 28, 43), font=font)
        draw.text((970, y), f"{item['brake_request_amp_nm']:.0f}", fill=(20, 28, 43), font=font)
        draw.text((1090, y), f"{item['brake_response_amp_nm']:.0f}", fill=(20, 28, 43), font=font)
        draw.text((1240, y), str(item["kind"])[:28], fill=(20, 28, 43), font=font)
        draw.text((1430, y), Path(str(item.get("plot", ""))).name[:32], fill=(20, 95, 180), font=font)
        y += row_height

    output = output_dir / "index_control_shake.png"
    image.save(output)
    return str(output)


def collect_blf_files(args: argparse.Namespace) -> List[Path]:
    files = [Path(item) for item in args.blf]
    if args.blf_dir:
        files.extend(sorted(args.blf_dir.glob("*.blf")))
    unique: List[Path] = []
    seen = set()
    for path in files:
        resolved = str(path.resolve())
        if resolved not in seen:
            unique.append(path)
            seen.add(resolved)
    if not unique:
        raise RuntimeError("No BLF files provided. Use --blf or --blf-dir.")
    return unique


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config = DetectorConfig(max_selected_windows_per_file=max(1, int(args.max_plots_per_file)))
    args.out_dir.mkdir(parents=True, exist_ok=True)

    frames = load_frames(args.arxml)
    blf_files = collect_blf_files(args)

    all_items: List[Dict[str, object]] = []
    file_summaries: List[Dict[str, object]] = []
    for blf_path in blf_files:
        if args.progress:
            print(f"[INFO] processing {blf_path}", flush=True)
        try:
            decoded, diagnostics = decode_blf_signals(blf_path, frames, progress=args.progress)
            grid, arrays = resample(decoded, config)
            windows = detect_windows(grid, arrays, config)
            window_items = []
            for index, result in enumerate(windows, 1):
                if not args.no_plots:
                    result.plot = plot_window(blf_path.name, result, grid, arrays, args.out_dir, index)
                item = asdict(result)
                item["file"] = blf_path.name
                item["blf_path"] = str(blf_path)
                all_items.append(item)
                window_items.append(item)
            file_summaries.append(
                {
                    "file": blf_path.name,
                    "blf_path": str(blf_path),
                    "detected": bool(windows),
                    "windows": window_items,
                    "diagnostics": diagnostics,
                }
            )
        except Exception as exc:
            file_summaries.append(
                {
                    "file": blf_path.name,
                    "blf_path": str(blf_path),
                    "detected": False,
                    "error": str(exc),
                }
            )

    index_plot = "" if args.no_plots else write_index_plot(all_items, args.out_dir)
    summary = {
        "arxml": str(args.arxml),
        "config": asdict(config),
        "signals": TARGET_SIGNALS,
        "brake_evidence_signals": ["IACCBSCE_ACCTrq1", "ICSTBATS_TrqVl"],
        "removed_brake_signals": ["IDrvIntndTtlBrkTrq", "IAvgWhlBrkPrsrEst", "IAvgWhlBrkPrsrEst_1"],
        "index_plot": index_plot,
        "files": file_summaries,
    }
    summary_path = args.out_dir / "control_shake_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.progress:
        print(f"[INFO] summary: {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
