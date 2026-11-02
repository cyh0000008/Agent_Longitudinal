#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_J6B_BLF_BASE_DIR = os.getenv("BATCH_ROOT_CAUSE_BLF_SEARCH_BASE_DIR", "")
LOOKUP_TIME_TOLERANCE_SECONDS = 2.0
try:
    LAST_SEGMENT_COARSE_WINDOW_SECONDS = max(
        1.0,
        float(os.getenv("J6B_BLF_LAST_SEGMENT_COARSE_WINDOW_SECONDS", str(5 * 60))),
    )
except ValueError:
    LAST_SEGMENT_COARSE_WINDOW_SECONDS = 5 * 60

@dataclass(frozen=True)
class BlfCandidate:
    path: str
    start_time: dt.datetime
    next_start_time: dt.datetime | None
    matched_by_adjacent_start: bool


@dataclass(frozen=True)
class _StartedPath:
    path: str
    start_time: dt.datetime
    parent: str


def find_j6b_blf(
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

    root_dir = base_dir or DEFAULT_J6B_BLF_BASE_DIR
    if not root_dir:
        raise ValueError("请通过 --base-dir 或 BATCH_ROOT_CAUSE_BLF_SEARCH_BASE_DIR 配置 BLF 数据目录。")
    root = Path(root_dir).expanduser()
    target_time = parse_problem_datetime(date_value, time_value)
    vehicle_dirs = _matched_vehicle_dirs(root, model, normalize_car_id(car_id))
    date_dirs = _matched_date_dirs(vehicle_dirs, target_time.date())
    search_roots = date_dirs or vehicle_dirs
    candidate_blf = _collect_blf_candidates(search_roots, target_time.date())
    matched_blf = [candidate.path for candidate in select_blf_candidates_for_time(candidate_blf, target_time)]
    return matched_blf, sorted(str(path) for path in set(date_dirs)), sorted(str(path) for path in set(vehicle_dirs))


def select_blf_candidates_for_time(paths: Iterable[str | Path], target_time: dt.datetime) -> list[BlfCandidate]:
    started_paths = _parse_started_paths(paths)
    if not started_paths:
        return []
    groups = _group_started_paths_by_parent(started_paths)
    result: list[BlfCandidate] = []
    for group in groups:
        for index, item in enumerate(group):
            next_start = group[index + 1].start_time if index + 1 < len(group) else None
            if _is_adjacent_start_match(item.start_time, next_start, target_time):
                result.append(
                    BlfCandidate(
                        path=item.path,
                        start_time=item.start_time,
                        next_start_time=next_start,
                        matched_by_adjacent_start=True,
                    )
                )
    if not result:
        nearest = sorted(started_paths, key=lambda item: abs((item.start_time - target_time).total_seconds()))
        for item in nearest[:3]:
            if abs((item.start_time - target_time).total_seconds()) <= LAST_SEGMENT_COARSE_WINDOW_SECONDS:
                result.append(
                    BlfCandidate(
                        path=item.path,
                        start_time=item.start_time,
                        next_start_time=_next_start_for_path(started_paths, item),
                        matched_by_adjacent_start=False,
                    )
                )
    return sorted(
        result,
        key=lambda candidate: (abs((candidate.start_time - target_time).total_seconds()), candidate.path),
    )


def output_dir_for_blf(
    *,
    blf_path: str | Path,
    blf_base_dir: str | Path,
    h5_base_dir: str | Path,
    car_model: str = "",
    car_id: str = "",
    date_value: str = "",
) -> Path:
    blf = Path(blf_path).expanduser().resolve()
    source_root = Path(blf_base_dir).expanduser().resolve()
    target_root = Path(h5_base_dir).expanduser()
    try:
        relative_parent = blf.parent.relative_to(source_root)
    except ValueError:
        relative_parent = Path(blf.parent.name)
    source_parts = [part for part in relative_parent.parts if _normalized_token(part) != "parsed"]
    vehicle_name = _h5_vehicle_folder_name(source_parts, car_model=car_model, car_id=car_id)
    date_compact = _compact_output_date(date_value, blf.name)
    return target_root / vehicle_name / f"{date_compact}-{vehicle_name}_parsed"


def normalize_date(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    match = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", text)
    if not match:
        raise ValueError("日期格式应为 yyyy/mm/dd，例如 2026/08/17。")
    year, month, day = match.groups()
    compact = f"{int(year):04d}{int(month):02d}{int(day):02d}"
    return compact, f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"


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
        candidates.append(full_match.group(1))
    if text.isdigit():
        candidates.append(text.zfill(3))
    return list(dict.fromkeys(candidates))


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


def parse_blf_file_start_datetime(file_name: str) -> dt.datetime | None:
    match = re.search(
        r"(20\d{2})[-_](\d{1,2})[-_](\d{1,2})[-_](\d{1,2})[-_](\d{1,2})(?:[-_](\d{1,2}))?",
        str(file_name or ""),
    )
    if not match:
        return None
    try:
        year, month, day, hour, minute, second = match.groups(default="0")
        return dt.datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
    except ValueError:
        return None


def _matched_vehicle_dirs(root: Path, model: str, car_ids: list[str]) -> list[Path]:
    if not root.is_dir():
        return []
    model_token = _normalized_token(model)
    car_tokens = [_normalized_token(item) for item in car_ids]
    result: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir() or _normalized_token(child.name) == "parsed":
            continue
        name_token = _normalized_token(child.name)
        if model_token in name_token and any(car_token in name_token for car_token in car_tokens):
            result.append(child)
    return sorted(result)


def _matched_date_dirs(vehicle_dirs: Iterable[Path], date_value: dt.date) -> list[Path]:
    date_tokens = {
        _normalized_token(date_value.strftime("%Y%m%d")),
        _normalized_token(date_value.strftime("%Y-%m-%d")),
    }
    result: list[Path] = []
    for vehicle_dir in vehicle_dirs:
        for current, dirnames, _filenames in os.walk(vehicle_dir):
            dirnames[:] = [name for name in dirnames if _normalized_token(name) != "parsed"]
            current_path = Path(current)
            if any(token in _normalized_token(current_path.name) for token in date_tokens):
                result.append(current_path)
                dirnames[:] = []
    return sorted(set(result))


def _collect_blf_candidates(search_roots: Iterable[Path], target_date: dt.date) -> list[str]:
    accepted_date = target_date.strftime("%Y%m%d")
    result: list[str] = []
    for search_root in search_roots:
        for current, dirnames, filenames in os.walk(search_root):
            dirnames[:] = [name for name in dirnames if _normalized_token(name) != "parsed"]
            for filename in filenames:
                if not filename.lower().endswith(".blf"):
                    continue
                path = Path(current) / filename
                start_time = parse_blf_file_start_datetime(filename)
                if start_time is None or start_time.strftime("%Y%m%d") != accepted_date:
                    continue
                result.append(str(path))
    return sorted(set(result))


def _parse_started_paths(paths: Iterable[str | Path]) -> list[_StartedPath]:
    result: list[_StartedPath] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = str(Path(raw_path).expanduser())
        key = path.replace("\\", "/").lower()
        if key in seen:
            continue
        seen.add(key)
        start_time = parse_blf_file_start_datetime(Path(path).name)
        if start_time is None:
            continue
        result.append(_StartedPath(path=path, start_time=start_time, parent=str(Path(path).parent)))
    return sorted(result, key=lambda item: (item.parent, item.start_time, item.path))


def _group_started_paths_by_parent(items: list[_StartedPath]) -> list[list[_StartedPath]]:
    groups: dict[str, list[_StartedPath]] = {}
    for item in items:
        groups.setdefault(item.parent, []).append(item)
    return [sorted(group, key=lambda item: (item.start_time, item.path)) for group in groups.values()]


def _next_start_for_path(items: list[_StartedPath], item: _StartedPath) -> dt.datetime | None:
    group = sorted([candidate for candidate in items if candidate.parent == item.parent], key=lambda candidate: (candidate.start_time, candidate.path))
    for index, candidate in enumerate(group):
        if candidate.path == item.path:
            return group[index + 1].start_time if index + 1 < len(group) else None
    return None


def _is_adjacent_start_match(start_time: dt.datetime, next_start_time: dt.datetime | None, target_time: dt.datetime) -> bool:
    if target_time < start_time - dt.timedelta(seconds=LOOKUP_TIME_TOLERANCE_SECONDS):
        return False
    if next_start_time is not None:
        return target_time < next_start_time + dt.timedelta(seconds=LOOKUP_TIME_TOLERANCE_SECONDS)
    return (target_time - start_time).total_seconds() <= LAST_SEGMENT_COARSE_WINDOW_SECONDS


def _date_infos_around(date_value: dt.date) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for offset in (-1, 0, 1):
        current = date_value + dt.timedelta(days=offset)
        result.append((current.strftime("%Y%m%d"), current.strftime("%Y-%m-%d")))
    return result


def _normalized_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _is_capture_tool_dir(value: str) -> bool:
    token = _normalized_token(value)
    return any(marker in token for marker in ("adasdata", "camplayerdata", "canapedata", "apadata", "ddt"))


def _h5_vehicle_folder_name(source_parts: list[str], *, car_model: str, car_id: str) -> str:
    car_id_text = str(car_id or "").strip().upper()
    model_variant = _model_variant_from_source(source_parts, car_model)
    return f"{model_variant}-{car_id_text or 'UNKNOWN'}"


def _model_variant_from_source(source_parts: list[str], car_model: str) -> str:
    model = str(car_model or "").strip().upper().replace("_", "-")
    if not model:
        return "UNKNOWN"
    model_base = model.split("-", 1)[0]
    pattern = re.compile(rf"(?i)(?<![A-Z0-9])({re.escape(model_base)}(?:[-_]?\d+)?)(?![A-Z0-9])")
    for part in source_parts:
        if _is_capture_tool_dir(part):
            continue
        match = pattern.search(part)
        if match:
            return match.group(1).replace("_", "-").upper()
    return model


def _compact_output_date(date_value: str, blf_file_name: str) -> str:
    if str(date_value or "").strip():
        compact, _dash = normalize_date(date_value)
        return compact
    start_time = parse_blf_file_start_datetime(blf_file_name)
    if start_time is not None:
        return start_time.strftime("%Y%m%d")
    return dt.datetime.now().strftime("%Y%m%d")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find J6B BLF files by vehicle, date, and target time.")
    parser.add_argument("--base-dir", default=os.getenv("BATCH_ROOT_CAUSE_BLF_SEARCH_BASE_DIR", DEFAULT_J6B_BLF_BASE_DIR))
    parser.add_argument("--model", default="", help="车型，例如 C1UL、E2LB")
    parser.add_argument("--car-id", default="", help="数据源中的车号")
    parser.add_argument("--date", default="", help="日期，yyyy/mm/dd，例如 2026/08/09")
    parser.add_argument("--time", default="", help="时间，HH:MM:SS，例如 09:26:00")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        matched_blf, matched_date_dirs, matched_vehicle_dirs = find_j6b_blf(
            base_dir=args.base_dir,
            car_model=args.model,
            car_id=args.car_id,
            date_value=args.date,
            time_value=args.time,
        )
    except Exception as exc:
        print(f"输入错误：{exc}")
        return 2

    if matched_blf:
        print("\n成功找到 BLF 文件路径：")
        for path in matched_blf:
            print(path)
        return 0

    print("\n找不到对应的 BLF 文件。")
    if matched_date_dirs:
        print("\n找到了对应车型/日期目录，但没有找到覆盖该时间的 BLF：")
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
