#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from batch_root_cause_analysis import (
    AGENT_CAR_ID_FIELD,
    AGENT_CAR_MODEL_FIELD,
    AGENT_PROBLEM_DATE_FIELD,
    AGENT_PROBLEM_TIME_FIELD,
    LEGACY_CAR_ID_FIELD,
    LEGACY_CAR_MODEL_FIELD,
    LEGACY_PROBLEM_DATE_FIELD,
    LEGACY_PROBLEM_TIME_FIELD,
    BatchRootCauseConfig,
    _field_text_with_legacy,
    _list_records,
    _update_record_fields,
    load_batch_root_cause_config,
)


@dataclass(frozen=True)
class ParsedH5Metadata:
    car_id: str
    car_model: str
    problem_date_text: str
    problem_date_timestamp_ms: int
    problem_time: str


@dataclass
class BackfillItem:
    record_id: str
    row_index: int
    h5_path: str
    updates: dict[str, Any]
    parsed: ParsedH5Metadata | None = None
    error: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill Agent_车号/车型/问题日期/问题时间 from Agent_h5数据路径.",
    )
    parser.add_argument("--apply", action="store_true", help="真正写回飞书多维表格；默认只预览。")
    parser.add_argument("--max-rows", type=int, default=0, help="覆盖 BATCH_ROOT_CAUSE_MAX_ROWS。")
    args = parser.parse_args()

    config = load_batch_root_cause_config()
    if not config.is_configured:
        raise SystemExit("missing BATCH_ROOT_CAUSE_BASE_TOKEN or BATCH_ROOT_CAUSE_TABLE_ID")
    if args.max_rows > 0:
        config = BatchRootCauseConfig(**{**config.__dict__, "max_rows": args.max_rows})

    records = _list_records(config)
    items = collect_backfill_items(records, config)

    candidates = [item for item in items if item.updates]
    failures = [item for item in items if item.error]
    print(f"读取记录: {len(records)}")
    print(f"可回填记录: {len(candidates)}")
    print(f"解析失败记录: {len(failures)}")
    print("")

    if candidates:
        print("回填预览:")
        for item in candidates:
            values = ", ".join(f"{key}={_display_value(value)}" for key, value in item.updates.items())
            print(f"- row {item.row_index}: {Path(item.h5_path.replace(chr(92), '/')).name} -> {values}")
        print("")

    if failures:
        print("解析失败:")
        for item in failures:
            print(f"- row {item.row_index}: {item.error} | {item.h5_path}")
        print("")

    if not args.apply:
        print("dry-run only; add --apply to write changes.")
        return 0

    updated = 0
    for item in candidates:
        _update_record_fields(config, item.record_id, item.updates)
        updated += 1
        print(f"updated row {item.row_index}: {', '.join(item.updates)}")
    print(f"完成回填: {updated} 条")
    return 0


def collect_backfill_items(records: list[dict[str, Any]], config: BatchRootCauseConfig) -> list[BackfillItem]:
    items: list[BackfillItem] = []
    for index, record in enumerate(records, start=2):
        fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        record_id = str(record.get("record_id") or record.get("id") or "")
        h5_path = _field_text_with_legacy(fields, config.path_field, config.path_field_id)
        if not h5_path:
            continue
        item = BackfillItem(record_id=record_id, row_index=index, h5_path=h5_path, updates={})
        errors: list[str] = []
        parsed = parse_h5_metadata_partial(h5_path, errors)
        item.parsed = parsed
        if parsed is not None:
            current_car_id = _field_text_with_legacy(
                fields,
                config.car_id_field,
                config.car_id_field_id,
                LEGACY_CAR_ID_FIELD,
            )
            if _should_update_car_id(current_car_id, parsed.car_id):
                item.updates[config.car_id_field] = parsed.car_id
            if not _field_text_with_legacy(
                fields,
                config.car_model_field,
                config.car_model_field_id,
                LEGACY_CAR_MODEL_FIELD,
            ):
                if parsed.car_model:
                    item.updates[config.car_model_field] = parsed.car_model
            if not _field_text_with_legacy(
                fields,
                config.problem_date_field,
                config.problem_date_field_id,
                LEGACY_PROBLEM_DATE_FIELD,
            ):
                item.updates[config.problem_date_field] = parsed.problem_date_timestamp_ms
            if not _field_text_with_legacy(
                fields,
                config.problem_time_field,
                config.problem_time_field_id,
                LEGACY_PROBLEM_TIME_FIELD,
            ):
                item.updates[config.problem_time_field] = parsed.problem_time
        if errors:
            item.error = "; ".join(errors)
        if item.updates or (item.error and not parsed):
            items.append(item)
    return items


def parse_h5_metadata(h5_path: str) -> ParsedH5Metadata:
    normalized = str(h5_path or "").strip().replace("\\", "/")
    if not normalized:
        raise ValueError("empty h5 path")
    parts = [part for part in normalized.split("/") if part]
    file_name = parts[-1] if parts else normalized
    timestamp = _parse_h5_timestamp(file_name)
    car_model, car_id = _parse_vehicle_from_path(parts)
    problem_date = dt.datetime(timestamp.year, timestamp.month, timestamp.day)
    return ParsedH5Metadata(
        car_id=car_id,
        car_model=car_model,
        problem_date_text=problem_date.strftime("%Y-%m-%d 00:00:00"),
        problem_date_timestamp_ms=int(problem_date.timestamp() * 1000),
        problem_time=timestamp.strftime("%H:%M:%S"),
    )


def parse_h5_metadata_partial(h5_path: str, errors: list[str]) -> ParsedH5Metadata | None:
    normalized = str(h5_path or "").strip().replace("\\", "/")
    if not normalized:
        errors.append("empty h5 path")
        return None
    parts = [part for part in normalized.split("/") if part]
    file_name = parts[-1] if parts else normalized
    try:
        timestamp = _parse_h5_timestamp(file_name)
    except Exception as exc:
        errors.append(str(exc))
        return None

    car_model = ""
    car_id = ""
    try:
        car_model, car_id = _parse_vehicle_from_path(parts)
    except Exception as exc:
        errors.append(str(exc))

    problem_date = dt.datetime(timestamp.year, timestamp.month, timestamp.day)
    return ParsedH5Metadata(
        car_id=car_id,
        car_model=car_model,
        problem_date_text=problem_date.strftime("%Y-%m-%d 00:00:00"),
        problem_date_timestamp_ms=int(problem_date.timestamp() * 1000),
        problem_time=timestamp.strftime("%H:%M:%S"),
    )


def _parse_h5_timestamp(file_name: str) -> dt.datetime:
    match = re.search(
        r"(20\d{2})[-_](\d{1,2})[-_](\d{1,2})[-_](\d{1,2})[-_](\d{1,2})(?:[-_](\d{1,2}))?",
        file_name,
    )
    if not match:
        raise ValueError(f"cannot parse date/time from h5 filename: {file_name}")
    year, month, day, hour, minute, second = match.groups(default="0")
    return dt.datetime(
        int(year),
        int(month),
        int(day),
        int(hour),
        int(minute),
        int(second),
    )


def _parse_vehicle_from_path(parts: list[str]) -> tuple[str, str]:
    for raw_part in reversed(parts[:-1]):
        part = _strip_date_prefix(raw_part.strip())
        part = re.sub(r"(?i)(?:[_-]?parsed|_can)$", "", part)
        match = re.search(
            r"(?P<model>[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*?)[-_](?P<car_id>[A-Za-z]{1,4}\d{2}[A-Za-z]{2}\d{3})",
            part,
        )
        if match:
            return match.group("model").replace("_", "-"), match.group("car_id").upper()

    whole_path = "/".join(parts)
    car_match = re.search(r"(?P<car_id>[A-Za-z]{1,4}\d{2}[A-Za-z]{2}\d{3})", whole_path, flags=re.IGNORECASE)
    if not car_match:
        raise ValueError("cannot parse vehicle id from path")
    car_id = car_match.group("car_id").upper()
    for raw_part in reversed(parts[:-1]):
        part = _strip_date_prefix(raw_part.strip())
        part_upper = part.upper()
        if car_id in part_upper:
            model = part[: part_upper.index(car_id)].rstrip("-_")
            model = re.sub(r"[-_][A-Za-z]{1,4}\d{2}[A-Za-z]{2}$", "", model)
            if model:
                return model.replace("_", "-"), car_id
    raise ValueError("cannot parse vehicle model from path")


def _should_update_car_id(current: str, parsed: str) -> bool:
    parsed_text = str(parsed or "").strip().upper()
    if not parsed_text:
        return False
    current_text = str(current or "").strip().upper()
    if not current_text:
        return True
    if current_text == parsed_text:
        return False
    parsed_tail_match = re.search(r"(?P<tail>\d{3})$", parsed_text)
    parsed_tail = parsed_tail_match.group("tail") if parsed_tail_match else ""
    if current_text.isdigit() and parsed_tail and current_text.zfill(3) == parsed_tail:
        return True
    if re.fullmatch(r"[A-Z]{2}\d{1,3}", current_text) and parsed_tail and current_text[-3:].zfill(3) == parsed_tail:
        return True
    return False


def _strip_date_prefix(value: str) -> str:
    return re.sub(r"^(?:20\d{6}|20\d{2}[-_]\d{1,2}[-_]\d{1,2})[-_]", "", value)


def _display_value(value: Any) -> str:
    if isinstance(value, int) and value > 1_000_000_000_000:
        return dt.datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d 00:00:00")
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
