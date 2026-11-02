#!/usr/bin/env python3
"""Find J6B parsed H5 files by vehicle, date, and target time.

The expected H5 filename format is like:
    2026-08-17-19-38-37_can_parsed.h5

The file name represents the segment start time.  The lookup first gathers
coarse candidates by vehicle/model/date and file start time, then opens nearby
H5 files and confirms whether the requested problem time falls inside the
actual timestamp span stored in the file.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import h5py
except Exception:  # pragma: no cover - deployment doctor checks h5py separately.
    h5py = None  # type: ignore[assignment]


DEFAULT_J6B_BASE_DIR = os.getenv("J6B_BASE_DIR", "")
LOOKUP_TIME_TOLERANCE_SECONDS = 2.0
try:
    LAST_SEGMENT_COARSE_WINDOW_SECONDS = max(
        1.0,
        float(os.getenv("J6B_H5_LAST_SEGMENT_COARSE_WINDOW_SECONDS", str(5 * 60))),
    )
except ValueError:
    LAST_SEGMENT_COARSE_WINDOW_SECONDS = 5 * 60


@dataclass(frozen=True)
class H5Candidate:
    path: str
    start_time: dt.datetime
    next_start_time: dt.datetime | None
    confirmed_by_h5: bool
    matched_by_adjacent_start: bool
    span_start: dt.datetime | None = None
    span_end: dt.datetime | None = None


@dataclass(frozen=True)
class _StartedPath:
    path: str
    start_time: dt.datetime
    parent: str


def normalize_date(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    match = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", text)
    if not match:
        raise ValueError("日期格式应为 yyyy/mm/dd，例如 2026/08/17。")
    year, month, day = match.groups()
    digits = f"{int(year):04d}{int(month):02d}{int(day):02d}"
    if len(digits) != 8:
        raise ValueError("日期格式应为 yyyy/mm/dd，例如 2026/08/17。")
    date_compact = digits
    date_dash = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return date_compact, date_dash


def normalize_time_minute(value: str) -> tuple[str, str]:
    hour, minute, _second = normalize_time(value)
    return hour, minute


def normalize_time(value: str) -> tuple[str, str, str]:
    text = (value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if not match:
        raise ValueError("时间格式应为 HH:MM:SS，例如 19:38:37。")
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError("时间超出范围，应为 00:00:00 到 23:59:59。")
    return f"{hour:02d}", f"{minute:02d}", f"{second:02d}"


def normalize_car_id(value: str) -> list[str]:
    text = (value or "").strip().upper()
    if not text:
        raise ValueError("车号不能为空。")
    candidates = [text]
    full_match = re.fullmatch(r"[A-Z]{1,4}\d{2}[A-Z]{2}(\d{3})", text)
    if full_match:
        short = full_match.group(1)
        if short not in candidates:
            candidates.append(short)
    if text.isdigit():
        padded = text.zfill(3)
        if padded not in candidates:
            candidates.append(padded)
    return candidates


def find_j6b_h5(
    *,
    base_dir: str,
    car_model: str,
    car_id: str,
    date_value: str,
    time_value: str,
) -> tuple[list[str], list[str], list[str]]:
    model = (car_model or "").strip()
    if not model:
        raise ValueError("车型不能为空，例如 C1UL 或 E2LB。")
    if not base_dir:
        raise ValueError("请通过 --base-dir 或 J6B_BASE_DIR 配置 H5 数据目录。")

    target_time = parse_problem_datetime(date_value, time_value)

    candidate_h5: list[str] = []
    matched_date_dirs: list[str] = []
    matched_vehicle_dirs: list[str] = []

    for candidate_car_id in normalize_car_id(car_id):
        vehicle_pattern = f"*{model}*{candidate_car_id}*"
        date_infos = _date_infos_around(target_time.date())

        search_patterns: list[tuple[str, bool]] = []
        for date_compact, date_dash in date_infos:
            # Common layout:
            # Parsed / <vehicle-folder> / <date-folder> / <segment-start>_can_parsed.h5
            search_patterns.extend(
                [
                    (os.path.join(base_dir, vehicle_pattern, f"*{date_compact}*", f"{date_dash}-*.h5"), False),
                    (os.path.join(base_dir, vehicle_pattern, f"*{date_dash}*", f"{date_dash}-*.h5"), False),
                    (os.path.join(base_dir, vehicle_pattern, "**", f"{date_dash}-*.h5"), True),
                    # Some parsed folders put the date and vehicle in one level.
                    (os.path.join(base_dir, f"*{date_compact}*{model}*{candidate_car_id}*", f"{date_dash}-*.h5"), False),
                    (os.path.join(base_dir, f"*{date_dash}*{model}*{candidate_car_id}*", f"{date_dash}-*.h5"), False),
                ]
            )
        for pattern, recursive in search_patterns:
            candidate_h5.extend(glob.glob(pattern, recursive=recursive))

        date_dir_patterns = [
            os.path.join(base_dir, vehicle_pattern, f"*{compact}*")
            for compact, _dash in date_infos
        ] + [
            os.path.join(base_dir, vehicle_pattern, f"*{dash}*")
            for _compact, dash in date_infos
        ] + [
            os.path.join(base_dir, f"*{compact}*{model}*{candidate_car_id}*")
            for compact, _dash in date_infos
        ] + [
            os.path.join(base_dir, f"*{dash}*{model}*{candidate_car_id}*")
            for _compact, dash in date_infos
        ]
        for pattern in date_dir_patterns:
            matched_date_dirs.extend(path for path in glob.glob(pattern) if Path(path).is_dir())

        vehicle_dir_pattern = os.path.join(base_dir, vehicle_pattern)
        matched_vehicle_dirs.extend(path for path in glob.glob(vehicle_dir_pattern) if Path(path).is_dir())

    matched_h5 = [candidate.path for candidate in select_h5_candidates_for_time(candidate_h5, target_time)]
    matched_date_dirs = sorted(set(matched_date_dirs))
    matched_vehicle_dirs = sorted(set(matched_vehicle_dirs))
    return matched_h5, matched_date_dirs, matched_vehicle_dirs


def parse_problem_datetime(date_value: str, time_value: str) -> dt.datetime:
    date_compact, _date_dash = normalize_date(date_value)
    hour, minute, second = normalize_time(time_value)
    return dt.datetime(
        int(date_compact[:4]),
        int(date_compact[4:6]),
        int(date_compact[6:8]),
        int(hour),
        int(minute),
        int(second),
    )


def select_h5_candidates_for_time(paths: Iterable[str], target_time: dt.datetime) -> list[H5Candidate]:
    started_paths = _parse_started_paths(paths)
    if not started_paths:
        return []

    coarse: list[H5Candidate] = []
    all_started_by_path = {item.path: item for item in started_paths}
    groups = _group_started_paths_by_parent(started_paths)
    for group in groups:
        for index, item in enumerate(group):
            next_start = group[index + 1].start_time if index + 1 < len(group) else None
            if _is_adjacent_start_match(item.start_time, next_start, target_time):
                coarse.append(
                    H5Candidate(
                        path=item.path,
                        start_time=item.start_time,
                        next_start_time=next_start,
                        confirmed_by_h5=False,
                        matched_by_adjacent_start=True,
                    )
                )

    # Add nearest neighbours so a short/long actual H5 span can correct the
    # coarse adjacent-start guess.
    coarse_paths = {candidate.path for candidate in coarse}
    inspection_paths = set(coarse_paths)
    for group in groups:
        for index, item in enumerate(group):
            if item.path not in coarse_paths:
                continue
            start_index = max(0, index - 1)
            end_index = min(len(group), index + 2)
            inspection_paths.update(group[neighbor_index].path for neighbor_index in range(start_index, end_index))
    if not inspection_paths:
        nearest = sorted(started_paths, key=lambda item: abs((item.start_time - target_time).total_seconds()))
        inspection_paths.update(item.path for item in nearest[:3])

    confirmed: list[H5Candidate] = []
    fallbacks: list[H5Candidate] = []
    coarse_by_path = {candidate.path: candidate for candidate in coarse}
    for path in inspection_paths:
        item = all_started_by_path.get(path)
        if item is None:
            continue
        coarse_candidate = coarse_by_path.get(path)
        next_start = coarse_candidate.next_start_time if coarse_candidate else _next_start_for_path(started_paths, item)
        span = read_h5_time_span(path, item.start_time)
        if span is None:
            if coarse_candidate is not None:
                fallbacks.append(coarse_candidate)
            continue
        span_start, span_end = span
        if _datetime_in_range(target_time, span_start, span_end, LOOKUP_TIME_TOLERANCE_SECONDS):
            confirmed.append(
                H5Candidate(
                    path=path,
                    start_time=item.start_time,
                    next_start_time=next_start,
                    confirmed_by_h5=True,
                    matched_by_adjacent_start=coarse_candidate is not None,
                    span_start=span_start,
                    span_end=span_end,
                )
            )
        elif coarse_candidate is not None:
            # The H5 was readable and says the target is outside the segment, so
            # do not use the adjacent-start fallback for this file.
            pass

    result = confirmed or fallbacks
    return sorted(
        result,
        key=lambda candidate: (
            0 if candidate.confirmed_by_h5 else 1,
            abs((candidate.start_time - target_time).total_seconds()),
            candidate.path,
        ),
    )


def read_h5_time_span(path: str, file_start_time: dt.datetime) -> tuple[dt.datetime, dt.datetime] | None:
    if h5py is None:
        return None

    span_start: dt.datetime | None = None
    span_end: dt.datetime | None = None

    def include_span(low: float, high: float) -> None:
        nonlocal span_start, span_end
        converted = _convert_timestamp_span_to_datetime(low, high, file_start_time)
        if converted is None:
            return
        current_start, current_end = converted
        if span_start is None or current_start < span_start:
            span_start = current_start
        if span_end is None or current_end > span_end:
            span_end = current_end

    try:
        with h5py.File(path, "r") as handle:
            def visitor(name: str, obj: Any) -> None:
                if not isinstance(obj, h5py.Dataset):
                    return
                low_high = _read_timestamp_dataset_span(name, obj)
                if low_high is None:
                    return
                include_span(*low_high)

            handle.visititems(visitor)
    except Exception:
        return None

    if span_start is None or span_end is None or span_end <= span_start:
        return None
    return span_start, span_end


def _read_timestamp_dataset_span(name: str, dataset: Any) -> tuple[float, float] | None:
    try:
        if not dataset.shape or int(dataset.shape[0]) < 2:
            return None
    except Exception:
        return None

    base_name = str(name).rsplit("/", 1)[-1].lower()
    first: Any
    last: Any
    if base_name == "timestamp":
        first = _dataset_edge_value(dataset, 0)
        last = _dataset_edge_value(dataset, -1)
    elif getattr(dataset.dtype, "names", None) and "timestamp" in dataset.dtype.names:
        first = _compound_dataset_edge_value(dataset, 0, "timestamp")
        last = _compound_dataset_edge_value(dataset, -1, "timestamp")
    else:
        return None

    low_high = _clean_timestamp_edges(first, last)
    if low_high is None:
        return None
    low, high = low_high
    if high <= low or high - low > 36 * 60 * 60:
        return None
    return low, high


def _dataset_edge_value(dataset: Any, index: int) -> Any:
    try:
        value = dataset[index]
        if hasattr(value, "item"):
            return value.item()
        return value
    except Exception:
        return None


def _compound_dataset_edge_value(dataset: Any, index: int, field_name: str) -> Any:
    try:
        value = dataset[index][field_name]
        if hasattr(value, "item"):
            return value.item()
        return value
    except Exception:
        return None


def _clean_timestamp_edges(first: Any, last: Any) -> tuple[float, float] | None:
    try:
        left = float(first)
        right = float(last)
    except Exception:
        return None
    if not (_is_finite(left) and _is_finite(right)):
        return None
    return min(left, right), max(left, right)


def _convert_timestamp_span_to_datetime(
    low: float,
    high: float,
    file_start_time: dt.datetime,
) -> tuple[dt.datetime, dt.datetime] | None:
    epoch_low = _epoch_seconds(low)
    epoch_high = _epoch_seconds(high)
    if epoch_low is not None and epoch_high is not None:
        try:
            return dt.datetime.fromtimestamp(epoch_low), dt.datetime.fromtimestamp(epoch_high)
        except Exception:
            return None

    if low < -60 or high > 36 * 60 * 60:
        return None
    return (
        file_start_time + dt.timedelta(seconds=low),
        file_start_time + dt.timedelta(seconds=high),
    )


def _epoch_seconds(value: float) -> float | None:
    if value > 1e12:
        return value / 1000.0
    if value > 1e9:
        return value
    return None


def _is_finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _parse_started_paths(paths: Iterable[str]) -> list[_StartedPath]:
    items: list[_StartedPath] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = str(Path(str(raw_path)).expanduser())
        if path in seen:
            continue
        seen.add(path)
        start_time = parse_h5_file_start_datetime(Path(path).name)
        if start_time is None:
            continue
        items.append(_StartedPath(path=path, start_time=start_time, parent=str(Path(path).parent)))
    return sorted(items, key=lambda item: (item.parent, item.start_time, item.path))


def _group_started_paths_by_parent(items: list[_StartedPath]) -> list[list[_StartedPath]]:
    groups: dict[str, list[_StartedPath]] = {}
    for item in items:
        groups.setdefault(item.parent, []).append(item)
    return [sorted(group, key=lambda item: (item.start_time, item.path)) for group in groups.values()]


def _next_start_for_path(items: list[_StartedPath], item: _StartedPath) -> dt.datetime | None:
    group = sorted(
        [candidate for candidate in items if candidate.parent == item.parent],
        key=lambda candidate: (candidate.start_time, candidate.path),
    )
    for index, candidate in enumerate(group):
        if candidate.path == item.path:
            return group[index + 1].start_time if index + 1 < len(group) else None
    return None


def _is_adjacent_start_match(
    start_time: dt.datetime,
    next_start_time: dt.datetime | None,
    target_time: dt.datetime,
) -> bool:
    if target_time < start_time - dt.timedelta(seconds=LOOKUP_TIME_TOLERANCE_SECONDS):
        return False
    if next_start_time is not None:
        return target_time < next_start_time + dt.timedelta(seconds=LOOKUP_TIME_TOLERANCE_SECONDS)
    return (target_time - start_time).total_seconds() <= LAST_SEGMENT_COARSE_WINDOW_SECONDS


def _datetime_in_range(
    value: dt.datetime,
    start: dt.datetime,
    end: dt.datetime,
    tolerance_seconds: float,
) -> bool:
    tolerance = dt.timedelta(seconds=tolerance_seconds)
    return start - tolerance <= value <= end + tolerance


def parse_h5_file_start_datetime(file_name: str) -> dt.datetime | None:
    match = re.search(
        r"(20\d{2})[-_](\d{1,2})[-_](\d{1,2})[-_](\d{1,2})[-_](\d{1,2})(?:[-_](\d{1,2}))?",
        str(file_name or ""),
    )
    if not match:
        return None
    try:
        year, month, day, hour, minute, second = match.groups(default="0")
        return dt.datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
        )
    except ValueError:
        return None


def _date_infos_around(date_value: dt.date) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for offset in (-1, 0, 1):
        current = date_value + dt.timedelta(days=offset)
        result.append((current.strftime("%Y%m%d"), current.strftime("%Y-%m-%d")))
    return result


def prompt_if_missing(value: str | None, prompt: str) -> str:
    text = (value or "").strip()
    if text:
        return text
    return input(prompt).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find J6B parsed H5 files.")
    parser.add_argument("--base-dir", default=os.getenv("J6B_BASE_DIR", DEFAULT_J6B_BASE_DIR))
    parser.add_argument("--model", default="", help="车型，例如 C1UL、E2LB、C1")
    parser.add_argument("--car-id", default="", help="数据源中的车号；历史短号仍会兼容")
    parser.add_argument("--date", default="", help="日期，yyyy/mm/dd，例如 2026/08/17")
    parser.add_argument("--time", default="", help="时间，HH:MM:SS，例如 19:38:37；秒不强制匹配")
    parser.add_argument("--all", action="store_true", help="打印所有命中路径；默认也会打印全部路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        car_model = prompt_if_missing(args.model, "请输入车型（支持简写，如 C1）：")
        car_id = prompt_if_missing(args.car_id, "请输入车号：")
        date_value = prompt_if_missing(args.date, "请输入日期（yyyy/mm/dd，如 2026/08/17）：")
        time_value = prompt_if_missing(args.time, "请输入时间（HH:MM:SS，如 19:38:37）：")

        matched_h5, matched_date_dirs, matched_vehicle_dirs = find_j6b_h5(
            base_dir=args.base_dir,
            car_model=car_model,
            car_id=car_id,
            date_value=date_value,
            time_value=time_value,
        )
    except Exception as exc:
        print(f"输入错误：{exc}")
        return 2

    if matched_h5:
        print("\n成功找到 H5 文件路径：")
        for path in matched_h5:
            print(path)
        return 0

    print("\n找不到对应的 H5 文件。")
    if matched_date_dirs:
        print("\n找到了对应车型/日期目录，但没有找到该分钟的 H5：")
        for path in matched_date_dirs[:10]:
            print(path)
    elif matched_vehicle_dirs:
        print("\n找到了车辆目录，但没有找到对应日期/时间的数据：")
        for path in matched_vehicle_dirs[:10]:
            print(path)
    else:
        print("\n车型和车辆编号目录也没有匹配到。")

    print("\n当前搜索根目录：")
    print(args.base_dir)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
