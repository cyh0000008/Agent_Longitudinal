from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from batch_root_cause_analysis import (  # noqa: E402
    FUNCTION_EXIT_PROBLEM_TYPE,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    _upload_attachments,
    _value_text,
    load_batch_root_cause_config,
    run_single_function_exit_analysis,
)
from deepseek_root_cause_analysis import run_deepseek_root_cause_investigation  # noqa: E402
from feishu_config import get_feishu_credentials, load_env  # noqa: E402


OUTPUT_ROOT = ROOT / "analysis_outputs" / "deepseek_base_samples"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Run all sample rows in the configured Feishu Base.")
    parser.add_argument("--max-signals", type=int, default=32)
    parser.add_argument("--record-id", action="append", default=[], help="Only run the given Base record ID. Repeatable.")
    parser.add_argument("--run-label", default="", help="Suffix progress/report files for a partial rerun.")
    parser.add_argument("--llm-timeout-seconds", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_env()
    os.environ["LLM_TIMEOUT_SECONDS"] = str(max(5, args.llm_timeout_seconds))

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    suffix = f"_{_safe_label(args.run_label)}" if args.run_label else ""
    progress_path = OUTPUT_ROOT / f"progress{suffix}.json"
    report_path = OUTPUT_ROOT / f"run_report{suffix}.json"
    cfg = load_batch_root_cause_config()
    token = _tenant_access_token()
    records = _list_records(token, cfg)
    sample_rows = [_sample_row(record, cfg) for record in records]
    sample_rows = [row for row in sample_rows if row is not None]
    target_record_ids = _target_record_ids(args.record_id)
    if target_record_ids:
        sample_rows = [row for row in sample_rows if row["record_id"] in target_record_ids]
        found_record_ids = {row["record_id"] for row in sample_rows}
        missing_record_ids = sorted(target_record_ids - found_record_ids)
        if missing_record_ids:
            raise RuntimeError(f"sample record IDs not found: {', '.join(missing_record_ids)}")

    results: list[dict[str, Any]] = []
    total = len(sample_rows)
    _write_progress(progress_path, total=total, done=0, current="", results=results)
    print(f"Found {total} sample rows: {dict(Counter(row['problem_type'] for row in sample_rows))}", flush=True)

    for index, row in enumerate(sample_rows, start=1):
        current_label = f"{index}/{total} {row['problem_type']} {Path(row['raw_path']).name}"
        print(f"[{index}/{total}] start {row['record_id']} {row['problem_type']} {row['raw_path']}", flush=True)
        _write_progress(progress_path, total=total, done=index - 1, current=current_label, results=results)

        result_row = {
            "record_id": row["record_id"],
            "problem_type": row["problem_type"],
            "description": row["description"],
            "raw_path": row["raw_path"],
            "resolved_path": "",
            "status": "pending",
            "summary": "",
            "error": "",
        }
        started_at = time.time()
        try:
            resolved_path = _resolve_sample_path(row["raw_path"])
            result_row["resolved_path"] = str(resolved_path)
            if args.dry_run:
                result_row["status"] = "dry_run"
                result_row["summary"] = "dry-run: not executed"
                continue

            _update_record(token, cfg, row["record_id"], {cfg.status_field_id: STATUS_RUNNING})
            if row["problem_type"] == FUNCTION_EXIT_PROBLEM_TYPE:
                analysis = run_single_function_exit_analysis(
                    data_path=resolved_path,
                    problem_description=row["description"],
                )
                conclusion = "功能退出样例分析\n\n" + analysis.conclusion
                context = analysis.context
                summary_source = analysis.summary
                root_cause_factor = analysis.root_cause_factor
                key_signals = analysis.key_signals
                image_paths = analysis.image_paths
            else:
                investigation = run_deepseek_root_cause_investigation(
                    h5_path=resolved_path,
                    problem_type=row["problem_type"],
                    problem_description=row["description"],
                    max_signals=args.max_signals,
                    image_root=OUTPUT_ROOT / f"{row['record_id']}_plots",
                )
                conclusion = investigation.conclusion
                context = investigation.context
                summary_source = investigation.summary
                root_cause_factor = investigation.root_cause_factor
                key_signals = investigation.key_signals_text
                image_paths = investigation.image_paths

            summary = _compact_summary(summary_source)
            _write_artifacts(row["record_id"], conclusion, context)
            _update_record(
                token,
                cfg,
                row["record_id"],
                {
                    cfg.status_field_id: STATUS_DONE,
                    cfg.summary_field_id: summary,
                    cfg.conclusion_field_id: conclusion,
                },
            )
            uploaded_images = _upload_attachments(cfg, row["record_id"], image_paths)
            result_row["status"] = "success"
            result_row["summary"] = summary
            result_row["root_cause_factor"] = root_cause_factor
            result_row["key_signals"] = key_signals
            result_row["image_count"] = len(image_paths)
            result_row["uploaded_images"] = uploaded_images
        except Exception as exc:
            error = str(exc)
            result_row["status"] = "failed"
            result_row["error"] = error
            result_row["summary"] = _compact_summary(error)
            if not args.dry_run:
                try:
                    _update_record(
                        token,
                        cfg,
                        row["record_id"],
                        {
                            cfg.status_field_id: STATUS_FAILED,
                            cfg.summary_field_id: result_row["summary"],
                            cfg.conclusion_field_id: f"样例分析失败：{error}",
                        },
                    )
                except Exception as update_exc:
                    result_row["error"] = f"{error}; base update failed: {update_exc}"
        finally:
            result_row["elapsed_seconds"] = round(time.time() - started_at, 2)
            results.append(result_row)
            report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            _write_progress(progress_path, total=total, done=index, current=current_label, results=results)
            print(
                f"[{index}/{total}] {result_row['status']} {row['record_id']} "
                f"{result_row.get('summary') or result_row.get('error')}",
                flush=True,
            )

    print("DONE", json.dumps(_summary_counts(results), ensure_ascii=False), flush=True)


def _tenant_access_token() -> str:
    app_id, app_secret = get_feishu_credentials()
    response = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu auth failed: {data}")
    return str(data["tenant_access_token"])


def _list_records(token: str, cfg: Any) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{cfg.base_token}/tables/{cfg.table_id}/records"
    records: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        response = requests.get(url, headers=headers, params=params, timeout=60)
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"Base record list failed: {body}")
        data = body.get("data") or {}
        records.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "")
        if not page_token:
            break
    return records


def _sample_row(record: dict[str, Any], cfg: Any) -> dict[str, str] | None:
    fields = record.get("fields") or {}
    description = _field_text(fields, cfg.problem_field, cfg.problem_field_id)
    raw_path = _field_text(fields, cfg.path_field, cfg.path_field_id)
    problem_type = _field_text(fields, cfg.type_field, cfg.type_field_id)
    status = _field_text(fields, cfg.status_field, cfg.status_field_id)
    sample_text = f"{description} {raw_path}".lower()
    is_sample = any(marker in sample_text for marker in ("样例", "sample_data", "synthetic", "批量测试"))
    if not is_sample:
        return None
    return {
        "record_id": str(record.get("record_id") or ""),
        "description": description,
        "raw_path": raw_path,
        "problem_type": problem_type,
        "status": status,
    }


def _field_text(fields: dict[str, Any], name: str, field_id: str) -> str:
    if field_id and field_id in fields:
        return _value_text(fields.get(field_id))
    return _value_text(fields.get(name))


def _resolve_sample_path(raw_path: str) -> Path:
    value = str(raw_path or "").strip().strip('"').strip("'")
    path = Path(value)
    if path.is_file():
        return path.resolve()
    normalized = value.replace("\\", "/")
    marker = "/sample_data/"
    if marker in normalized:
        suffix = normalized.split(marker, 1)[1]
        candidate = ROOT / "sample_data" / Path(*suffix.split("/"))
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"sample H5 not found: {raw_path}")


def _update_record(token: str, cfg: Any, record_id: str, fields: dict[str, Any]) -> None:
    key_map = {
        cfg.status_field_id: cfg.status_field,
        cfg.summary_field_id: cfg.summary_field,
        cfg.conclusion_field_id: cfg.conclusion_field,
    }
    payload_fields = {
        key_map.get(key, key): value
        for key, value in fields.items()
        if key and value is not None
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{cfg.base_token}"
        f"/tables/{cfg.table_id}/records/{record_id}"
    )
    response = requests.put(url, headers=headers, json={"fields": payload_fields}, timeout=60)
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 0:
        raise RuntimeError(f"Base record update failed: {body}")


def _write_artifacts(record_id: str, conclusion: str, context: str) -> None:
    (OUTPUT_ROOT / f"{record_id}.md").write_text(conclusion, encoding="utf-8")
    (OUTPUT_ROOT / f"{record_id}.json").write_text(context or "{}", encoding="utf-8")


def _compact_summary(text: str, limit: int = 80) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _write_progress(
    path: Path,
    *,
    total: int,
    done: int,
    current: str,
    results: list[dict[str, Any]],
) -> None:
    payload = {
        "total": total,
        "done": done,
        "current": current,
        "summary": _summary_counts(results),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _summary_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counter = Counter(str(item.get("status") or "unknown") for item in results)
    return dict(counter)


def _target_record_ids(values: list[str]) -> set[str]:
    record_ids: set[str] = set()
    for value in values:
        for item in str(value or "").replace("，", ",").split(","):
            item = item.strip()
            if item:
                record_ids.add(item)
    return record_ids


def _safe_label(value: str) -> str:
    return "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in value.strip())[:80]


if __name__ == "__main__":
    main()
