from __future__ import annotations

import json
import os
import subprocess
import sys
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
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    allowed_problem_types,
)
from feishu_config import load_env


STATUS_OPTIONS = [
    {"name": STATUS_PENDING, "hue": "Yellow", "lightness": "Light"},
    {"name": STATUS_RUNNING, "hue": "Blue", "lightness": "Light"},
    {"name": STATUS_DONE, "hue": "Green", "lightness": "Light"},
    {"name": STATUS_FAILED, "hue": "Red", "lightness": "Light"},
]

PROBLEM_TYPE_COLORS = {
    "顿挫": ("Blue", "Light"),
    "重制动": ("Red", "Light"),
    "误制动": ("Orange", "Light"),
    "溜车": ("Yellow", "Light"),
    "稳速未达 Setspeed": ("Purple", "Light"),
    "不合理低速": ("Carmine", "Light"),
    "跟车距离": ("Green", "Light"),
    "横向-猛打": ("Purple", "Light"),
    "功能退出": ("Gray", "Light"),
}

def main() -> None:
    load_env(ROOT / ".env")
    base_token = os.getenv("BATCH_ROOT_CAUSE_BASE_TOKEN", "")
    table_id = os.getenv("BATCH_ROOT_CAUSE_TABLE_ID", "")
    as_identity = os.getenv("BATCH_ROOT_CAUSE_LARK_CLI_AS", "user")
    lark_cli = os.getenv("BATCH_ROOT_CAUSE_LARK_CLI", "lark-cli.cmd")
    if not base_token or not table_id:
        raise SystemExit("missing BATCH_ROOT_CAUSE_BASE_TOKEN or BATCH_ROOT_CAUSE_TABLE_ID")

    fields = _list_fields(lark_cli, as_identity, base_token, table_id)
    fields_by_name = {str(field.get("name")): field for field in fields}

    _ensure_problem_time_hint_fields(lark_cli, as_identity, base_token, table_id, fields_by_name)
    _ensure_text_field(lark_cli, as_identity, base_token, table_id, fields_by_name, AGENT_CAR_ID_FIELD)
    _ensure_text_field(lark_cli, as_identity, base_token, table_id, fields_by_name, AGENT_CAR_MODEL_FIELD)
    _ensure_text_field(lark_cli, as_identity, base_token, table_id, fields_by_name, "Agent_结论概要")
    _ensure_attachment_field(lark_cli, as_identity, base_token, table_id, fields_by_name)
    _ensure_status_field(lark_cli, as_identity, base_token, table_id, fields_by_name)
    _ensure_problem_type_options(lark_cli, as_identity, base_token, table_id, fields_by_name)
    fields = _list_fields(lark_cli, as_identity, base_token, table_id)
    _print_field_env_hints(fields)

    print("batch root-cause Base schema is ready")


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


def _list_fields(lark_cli: str, as_identity: str, base_token: str, table_id: str) -> list[dict[str, Any]]:
    payload = _run_json(
        [
            lark_cli,
            "base",
            "+field-list",
            "--as",
            as_identity,
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--format",
            "json",
        ]
    )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    fields = data.get("fields")
    return fields if isinstance(fields, list) else []


def _create_field(
    lark_cli: str,
    as_identity: str,
    base_token: str,
    table_id: str,
    definition: dict[str, Any],
) -> None:
    _run_json(
        [
            lark_cli,
            "base",
            "+field-create",
            "--as",
            as_identity,
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--json",
            json.dumps(definition, ensure_ascii=False),
            "--format",
            "json",
        ]
    )
    print(f"created field: {definition.get('name')}")


def _ensure_text_field(
    lark_cli: str,
    as_identity: str,
    base_token: str,
    table_id: str,
    fields_by_name: dict[str, dict[str, Any]],
    name: str,
) -> None:
    if name in fields_by_name:
        return
    _create_field(lark_cli, as_identity, base_token, table_id, {"type": "text", "name": name})


def _ensure_attachment_field(
    lark_cli: str,
    as_identity: str,
    base_token: str,
    table_id: str,
    fields_by_name: dict[str, dict[str, Any]],
) -> None:
    if "Agent_分析信号图示" in fields_by_name:
        return
    _create_field(lark_cli, as_identity, base_token, table_id, {"type": "attachment", "name": "Agent_分析信号图示"})


def _ensure_problem_time_hint_fields(
    lark_cli: str,
    as_identity: str,
    base_token: str,
    table_id: str,
    fields_by_name: dict[str, dict[str, Any]],
) -> None:
    _ensure_or_rename_field(
        lark_cli,
        as_identity,
        base_token,
        table_id,
        fields_by_name,
        target_name=AGENT_PROBLEM_DATE_FIELD,
        legacy_name=LEGACY_PROBLEM_DATE_FIELD,
        definition={
            "type": "datetime",
            "name": AGENT_PROBLEM_DATE_FIELD,
            "description": "必填",
            "style": {"format": "yyyy/MM/dd"},
        },
    )
    _ensure_or_rename_field(
        lark_cli,
        as_identity,
        base_token,
        table_id,
        fields_by_name,
        target_name=AGENT_PROBLEM_TIME_FIELD,
        legacy_name=LEGACY_PROBLEM_TIME_FIELD,
        definition={
            "type": "text",
            "name": AGENT_PROBLEM_TIME_FIELD,
            "description": "必填",
        },
    )


def _ensure_or_rename_field(
    lark_cli: str,
    as_identity: str,
    base_token: str,
    table_id: str,
    fields_by_name: dict[str, dict[str, Any]],
    *,
    target_name: str,
    legacy_name: str,
    definition: dict[str, Any],
) -> None:
    if target_name in fields_by_name:
        return

    legacy = fields_by_name.get(legacy_name)
    try:
        if legacy is not None:
            field_id = str(legacy.get("id") or legacy.get("field_id") or "")
            if not field_id:
                raise RuntimeError(f"missing field id for {legacy_name}")
            _update_field(lark_cli, as_identity, base_token, table_id, field_id, definition)
            print(f"renamed field: {legacy_name} -> {target_name}")
            return
        _create_field(lark_cli, as_identity, base_token, table_id, definition)
    except RuntimeError as exc:
        print(f"warning: failed to prepare field {target_name}: {exc}")


def _ensure_status_field(
    lark_cli: str,
    as_identity: str,
    base_token: str,
    table_id: str,
    fields_by_name: dict[str, dict[str, Any]],
) -> None:
    existing = fields_by_name.get("Agent_问题状态")
    definition = {
        "type": "select",
        "name": "Agent_问题状态",
        "multiple": False,
        "options": STATUS_OPTIONS,
    }
    if existing is None:
        _create_field(lark_cli, as_identity, base_token, table_id, definition)
        return
    current = {str(option.get("name")) for option in existing.get("options") or []}
    if {option["name"] for option in STATUS_OPTIONS}.issubset(current):
        return
    _update_field(lark_cli, as_identity, base_token, table_id, str(existing["id"]), definition)
    print("updated field options: Agent_问题状态")


def _ensure_problem_type_options(
    lark_cli: str,
    as_identity: str,
    base_token: str,
    table_id: str,
    fields_by_name: dict[str, dict[str, Any]],
) -> None:
    field = fields_by_name.get("Agent_问题类型")
    if not field:
        options = [_option_for_problem_type(label) for label in allowed_problem_types()]
        _create_field(
            lark_cli,
            as_identity,
            base_token,
            table_id,
            {"type": "select", "name": "Agent_问题类型", "multiple": False, "options": options},
        )
        return

    existing_options = field.get("options") or []
    by_name = {str(option.get("name")): option for option in existing_options if option.get("name")}
    changed = False
    for label in allowed_problem_types():
        if label not in by_name:
            by_name[label] = _option_for_problem_type(label)
            changed = True
    if not changed:
        return

    ordered_names = [str(option.get("name")) for option in existing_options if option.get("name")]
    for label in allowed_problem_types():
        if label not in ordered_names:
            ordered_names.append(label)
    options = [by_name[name] for name in ordered_names]
    _update_field(
        lark_cli,
        as_identity,
        base_token,
        table_id,
        str(field["id"]),
        {"type": "select", "name": "Agent_问题类型", "multiple": False, "options": options},
    )
    print("updated field options: Agent_问题类型")


def _option_for_problem_type(label: str) -> dict[str, str]:
    hue, lightness = PROBLEM_TYPE_COLORS.get(label, ("Gray", "Light"))
    return {"name": label, "hue": hue, "lightness": lightness}


def _update_field(
    lark_cli: str,
    as_identity: str,
    base_token: str,
    table_id: str,
    field_id: str,
    definition: dict[str, Any],
) -> None:
    _run_json(
        [
            lark_cli,
            "base",
            "+field-update",
            "--as",
            as_identity,
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--field-id",
            field_id,
            "--json",
            json.dumps(definition, ensure_ascii=False),
            "--yes",
            "--format",
            "json",
        ]
    )


def _print_field_env_hints(fields: list[dict[str, Any]]) -> None:
    env_names = [
        ("Agent_问题描述", "BATCH_ROOT_CAUSE_FIELD_PROBLEM_ID", ()),
        ("Agent_h5数据路径", "BATCH_ROOT_CAUSE_FIELD_H5_PATH_ID", ()),
        ("Agent_问题类型", "BATCH_ROOT_CAUSE_FIELD_PROBLEM_TYPE_ID", ()),
        (AGENT_PROBLEM_DATE_FIELD, "BATCH_ROOT_CAUSE_FIELD_PROBLEM_DATE_ID", (LEGACY_PROBLEM_DATE_FIELD,)),
        (AGENT_PROBLEM_TIME_FIELD, "BATCH_ROOT_CAUSE_FIELD_PROBLEM_TIME_ID", (LEGACY_PROBLEM_TIME_FIELD,)),
        (AGENT_CAR_ID_FIELD, "BATCH_ROOT_CAUSE_FIELD_CAR_ID_ID", (LEGACY_CAR_ID_FIELD,)),
        (AGENT_CAR_MODEL_FIELD, "BATCH_ROOT_CAUSE_FIELD_CAR_MODEL_ID", (LEGACY_CAR_MODEL_FIELD,)),
        ("Agent_分析结论", "BATCH_ROOT_CAUSE_FIELD_CONCLUSION_ID", ()),
        ("Agent_分析信号图示", "BATCH_ROOT_CAUSE_ATTACHMENT_FIELD_ID", ()),
        ("Agent_问题状态", "BATCH_ROOT_CAUSE_FIELD_STATUS_ID", ()),
        ("Agent_结论概要", "BATCH_ROOT_CAUSE_FIELD_SUMMARY_ID", ()),
    ]
    by_name = {str(field.get("name") or ""): str(field.get("id") or field.get("field_id") or "") for field in fields}
    print("field id hints:")
    for field_name, env_name, aliases in env_names:
        field_id = by_name.get(field_name, "")
        if not field_id:
            for alias in aliases:
                field_id = by_name.get(alias, "")
                if field_id:
                    break
        if field_id:
            print(f"{env_name}={field_id}")


if __name__ == "__main__":
    main()
