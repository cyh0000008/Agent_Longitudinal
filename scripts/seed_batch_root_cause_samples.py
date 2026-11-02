from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from batch_root_cause_analysis import (
    STATUS_PENDING,
    BatchRootCauseConfig,
    allowed_problem_types,
    load_batch_root_cause_config,
)
from feishu_config import load_env


ROOT_CAUSE_CASES = [
    ("顿挫", "jerkiness.h5", "样例批量测试：分析顿挫原因"),
    ("重制动", "heavy_braking.h5", "样例批量测试：分析重制动原因"),
    ("误制动", "false_braking.h5", "样例批量测试：分析误制动原因"),
    ("漏制动", "missed_braking.h5", "样例批量测试：分析漏制动原因"),
    ("溜车", "rollback.h5", "样例批量测试：分析溜车原因"),
    ("稳速未达 Setspeed", "set_speed_not_reached.h5", "样例批量测试：分析稳速未达 Setspeed 原因"),
    ("不合理低速", "unreasonable_low_speed.h5", "样例批量测试：分析不合理低速原因"),
    ("跟车距离", "following_distance.h5", "样例批量测试：分析跟车距离异常原因"),
]

FUNCTION_EXIT_CASES = [
    ("功能退出", "acc_exit_brake_pedal.h5", "样例批量测试：分析 ACC 因制动踏板介入退出的原因"),
    ("功能退出", "acc_exit_esp_off.h5", "样例批量测试：分析 ACC 因 ESP Off 退出的原因"),
    ("功能退出", "lcc_exit_curve_radius.h5", "样例批量测试：分析 LCC 因弯道半径过小退出的原因"),
]

LATERAL_CASES = [
    ("横向-猛打", "lateral_sudden_steering_request_lane.h5", "样例批量测试：分析 LCC 方向盘猛打是否由车道线跳变触发"),
]


def main() -> None:
    load_env(ROOT / ".env")
    config = load_batch_root_cause_config()
    if not config.base_token or not config.table_id:
        raise SystemExit("missing BATCH_ROOT_CAUSE_BASE_TOKEN or BATCH_ROOT_CAUSE_TABLE_ID")

    _generate_samples()

    allowed = set(allowed_problem_types())
    records = _list_records(config)
    existing = {
        (
            _field_text(record, config.problem_field, config.problem_field_id),
            _field_text(record, config.path_field, config.path_field_id),
            _field_text(record, config.type_field, config.type_field_id),
        ): str(record.get("record_id") or record.get("id") or "")
        for record in records
    }

    samples = _sample_records(allowed)
    created = 0
    updated = 0
    for fields in samples:
        key = (fields["Agent_问题描述"], fields["Agent_h5数据路径"], fields["Agent_问题类型"])
        record_id = existing.get(key)
        payload = dict(fields)
        payload["Agent_问题状态"] = STATUS_PENDING
        payload["Agent_结论概要"] = ""
        payload["Agent_分析结论"] = ""
        if record_id:
            _upsert_record(config, payload, record_id=record_id)
            updated += 1
        else:
            _upsert_record(config, payload)
            created += 1

    print(f"seeded batch samples: created={created}, updated={updated}, total={len(samples)}")


def _generate_samples() -> None:
    runpy.run_path(str(ROOT / "scripts" / "generate_root_cause_synthetic_report.py"), run_name="__main__")
    runpy.run_path(str(ROOT / "scripts" / "generate_function_exit_synthetic_cases.py"), run_name="__main__")
    runpy.run_path(str(ROOT / "scripts" / "generate_lateral_sudden_steering_synthetic_case.py"), run_name="__main__")


def _sample_records(allowed: set[str]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    sample_root = ROOT / "sample_data" / "batch_root_cause"
    root_case_dir = sample_root / "root_cause_synthetic_cases"
    function_exit_dir = sample_root / "function_exit_synthetic_cases"

    for problem_type, file_name, description in _root_cause_cases_from_summary(root_case_dir):
        if problem_type in allowed:
            records.append(
                {
                    "Agent_问题描述": description,
                    "Agent_h5数据路径": str(root_case_dir / file_name),
                    "Agent_问题类型": problem_type,
                }
            )
    for problem_type, file_name, description in FUNCTION_EXIT_CASES:
        if problem_type in allowed:
            records.append(
                {
                    "Agent_问题描述": description,
                    "Agent_h5数据路径": str(function_exit_dir / file_name),
                    "Agent_问题类型": problem_type,
                }
            )
    for problem_type, file_name, description in LATERAL_CASES:
        if problem_type in allowed:
            records.append(
                {
                    "Agent_问题描述": description,
                    "Agent_h5数据路径": str(sample_root / file_name),
                    "Agent_问题类型": problem_type,
                }
            )
    return records


def _root_cause_cases_from_summary(root_case_dir: Path) -> list[tuple[str, str, str]]:
    summary_path = root_case_dir / "root_cause_synthetic_summary.json"
    if not summary_path.exists():
        return ROOT_CAUSE_CASES

    try:
        items = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ROOT_CAUSE_CASES

    cases: list[tuple[str, str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        problem_type = str(item.get("label") or "").strip()
        h5_file = Path(str(item.get("h5_file") or "")).name
        if not problem_type or not h5_file:
            continue
        request_text = str(item.get("request_text") or f"分析{problem_type}原因").strip()
        scenario = str(item.get("scenario") or "基础触发").strip()
        description = f"样例批量测试：{request_text}（{scenario}）"
        cases.append((problem_type, h5_file, description))

    return cases or ROOT_CAUSE_CASES


def _run_json(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"lark-cli returned non-json output: {completed.stdout}") from exc
    if payload.get("ok") is False:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False))
    return payload


def _list_records(config: BatchRootCauseConfig) -> list[dict[str, Any]]:
    payload = _run_json(
        [
            config.lark_cli,
            "base",
            "+record-list",
            "--as",
            config.cli_identity,
            "--base-token",
            config.base_token,
            "--table-id",
            config.table_id,
            "--field-id",
            _field_selector(config.problem_field, config.problem_field_id),
            "--field-id",
            _field_selector(config.path_field, config.path_field_id),
            "--field-id",
            _field_selector(config.type_field, config.type_field_id),
            "--format",
            "json",
        ]
    )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    items = data.get("items") or data.get("records") or data.get("record_list") or []
    if isinstance(items, list) and items:
        return [item for item in items if isinstance(item, dict)]

    matrix_rows = data.get("data")
    field_names = data.get("fields") or []
    record_ids = data.get("record_id_list") or []
    if not isinstance(matrix_rows, list) or not isinstance(field_names, list):
        return []

    records: list[dict[str, Any]] = []
    for index, row in enumerate(matrix_rows):
        if not isinstance(row, list):
            continue
        fields: dict[str, Any] = {}
        for field_name, value in zip(field_names, row):
            fields[str(field_name)] = value
        record_id = record_ids[index] if index < len(record_ids) else ""
        records.append({"record_id": str(record_id or ""), "fields": fields})
    return records


def _upsert_record(
    config: BatchRootCauseConfig,
    fields: dict[str, str],
    *,
    record_id: str | None = None,
) -> None:
    command = [
        config.lark_cli,
        "base",
        "+record-upsert",
        "--as",
        config.cli_identity,
        "--base-token",
        config.base_token,
        "--table-id",
        config.table_id,
        "--json",
        json.dumps(_record_write_payload(config, fields), ensure_ascii=True),
        "--format",
        "json",
    ]
    if record_id:
        command.extend(["--record-id", record_id])
    _run_json(command)


def _field_selector(name: str, field_id: str = "") -> str:
    return str(field_id or name).strip()


def _record_write_payload(config: BatchRootCauseConfig, fields: dict[str, str]) -> dict[str, str]:
    key_map = {
        config.problem_field: config.problem_field_id,
        config.path_field: config.path_field_id,
        config.type_field: config.type_field_id,
        config.status_field: config.status_field_id,
        config.summary_field: config.summary_field_id,
        config.conclusion_field: config.conclusion_field_id,
    }
    payload: dict[str, str] = {}
    for key, value in fields.items():
        payload[_field_selector(str(key), key_map.get(str(key), ""))] = value
    return payload


def _field_text(record: dict[str, Any], *field_names: str) -> str:
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
    value = None
    lowered = {str(key).lower(): key for key in fields}
    for field_name in field_names:
        if not field_name:
            continue
        value = fields.get(field_name)
        if value is None:
            key = lowered.get(str(field_name).lower())
            value = fields.get(key) if key else None
        if value is not None:
            break
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("text", "name", "value"):
            item = value.get(key)
            if item is not None:
                return str(item).strip()
    if isinstance(value, list):
        return ",".join(str(item.get("name") if isinstance(item, dict) and item.get("name") else item) for item in value).strip()
    return str(value).strip()


if __name__ == "__main__":
    main()
