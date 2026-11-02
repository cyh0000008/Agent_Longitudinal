from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from openpyxl import load_workbook
from openpyxl.styles import Alignment

from batch_root_cause_analysis import (
    AGENT_CAR_ID_FIELD,
    AGENT_CAR_MODEL_FIELD,
    BatchDeepSeekAPIUnavailable,
    AGENT_PROBLEM_DATE_FIELD,
    AGENT_PROBLEM_TIME_FIELD,
    DEFAULT_JIRA_LINK_FIELD,
    H5_AUTO_LOOKUP_NOT_FOUND_MESSAGE,
    LEGACY_CAR_ID_FIELD,
    LEGACY_CAR_MODEL_FIELD,
    LEGACY_PROBLEM_DATE_FIELD,
    LEGACY_PROBLEM_TIME_FIELD,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    _cleanup_empty_runtime_roots,
    _cleanup_generated_paths,
    _problem_type_requires_llm,
    allowed_problem_types,
    run_single_batch_analysis,
)
from llm_service import ensure_llm_api_available, get_llm_api_unavailable_message
from runtime_paths import ensure_writable_dir, ensure_writable_file_path, get_runtime_subdir, safe_runtime_name

try:
    from scripts.find_j6b_h5_path import (
        DEFAULT_J6B_BASE_DIR,
        find_j6b_h5,
        parse_problem_datetime as parse_h5_lookup_problem_datetime,
        select_h5_candidates_for_time,
    )
except Exception:  # pragma: no cover - packaged deployments include this script.
    DEFAULT_J6B_BASE_DIR = os.getenv("BATCH_ROOT_CAUSE_H5_SEARCH_BASE_DIR", "")
    find_j6b_h5 = None
    parse_h5_lookup_problem_datetime = None
    select_h5_candidates_for_time = None


WINDOWS_ABS_RE = re.compile(r"^[a-zA-Z]:[\\/]")
UNC_RE = re.compile(r"^[\\/]{2}[^\\/]+[\\/][^\\/]+")


@dataclass
class LocalBatchExcelConfig:
    problem_field: str = "Agent_问题描述"
    path_field: str = "Agent_h5数据路径"
    type_field: str = "Agent_问题类型"
    status_field: str = "Agent_问题状态"
    summary_field: str = "Agent_结论概要"
    conclusion_field: str = "Agent_分析结论"
    image_field: str = "Agent_分析信号图示"
    problem_date_field: str = AGENT_PROBLEM_DATE_FIELD
    problem_time_field: str = AGENT_PROBLEM_TIME_FIELD
    car_id_field: str = AGENT_CAR_ID_FIELD
    car_model_field: str = AGENT_CAR_MODEL_FIELD
    jira_link_field: str = DEFAULT_JIRA_LINK_FIELD
    max_rows: int = 50
    database_name: str = ""
    data_root: str = ""
    h5_search_base_dir: str = DEFAULT_J6B_BASE_DIR
    path_map: dict[str, str] = field(default_factory=dict)


@dataclass
class LocalBatchRowResult:
    row_index: int
    problem_description: str
    h5_path: str
    resolved_h5_path: str
    problem_type: str
    status: str
    conclusion: str
    summary: str = ""
    image_paths: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class LocalBatchRunResult:
    input_excel: str
    output_excel: str
    output_zip: str
    output_dir: str
    total_rows: int
    pending_rows: int
    processed_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    rows: list[LocalBatchRowResult] = field(default_factory=list)


class LocalH5AutoLookupNotFound(FileNotFoundError):
    """Raised when metadata-based H5 lookup does not find a parsed file yet."""


def get_local_batch_runtime_dir() -> Path:
    return get_runtime_subdir("local_batch_root_cause")


def load_local_batch_config(
    *,
    path_map_text: str = "",
    data_root: str = "",
    database_name: str = "",
    max_rows: int | None = None,
) -> LocalBatchExcelConfig:
    env_map = load_path_map_from_env()
    user_map = parse_path_map_text(path_map_text)
    merged_map = {**env_map, **user_map}
    configured_data_root = data_root or os.getenv("LOCAL_BATCH_DATA_ROOT", "").strip()
    h5_search_base_dir = (
        os.getenv("LOCAL_BATCH_H5_SEARCH_BASE_DIR", "").strip()
        or os.getenv("BATCH_ROOT_CAUSE_H5_SEARCH_BASE_DIR", "").strip()
        or configured_data_root
        or DEFAULT_J6B_BASE_DIR
    )
    return LocalBatchExcelConfig(
        max_rows=max_rows or _env_int("LOCAL_BATCH_MAX_ROWS", _env_int("BATCH_ROOT_CAUSE_MAX_ROWS", 50)),
        database_name=database_name or os.getenv("BATCH_ROOT_CAUSE_DATABASE", "").strip(),
        data_root=configured_data_root,
        h5_search_base_dir=h5_search_base_dir,
        jira_link_field=(
            os.getenv("LOCAL_BATCH_FIELD_JIRA_LINK", "").strip()
            or os.getenv("BATCH_ROOT_CAUSE_FIELD_JIRA_LINK", "").strip()
            or DEFAULT_JIRA_LINK_FIELD
        ),
        path_map=merged_map,
    )


def _local_row_progress_text(
    *,
    total: int,
    processed: int,
    current: int,
    row_index: int,
    problem_type: str,
    h5_path: str,
    step: str,
    detail: str = "",
    step_index: int | None = None,
    step_total: int = 7,
) -> str:
    total_display = total or current
    file_name = Path(h5_path).name if h5_path else "未填写文件路径"
    issue_type = problem_type or "未填写问题类型"
    step_prefix = f"单条进度 {step_index}/{step_total}" if step_index else "单条进度"
    lines = [
        f"共 {total} 条待分析，已处理 {processed} 条；当前第 {current}/{total_display} 条（表格第 {row_index} 行）：{issue_type} / {file_name}",
        f"{step_prefix}：{step}",
    ]
    if detail:
        lines.append(_clip(detail, 320))
    return "\n".join(lines)


def run_local_excel_batch_analysis(
    *,
    input_excel: str | Path,
    output_dir: str | Path,
    config: LocalBatchExcelConfig | None = None,
    progress_callback: Callable[[str, str], None] | None = None,
) -> LocalBatchRunResult:
    config = config or load_local_batch_config()
    input_path = Path(input_excel).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"未找到输入 Excel：{input_path}")

    output_root = ensure_writable_dir(output_dir)
    images_root = ensure_writable_dir(output_root / "images")
    output_excel = ensure_writable_file_path(output_root / "分析结果.xlsx", "analysis_result.xlsx")
    output_zip = ensure_writable_file_path(output_root / "分析结果包.zip", "analysis_result.zip")
    report_path = ensure_writable_file_path(output_root / "run_report.json", "run_report.json")

    shutil.copy2(input_path, output_excel)
    workbook = load_workbook(output_excel)
    worksheet = workbook.active
    headers = _read_headers(worksheet)
    _ensure_required_columns(headers, config)
    _ensure_output_columns(worksheet, headers, config)
    _style_output_columns(worksheet, headers, config)

    records = list(_iter_data_rows(worksheet, headers, config, config.max_rows))
    pending_rows = [item for item in records if item["status"] == STATUS_PENDING]
    jira_pending_rows = [item for item in pending_rows if str(item.get("jira_link") or "").strip()]
    records = _prioritize_local_jira_rows(records)
    allowed_types = set(allowed_problem_types())
    row_results: list[LocalBatchRowResult] = []
    processed_count = success_count = failed_count = skipped_count = 0
    llm_prechecked = False

    jira_priority_note = f"；其中 {len(jira_pending_rows)} 条带 JIRA链接，将优先处理" if jira_pending_rows else ""
    _notify(
        progress_callback,
        "读取完成",
        f"已读取 {len(records)} 条有效记录，其中 {len(pending_rows)} 条为待分析{jira_priority_note}。",
    )

    for item in records:
        row_index = int(item["row_index"])
        problem_description = str(item["problem_description"] or "")
        h5_path = str(item["h5_path"] or "")
        problem_type = str(item["problem_type"] or "")
        status = str(item["status"] or "")
        problem_date = str(item.get("problem_date") or "")
        problem_time = str(item.get("problem_time") or "")
        car_id = str(item.get("car_id") or "")
        car_model = str(item.get("car_model") or "")

        if status != STATUS_PENDING:
            skipped_count += 1
            continue

        processed_count += 1

        def row_notify(
            stage: str,
            step: str,
            detail: str = "",
            *,
            step_index: int | None = None,
            step_total: int = 7,
        ) -> None:
            _notify(
                progress_callback,
                stage,
                _local_row_progress_text(
                    total=len(pending_rows),
                    processed=processed_count - 1,
                    current=processed_count,
                    row_index=row_index,
                    problem_type=problem_type,
                    h5_path=h5_path,
                    step=step,
                    detail=detail,
                    step_index=step_index,
                    step_total=step_total,
                ),
            )

        def row_analysis_progress(stage: str, detail: str = "") -> None:
            row_notify(
                stage or "问题分析中",
                "读取信号并推理根因",
                detail,
                step_index=4,
            )

        row_notify("问题分析中", "校验表格字段", "正在检查 H5 路径和问题类型。", step_index=1)

        generated_images: list[Path] = []
        copied_images: list[str] = []
        resolved_text = ""
        try:
            if not h5_path:
                row_notify(
                    "问题分析中",
                    "自动定位 H5 路径",
                    "Excel 未填写 H5 路径，正在根据车号、车型、问题日期和问题时间查找解析后的 H5。",
                    step_index=2,
                )
                h5_path = find_local_h5_path(
                    car_id=car_id,
                    car_model=car_model,
                    problem_date=problem_date,
                    problem_time=problem_time,
                    config=config,
                )
                _set_cell(worksheet, row_index, headers[config.path_field], h5_path)
                workbook.save(output_excel)
                row_notify("问题分析中", "自动定位 H5 路径", f"已定位数据文件：{h5_path}", step_index=2)
            if not problem_type:
                raise ValueError("缺少 Agent_问题类型。")
            if problem_type not in allowed_types:
                raise ValueError(f"Agent_问题类型不在当前覆盖范围内：{problem_type}。可选：{', '.join(sorted(allowed_types))}")

            row_notify("问题分析中", "解析 H5 路径", h5_path, step_index=2)
            try:
                resolved_path = resolve_local_data_path(
                    h5_path,
                    config=config,
                    workbook_dir=input_path.parent,
                )
            except FileNotFoundError as exc:
                row_notify(
                    "问题分析中",
                    "重新定位 H5 路径",
                    f"已填写的 H5 当前不可用：{exc}；尝试按车号/车型/日期/时间重新查找。",
                    step_index=2,
                )
                try:
                    h5_path = find_local_h5_path(
                        car_id=car_id,
                        car_model=car_model,
                        problem_date=problem_date,
                        problem_time=problem_time,
                        config=config,
                    )
                    resolved_path = resolve_local_data_path(
                        h5_path,
                        config=config,
                        workbook_dir=input_path.parent,
                    )
                except Exception as lookup_exc:
                    raise LocalH5AutoLookupNotFound(
                        f"已填写的 H5 当前不可用：{exc}；{H5_AUTO_LOOKUP_NOT_FOUND_MESSAGE}"
                    ) from lookup_exc
            h5_path, resolved_path, coverage_detail = resolve_local_h5_for_problem_time(
                h5_path=h5_path,
                resolved_path=resolved_path,
                car_id=car_id,
                car_model=car_model,
                problem_date=problem_date,
                problem_time=problem_time,
                config=config,
                workbook_dir=input_path.parent,
            )
            if coverage_detail:
                row_notify("问题分析中", "确认 H5 覆盖范围", coverage_detail, step_index=2)
            _set_cell(worksheet, row_index, headers[config.path_field], h5_path)
            workbook.save(output_excel)
            resolved_text = str(resolved_path)
            if _problem_type_requires_llm(problem_type) and not llm_prechecked:
                row_notify(
                    "问题分析中",
                    "校验模型接口",
                    "正在用短请求确认当前模型 API 可用。",
                    step_index=3,
                )
                try:
                    ensure_llm_api_available()
                except Exception as exc:
                    raise BatchDeepSeekAPIUnavailable(str(exc) or get_llm_api_unavailable_message()) from exc
                llm_prechecked = True
            _set_cell(worksheet, row_index, headers[config.status_field], STATUS_RUNNING)
            workbook.save(output_excel)
            row_notify("问题分析中", "启动单条分析", f"已定位数据文件：{resolved_path.name}", step_index=3)
            analysis = run_single_batch_analysis(
                data_path=resolved_path,
                problem_type=problem_type,
                problem_description=problem_description,
                database_name=config.database_name,
                problem_date=problem_date,
                problem_time=problem_time,
                progress_callback=row_analysis_progress,
            )
            generated_images = [Path(path) for path in analysis.image_paths]
            row_notify("回写结果中", "复制关键信号图示", f"准备复制 {len(generated_images)} 张图示到结果包。", step_index=5)
            copied_images = copy_analysis_images(generated_images, images_root, row_index)
            status_value = STATUS_FAILED if analysis.status == "error" else STATUS_DONE
            row_notify("回写结果中", "写入 Excel 结果", "正在写入结论、概要、状态和图示路径。", step_index=6)
            _write_result_row(
                worksheet,
                headers,
                config,
                row_index,
                status=status_value,
                summary=analysis.summary,
                conclusion=analysis.conclusion,
                image_paths=copied_images,
            )
            if analysis.status == "error":
                failed_count += 1
                row_status = "failed"
            else:
                success_count += 1
                row_status = "success"
            row_results.append(
                LocalBatchRowResult(
                    row_index=row_index,
                    problem_description=problem_description,
                    h5_path=h5_path,
                    resolved_h5_path=resolved_text,
                    problem_type=problem_type,
                    status=row_status,
                    conclusion=analysis.conclusion,
                    summary=analysis.summary,
                    image_paths=copied_images,
                )
            )
            row_notify(
                "问题分析中",
                "本条完成",
                f"已处理 {success_count + failed_count}/{len(pending_rows)} 条，准备处理下一条。",
                step_index=7,
            )
        except LocalH5AutoLookupNotFound as exc:
            skipped_count += 1
            conclusion = H5_AUTO_LOOKUP_NOT_FOUND_MESSAGE
            summary = H5_AUTO_LOOKUP_NOT_FOUND_MESSAGE
            row_notify(
                "回写结果中",
                "等待 H5 数据",
                f"{conclusion}；问题状态保持为“{STATUS_PENDING}”。",
                step_index=6,
            )
            _write_result_row(
                worksheet,
                headers,
                config,
                row_index,
                status=STATUS_PENDING,
                summary=summary,
                conclusion=conclusion,
                image_paths=[],
            )
            row_results.append(
                LocalBatchRowResult(
                    row_index=row_index,
                    problem_description=problem_description,
                    h5_path=h5_path,
                    resolved_h5_path=resolved_text,
                    problem_type=problem_type,
                    status="pending_h5_not_found",
                    conclusion=conclusion,
                    summary=summary,
                    error=str(exc),
                )
            )
            row_notify(
                "问题分析中",
                "等待 H5 数据",
                f"本条等待 H5 数据并保持“{STATUS_PENDING}”，后续数据解析完成后可重新分析。",
                step_index=7,
            )
        except BatchDeepSeekAPIUnavailable as exc:
            skipped_count += 1
            unavailable_message = str(exc) or get_llm_api_unavailable_message()
            row_notify(
                "回写结果中",
                "模型接口不可用",
                f"{unavailable_message}；本条状态保持为“{STATUS_PENDING}”。",
                step_index=6,
            )
            _write_result_row(
                worksheet,
                headers,
                config,
                row_index,
                status=STATUS_PENDING,
                summary=unavailable_message,
                conclusion=f"未分析：{unavailable_message}。",
                image_paths=[],
            )
            row_results.append(
                LocalBatchRowResult(
                    row_index=row_index,
                    problem_description=problem_description,
                    h5_path=h5_path,
                    resolved_h5_path=resolved_text,
                    problem_type=problem_type,
                    status="pending_api_unavailable",
                    conclusion=f"未分析：{unavailable_message}。",
                    summary=unavailable_message,
                    error=unavailable_message,
                )
            )
            break
        except Exception as exc:
            failed_count += 1
            conclusion = f"分析失败：{exc}"
            summary = _clip(f"失败：{exc}", 240)
            row_notify("回写结果中", "写入失败状态", f"本条分析失败，正在写入失败原因：{exc}", step_index=6)
            _write_result_row(
                worksheet,
                headers,
                config,
                row_index,
                status=STATUS_FAILED,
                summary=summary,
                conclusion=conclusion,
                image_paths=[],
            )
            row_results.append(
                LocalBatchRowResult(
                    row_index=row_index,
                    problem_description=problem_description,
                    h5_path=h5_path,
                    resolved_h5_path=resolved_text,
                    problem_type=problem_type,
                    status="failed",
                    conclusion=conclusion,
                    summary=summary,
                    error=str(exc),
                )
            )
            row_notify(
                "问题分析中",
                "本条失败",
                f"已处理 {success_count + failed_count}/{len(pending_rows)} 条，继续处理下一条。失败原因：{exc}",
                step_index=7,
            )
        finally:
            workbook.save(output_excel)
            if generated_images:
                _cleanup_generated_paths(generated_images)
            _cleanup_empty_runtime_roots()

    result = LocalBatchRunResult(
        input_excel=str(input_path),
        output_excel=str(output_excel),
        output_zip=str(output_zip),
        output_dir=str(output_root),
        total_rows=len(records),
        pending_rows=len(pending_rows),
        processed_count=processed_count,
        success_count=success_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        rows=row_results,
    )
    report_path.write_text(json.dumps(_result_to_json(result), ensure_ascii=False, indent=2), encoding="utf-8")
    build_result_zip(output_root, output_zip, [output_excel, report_path], images_root)
    _notify(
        progress_callback,
        "完成",
        f"分析完成：待分析 {len(pending_rows)} 条，成功 {success_count} 条，失败 {failed_count} 条。",
    )
    return result


def resolve_local_h5_for_problem_time(
    *,
    h5_path: str,
    resolved_path: Path,
    car_id: str,
    car_model: str,
    problem_date: str,
    problem_time: str,
    config: LocalBatchExcelConfig,
    workbook_dir: Path,
) -> tuple[str, Path, str]:
    replacement, coverage_state, coverage_detail = validate_local_h5_covers_problem_time(
        resolved_path,
        problem_date=problem_date,
        problem_time=problem_time,
    )
    if replacement:
        return replacement, Path(replacement).expanduser().resolve(), coverage_detail
    if coverage_state is False:
        found_path = find_local_h5_path(
            car_id=car_id,
            car_model=car_model,
            problem_date=problem_date,
            problem_time=problem_time,
            config=config,
        )
        found_resolved = resolve_local_data_path(found_path, config=config, workbook_dir=workbook_dir)
        return (
            found_path,
            found_resolved,
            f"{coverage_detail}；已重新自动定位覆盖问题时间的 H5：{found_resolved}",
        )
    return h5_path, resolved_path, coverage_detail


def validate_local_h5_covers_problem_time(
    resolved_path: Path,
    *,
    problem_date: str,
    problem_time: str,
) -> tuple[str | None, bool | None, str]:
    if not problem_date or not problem_time:
        return None, None, "未填写完整问题日期/时间，跳过 H5 覆盖范围校验。"
    if parse_h5_lookup_problem_datetime is None or select_h5_candidates_for_time is None:
        return None, None, "H5 自动定位工具不可用，跳过 H5 覆盖范围校验。"
    try:
        target_time = parse_h5_lookup_problem_datetime(problem_date, problem_time)
    except Exception:
        return None, None, "问题日期/时间格式无法解析，跳过 H5 覆盖范围校验。"

    sibling_paths = [str(resolved_path)]
    try:
        sibling_paths.extend(str(path) for path in resolved_path.parent.glob("*.h5"))
    except Exception:
        pass
    try:
        matches = select_h5_candidates_for_time(sibling_paths, target_time)
    except Exception as exc:
        return None, None, f"确认 H5 覆盖范围时读取失败：{exc}"

    current_key = _path_identity(resolved_path)
    for candidate in matches:
        if _path_identity(candidate.path) == current_key:
            return (
                None,
                True,
                f"已确认当前 H5 覆盖问题时间 {target_time.strftime('%Y-%m-%d %H:%M:%S')}。",
            )
    if matches:
        replacement = str(Path(matches[0].path).expanduser().resolve())
        return (
            replacement,
            False,
            "当前 H5 不覆盖问题时间 "
            f"{target_time.strftime('%Y-%m-%d %H:%M:%S')}；"
            f"同目录匹配到覆盖该时间的 H5：{replacement}",
        )
    return (
        None,
        False,
        "当前 H5 不覆盖问题时间 "
        f"{target_time.strftime('%Y-%m-%d %H:%M:%S')}，同目录也没有覆盖该时间的 H5。",
    )


def _path_identity(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve()).replace("\\", "/").lower()


def resolve_local_data_path(
    raw_path: str,
    *,
    config: LocalBatchExcelConfig,
    workbook_dir: Path,
) -> Path:
    value = str(raw_path or "").strip().strip('"').strip("'")
    if not value:
        raise ValueError("缺少 Agent_h5数据路径。")

    mapped = apply_path_map(value, config.path_map)
    if mapped is None:
        if _looks_like_windows_path(value) and os.name != "nt":
            raise FileNotFoundError(
                "Excel 中是 Windows 路径，当前容器无法直接访问。请在 UI 或 LOCAL_BATCH_PATH_MAP 中配置路径映射，"
                f"例如把 {value.split(':', 1)[0]}:\\\\ 映射到 /data/..."
            )
        candidate = Path(value).expanduser()
    else:
        candidate = Path(mapped).expanduser()

    if not candidate.is_absolute():
        if config.data_root:
            candidate = Path(config.data_root).expanduser() / candidate
        else:
            candidate = workbook_dir / candidate

    if not candidate.is_file():
        raise FileNotFoundError(f"未找到 H5 数据文件：{candidate}")
    if candidate.suffix.lower() != ".h5":
        raise ValueError(f"当前只支持 .h5 数据文件：{candidate}")
    return candidate.resolve()


def find_local_h5_path(
    *,
    car_id: str,
    car_model: str,
    problem_date: str,
    problem_time: str,
    config: LocalBatchExcelConfig,
) -> str:
    if find_j6b_h5 is None:
        raise LocalH5AutoLookupNotFound("缺少 scripts/find_j6b_h5_path.py，无法自动查找 H5。")

    missing = []
    if not car_id:
        missing.append(config.car_id_field)
    if not car_model:
        missing.append(config.car_model_field)
    if not problem_date:
        missing.append(config.problem_date_field)
    if not problem_time:
        missing.append(config.problem_time_field)
    if missing:
        raise LocalH5AutoLookupNotFound(
            "缺少 Agent_h5数据路径，且无法自动查找 H5："
            f"缺少 {', '.join(missing)}。"
        )

    matched_h5, matched_date_dirs, matched_vehicle_dirs = find_j6b_h5(
        base_dir=config.h5_search_base_dir,
        car_model=car_model,
        car_id=car_id,
        date_value=problem_date,
        time_value=problem_time,
    )
    if not matched_h5:
        hints = []
        if matched_date_dirs:
            hints.append("已找到车型/日期目录，但目标时间不在候选 H5 覆盖范围内")
        elif matched_vehicle_dirs:
            hints.append("已找到车辆目录，但没有匹配日期/时间")
        else:
            hints.append("未匹配到车型/车号目录")
        raise LocalH5AutoLookupNotFound(
            "未能自动找到 H5 文件："
            f"车型={car_model}，车号={car_id}，日期={problem_date}，时间={problem_time}；"
            f"搜索根目录={config.h5_search_base_dir}；{'; '.join(hints)}。"
        )
    return str(Path(matched_h5[0]).expanduser().resolve())


def apply_path_map(raw_path: str, path_map: dict[str, str]) -> str | None:
    if not path_map:
        return None
    raw_clean = _path_prefix_clean(raw_path)
    raw_norm = raw_clean.lower()
    for source, target in sorted(path_map.items(), key=lambda item: len(_path_prefix_norm(item[0])), reverse=True):
        source_clean = _path_prefix_clean(source)
        source_norm = source_clean.lower()
        if not source_norm:
            continue
        if raw_norm == source_norm or raw_norm.startswith(f"{source_norm}/"):
            suffix = raw_clean[len(source_clean) :].lstrip("/")
            parts = [part for part in suffix.split("/") if part]
            return str(Path(target).expanduser().joinpath(*parts))
    return None


def parse_path_map_text(text: str) -> dict[str, str]:
    value = (text or "").strip()
    if not value:
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        return {str(key).strip(): str(item).strip() for key, item in data.items() if str(key).strip() and str(item).strip()}

    result: dict[str, str] = {}
    for line in value.splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if "=" in item:
            source, target = item.split("=", 1)
        elif "=>" in item:
            source, target = item.split("=>", 1)
        else:
            raise ValueError("路径映射格式应为 JSON 对象，或每行 source=target。")
        source = source.strip()
        target = target.strip()
        if source and target:
            result[source] = target
    return result


def load_path_map_from_env() -> dict[str, str]:
    env_text = os.getenv("LOCAL_BATCH_PATH_MAP", "").strip()
    env_file = os.getenv("LOCAL_BATCH_PATH_MAP_FILE", "").strip()
    result = parse_path_map_text(env_text) if env_text else {}
    if env_file:
        path = Path(env_file).expanduser()
        if path.exists():
            result.update(parse_path_map_text(path.read_text(encoding="utf-8")))
    return result


def copy_analysis_images(image_paths: list[Path], images_root: Path, row_index: int) -> list[str]:
    row_dir = ensure_writable_dir(images_root / f"row_{row_index:04d}")
    copied: list[str] = []
    for index, image_path in enumerate(image_paths, start=1):
        source = Path(image_path)
        if not source.exists() or source.stat().st_size <= 0:
            continue
        suffix = source.suffix or ".png"
        safe_name = f"{index:02d}_{safe_runtime_name(source.stem, 'signal')}{suffix}"
        target = ensure_writable_file_path(row_dir / safe_name, safe_name)
        shutil.copy2(source, target)
        copied.append(target.relative_to(images_root.parent).as_posix())
    return copied


def build_result_zip(output_root: Path, zip_path: Path, files: list[Path], images_root: Path) -> Path:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in files:
            if file_path.exists():
                archive.write(file_path, arcname=file_path.relative_to(output_root).as_posix())
        if images_root.exists():
            for image_path in images_root.rglob("*"):
                if image_path.is_file():
                    archive.write(image_path, arcname=image_path.relative_to(output_root).as_posix())
    return zip_path


def _read_headers(worksheet: Any) -> dict[str, int]:
    headers: dict[str, int] = {}
    for cell in worksheet[1]:
        value = str(cell.value or "").strip()
        if value:
            headers[value] = int(cell.column)
    return headers


def _ensure_required_columns(headers: dict[str, int], config: LocalBatchExcelConfig) -> None:
    required = [config.problem_field, config.path_field, config.type_field, config.status_field]
    missing = [name for name in required if name not in headers]
    if missing:
        raise ValueError(f"Excel 缺少必要列：{', '.join(missing)}")


def _ensure_output_columns(worksheet: Any, headers: dict[str, int], config: LocalBatchExcelConfig) -> None:
    for name in (config.summary_field, config.conclusion_field, config.image_field):
        if name in headers:
            continue
        column = worksheet.max_column + 1
        worksheet.cell(row=1, column=column).value = name
        headers[name] = column


def _style_output_columns(worksheet: Any, headers: dict[str, int], config: LocalBatchExcelConfig) -> None:
    widths = {
        config.problem_field: 34,
        config.path_field: 46,
        config.type_field: 18,
        config.status_field: 14,
        config.problem_date_field: 16,
        config.problem_time_field: 16,
        config.car_id_field: 18,
        config.car_model_field: 16,
        config.jira_link_field: 26,
        config.summary_field: 42,
        config.conclusion_field: 72,
        config.image_field: 44,
    }
    for name, width in widths.items():
        column = headers.get(name)
        if column:
            worksheet.column_dimensions[worksheet.cell(row=1, column=column).column_letter].width = width
    for row in worksheet.iter_rows():
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")


def _iter_data_rows(
    worksheet: Any,
    headers: dict[str, int],
    config: LocalBatchExcelConfig,
    max_rows: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    limit = max(1, max_rows)
    for row_index in range(2, worksheet.max_row + 1):
        problem = _cell_text(worksheet, row_index, headers[config.problem_field])
        h5_path = _cell_text(worksheet, row_index, headers[config.path_field])
        problem_type = _cell_text(worksheet, row_index, headers[config.type_field])
        status = _cell_text(worksheet, row_index, headers[config.status_field])
        problem_date = _optional_cell_text(
            worksheet,
            row_index,
            headers,
            config.problem_date_field,
            LEGACY_PROBLEM_DATE_FIELD,
        )
        problem_time = _optional_cell_text(
            worksheet,
            row_index,
            headers,
            config.problem_time_field,
            LEGACY_PROBLEM_TIME_FIELD,
        )
        car_id = _optional_cell_text(
            worksheet,
            row_index,
            headers,
            config.car_id_field,
            LEGACY_CAR_ID_FIELD,
        )
        car_model = _optional_cell_text(
            worksheet,
            row_index,
            headers,
            config.car_model_field,
            LEGACY_CAR_MODEL_FIELD,
        )
        jira_link = _optional_cell_text(worksheet, row_index, headers, config.jira_link_field)
        if not any((problem, h5_path, problem_type, status)):
            continue
        rows.append(
            {
                "row_index": row_index,
                "problem_description": problem,
                "h5_path": h5_path,
                "problem_type": problem_type,
                "status": status,
                "problem_date": problem_date,
                "problem_time": problem_time,
                "car_id": car_id,
                "car_model": car_model,
                "jira_link": jira_link,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _prioritize_local_jira_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda item: (
            0
            if str(item.get("status") or "") == STATUS_PENDING
            and str(item.get("jira_link") or "").strip()
            else 1,
            int(item.get("row_index") or 0),
        ),
    )


def _write_result_row(
    worksheet: Any,
    headers: dict[str, int],
    config: LocalBatchExcelConfig,
    row_index: int,
    *,
    status: str,
    summary: str,
    conclusion: str,
    image_paths: list[str],
) -> None:
    _set_cell(worksheet, row_index, headers[config.status_field], status)
    _set_cell(worksheet, row_index, headers[config.summary_field], summary)
    _set_cell(worksheet, row_index, headers[config.conclusion_field], conclusion)
    image_text = "\n".join(image_paths)
    image_cell = _set_cell(worksheet, row_index, headers[config.image_field], image_text)
    if len(image_paths) == 1:
        image_cell.hyperlink = image_paths[0]
        image_cell.style = "Hyperlink"


def _set_cell(worksheet: Any, row: int, column: int, value: Any) -> Any:
    cell = worksheet.cell(row=row, column=column)
    cell.value = value
    cell.alignment = Alignment(wrap_text=True, vertical="top")
    return cell


def _cell_text(worksheet: Any, row: int, column: int) -> str:
    value = worksheet.cell(row=row, column=column).value
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _optional_cell_text(worksheet: Any, row: int, headers: dict[str, int], *names: str) -> str:
    for name in names:
        if name and name in headers:
            return _cell_text(worksheet, row, headers[name])
    return ""


def _path_prefix_norm(value: str) -> str:
    return _path_prefix_clean(value).lower()


def _path_prefix_clean(value: str) -> str:
    return str(value or "").strip().strip('"').strip("'").replace("\\", "/").rstrip("/")


def _looks_like_windows_path(value: str) -> bool:
    return bool(WINDOWS_ABS_RE.match(value or "") or UNC_RE.match(value or ""))


def _result_to_json(result: LocalBatchRunResult) -> dict[str, Any]:
    data = asdict(result)
    data["rows"] = [asdict(row) for row in result.rows]
    return data


def _clip(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _notify(callback: Callable[[str, str], None] | None, stage: str, detail: str) -> None:
    if not callback:
        return
    try:
        callback(stage, detail)
    except Exception as exc:
        print(f"local batch progress callback failed: {exc}", flush=True)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local Excel batch root-cause analysis.")
    parser.add_argument("excel", help="Input Excel file with Agent_* columns.")
    parser.add_argument("--output-dir", default=str(get_local_batch_runtime_dir() / f"run_{int(time.time())}"))
    parser.add_argument("--path-map", default="", help="JSON object or source=target lines for path mapping.")
    parser.add_argument("--data-root", default="", help="Base directory for relative H5 paths.")
    parser.add_argument("--database", default="", help="Optional ARXML database name for non-DeepSeek fallback mode.")
    parser.add_argument("--max-rows", type=int, default=None)
    args = parser.parse_args()

    config = load_local_batch_config(
        path_map_text=args.path_map,
        data_root=args.data_root,
        database_name=args.database,
        max_rows=args.max_rows,
    )

    def progress(stage: str, detail: str) -> None:
        print(f"[{stage}] {detail}", flush=True)

    result = run_local_excel_batch_analysis(
        input_excel=args.excel,
        output_dir=args.output_dir,
        config=config,
        progress_callback=progress,
    )
    print(json.dumps(_result_to_json(result), ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
