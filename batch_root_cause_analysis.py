from __future__ import annotations

import datetime
import json
import os
import mimetypes
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from actuator_handshake import run_actuator_handshake_analysis_with_database
from performance_analysis import (
    get_result_dir as get_performance_result_dir,
    load_performance_problem_presets,
    metric_title,
    run_performance_analysis,
)
from root_cause_analysis import (
    build_root_cause_evidence_plots,
    build_root_cause_report,
    infer_root_cause_problem_groups,
    root_cause_report_context,
)
from function_exit_analysis import (
    format_function_exit_result_context,
    run_function_exit_analysis,
)
from llm_service import ensure_llm_api_available, get_llm_api_unavailable_message
from runtime_paths import (
    ensure_writable_dir,
    ensure_writable_file_path,
    get_runtime_subdir,
    get_runtime_subprocess_env,
    safe_runtime_name,
)

try:
    from scripts.find_j6b_h5_path import (
        DEFAULT_J6B_BASE_DIR,
        find_j6b_h5,
        parse_problem_datetime as parse_h5_lookup_problem_datetime,
        select_h5_candidates_for_time,
    )
except Exception:
    DEFAULT_J6B_BASE_DIR = os.getenv("BATCH_ROOT_CAUSE_H5_SEARCH_BASE_DIR", "")
    find_j6b_h5 = None
    parse_h5_lookup_problem_datetime = None
    select_h5_candidates_for_time = None

try:
    from scripts.blf_to_h5_collect import get_output_path as blf_h5_output_path
    from scripts.blf_to_h5_collect import parse_one_blf
    from scripts.find_j6b_blf_path import DEFAULT_J6B_BLF_BASE_DIR, find_j6b_blf, output_dir_for_blf
except Exception:
    DEFAULT_J6B_BLF_BASE_DIR = os.getenv("BATCH_ROOT_CAUSE_BLF_SEARCH_BASE_DIR", "")
    blf_h5_output_path = None
    parse_one_blf = None
    find_j6b_blf = None
    output_dir_for_blf = None


ROOT = Path(__file__).resolve().parent
DATABASE_DIR = ROOT / "database"


def get_batch_runtime_dir() -> Path:
    return get_runtime_subdir("batch_root_cause")


def get_batch_artifact_dir() -> Path:
    return ensure_writable_dir(get_batch_runtime_dir() / "artifacts")


def get_analysis_results_dir() -> Path:
    return get_runtime_subdir("analysis_results")


def get_cli_payload_dir() -> Path:
    return ensure_writable_dir(get_batch_runtime_dir() / "cli_payload")


def get_cli_attachment_dir() -> Path:
    return ensure_writable_dir(get_batch_runtime_dir() / "cli_attachments")


RUNTIME_DIR = get_batch_runtime_dir()
BATCH_ARTIFACT_DIR = get_batch_artifact_dir()
ANALYSIS_RESULTS_DIR = get_analysis_results_dir()
CLI_PAYLOAD_DIR = get_cli_payload_dir()
CLI_ATTACHMENT_DIR = get_cli_attachment_dir()
_TENANT_ACCESS_TOKEN_CACHE = {"token": "", "expires_at": 0.0}
SUPPORTED_DATA_SUFFIXES = {".h5"}
FUNCTION_EXIT_PROBLEM_TYPE = "功能退出"
LATERAL_SUDDEN_STEERING_PROBLEM_TYPE = "横向-猛打"
STATUS_PENDING = "待分析"
STATUS_RUNNING = "分析中"
STATUS_DONE = "已分析"
STATUS_FAILED = "分析失败"
H5_AUTO_LOOKUP_NOT_FOUND_MESSAGE = "未找到对应h5数据，请确认数据解析是否完成"

SUMMARY_MAX_CHARS = 120
DEEPSEEK_SUMMARY_MAX_CHARS = 80


class BatchRootCauseCancelled(RuntimeError):
    """Raised when a running batch root-cause analysis is cancelled by the user."""


class BatchDeepSeekAPIUnavailable(RuntimeError):
    """Raised when the configured LLM cannot be called, so the current batch must stop."""


class BatchFeishuWritebackError(RuntimeError):
    """Raised when Feishu Base record writeback fails after retries."""


class BatchH5AutoLookupNotFound(FileNotFoundError):
    """Raised when metadata-based H5 lookup does not find a parsed file yet."""


def _problem_type_requires_llm(problem_type: str) -> bool:
    return problem_type != FUNCTION_EXIT_PROBLEM_TYPE and _use_deepseek_investigation_mode()

SIGNAL_MEANING_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("iimulonacc", "imulonacc"), "实车纵向加速度/减速度"),
    (("vehspdavg",), "实车车速"),
    (("iaccdrvrseltdspd", "setspeed"), "驾驶员设定车速"),
    (("iacact376", "iaccact376", "accatc_accact", "accbsce_accact"), "ACC 控制激活状态"),
    (("accbsce_accaccl", "accatc_axltrqrq", "ctrlacc"), "算法目标加速度/驱动请求"),
    (("accbsce_acctrq", "fcacbsc_ctrltrq"), "算法制动扭矩请求"),
    (("autbrk", "brkrqtp"), "自动制动请求类型"),
    (("accatcs_rqstat", "icpsatcs_reqstatus"), "驱动系统对请求的响应状态"),
    (("accatcs_lmtgstat",), "驱动限扭/仲裁状态"),
    (("actaxletrq", "icstbats"), "驱动/液压制动实际扭矩响应"),
    (("iregenbrkaxtrqach",), "电制动实际扭矩响应"),
    (("accbrkngact", "autobrkngact", "brksysautbrkstat"), "制动系统自动制动响应状态"),
    (("leadtargetid", "fltfusnobjid", "targetid"), "前车/跟车目标 ID"),
    (("cutintargetid", "cutin"), "Cut-in 目标 ID/切入标志"),
    (("leadtargetdistance", "cutintargetdistance", "long_range", "distance"), "目标距离"),
    (("leadtargetvelocity", "cutintargetvelocity", "long_velocity", "velocity"), "目标相对速度"),
    (("leadtargetaccel", "cutintargetaccel", "long_accel", "accel"), "目标加速度"),
    (("ttc",), "目标 TTC/碰撞时间"),
    (("debug_uint_reserved_48", "curvaturelimitspeed"), "曲率限速值"),
    (("debug_uint_reserved_49", "curvaturelimitenable"), "曲率限速使能状态"),
    (("lane_modelc2", "curvature"), "道路曲率"),
    (("acchdwystg",), "跟车时距设置"),
    (("iaccpdlovrrdatv", "override"), "驾驶员接管状态"),
    (("ivehmvngstat", "vehmvngstat"), "车辆运动/静止状态"),
    (("itransestgear",), "挡位状态"),
    (("whlrotdir", "whlangvel"), "车轮方向/轮速"),
    (("roadslope", "debug_single_reserved_51"), "坡度角"),
)

SIDE_LABELS = {
    "现象层": "实车表现",
    "算法请求诱因": "感知/场景输入",
    "算法请求侧": "算法请求",
    "执行器响应侧": "执行器响应",
}

AGENT_PROBLEM_DATE_FIELD = "Agent_问题日期"
AGENT_PROBLEM_TIME_FIELD = "Agent_问题时间"
AGENT_CAR_ID_FIELD = "Agent_车号"
AGENT_CAR_MODEL_FIELD = "Agent_车型"
DEFAULT_JIRA_LINK_FIELD = "JIRA链接"
LEGACY_PROBLEM_DATE_FIELD = "问题日期"
LEGACY_PROBLEM_TIME_FIELD = "问题时间"
LEGACY_CAR_ID_FIELD = "车号"
LEGACY_CAR_MODEL_FIELD = "车型"


@dataclass(frozen=True)
class BatchRootCauseConfig:
    base_token: str
    table_id: str
    problem_field: str = "Agent_问题描述"
    path_field: str = "Agent_h5数据路径"
    type_field: str = "Agent_问题类型"
    conclusion_field: str = "Agent_分析结论"
    attachment_field: str = "Agent_分析信号图示"
    status_field: str = "Agent_问题状态"
    summary_field: str = "Agent_结论概要"
    problem_date_field: str = AGENT_PROBLEM_DATE_FIELD
    problem_time_field: str = AGENT_PROBLEM_TIME_FIELD
    car_id_field: str = AGENT_CAR_ID_FIELD
    car_model_field: str = AGENT_CAR_MODEL_FIELD
    jira_link_field: str = DEFAULT_JIRA_LINK_FIELD
    problem_field_id: str = ""
    path_field_id: str = ""
    type_field_id: str = ""
    conclusion_field_id: str = ""
    attachment_field_id: str = ""
    status_field_id: str = ""
    summary_field_id: str = ""
    problem_date_field_id: str = ""
    problem_time_field_id: str = ""
    car_id_field_id: str = ""
    car_model_field_id: str = ""
    jira_link_field_id: str = ""
    cli_identity: str = "bot"
    lark_cli: str = "lark-cli.cmd"
    scan_max_rows: int = 5000
    max_rows: int = 0
    database_name: str = ""
    h5_search_base_dir: str = DEFAULT_J6B_BASE_DIR
    blf_search_base_dir: str = DEFAULT_J6B_BLF_BASE_DIR

    @property
    def is_configured(self) -> bool:
        return bool(self.base_token and self.table_id)


@dataclass
class BatchRowResult:
    record_id: str
    row_index: int
    problem_description: str
    h5_path: str
    problem_type: str
    status: str
    conclusion: str
    summary: str = ""
    image_paths: list[Path] = field(default_factory=list)
    uploaded_images: int = 0
    error: str = ""


@dataclass
class BatchRootCauseResult:
    total_rows: int
    processed_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    base_token: str
    table_id: str
    pending_count: int = 0
    planned_count: int = 0
    jira_pending_count: int = 0
    rows: list[BatchRowResult] = field(default_factory=list)


def load_batch_root_cause_config() -> BatchRootCauseConfig:
    try:
        from feishu_config import load_env

        load_env()
    except Exception:
        pass

    def value(*names: str, default: str = "") -> str:
        for name in names:
            current = os.getenv(name, "").strip()
            if current:
                return current
        return default

    def int_value(name: str, default: int, *, minimum: int = 1) -> int:
        try:
            return max(minimum, int(os.getenv(name, str(default))))
        except (TypeError, ValueError):
            return default

    return BatchRootCauseConfig(
        base_token=value("BATCH_ROOT_CAUSE_BASE_TOKEN", "FEISHU_BATCH_ROOT_CAUSE_BASE_TOKEN"),
        table_id=value("BATCH_ROOT_CAUSE_TABLE_ID", "FEISHU_BATCH_ROOT_CAUSE_TABLE_ID"),
        problem_field=value("BATCH_ROOT_CAUSE_FIELD_PROBLEM", default="Agent_问题描述"),
        path_field=value("BATCH_ROOT_CAUSE_FIELD_H5_PATH", default="Agent_h5数据路径"),
        type_field=value("BATCH_ROOT_CAUSE_FIELD_PROBLEM_TYPE", default="Agent_问题类型"),
        conclusion_field=value("BATCH_ROOT_CAUSE_FIELD_CONCLUSION", default="Agent_分析结论"),
        attachment_field=value("BATCH_ROOT_CAUSE_ATTACHMENT_FIELD", default="Agent_分析信号图示"),
        status_field=value("BATCH_ROOT_CAUSE_FIELD_STATUS", default="Agent_问题状态"),
        summary_field=value("BATCH_ROOT_CAUSE_FIELD_SUMMARY", default="Agent_结论概要"),
        problem_date_field=value("BATCH_ROOT_CAUSE_FIELD_PROBLEM_DATE", default=AGENT_PROBLEM_DATE_FIELD),
        problem_time_field=value("BATCH_ROOT_CAUSE_FIELD_PROBLEM_TIME", default=AGENT_PROBLEM_TIME_FIELD),
        car_id_field=value("BATCH_ROOT_CAUSE_FIELD_CAR_ID", default=AGENT_CAR_ID_FIELD),
        car_model_field=value("BATCH_ROOT_CAUSE_FIELD_CAR_MODEL", default=AGENT_CAR_MODEL_FIELD),
        jira_link_field=value("BATCH_ROOT_CAUSE_FIELD_JIRA_LINK", default=DEFAULT_JIRA_LINK_FIELD),
        problem_field_id=value("BATCH_ROOT_CAUSE_FIELD_PROBLEM_ID", default=""),
        path_field_id=value("BATCH_ROOT_CAUSE_FIELD_H5_PATH_ID", default=""),
        type_field_id=value("BATCH_ROOT_CAUSE_FIELD_PROBLEM_TYPE_ID", default=""),
        conclusion_field_id=value("BATCH_ROOT_CAUSE_FIELD_CONCLUSION_ID", default=""),
        attachment_field_id=value("BATCH_ROOT_CAUSE_ATTACHMENT_FIELD_ID", default=""),
        status_field_id=value("BATCH_ROOT_CAUSE_FIELD_STATUS_ID", default=""),
        summary_field_id=value("BATCH_ROOT_CAUSE_FIELD_SUMMARY_ID", default=""),
        problem_date_field_id=value("BATCH_ROOT_CAUSE_FIELD_PROBLEM_DATE_ID", default=""),
        problem_time_field_id=value("BATCH_ROOT_CAUSE_FIELD_PROBLEM_TIME_ID", default=""),
        car_id_field_id=value("BATCH_ROOT_CAUSE_FIELD_CAR_ID_ID", default=""),
        car_model_field_id=value("BATCH_ROOT_CAUSE_FIELD_CAR_MODEL_ID", default=""),
        jira_link_field_id=value("BATCH_ROOT_CAUSE_FIELD_JIRA_LINK_ID", default=""),
        cli_identity=value("BATCH_ROOT_CAUSE_LARK_CLI_AS", default="bot"),
        lark_cli=value("BATCH_ROOT_CAUSE_LARK_CLI", default="lark-cli.cmd"),
        scan_max_rows=int_value("BATCH_ROOT_CAUSE_SCAN_MAX_ROWS", 5000, minimum=0),
        max_rows=int_value("BATCH_ROOT_CAUSE_MAX_ROWS", 0, minimum=0),
        database_name=value("BATCH_ROOT_CAUSE_DATABASE", default=""),
        h5_search_base_dir=value(
            "BATCH_ROOT_CAUSE_H5_SEARCH_BASE_DIR",
            "J6B_BASE_DIR",
            default=DEFAULT_J6B_BASE_DIR,
        ),
        blf_search_base_dir=value(
            "BATCH_ROOT_CAUSE_BLF_SEARCH_BASE_DIR",
            "J6B_BLF_BASE_DIR",
            default=DEFAULT_J6B_BLF_BASE_DIR,
        ),
    )


def allowed_problem_types() -> list[str]:
    labels = [preset.label for preset in load_performance_problem_presets().values()]
    if LATERAL_SUDDEN_STEERING_PROBLEM_TYPE not in labels:
        labels.append(LATERAL_SUDDEN_STEERING_PROBLEM_TYPE)
    if FUNCTION_EXIT_PROBLEM_TYPE not in labels:
        labels.append(FUNCTION_EXIT_PROBLEM_TYPE)
    return labels


def resolve_batch_data_path(
    raw_path: str | Path,
    config: BatchRootCauseConfig | None = None,
) -> Path:
    _ = config
    value = str(raw_path or "").strip().strip('"').strip("'")
    if not value:
        raise ValueError("缺少 Agent_h5数据路径。")

    data_path = Path(value).expanduser()
    if not data_path.is_absolute():
        raise ValueError(
            "Agent_h5数据路径必须填写布鲁运行机器/服务器上的绝对路径；"
            f"当前填写的是相对路径或文件名：{value}"
        )
    if not data_path.is_file():
        raise FileNotFoundError(
            "未找到数据文件："
            f"{data_path}。请确认该路径是在布鲁运行机器/服务器上真实存在的绝对路径，"
            "不是填表人电脑上的本地路径。"
        )
    return data_path.resolve()


def _resolve_or_find_h5_path(
    *,
    fields: dict[str, Any],
    config: BatchRootCauseConfig,
    current_h5_path: str,
    problem_date: str,
    problem_time: str,
    progress_callback: Callable[[str, str], None] | None = None,
) -> tuple[str, bool, str]:
    current = str(current_h5_path or "").strip()
    if current:
        replacement, coverage_state, coverage_detail = _validate_current_h5_path_for_problem_time(
            current,
            config=config,
            problem_date=problem_date,
            problem_time=problem_time,
        )
        if replacement:
            return replacement, True, coverage_detail
        if coverage_state is False:
            try:
                found_path, _auto_filled, lookup_detail = _find_h5_path_by_metadata(
                    fields=fields,
                    config=config,
                    problem_date=problem_date,
                    problem_time=problem_time,
                    progress_callback=progress_callback,
                )
                return (
                    found_path,
                    True,
                    f"{coverage_detail}；已重新自动定位覆盖问题时间的 H5。{lookup_detail}",
                )
            except BatchH5AutoLookupNotFound as exc:
                raise BatchH5AutoLookupNotFound(
                    f"{coverage_detail}；{H5_AUTO_LOOKUP_NOT_FOUND_MESSAGE}"
                ) from exc
        if coverage_state is None:
            try:
                resolve_batch_data_path(current, config)
            except Exception as exc:
                try:
                    found_path, _auto_filled, lookup_detail = _find_h5_path_by_metadata(
                        fields=fields,
                        config=config,
                        problem_date=problem_date,
                        problem_time=problem_time,
                        progress_callback=progress_callback,
                    )
                    return (
                        found_path,
                        True,
                        f"表格中已填写的 H5 当前不可用：{exc}；已重新自动定位 H5。{lookup_detail}",
                    )
                except Exception as lookup_exc:
                    raise BatchH5AutoLookupNotFound(
                        f"表格中已填写的 H5 当前不可用：{exc}；{H5_AUTO_LOOKUP_NOT_FOUND_MESSAGE}"
                    ) from lookup_exc
        detail = "使用表格中已填写的 Agent_h5数据路径。"
        if coverage_detail:
            detail += f" {coverage_detail}"
        return current, False, detail

    return _find_h5_path_by_metadata(
        fields=fields,
        config=config,
        problem_date=problem_date,
        problem_time=problem_time,
        progress_callback=progress_callback,
    )


def _find_h5_path_by_metadata(
    *,
    fields: dict[str, Any],
    config: BatchRootCauseConfig,
    problem_date: str,
    problem_time: str,
    progress_callback: Callable[[str, str], None] | None = None,
) -> tuple[str, bool, str]:
    if find_j6b_h5 is None:
        raise RuntimeError("缺少 scripts/find_j6b_h5_path.py，无法根据车号/车型/日期/时间自动查找 H5。")

    car_id = _field_text_with_legacy(fields, config.car_id_field, config.car_id_field_id, LEGACY_CAR_ID_FIELD)
    car_model = _field_text_with_legacy(
        fields,
        config.car_model_field,
        config.car_model_field_id,
        LEGACY_CAR_MODEL_FIELD,
    )
    date_value = _normalize_h5_lookup_date(problem_date)
    time_value = _normalize_h5_lookup_time(problem_time)
    missing = []
    if not car_id:
        missing.append(config.car_id_field)
    if not car_model:
        missing.append(config.car_model_field)
    if not date_value:
        missing.append(config.problem_date_field)
    if not time_value:
        missing.append(config.problem_time_field)
    if missing:
        raise BatchH5AutoLookupNotFound(
            "缺少 Agent_h5数据路径，且无法自动查找 H5："
            f"缺少 {', '.join(missing)}。"
        )

    matched_h5, matched_date_dirs, matched_vehicle_dirs = find_j6b_h5(
        base_dir=config.h5_search_base_dir,
        car_model=car_model,
        car_id=car_id,
        date_value=date_value,
        time_value=time_value,
    )
    if not matched_h5:
        blf_failure_detail = ""
        try:
            parsed_h5, blf_detail = _find_and_parse_h5_from_blf(
                config=config,
                car_model=car_model,
                car_id=car_id,
                date_value=date_value,
                time_value=time_value,
                progress_callback=progress_callback,
            )
            return parsed_h5, True, blf_detail
        except BatchH5AutoLookupNotFound as exc:
            blf_failure_detail = str(exc).strip()

        hints = []
        if matched_date_dirs:
            hints.append("已找到车型/日期目录，但该分钟没有 H5")
        elif matched_vehicle_dirs:
            hints.append("已找到车辆目录，但没有匹配日期/时间")
        else:
            hints.append("未匹配到车型/车号目录")
        if blf_failure_detail:
            hints.append(f"BLF 补充生成未完成：{blf_failure_detail}")
        raise BatchH5AutoLookupNotFound(
            "未能自动找到 H5 文件："
            f"车型={car_model}，车号={car_id}，日期={date_value}，时间={time_value}；"
            f"搜索根目录={config.h5_search_base_dir}；{'; '.join(hints)}。"
        )

    selected = str(Path(_select_best_h5_match(matched_h5, date_value=date_value, time_value=time_value)).expanduser().resolve())
    detail = f"自动定位 H5：{selected}"
    if len(matched_h5) > 1:
        detail += f"（命中 {len(matched_h5)} 个候选，已按文件起始时间和 H5 内时间范围选择最匹配文件）"
    return selected, True, detail


def _find_and_parse_h5_from_blf(
    *,
    config: BatchRootCauseConfig,
    car_model: str,
    car_id: str,
    date_value: str,
    time_value: str,
    progress_callback: Callable[[str, str], None] | None = None,
) -> tuple[str, str]:
    if find_j6b_blf is None or output_dir_for_blf is None or blf_h5_output_path is None or parse_one_blf is None:
        raise BatchH5AutoLookupNotFound("BLF 自动查找/转换工具不可用。")

    _notify(
        progress_callback,
        "寻找 BLF",
        "未找到覆盖问题时间的 H5，正在根据车号、车型、问题日期和问题时间查找原始 BLF。",
    )
    matched_blf, matched_date_dirs, matched_vehicle_dirs = find_j6b_blf(
        base_dir=config.blf_search_base_dir,
        car_model=car_model,
        car_id=car_id,
        date_value=date_value,
        time_value=time_value,
    )
    if not matched_blf:
        hints = []
        if matched_date_dirs:
            hints.append("已找到车型/日期目录，但没有覆盖该时间的 BLF")
        elif matched_vehicle_dirs:
            hints.append("已找到车辆目录，但没有匹配日期/时间的 BLF")
        else:
            hints.append("未匹配到车型/车号 BLF 目录")
        raise BatchH5AutoLookupNotFound(
            "未能自动找到 BLF 文件："
            f"车型={car_model}，车号={car_id}，日期={date_value}，时间={time_value}；"
            f"搜索根目录={config.blf_search_base_dir}；{'; '.join(hints)}。"
        )

    blf_path = Path(matched_blf[0]).expanduser().resolve()
    out_dir = output_dir_for_blf(
        blf_path=blf_path,
        blf_base_dir=config.blf_search_base_dir,
        h5_base_dir=config.h5_search_base_dir,
        car_model=car_model,
        car_id=car_id,
        date_value=date_value,
    )
    out_path = blf_h5_output_path(blf_path, out_dir)
    if out_path.exists():
        return str(out_path.resolve()), f"H5 未找到，已根据 BLF 自动定位已有解析结果：{out_path.resolve()}（BLF：{blf_path}）"

    _notify(
        progress_callback,
        "BLF 转 H5",
        f"已找到 BLF：{blf_path}，准备解析并生成 H5。",
    )
    _notify(
        progress_callback,
        "H5 创建中",
        f"正在创建/检查输出目录并写入 H5：{out_path}",
    )
    try:
        _ensure_h5_output_dir_writable(out_dir)
    except Exception as exc:
        raise BatchH5AutoLookupNotFound(f"H5 创建失败：{exc}") from exc
    print(f"  H5 not found; parsing BLF to H5: {blf_path} -> {out_path}")
    try:
        converted = parse_one_blf(blf_path, out_path)
    except Exception as exc:
        raise BatchH5AutoLookupNotFound(f"BLF 转 H5 失败：{blf_path} -> {out_path}；原始错误：{exc}") from exc
    if not converted:
        raise BatchH5AutoLookupNotFound(f"BLF 转 H5 失败：{blf_path} -> {out_path}")
    return str(out_path.resolve()), f"H5 未找到，已自动定位 BLF 并解析生成 H5：{out_path.resolve()}（BLF：{blf_path}）"


def _format_h5_auto_lookup_failure(detail: str) -> tuple[str, str]:
    detail = str(detail or "").strip()
    conclusion_lines = [
        H5_AUTO_LOOKUP_NOT_FOUND_MESSAGE,
        "数据准备结果：H5 自动定位失败，BLF 补充生成未完成。",
    ]
    if detail:
        conclusion_lines.append(f"失败原因：{_clip(detail, 1200)}")
    summary_detail = f"；{detail}" if detail else ""
    return "\n".join(conclusion_lines), _clip(f"{H5_AUTO_LOOKUP_NOT_FOUND_MESSAGE}{summary_detail}", SUMMARY_MAX_CHARS)


def _ensure_h5_output_dir_writable(out_dir: Path) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise RuntimeError(f"没有权限创建解析后的 H5 目录：{out_dir}；请检查 Ubuntu 挂载目录写权限。原始错误：{exc}") from exc
    probe = out_dir / f".blu_write_test_{os.getpid()}_{int(time.time() * 1000)}.tmp"
    try:
        probe.write_text("ok", encoding="utf-8")
    except Exception as exc:
        raise RuntimeError(f"没有权限写入解析后的 H5 目录：{out_dir}；请检查 Ubuntu 挂载目录写权限。原始错误：{exc}") from exc
    finally:
        try:
            probe.unlink(missing_ok=True)
        except Exception:
            pass


def _validate_current_h5_path_for_problem_time(
    current_h5_path: str,
    *,
    config: BatchRootCauseConfig,
    problem_date: str,
    problem_time: str,
) -> tuple[str | None, bool | None, str]:
    date_value = _normalize_h5_lookup_date(problem_date)
    time_value = _normalize_h5_lookup_time(problem_time)
    if not date_value or not time_value:
        return None, None, "未填写完整问题日期/时间，跳过 H5 覆盖范围校验。"
    if parse_h5_lookup_problem_datetime is None or select_h5_candidates_for_time is None:
        return None, None, "H5 自动定位工具不可用，跳过 H5 覆盖范围校验。"

    try:
        target_time = parse_h5_lookup_problem_datetime(date_value, time_value)
    except Exception:
        return None, None, "问题日期/时间格式无法解析，跳过 H5 覆盖范围校验。"

    try:
        resolved_current = resolve_batch_data_path(current_h5_path, config)
    except Exception as exc:
        return None, False, f"当前机器无法确认已填写 H5 覆盖范围：{exc}"

    sibling_paths = [str(resolved_current)]
    try:
        sibling_paths.extend(str(path) for path in resolved_current.parent.glob("*.h5"))
    except Exception:
        pass

    try:
        matches = select_h5_candidates_for_time(sibling_paths, target_time)
    except Exception as exc:
        return None, None, f"确认 H5 覆盖范围时读取失败：{exc}"

    current_key = _path_identity(resolved_current)
    for candidate in matches:
        if _path_identity(candidate.path) == current_key:
            return (
                None,
                True,
                f"已确认表格 H5 覆盖问题时间 {target_time.strftime('%Y-%m-%d %H:%M:%S')}。",
            )

    if matches:
        replacement = str(Path(matches[0].path).expanduser().resolve())
        return (
            replacement,
            False,
            "表格中已填写的 H5 不覆盖问题时间 "
            f"{target_time.strftime('%Y-%m-%d %H:%M:%S')}；"
            f"同目录匹配到覆盖该时间的 H5：{replacement}",
        )

    return (
        None,
        False,
        "表格中已填写的 H5 不覆盖问题时间 "
        f"{target_time.strftime('%Y-%m-%d %H:%M:%S')}，同目录也没有覆盖该时间的 H5。",
    )


def _path_identity(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve()).replace("\\", "/").lower()


def _normalize_h5_lookup_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = _parse_datetime_like_text(text)
    if parsed:
        return parsed.strftime("%Y/%m/%d")
    return text.replace("-", "/")


def _normalize_h5_lookup_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = _parse_datetime_like_text(text)
    if parsed:
        return parsed.strftime("%H:%M:%S")
    return text


def _parse_datetime_like_text(value: str) -> datetime.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
        if number > 1e12:
            number /= 1000.0
        if number > 1e9:
            return datetime.datetime.fromtimestamp(number)
    except Exception:
        pass
    normalized = text.replace("T", " ").replace("/", "-")
    normalized = normalized.split("+", 1)[0].strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%H:%M:%S",
        "%H:%M",
    ):
        try:
            return datetime.datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None


def _select_best_h5_match(paths: list[str], *, date_value: str, time_value: str) -> str:
    if len(paths) <= 1:
        return str(paths[0])
    target = _parse_datetime_like_text(f"{date_value} {time_value}")
    if target is None:
        return sorted(paths)[0]

    def score(path: str) -> tuple[float, str]:
        parsed = _parse_h5_file_start_datetime(Path(path).name)
        if parsed is None:
            return (float("inf"), path)
        return (abs((parsed - target).total_seconds()), path)

    return min(sorted(paths), key=score)


def _parse_h5_file_start_datetime(file_name: str) -> datetime.datetime | None:
    import re

    match = re.search(
        r"(20\d{2})[-_](\d{1,2})[-_](\d{1,2})[-_](\d{1,2})[-_](\d{1,2})[-_](\d{1,2})",
        str(file_name or ""),
    )
    if not match:
        return None
    try:
        year, month, day, hour, minute, second = [int(item) for item in match.groups()]
        return datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _count_pending_records(records: list[dict[str, Any]], config: BatchRootCauseConfig) -> int:
    count = 0
    for record in records:
        if _is_pending_problem_record(record, config):
            count += 1
    return count


def _count_pending_jira_records(records: list[dict[str, Any]], config: BatchRootCauseConfig) -> int:
    return sum(
        1
        for record in records
        if _is_pending_problem_record(record, config) and _has_jira_link(record, config)
    )


def _prioritize_jira_link_records(
    records: list[dict[str, Any]],
    config: BatchRootCauseConfig,
) -> list[dict[str, Any]]:
    prioritized: list[tuple[int, int, dict[str, Any]]] = []
    for original_index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            continue
        enriched = dict(record)
        enriched["_batch_original_index"] = original_index
        priority = 0 if _is_pending_problem_record(enriched, config) and _has_jira_link(enriched, config) else 1
        prioritized.append((priority, original_index, enriched))
    return [record for _, _, record in sorted(prioritized, key=lambda item: (item[0], item[1]))]


def _is_pending_problem_record(record: dict[str, Any], config: BatchRootCauseConfig) -> bool:
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
    problem_description = _field_text(fields, config.problem_field, config.problem_field_id)
    h5_path = _field_text(fields, config.path_field, config.path_field_id)
    problem_type = _field_text(fields, config.type_field, config.type_field_id)
    problem_status = _field_text(fields, config.status_field, config.status_field_id)
    return bool(any((problem_description, h5_path, problem_type)) and problem_status == STATUS_PENDING)


def _has_jira_link(record: dict[str, Any], config: BatchRootCauseConfig) -> bool:
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
    return bool(_field_text(fields, config.jira_link_field, config.jira_link_field_id).strip())


def _original_record_index(record: dict[str, Any], fallback: int) -> int:
    raw_value = record.get("_batch_original_index") or record.get("record_index") or record.get("row_index")
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return fallback


def _batch_progress_text(
    *,
    total: int,
    total_pending: int | None = None,
    processed: int,
    current: int,
    row_index: int,
    problem_type: str,
    h5_path: str,
) -> str:
    total_display = total or current
    if total_pending is not None and total_pending > total_display:
        total_text = f"本轮 {total_display}/{total_pending} 条待分析"
    else:
        total_text = f"共 {total_display} 条问题"
    file_name = Path(h5_path).name if h5_path else "未填写文件路径"
    issue_type = problem_type or "未填写问题类型"
    return (
        f"{total_text}，已处理 {processed} 条；"
        f"当前第 {current}/{total_display} 条（表格第 {row_index} 行）："
        f"{issue_type} / {file_name}"
    )


def _batch_row_progress_text(
    *,
    total: int,
    total_pending: int | None = None,
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
    base = _batch_progress_text(
        total=total,
        total_pending=total_pending,
        processed=processed,
        current=current,
        row_index=row_index,
        problem_type=problem_type,
        h5_path=h5_path,
    )
    step_prefix = f"单条进度 {step_index}/{step_total}" if step_index else "单条进度"
    lines = [base, f"{step_prefix}：{step}"]
    if detail:
        lines.append(_clip(detail, 320))
    return "\n".join(lines)


def run_batch_root_cause_analysis(
    *,
    config: BatchRootCauseConfig | None = None,
    progress_callback: Callable[[str, str], None] | None = None,
    cancellation_check: Callable[[], bool] | None = None,
) -> BatchRootCauseResult:
    config = config or load_batch_root_cause_config()
    if not config.is_configured:
        raise RuntimeError("未配置 BATCH_ROOT_CAUSE_BASE_TOKEN 和 BATCH_ROOT_CAUSE_TABLE_ID。")

    _raise_if_cancelled(cancellation_check)
    _notify(progress_callback, "查询中", "正在读取飞书多维表格中的待分析问题。")
    records = _list_records(config)
    _raise_if_cancelled(cancellation_check)
    allowed_types = set(allowed_problem_types())
    result_rows: list[BatchRowResult] = []
    pending_total = _count_pending_records(records, config)
    jira_pending_total = _count_pending_jira_records(records, config)
    prioritized_records = _prioritize_jira_link_records(records, config)
    pending_records = [
        record for record in prioritized_records if _is_pending_problem_record(record, config)
    ]
    process_limit = int(config.max_rows or 0)
    planned_total = len(pending_records) if process_limit <= 0 else min(len(pending_records), process_limit)
    records_to_process = pending_records[:planned_total]
    jira_priority_note = f"；其中 {jira_pending_total} 条带 JIRA链接，将优先处理" if jira_pending_total else ""
    process_limit_note = (
        f"；本轮按 BATCH_ROOT_CAUSE_MAX_ROWS={process_limit} 处理前 {planned_total} 条"
        if process_limit > 0 and pending_total > planned_total
        else ""
    )
    _notify(
        progress_callback,
        "查询中",
        f"已扫描 {len(records)} 条记录，其中 {pending_total} 条问题等待分析{jira_priority_note}{process_limit_note}。",
    )
    processed_count = 0
    success_count = 0
    failed_count = 0
    skipped_count = len(records) - planned_total
    llm_prechecked = False

    for process_index, record in enumerate(records_to_process, start=1):
        _raise_if_cancelled(cancellation_check)
        index = _original_record_index(record, process_index)
        record_id = str(record.get("record_id") or record.get("id") or "")
        fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        problem_description = _field_text(fields, config.problem_field, config.problem_field_id)
        h5_path = _field_text(fields, config.path_field, config.path_field_id)
        problem_type = _field_text(fields, config.type_field, config.type_field_id)
        problem_status = _field_text(fields, config.status_field, config.status_field_id)
        problem_date = _field_text_with_legacy(
            fields,
            config.problem_date_field,
            config.problem_date_field_id,
            LEGACY_PROBLEM_DATE_FIELD,
        )
        problem_time = _field_text_with_legacy(
            fields,
            config.problem_time_field,
            config.problem_time_field_id,
            LEGACY_PROBLEM_TIME_FIELD,
        )

        if not any((problem_description, h5_path, problem_type)):
            skipped_count += 1
            continue
        if problem_status != STATUS_PENDING:
            skipped_count += 1
            continue

        processed_count += 1
        row_started = False
        row_finished = False

        def row_notify(
            stage: str,
            step: str,
            detail: str = "",
            *,
            step_index: int | None = None,
            step_total: int = 7,
        ) -> None:
            _raise_if_cancelled(cancellation_check)
            _notify(
                progress_callback,
                stage,
                _batch_row_progress_text(
                    total=planned_total,
                    total_pending=pending_total,
                    processed=processed_count - 1,
                    current=processed_count,
                    row_index=index,
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
                step_index=5,
                step_total=8,
            )

        row_notify(
            "问题分析中",
            "校验表格字段",
            "正在检查记录 ID、问题类型，以及 H5 路径或自动定位所需字段。",
            step_index=1,
            step_total=8,
        )
        analysis = None
        try:
            if not record_id:
                raise RuntimeError("表格记录缺少 record_id，无法回写结果。")
            if not problem_type:
                raise ValueError("缺少 Agent_问题类型。")
            if problem_type not in allowed_types:
                raise ValueError(
                    "Agent_问题类型不在当前覆盖范围内："
                    f"{problem_type}。可选：{', '.join(sorted(allowed_types))}"
                )

            if h5_path:
                row_notify(
                    "问题分析中",
                    "确认 H5 路径来源",
                    "表格已填写 H5 路径，正在确认该文件是否覆盖表格问题时间。",
                    step_index=2,
                    step_total=8,
                )
            else:
                row_notify(
                    "问题分析中",
                    "自动定位 H5 路径",
                    "表格未填写 H5 路径，正在根据车号、车型、问题日期和问题时间查找解析后的 H5。",
                    step_index=2,
                    step_total=8,
                )
            h5_path, auto_filled_h5_path, lookup_detail = _resolve_or_find_h5_path(
                fields=fields,
                config=config,
                current_h5_path=h5_path,
                problem_date=problem_date,
                problem_time=problem_time,
                progress_callback=lambda step, detail: row_notify(
                    "问题分析中",
                    step,
                    detail,
                    step_index=2,
                    step_total=8,
                ),
            )
            row_notify("问题分析中", "确认 H5 路径", lookup_detail, step_index=2, step_total=8)

            row_notify("问题分析中", "检查 H5 路径", str(h5_path), step_index=3, step_total=8)
            resolved_data_path = resolve_batch_data_path(h5_path, config)
            if _problem_type_requires_llm(problem_type) and not llm_prechecked:
                row_notify(
                    "问题分析中",
                    "校验模型接口",
                    "正在用短请求确认当前模型 API 可用。",
                    step_index=4,
                    step_total=8,
                )
                try:
                    ensure_llm_api_available()
                except Exception as exc:
                    raise BatchDeepSeekAPIUnavailable(str(exc) or get_llm_api_unavailable_message()) from exc
                llm_prechecked = True

            row_notify("问题分析中", "回写当前行状态", "正在将该行状态置为“分析中”。", step_index=3, step_total=8)
            running_fields = {config.status_field: STATUS_RUNNING}
            if auto_filled_h5_path:
                running_fields[config.path_field] = h5_path
            _update_record_fields(config, record_id, running_fields)
            row_started = True
            row_notify(
                "问题分析中",
                "启动单条分析",
                f"已定位数据文件：{resolved_data_path.name}",
                step_index=5,
                step_total=8,
            )
            analysis = run_single_batch_analysis(
                data_path=resolved_data_path,
                problem_type=problem_type,
                problem_description=problem_description,
                database_name=config.database_name,
                problem_date=problem_date,
                problem_time=problem_time,
                progress_callback=row_analysis_progress,
            )
            conclusion = analysis.conclusion
            summary = analysis.summary
            analysis_failed = analysis.status == "error"
            write_status = STATUS_FAILED if analysis_failed else STATUS_DONE
            row_notify(
                "回传结果中",
                "回写结论和概要",
                "分析完成，正在写入结论、概要和状态。",
                step_index=6,
                step_total=8,
            )
            _update_record_fields(
                config,
                record_id,
                {
                    config.conclusion_field: conclusion,
                    config.summary_field: summary,
                    config.status_field: write_status,
                },
            )
            row_notify(
                "回传结果中",
                "上传关键信号图示",
                f"准备上传 {len(analysis.image_paths)} 张图示。",
                step_index=7,
                step_total=8,
            )
            uploaded_images = _upload_attachments(config, record_id, analysis.image_paths)
            if analysis_failed:
                failed_count += 1
            else:
                success_count += 1
            result_rows.append(
                BatchRowResult(
                    record_id=record_id,
                    row_index=index,
                    problem_description=problem_description,
                    h5_path=h5_path,
                    problem_type=problem_type,
                    status="failed" if analysis_failed else "success",
                    conclusion=conclusion,
                    summary=summary,
                    image_paths=analysis.image_paths,
                    uploaded_images=uploaded_images,
                )
            )
            row_finished = True
            row_notify(
                "问题分析中",
                "本条完成",
                f"已处理 {success_count + failed_count}/{planned_total} 条，准备处理下一条。",
                step_index=8,
                step_total=8,
            )
        except BatchRootCauseCancelled as exc:
            if record_id and row_started and not row_finished:
                try:
                    _update_record_fields(
                        config,
                        record_id,
                        {
                            config.conclusion_field: "已取消：用户触发“取消当前问题分析”，本条未完成分析。",
                            config.summary_field: "已取消：等待重新分析",
                            config.status_field: STATUS_PENDING,
                        },
                    )
                except Exception as update_exc:
                    print(f"batch root cause cancel status reset failed: {update_exc}", flush=True)
            raise BatchRootCauseCancelled(
                "用户已取消当前批量行车问题原因分析；"
                f"表格待分析 {pending_total} 条，本轮计划 {planned_total} 条，已完成 {success_count} 条，失败 {failed_count} 条。"
            ) from exc
        except BatchDeepSeekAPIUnavailable as exc:
            unavailable_message = str(exc) or get_llm_api_unavailable_message()
            if record_id and row_started and not row_finished:
                try:
                    _update_record_fields(
                        config,
                        record_id,
                        {
                            config.conclusion_field: f"未分析：{unavailable_message}。",
                            config.summary_field: unavailable_message,
                            config.status_field: STATUS_PENDING,
                        },
                    )
                except Exception as update_exc:
                    print(f"batch root cause LLM unavailable status reset failed: {update_exc}", flush=True)
            raise BatchDeepSeekAPIUnavailable(unavailable_message) from exc
        except BatchH5AutoLookupNotFound as exc:
            skipped_count += 1
            conclusion, summary = _format_h5_auto_lookup_failure(str(exc))
            if record_id:
                try:
                    row_notify(
                        "回传结果中",
                        "等待 H5 数据",
                        f"{conclusion}；问题状态保持为“{STATUS_PENDING}”。",
                        step_index=6,
                        step_total=8,
                    )
                    _update_record_fields(
                        config,
                        record_id,
                        {
                            config.conclusion_field: conclusion,
                            config.summary_field: summary,
                            config.status_field: STATUS_PENDING,
                        },
                    )
                except Exception as update_exc:
                    conclusion = f"{conclusion}\n回写失败：{update_exc}"
            result_rows.append(
                BatchRowResult(
                    record_id=record_id,
                    row_index=index,
                    problem_description=problem_description,
                    h5_path=h5_path,
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
                step_index=8,
                step_total=8,
            )
        except BatchFeishuWritebackError as exc:
            failed_count += 1
            detail = _friendly_openapi_error(exc)
            if analysis is not None:
                conclusion = analysis.conclusion
                summary = analysis.summary or "飞书写回失败：分析结果已生成但未写入多维表格"
                status = "writeback_failed_after_analysis"
            else:
                conclusion = "飞书写回失败：未能更新当前表格行状态，本条未继续消耗模型 token。"
                summary = "飞书写回失败：本条未启动分析"
                status = "writeback_failed_before_analysis"
            result_rows.append(
                BatchRowResult(
                    record_id=record_id,
                    row_index=index,
                    problem_description=problem_description,
                    h5_path=h5_path,
                    problem_type=problem_type,
                    status=status,
                    conclusion=conclusion,
                    summary=summary,
                    image_paths=getattr(analysis, "image_paths", []) if analysis is not None else [],
                    uploaded_images=0,
                    error=detail,
                )
            )
            row_notify(
                "回传结果中",
                "飞书写回失败",
                (
                    "飞书多维表格写回失败，不是模型接口失败。"
                    f"错误：{_clip(detail, 260)}。本轮停止，避免继续消耗模型 token。"
                ),
                step_index=8,
                step_total=8,
            )
            break
        except Exception as exc:
            failed_count += 1
            conclusion, summary, progress_error = _friendly_batch_analysis_failure(exc)
            if record_id:
                try:
                    row_notify(
                        "回传结果中",
                        "回写失败状态",
                        f"本条分析失败，正在写入失败原因：{progress_error}",
                        step_index=6,
                        step_total=8,
                    )
                    _update_record_fields(
                        config,
                        record_id,
                        {
                            config.conclusion_field: conclusion,
                            config.summary_field: summary,
                            config.status_field: STATUS_FAILED,
                        },
                    )
                except Exception as update_exc:
                    conclusion = f"{conclusion}\n回写失败：{update_exc}"
            result_rows.append(
                BatchRowResult(
                    record_id=record_id,
                    row_index=index,
                    problem_description=problem_description,
                    h5_path=h5_path,
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
                f"已处理 {success_count + failed_count}/{planned_total} 条，继续处理下一条。失败原因：{progress_error}",
                step_index=8,
                step_total=8,
            )
        finally:
            if analysis is not None:
                _cleanup_generated_paths(analysis.image_paths)
            _cleanup_empty_runtime_roots()

    waiting_data_count = sum(1 for row in result_rows if row.status == "pending_h5_not_found")
    completion_detail = (
        f"批量分析已结束：表格待分析 {pending_total} 条，本轮计划 {planned_total} 条，已处理 {processed_count} 条，"
        f"成功 {success_count} 条，失败 {failed_count} 条"
        + (f"，待数据 {waiting_data_count} 条" if waiting_data_count else "")
        + "。"
    )
    _notify(
        progress_callback,
        "完成",
        completion_detail,
    )
    return BatchRootCauseResult(
        total_rows=len(records),
        processed_count=processed_count,
        success_count=success_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        base_token=config.base_token,
        table_id=config.table_id,
        pending_count=pending_total,
        planned_count=planned_total,
        jira_pending_count=jira_pending_total,
        rows=result_rows,
    )


@dataclass
class SingleRootCauseBatchAnalysis:
    conclusion: str
    summary: str
    image_paths: list[Path]
    context: str
    status: str
    root_cause_factor: str = ""
    key_signals: str = ""


def run_single_batch_analysis(
    *,
    data_path: Path,
    problem_type: str,
    problem_description: str = "",
    database_name: str = "",
    problem_date: str = "",
    problem_time: str = "",
    progress_callback: Callable[[str, str], None] | None = None,
) -> SingleRootCauseBatchAnalysis:
    if problem_type == FUNCTION_EXIT_PROBLEM_TYPE:
        _notify(progress_callback, "问题分析中", "进入功能退出专项分析。")
        return run_single_function_exit_analysis(
            data_path=data_path,
            problem_description=problem_description,
            problem_date=problem_date,
            problem_time=problem_time,
        )
    if problem_type == LATERAL_SUDDEN_STEERING_PROBLEM_TYPE and not _use_deepseek_investigation_mode():
        _notify(progress_callback, "问题分析中", "进入横向猛打专项分析。")
        return run_single_lateral_sudden_steering_analysis(
            data_path=data_path,
            problem_description=problem_description,
            progress_callback=progress_callback,
        )
    return run_single_root_cause_analysis(
        h5_path=data_path,
        problem_type=problem_type,
        problem_description=problem_description,
        database_name=database_name,
        problem_date=problem_date,
        problem_time=problem_time,
        progress_callback=progress_callback,
    )


def run_single_lateral_sudden_steering_analysis(
    *,
    data_path: Path,
    problem_description: str = "",
    progress_callback: Callable[[str, str], None] | None = None,
) -> SingleRootCauseBatchAnalysis:
    data_path = resolve_batch_data_path(data_path)
    if data_path.suffix.lower() not in SUPPORTED_DATA_SUFFIXES:
        raise ValueError("批量横向猛打原因分析当前仅支持 .h5 数据文件；请先将 .blf/.hdf5 转换成标准 H5。")

    from analysis_engine.metrics.code import lateral_steering_sudden

    _notify(progress_callback, "问题分析中", "横向猛打 1/3：读取 H5 并计算横向关键指标。")
    with lateral_steering_sudden.H5FileAdapter(data_path) as session:
        result = lateral_steering_sudden.analyze_lcc_sudden_steering(session)
        _notify(progress_callback, "问题分析中", "横向猛打 2/3：生成横向证据信号图。")
        image_root = ensure_writable_dir(
            get_batch_artifact_dir()
            / f"lateral_sudden_{int(time.time() * 1000)}_{safe_runtime_name(data_path.stem, 'data')}"
        )
        image_paths = _build_lateral_sudden_steering_plots(session, result, image_root, data_path.name)

    return SingleRootCauseBatchAnalysis(
        conclusion=_format_lateral_sudden_steering_conclusion(
            problem_description=problem_description,
            file_name=data_path.name,
            result=result,
        ),
        summary=_format_lateral_sudden_steering_summary(result),
        image_paths=image_paths,
        context=json.dumps(result, ensure_ascii=False, default=str, indent=2),
        status="ok",
        root_cause_factor=_lateral_sudden_steering_factor(result),
        key_signals=_format_lateral_sudden_steering_key_signals(result),
    )


def run_single_root_cause_analysis(
    *,
    h5_path: Path,
    problem_type: str,
    problem_description: str = "",
    database_name: str = "",
    problem_date: str = "",
    problem_time: str = "",
    progress_callback: Callable[[str, str], None] | None = None,
) -> SingleRootCauseBatchAnalysis:
    data_path = resolve_batch_data_path(h5_path)
    if data_path.suffix.lower() not in SUPPORTED_DATA_SUFFIXES:
        raise ValueError("批量行车问题原因分析当前仅支持 .h5 数据文件；请先将 .blf/.hdf5 转换成标准 H5。")

    if _use_deepseek_investigation_mode():
        from deepseek_root_cause_analysis import run_deepseek_root_cause_investigation

        _notify(progress_callback, "问题分析中", "模型调查模式：开始检索信号并推理根因。")
        try:
            investigation = run_deepseek_root_cause_investigation(
                h5_path=data_path,
                problem_type=problem_type,
                problem_description=problem_description,
                problem_date=problem_date,
                problem_time=problem_time,
                image_root=ensure_writable_dir(
                    get_batch_artifact_dir()
                    / f"deepseek_{int(time.time() * 1000)}_{safe_runtime_name(data_path.stem, 'data')}"
                ),
                progress_callback=progress_callback,
            )
        except Exception as exc:
            if exc.__class__.__name__ == "DeepSeekAPIUnavailable":
                raise BatchDeepSeekAPIUnavailable(str(exc) or get_llm_api_unavailable_message()) from exc
            raise
        return SingleRootCauseBatchAnalysis(
            conclusion=investigation.conclusion,
            summary=_clip(investigation.summary, DEEPSEEK_SUMMARY_MAX_CHARS),
            image_paths=investigation.image_paths,
            context=investigation.context,
            status=investigation.status,
            root_cause_factor=investigation.root_cause_factor,
            key_signals=investigation.key_signals_text,
        )

    database_name = database_name or _latest_database_name()
    if not database_name:
        raise RuntimeError("未找到可用 `.arxml` 数据库。")
    database_path = DATABASE_DIR / database_name
    if not database_path.exists():
        raise RuntimeError(f"未找到数据库文件：{database_name}")

    request_text = f"{problem_type}：{problem_description}".strip("：")
    problem_groups = infer_root_cause_problem_groups(request_text, [])

    performance_result = None
    actuator_result = None
    performance_error = ""
    actuator_error = ""
    try:
        _notify(progress_callback, "问题分析中", "本地规则分析 1/3：检查性能指标。")
        performance_result = run_performance_analysis(
            data_path,
            database_name,
            problem_groups,
            request_text=request_text,
        )
    except Exception as exc:
        performance_error = str(exc)

    try:
        _notify(progress_callback, "问题分析中", "本地规则分析 2/3：检查执行器接口与请求响应。")
        actuator_result = run_actuator_handshake_analysis_with_database(
            data_path,
            request_text,
            database_path=database_path,
            artifact_root=ensure_writable_dir(
                get_batch_artifact_dir()
                / f"actuator_{int(time.time() * 1000)}_{safe_runtime_name(data_path.stem, 'data')}"
            ),
        )
    except Exception as exc:
        actuator_error = str(exc)

    _notify(progress_callback, "问题分析中", "本地规则分析 3/3：生成根因证据图。")
    report = build_root_cause_report(
        request_text=request_text,
        performance_result=performance_result,
        performance_error=performance_error,
        actuator_result=actuator_result,
        actuator_error=actuator_error,
    )
    setattr(report, "problem_type", problem_type)
    setattr(report, "problem_description", problem_description)
    evidence_root = ensure_writable_dir(
        get_batch_artifact_dir()
        / f"batch_root_cause_{int(time.time() * 1000)}_{safe_runtime_name(data_path.stem, 'data')}"
    )
    report.evidence_plots = build_root_cause_evidence_plots(
        h5_file=data_path,
        request_text=request_text,
        performance_result=performance_result,
        actuator_result=actuator_result,
        image_root=evidence_root,
    )
    image_paths = _collect_generated_image_paths(performance_result, actuator_result, report)
    conclusion = _format_batch_conclusion(
        problem_type=problem_type,
        problem_description=problem_description,
        database_name=database_name,
        file_name=data_path.name,
        report=report,
    )
    return SingleRootCauseBatchAnalysis(
        conclusion=conclusion,
        summary=_format_root_cause_summary(report),
        image_paths=image_paths,
        context=root_cause_report_context(report),
        status=report.status,
        root_cause_factor=_format_root_cause_factor(report),
        key_signals=_format_report_key_signals(report),
    )


def _use_deepseek_investigation_mode() -> bool:
    value = os.getenv("BATCH_ROOT_CAUSE_ANALYSIS_MODE", "").strip().lower()
    return value in {"deepseek", "deepseek_investigation", "llm", "llm_investigation"}


def _format_lateral_sudden_steering_conclusion(
    *,
    problem_description: str,
    file_name: str,
    result: dict[str, Any],
) -> str:
    events = list(result.get("events") or [])
    lines = [
        f"问题类型：{LATERAL_SUDDEN_STEERING_PROBLEM_TYPE}",
        f"数据文件：{file_name}",
    ]
    if problem_description:
        lines.append(f"问题描述：{problem_description}")
    lines.extend(
        [
            "",
            f"结论：{_lateral_sudden_steering_factor(result)}",
            f"LCC 激活时长：{float(result.get('lcc_active_duration_s') or 0.0):.1f}s",
            f"猛打事件数量：{len(events)}",
        ]
    )
    missing = list(result.get("missing_signals") or [])
    if missing:
        lines.append(f"缺失证据：{', '.join(str(item) for item in missing[:8])}")

    if events:
        lines.extend(["", "事件明细："])
        for event in events[:20]:
            evidence = dict(event.get("evidence") or {})
            lines.append(
                "{idx}. {start:.3f}s - {end:.3f}s，持续 {duration:.3f}s；"
                "实际方向盘角变化 {actual:.1f}deg；根因方向：{direction}".format(
                    idx=int(event.get("index") or 0),
                    start=float(event.get("start") or 0.0),
                    end=float(event.get("end") or 0.0),
                    duration=float(event.get("duration") or 0.0),
                    actual=float(evidence.get("actual_delta") or event.get("delta_deg") or 0.0),
                    direction=_lateral_direction_label(str(event.get("root_cause_direction") or "")),
                )
            )
            if evidence.get("request_data_available"):
                lines.append(
                    f"   - 请求层：转向请求变化 {float(evidence.get('request_delta') or 0.0):.1f}deg，"
                    f"请求/实际比例 {float(evidence.get('request_ratio') or 0.0):.2f}"
                )
            else:
                lines.append("   - 请求层：缺少转向请求信号，无法判断请求侧是否先突变。")
            lane_lines = _lateral_lane_evidence_lines(evidence)
            if lane_lines:
                lines.extend(f"   - 诱因层：{line}" for line in lane_lines[:6])
            elif evidence.get("request_sudden"):
                lines.append("   - 诱因层：未检测到车道 C0/C1 超阈值突变，需继续排查目标路径、模式切换或驾驶员接管。")
    else:
        lines.extend(["", "检查结果："])
        lines.append(_lateral_no_event_explanation(result))

    resolved = dict(result.get("resolved_signals") or {})
    if resolved:
        lines.extend(["", "已匹配关键信号："])
        for key in ("lcc_act", "steering_angle", "steering_request", "lane_c0_left", "lane_c0_right", "lane_c1_left", "lane_c1_right"):
            if key in resolved:
                lines.append(f"- {key}: {resolved[key]}")

    return _clip("\n".join(lines), 18000)


def _format_lateral_sudden_steering_summary(result: dict[str, Any]) -> str:
    return _clip(_lateral_sudden_steering_factor(result), DEEPSEEK_SUMMARY_MAX_CHARS)


def _lateral_sudden_steering_factor(result: dict[str, Any]) -> str:
    events = list(result.get("events") or [])
    if not events:
        return _lateral_no_event_explanation(result)
    conclusion = str(result.get("conclusion") or "")
    if conclusion == "request_side_lane_geometry_trigger":
        return "根因：车道几何突变触发转向请求猛打。"
    if conclusion == "request_side_other_trigger":
        return "根因：转向请求侧突变，诱因需继续排查目标路径/模式切换/接管。"
    if conclusion == "actuator_response_issue":
        return "根因：转向请求平稳但方向盘突变，偏 EPS/执行器响应异常。"
    if conclusion == "insufficient_evidence":
        return "证据不足：已检测到方向盘猛打，但缺少转向请求证据。"
    if conclusion == "mixed_evidence":
        return "根因：多事件证据不一致，需分别查看请求侧和执行器响应窗口。"
    direction = str(events[0].get("root_cause_direction") or "")
    return _lateral_direction_factor(direction)


def _lateral_direction_factor(direction: str) -> str:
    if direction == "request_side_lane_geometry":
        return "根因：车道几何突变触发转向请求猛打。"
    if direction == "request_side_other":
        return "根因：转向请求侧突变，诱因需继续排查。"
    if direction == "actuator_response":
        return "根因：请求平稳但方向盘突变，偏 EPS/执行器响应异常。"
    if direction == "insufficient_evidence":
        return "证据不足：缺少转向请求信号，不能区分请求侧/执行器侧。"
    return "根因：横向猛打证据需要结合事件窗口复核。"


def _lateral_direction_label(direction: str) -> str:
    return _lateral_direction_factor(direction).replace("根因：", "").replace("证据不足：", "证据不足：")


def _lateral_no_event_explanation(result: dict[str, Any]) -> str:
    conclusion = str(result.get("conclusion") or "")
    missing = list(result.get("missing_signals") or [])
    if "insufficient evidence" in conclusion or missing:
        return "证据不足：缺少 LCC 状态或方向盘角等核心信号，不能确认猛打现象。"
    if conclusion == "no_lcc_active":
        return "未检测到 LCC 激活，不能形成 LCC 猛打根因结论。"
    return "未检测到 1s 内方向盘角超过 30deg 的 LCC 猛打事件。"


def _lateral_lane_evidence_lines(evidence: dict[str, Any]) -> list[str]:
    lane_evidence = dict(evidence.get("lane_evidence") or {})
    lines: list[str] = []
    for key, item in lane_evidence.items():
        if not isinstance(item, dict):
            continue
        changed = bool(item.get("changed"))
        signal = str(item.get("signal") or key)
        delta = float(item.get("delta") or 0.0)
        threshold = float(item.get("threshold") or 0.0)
        state = "触发" if changed else "未触发"
        lines.append(f"{signal} 变化 {delta:.4g}，阈值 {threshold:.4g}，{state}")
    triggered = [line for line in lines if "，触发" in line]
    return triggered or lines


def _format_lateral_sudden_steering_key_signals(result: dict[str, Any]) -> str:
    lines: list[str] = []
    resolved = dict(result.get("resolved_signals") or {})
    for key, name in resolved.items():
        lines.append(f"- {key}: {name}")
    for event in list(result.get("events") or [])[:3]:
        evidence = dict(event.get("evidence") or {})
        lines.append(
            "- 事件{idx}: {start:.3f}-{end:.3f}s，实际角 {actual:.1f}deg，请求 {request:.1f}deg，方向 {direction}".format(
                idx=int(event.get("index") or 0),
                start=float(event.get("start") or 0.0),
                end=float(event.get("end") or 0.0),
                actual=float(evidence.get("actual_delta") or 0.0),
                request=float(evidence.get("request_delta") or 0.0),
                direction=_lateral_direction_label(str(event.get("root_cause_direction") or "")),
            )
        )
    return _clip("\n".join(lines), 1200)


def _build_lateral_sudden_steering_plots(
    session: Any,
    result: dict[str, Any],
    image_root: Path,
    file_name: str,
) -> list[Path]:
    try:
        from PIL import Image, ImageDraw, ImageFont
        import math
    except Exception:
        return []

    signal_names = [str(item) for item in list(result.get("plot_signals") or []) if item]
    signal_names = list(dict.fromkeys(signal_names))[:8]
    if not signal_names:
        return []

    events = list(result.get("events") or [])
    if events:
        start_ts = max(0.0, min(float(event.get("start") or 0.0) for event in events) - 2.0)
        end_ts = max(float(event.get("end") or 0.0) for event in events) + 2.0
    else:
        try:
            ranges = [session.get_signal(name)[0] for name in signal_names[:2]]
            start_ts = min(float(values[0]) for values in ranges if len(values))
            end_ts = max(float(values[-1]) for values in ranges if len(values))
        except Exception:
            return []
    if end_ts <= start_ts:
        return []

    width = 1400
    panel_height = 118
    top_margin = 48
    left_margin = 150
    right_margin = 30
    bottom_margin = 36
    height = top_margin + panel_height * len(signal_names) + bottom_margin
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    plot_left = left_margin
    plot_right = width - right_margin
    plot_width = plot_right - plot_left

    title = f"LCC sudden steering signals - {file_name}"
    draw.text((18, 16), title[:180], fill=(30, 30, 30), font=font)

    def x_of(timestamp: float) -> int:
        return int(plot_left + (timestamp - start_ts) / (end_ts - start_ts) * plot_width)

    colors = [
        (33, 111, 219),
        (213, 82, 70),
        (55, 142, 80),
        (143, 88, 177),
        (218, 132, 34),
        (35, 151, 153),
        (100, 100, 100),
        (190, 70, 120),
    ]

    for panel_index, signal_name in enumerate(signal_names):
        y_top = top_margin + panel_index * panel_height
        y_bottom = y_top + panel_height - 22
        draw.rectangle((plot_left, y_top, plot_right, y_bottom), outline=(210, 210, 210), fill=(252, 252, 252))
        for event in events:
            event_start = float(event.get("start") or 0.0)
            event_end = float(event.get("end") or event_start)
            x0 = max(plot_left, min(plot_right, x_of(event_start)))
            x1 = max(plot_left, min(plot_right, x_of(event_end)))
            draw.rectangle((x0, y_top + 1, x1, y_bottom - 1), fill=(255, 235, 232))

        try:
            timestamps, values = session.get_signal(signal_name)
        except Exception:
            continue
        points = []
        for timestamp, value in zip(timestamps, values):
            ts = float(timestamp)
            if ts < start_ts or ts > end_ts:
                continue
            val = float(value)
            if math.isnan(val) or math.isinf(val):
                continue
            points.append((ts, val))
        if len(points) < 2:
            draw.text((14, y_top + 18), _short_signal_label(signal_name), fill=(80, 80, 80), font=font)
            draw.text((plot_left + 8, y_top + 8), "no data in window", fill=(120, 120, 120), font=font)
            continue

        stride = max(1, int(math.ceil(len(points) / max(plot_width, 1))))
        points = points[::stride]
        values_only = [value for _, value in points]
        v_min = min(values_only)
        v_max = max(values_only)
        if abs(v_max - v_min) < 1e-9:
            v_min -= 1.0
            v_max += 1.0

        def y_of(value: float) -> int:
            return int(y_bottom - 8 - (value - v_min) / (v_max - v_min) * (panel_height - 40))

        xy = [(x_of(timestamp), y_of(value)) for timestamp, value in points]
        draw.line(xy, fill=colors[panel_index % len(colors)], width=2)
        label = _short_signal_label(signal_name)
        draw.text((14, y_top + 16), label, fill=(40, 40, 40), font=font)
        draw.text((plot_right - 150, y_top + 8), f"{v_min:.3g}..{v_max:.3g}", fill=(95, 95, 95), font=font)

    axis_y = height - bottom_margin + 4
    draw.line((plot_left, axis_y, plot_right, axis_y), fill=(120, 120, 120), width=1)
    for ratio in (0.0, 0.25, 0.5, 0.75, 1.0):
        ts = start_ts + (end_ts - start_ts) * ratio
        x = x_of(ts)
        draw.line((x, axis_y - 4, x, axis_y + 4), fill=(120, 120, 120), width=1)
        draw.text((x - 18, axis_y + 8), f"{ts:.1f}s", fill=(80, 80, 80), font=font)

    image_root = ensure_writable_dir(image_root)
    output_path = image_root / "lateral_sudden_steering.png"
    image.save(output_path)
    return [output_path]


def _short_signal_label(signal_name: str) -> str:
    value = str(signal_name or "")
    if len(value) <= 46:
        return value
    return "..." + value[-43:]


def run_single_function_exit_analysis(
    *,
    data_path: Path,
    problem_description: str = "",
    problem_date: str = "",
    problem_time: str = "",
) -> SingleRootCauseBatchAnalysis:
    data_path = resolve_batch_data_path(data_path)
    if data_path.suffix.lower() not in SUPPORTED_DATA_SUFFIXES:
        raise ValueError("批量功能退出原因分析当前仅支持 .h5 数据文件；请先将 .blf/.hdf5 转换成标准 H5。")

    request_text = problem_description or "分析 ACC/LCC 功能退出原因"
    result = run_function_exit_analysis(
        data_path,
        request_text=request_text,
        problem_date=problem_date,
        problem_time=problem_time,
    )
    conclusion = _format_function_exit_conclusion(result)
    return SingleRootCauseBatchAnalysis(
        conclusion=conclusion,
        summary=_format_function_exit_summary(result),
        image_paths=[],
        context=format_function_exit_result_context(result),
        status=result.status,
        root_cause_factor=_function_exit_factor(conclusion),
        key_signals="",
    )


def _format_batch_conclusion(
    *,
    problem_type: str,
    problem_description: str,
    database_name: str,
    file_name: str,
    report: Any,
) -> str:
    lines = [
        f"问题类型：{problem_type}",
        f"数据文件：{file_name}",
        f"数据库：{database_name}",
    ]
    if problem_description:
        lines.append(f"问题描述：{problem_description}")
    lines.extend(
        [
            "",
            f"结论：{getattr(report, 'headline', '')}",
            f"归因方向：{getattr(report, 'cause_side', '')}",
            f"关注现象：{getattr(report, 'problem_scope', '')}",
        ]
    )
    temporal = getattr(report, "temporal_judgement", {}) or {}
    final_cause = str(temporal.get("final_cause") or "").strip()
    order_text = str(temporal.get("order_text") or "").strip()
    if final_cause:
        lines.append(f"时序判断：{final_cause}")
    if order_text:
        lines.append(f"谁先变化：{order_text}")
    if getattr(report, "summary", ""):
        lines.extend(["", "摘要：", str(report.summary)])
    if getattr(report, "diagnosis_steps", None):
        lines.extend(["", "归因链路："])
        for item in report.diagnosis_steps[:8]:
            step = str(item.get("step") or "").strip()
            result = str(item.get("result") or "").strip()
            if step or result:
                lines.append(f"- {step}：{result}".strip("："))
    if getattr(report, "evidence_rows", None):
        lines.extend(["", "关键证据："])
        for row in report.evidence_rows[:10]:
            issue = str(row.get("issue") or "").strip()
            time_text = str(row.get("time") or "").strip()
            evidence = str(row.get("evidence") or "").strip()
            expected = str(row.get("expected") or "").strip()
            lines.append(f"- {issue} | {time_text} | {evidence}" + (f" | {expected}" if expected else ""))
    if getattr(report, "next_checks", None):
        lines.extend(["", "建议补充/复核："])
        for item in report.next_checks[:6]:
            lines.append(f"- {item}")
    return _clip("\n".join(lines), 18000)


def _format_root_cause_summary(report: Any) -> str:
    temporal = getattr(report, "temporal_judgement", {}) or {}
    final_cause = str(temporal.get("final_cause") or "").strip()
    headline = str(getattr(report, "headline", "") or "").strip()
    summary = str(getattr(report, "summary", "") or "").strip()
    blocking_gap = _batch_blocking_gap_summary(report)

    if blocking_gap:
        return _clip(blocking_gap, SUMMARY_MAX_CHARS)

    cause_text = final_cause or headline or summary
    readable = _specific_root_cause_summary(report, temporal, cause_text)
    if readable:
        return _clip(readable, SUMMARY_MAX_CHARS)

    gap_text = _batch_missing_evidence_summary(report)
    if gap_text:
        return _clip(gap_text, SUMMARY_MAX_CHARS)

    if not cause_text or _is_uncertain_batch_summary(cause_text):
        return _clip(_batch_friendly_uncertain_summary(report), SUMMARY_MAX_CHARS)
    return _clip(_friendly_root_cause_summary(report, temporal, cause_text), SUMMARY_MAX_CHARS)


def _format_root_cause_factor(report: Any) -> str:
    temporal = getattr(report, "temporal_judgement", {}) or {}
    for value in (
        temporal.get("final_cause"),
        getattr(report, "cause_side", ""),
        getattr(report, "headline", ""),
    ):
        text = _strip_root_cause_prefix(str(value or "").strip())
        if text:
            return _clip(text, 120)
    return ""


def _format_report_key_signals(report: Any, limit: int = 1200) -> str:
    rows = list(getattr(report, "evidence_rows", []) or [])
    lines: list[str] = []
    for row in rows[:10]:
        side = str(row.get("side") or "证据").strip()
        issue = str(row.get("issue") or "").strip()
        time_text = str(row.get("time") or "").strip()
        evidence = str(row.get("evidence") or "").strip()
        if not any((issue, evidence)):
            continue
        line = f"- {side}"
        if issue:
            line += f"：{issue}"
        if time_text:
            line += f"（{time_text}）"
        if evidence:
            line += f"；{evidence}"
        lines.append(line)
    return _clip("\n".join(lines), limit)


def _strip_root_cause_prefix(value: str) -> str:
    text = str(value or "").strip()
    for prefix in ("根因：", "根因:", "结论：", "结论:"):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def _batch_blocking_gap_summary(report: Any) -> str:
    issue = _batch_problem_label(report)
    phenomenon_missing = list(getattr(report, "missing_phenomenon_signals", []) or [])
    request_missing = list(getattr(report, "missing_request_signals", []) or [])
    response_missing = list(getattr(report, "missing_response_signals", []) or [])

    if phenomenon_missing and int(getattr(report, "phenomenon_signal_count", 0) or 0) <= 0:
        return f"暂不能确认{issue}是否真实发生：缺少{_missing_signal_meanings(phenomenon_missing)}等实车表现信号。"
    if request_missing and int(getattr(report, "request_signal_count", 0) or 0) <= 0:
        return (
            f"{issue}现象可能存在，但缺少{_missing_signal_meanings(request_missing)}等算法请求信号，"
            "暂不能判断是算法主动要求，还是车辆/执行器自己变化。"
        )
    if response_missing and int(getattr(report, "response_signal_count", 0) or 0) <= 0:
        return (
            f"{issue}现象可能存在，但缺少{_missing_signal_meanings(response_missing)}等执行器响应信号，"
            "暂不能判断执行器是否跟随请求。"
        )
    return ""


def _batch_missing_evidence_summary(report: Any) -> str:
    issue = _batch_problem_label(report)
    gaps = [
        ("现象层", list(getattr(report, "missing_phenomenon_signals", []) or []), f"缺少实车表现证据，暂不能确认{issue}是否成立。"),
        ("请求侧", list(getattr(report, "missing_request_signals", []) or []), f"缺少算法请求证据，暂不能判断{issue}是不是算法主动要求。"),
        ("响应侧", list(getattr(report, "missing_response_signals", []) or []), f"缺少执行器响应证据，暂不能判断{issue}是不是执行器没跟上。"),
        (
            "诱因侧",
            list(getattr(report, "missing_algorithm_cause_signals", []) or []),
            f"{issue}现象和请求/响应能继续对齐，但缺少目标、曲率限速或设定车速等场景输入，暂不能说明算法为什么这么请求。",
        ),
    ]
    for _, names, text in gaps:
        if names:
            return text
    return ""


def _specific_root_cause_summary(report: Any, temporal: dict[str, Any], cause_text: str) -> str:
    issue = _batch_problem_label(report)
    moment = _issue_moment_label(issue)
    cause_side = str(temporal.get("cause_side") or getattr(report, "cause_side", "") or "")
    headline = str(temporal.get("headline") or getattr(report, "headline", "") or "")
    text = " ".join(
        str(part or "")
        for part in (
            cause_side,
            headline,
            getattr(report, "problem_type", ""),
            getattr(report, "problem_description", ""),
            cause_text,
        )
    )
    order = list(temporal.get("order") or [])

    if _contains_any(text, ("未发现明确异常", "暂未发现明确异常", "没有发现明确异常")):
        return f"当前数据里没有看到明确的{issue}异常，实车表现信号没有形成可判定的问题窗口。"
    if not order and _contains_any(text, ("证据不足", "关键证据还没齐", "暂时不能", "暂不能", "不能稳定判断", "无法判断")):
        return _batch_friendly_uncertain_summary(report)

    phenomenon_event = _first_order_event(order, "现象层")
    algorithm_event = _best_algorithm_event(order, text) or _first_order_event(order, "算法请求诱因")
    request_event = _first_order_event(order, "算法请求侧")
    response_event = _first_order_event(order, "执行器响应侧")
    has_algorithm_context = int(getattr(report, "algorithm_cause_signal_count", 0) or 0) > 0

    if _contains_any(text, ("请求漏触发", "策略未响应", "场景风险先出现")) and algorithm_event and not request_event:
        return (
            f"这段{issue}更像算法请求未跟上：{moment}{_algorithm_source_phrase(algorithm_event, order, text)}已经先出现，"
            "但没有看到对应的减速/制动请求，原因点集中在目标选择、触发门限或功能接管状态没有把场景风险转成控制请求。"
        )

    if _contains_any(text, ("场景诱因先出现", "场景诱因与请求几乎同步", "前车", "Cut-in", "曲率", "SetSpeed")) and algorithm_event:
        request_part = _request_source_phrase(request_event) if request_event else "算法随后给出控制请求"
        source_label = _algorithm_source_label(algorithm_event, order, text)
        return (
            f"这段{issue}更像{source_label}导致：{moment}{_algorithm_source_phrase(algorithm_event, order, text)}先变化，"
            f"随后{request_part}，执行器响应不是最先异常的一侧。"
        )

    if _contains_any(text, ("执行器未跟随", "请求已经出现，但响应没有", "未跟随请求")):
        request_part = _request_source_phrase(request_event) if request_event else "算法请求"
        response_part = _response_source_phrase(response_event) if response_event else "执行器响应信号"
        return f"这段{issue}更像执行器响应未跟上：{moment}已经看到{request_part}，但没有看到{response_part}同步闭环。"

    if "算法" not in cause_side and _contains_any(text, ("执行器/接口侧", "响应侧先变", "早于请求侧", "执行器响应侧")):
        response_part = _response_source_phrase(response_event) if response_event else "执行器响应信号"
        if request_event:
            return (
                f"这段{issue}更像执行器响应导致：{moment}{response_part}早于{_request_source_phrase(request_event)}，"
                "说明问题集中在执行器接口响应、反馈状态或总线响应侧。"
            )
        return (
            f"这段{issue}更像执行器响应导致：{moment}未看到算法请求侧先导变化，"
            f"但{response_part}已经出现异常波动。"
        )

    if _contains_any(text, ("请求侧", "请求先变", "请求先变化", "控制请求侧", "执行器主要是在跟随", "请求和响应几乎同步")):
        request_part = _request_source_phrase(request_event) if request_event else "算法请求"
        if algorithm_event:
            source_label = _algorithm_source_label(algorithm_event, order, text)
            return (
                f"这段{issue}更像{source_label}触发的算法请求：{moment}{_algorithm_source_phrase(algorithm_event, order, text)}先变化，"
                f"{request_part}随后变化，执行器基本是在跟随。"
            )
        if not has_algorithm_context and list(getattr(report, "missing_algorithm_cause_signals", []) or []):
            return (
                f"这段{issue}更像算法请求侧导致：{moment}{request_part}先变化，执行器基本跟随；"
                "但缺少目标、曲率限速或设定车速等场景输入，暂不能说明算法为什么这么请求。"
            )
        if issue == "误制动" and not algorithm_event:
            return (
                "这段误制动更像算法请求侧异常：没有看到前车/Cut-in、曲率限速或设定车速等合理诱因先变化，"
                f"但{moment}{request_part}先变化，执行器响应基本跟随。"
            )
        return f"这段{issue}更像算法请求侧导致：{moment}{request_part}先变化，执行器响应基本跟随。"

    if phenomenon_event and not request_event and not response_event:
        return f"这段{issue}只确认了实车表现：{moment}{_phenomenon_source_phrase(phenomenon_event)}有异常，但请求和响应证据不足，暂不能定因。"
    return ""


def _friendly_root_cause_summary(report: Any, temporal: dict[str, Any], cause_text: str) -> str:
    issue = _batch_problem_label(report)
    cause_side = str(temporal.get("cause_side") or getattr(report, "cause_side", "") or "")
    headline = str(temporal.get("headline") or getattr(report, "headline", "") or "")
    text = " ".join(str(part or "") for part in (cause_side, headline, cause_text))

    if _contains_any(text, ("未发现明确异常", "暂未发现明确异常", "没有发现明确异常")):
        return f"当前数据里没有看到明确的{issue}异常。"
    if _contains_any(text, ("证据不足", "关键证据还没齐", "暂时不能", "暂不能", "不能稳定判断", "无法判断")):
        return _batch_friendly_uncertain_summary(report)
    if _contains_any(text, ("场景诱因先出现", "场景诱因与请求几乎同步")):
        return f"{issue}更像是前车、限速或交通场景先触发，算法随后给出控制请求。"
    if _contains_any(text, ("请求漏触发", "策略未响应")):
        return f"{issue}更像是场景风险已出现，但算法请求没有及时跟上。"
    if _contains_any(text, ("执行器未跟随", "请求已经出现，但响应没有", "未跟随请求")):
        return f"{issue}更像是执行器没有跟上控制请求。"
    if _contains_any(text, ("执行器/接口侧", "响应侧先变", "早于请求侧", "执行器响应侧")):
        return f"{issue}更像是执行器或接口侧先出现异常。"
    if _contains_any(text, ("请求和响应几乎同步", "几乎同步")):
        return f"{issue}现象成立，控制请求和执行器响应基本同步，需重点看请求是否合理。"
    if _contains_any(text, ("请求侧", "请求先变", "请求先变化", "控制请求侧", "执行器主要是在跟随")):
        return f"{issue}更像是控制请求先变化，执行器基本是在跟随。"
    return _batch_friendly_uncertain_summary(report)


def _batch_problem_label(report: Any) -> str:
    candidates = [
        getattr(report, "problem_type", ""),
        getattr(report, "problem_scope", ""),
        getattr(report, "summary", ""),
        getattr(report, "headline", ""),
    ]
    temporal = getattr(report, "temporal_judgement", {}) or {}
    candidates.extend([temporal.get("headline", ""), temporal.get("final_cause", "")])
    text = " ".join(str(item or "") for item in candidates)
    mapping = [
        ("误制动", ("误制动",)),
        ("重制动", ("重制动", "急减速")),
        ("顿挫", ("顿挫", "抖动")),
        ("溜车", ("溜车",)),
        ("跟车过远", ("跟车过远", "距离过远", "过远")),
        ("跟车过近", ("跟车过近", "距离过近", "过近")),
        ("跟车距离波动", ("距离波动", "距离震荡", "距离振荡")),
        ("跟车距离异常", ("跟车距离",)),
        ("稳速未达设定车速", ("稳速未达", "SetSpeed", "setspeed")),
        ("不合理低速", ("不合理低速", "低速")),
        ("功能退出", ("功能退出", "退出")),
        ("漏制动", ("漏制动",)),
    ]
    primary_text = str(getattr(report, "problem_type", "") or "")
    if primary_text:
        for label, keywords in mapping:
            if any(keyword in primary_text for keyword in keywords):
                return label
    for label, keywords in mapping:
        if any(keyword in text for keyword in keywords):
            return label
    return "该问题"


def _first_order_event(order: list[Any], side: str) -> dict[str, Any] | None:
    events = [item for item in order if isinstance(item, dict) and item.get("side") == side]
    if not events:
        return None

    def _event_time(item: dict[str, Any]) -> float:
        value = str(item.get("time") or "0").strip().rstrip("s")
        try:
            return float(value)
        except ValueError:
            return 0.0

    return min(events, key=_event_time)


def _best_algorithm_event(order: list[Any], context_text: str) -> dict[str, Any] | None:
    events = [item for item in order if isinstance(item, dict) and item.get("side") == "算法请求诱因"]
    if not events:
        return None

    context = str(context_text or "").lower()

    def event_time(item: dict[str, Any]) -> float:
        value = str(item.get("time") or "0").strip().rstrip("s")
        try:
            return float(value)
        except ValueError:
            return 0.0

    def pick(signal_keywords: tuple[str, ...], *, prefer_cutin: bool | None = None) -> dict[str, Any] | None:
        selected: list[dict[str, Any]] = []
        for item in events:
            signal = str(item.get("signal") or "").lower()
            if not any(keyword in signal for keyword in signal_keywords):
                continue
            if prefer_cutin is not None:
                role = _algorithm_source_label(item, order, context_text)
                if prefer_cutin and role != "Cut-in 目标":
                    continue
                if not prefer_cutin and role == "Cut-in 目标":
                    continue
            selected.append(item)
        return min(selected, key=event_time) if selected else None

    target_keywords = ("target", "flt", "ttc", "distance", "range", "relative", "velocity", "accel")
    priorities: list[dict[str, Any] | None] = []
    if any(keyword in context for keyword in ("cut-in", "cutin", "cut in")):
        priorities.append(pick(target_keywords + ("cutin",), prefer_cutin=True) or pick(target_keywords + ("cutin",)))
    if any(keyword in context for keyword in ("前车", "跟车目标", "目标车", "跟车距离")):
        priorities.append(pick(target_keywords, prefer_cutin=False) or pick(target_keywords))
    if any(keyword in context for keyword in ("曲率", "弯道", "曲率限速")):
        priorities.append(pick(("curvature", "lane_modelc2", "reserved_48", "reserved_49")))
    if any(keyword in context for keyword in ("setspeed", "set speed", "设定车速", "目标车速")):
        priorities.append(pick(("setspeed", "seltdspd")))
    if any(keyword in context for keyword in ("接管", "override", "油门", "制动踏板")):
        priorities.append(pick(("override", "ovrrd", "brkped", "accpdl")))

    for event in priorities:
        if event:
            return event
    return min(events, key=event_time)


def _issue_moment_label(issue: str) -> str:
    if issue in {"误制动", "重制动", "漏制动"}:
        return "制动/减速时刻"
    if issue == "顿挫":
        return "顿挫时刻"
    if issue.startswith("跟车"):
        return "距离异常时刻"
    if issue == "稳速未达设定车速":
        return "未达设定车速时刻"
    if issue == "不合理低速":
        return "低速异常时刻"
    if issue == "溜车":
        return "溜车时刻"
    if issue == "功能退出":
        return "功能退出时刻"
    return "问题时刻"


def _algorithm_source_phrase(
    event: dict[str, Any],
    order: list[Any] | None = None,
    context_text: str = "",
) -> str:
    signal = str(event.get("signal") or "")
    lower = signal.lower()
    display = _signal_display(signal)
    label = _algorithm_source_label(event, order, context_text)
    if label in {"前车/跟车目标", "Cut-in 目标"}:
        return f"{label}状态（{display}）"
    if any(keyword in lower for keyword in ("curvature", "lane_modelc2", "reserved_48", "reserved_49")):
        return f"曲率限速/道路曲率（{display}）"
    if any(keyword in lower for keyword in ("setspeed", "seltdspd")):
        return f"设定车速（{display}）"
    if "override" in lower or "ovrrd" in lower:
        return f"驾驶员接管状态（{display}）"
    return display


def _algorithm_source_label(
    event: dict[str, Any] | None,
    order: list[Any] | None = None,
    context_text: str = "",
) -> str:
    if not event:
        return "场景输入"
    signal = str(event.get("signal") or "")
    lower = signal.lower()
    context = str(context_text or "").lower()
    target_like = any(keyword in lower for keyword in ("target", "flt", "ttc", "distance", "range", "relative", "velocity", "accel"))
    if target_like and any(keyword in context for keyword in ("cut-in", "cutin", "cut in")):
        return "Cut-in 目标"
    if target_like and any(keyword in context for keyword in ("前车", "跟车目标", "目标车", "跟车距离")):
        return "前车/跟车目标"
    if any(keyword in lower for keyword in ("curvature", "lane_modelc2", "reserved_48", "reserved_49")):
        return "曲率限速"
    if any(keyword in lower for keyword in ("setspeed", "seltdspd")):
        return "设定车速变化"
    if "override" in lower or "ovrrd" in lower:
        return "驾驶员接管"
    if "cutin" in lower:
        flag_value = _event_current_number(event)
        if "flag" in lower and flag_value is not None and flag_value < 0.5:
            return "前车/跟车目标"
        return "Cut-in 目标"
    if any(keyword in lower for keyword in ("target", "flt", "ttc", "distance", "range", "relative", "velocity", "accel")):
        return _target_role_from_order(order or []) or "前车/跟车目标"
    return "场景输入"


def _target_role_from_order(order: list[Any]) -> str:
    for item in order:
        if not isinstance(item, dict) or item.get("side") != "算法请求诱因":
            continue
        signal = str(item.get("signal") or "").lower()
        if "cutin" not in signal or "flag" not in signal:
            continue
        value = _event_current_number(item)
        if value is None:
            continue
        return "Cut-in 目标" if value >= 0.5 else "前车/跟车目标"
    return ""


def _event_current_number(event: dict[str, Any]) -> float | None:
    for key in ("current", "value"):
        value = event.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    change = str(event.get("change") or "")
    if "->" not in change:
        return None
    value = change.rsplit("->", 1)[-1].strip()
    try:
        return float(value)
    except ValueError:
        return None


def _request_source_phrase(event: dict[str, Any] | None) -> str:
    if not event:
        return "算法请求"
    signal = str(event.get("signal") or "")
    lower = signal.lower()
    display = _signal_display(signal)
    if any(keyword in lower for keyword in ("brk", "brake", "autbrk")):
        return f"算法制动请求（{display}）"
    if any(keyword in lower for keyword in ("accaccl", "axltrq", "acctrq", "ctrlacc", "torque")):
        return f"算法加减速请求（{display}）"
    return f"算法请求（{display}）"


def _response_source_phrase(event: dict[str, Any] | None) -> str:
    if not event:
        return "执行器响应信号"
    signal = str(event.get("signal") or "")
    lower = signal.lower()
    display = _signal_display(signal)
    if any(keyword in lower for keyword in ("brk", "brake", "regen", "autbrk", "ebcm")):
        return f"制动执行器响应（{display}）"
    if any(keyword in lower for keyword in ("rqstat", "lmtgstat", "axle", "trq", "ecm", "ecp")):
        return f"驱动执行器响应（{display}）"
    return f"执行器响应（{display}）"


def _phenomenon_source_phrase(event: dict[str, Any]) -> str:
    return f"实车表现（{_signal_display(str(event.get('signal') or ''))}）"


def _signal_display(signal: str) -> str:
    short_name = _summary_signal_name(signal)
    meaning = _signal_meaning(short_name)
    if not short_name:
        return meaning
    if meaning == "关键信号":
        return short_name
    return f"{meaning} {short_name}"


def _summary_signal_name(signal: str) -> str:
    value = str(signal or "").strip()
    if not value:
        return ""
    for separator in ("\\", "/"):
        value = value.rsplit(separator, 1)[-1]
    return value.rsplit(".", 1)[-1]


def _signal_meaning(signal: str) -> str:
    lower = _summary_signal_name(signal).lower()
    if not lower:
        return "关键信号"
    for keywords, meaning in SIGNAL_MEANING_KEYWORDS:
        if any(keyword in lower for keyword in keywords):
            return meaning
    return "关键信号"


def _missing_signal_meanings(names: list[str]) -> str:
    meanings: list[str] = []
    for name in names:
        meaning = _signal_meaning(str(name or ""))
        if meaning not in meanings:
            meanings.append(meaning)
    if not meanings:
        return "关键"
    return "、".join(meanings[:3])


def _batch_friendly_uncertain_summary(report: Any) -> str:
    issue = _batch_problem_label(report)
    if list(getattr(report, "missing_request_signals", []) or []):
        return f"{issue}现象可能存在，但缺少控制请求证据，暂不能定因。"
    if list(getattr(report, "missing_response_signals", []) or []):
        return f"{issue}现象可能存在，但缺少执行器响应证据，暂不能定因。"
    if list(getattr(report, "missing_algorithm_cause_signals", []) or []):
        return f"{issue}现象可能存在，但缺少场景诱因证据，暂不能解释原因。"
    if list(getattr(report, "missing_phenomenon_signals", []) or []):
        return f"缺少实车表现证据，暂不能确认{issue}是否成立。"
    return f"{issue}证据链还不完整，暂不能稳定判断最终原因。"


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _is_uncertain_batch_summary(text: str) -> bool:
    value = str(text or "")
    return any(term in value for term in ("证据不足", "证据不完整", "暂不能", "不能判断", "无法判断", "还不能", "未形成明确"))


def _compact_batch_cause_summary(text: str) -> str:
    value = " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split())
    for prefix in ("归因结论：", "核心结论：", "结论：", "原因："):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
    for marker in (" 下一步：", "下一步：", " 建议：", "建议：", " 请先", "请先"):
        index = value.find(marker)
        if index > 0:
            value = value[:index].strip()
    for separator in ("。", "；", ";"):
        index = value.find(separator)
        if 12 <= index <= 120:
            value = value[: index + 1].strip()
            break
    return value or "证据不足，暂未形成明确原因"


def _format_function_exit_conclusion(result: Any) -> str:
    lines = [
        f"问题类型：{FUNCTION_EXIT_PROBLEM_TYPE}",
        f"数据文件：{getattr(result, 'file_name', '')}",
        f"检查范围：{getattr(result, 'checked_scope', 'ACC / LCC')}",
        "",
        f"结论：{getattr(result, 'summary', '')}",
    ]
    events = list(getattr(result, "events", []) or [])
    if events:
        lines.extend(["", "退出/原因置位事件："])
        for index, event in enumerate(events[:20], start=1):
            lines.append(
                f"{index}. {event.function} | {event.time_text} | "
                f"{event.from_state} -> {event.to_state} | {event.reason_summary}"
            )
            for evidence in (event.evidence or [])[:4]:
                lines.append(f"   - {evidence}")
    matched = list(getattr(result, "matched_signals", []) or [])
    missing = list(getattr(result, "missing_signals", []) or [])
    lines.extend(["", f"匹配信号：{len(matched)} 个"])
    if missing:
        lines.append(f"缺失信号：{len(missing)} 个")
        for item in missing[:10]:
            lines.append(f"- {item.function} / {item.label}：{item.note or '未匹配到信号'}")
    warnings = list(getattr(result, "warnings", []) or [])
    if warnings:
        lines.extend(["", "提示："])
        for warning in warnings[:8]:
            lines.append(f"- {warning}")
    return _clip("\n".join(lines), 18000)


def _format_function_exit_summary(result: Any) -> str:
    events = list(getattr(result, "events", []) or [])
    if events:
        first = events[0]
        return _clip(_friendly_function_exit_summary(first), 120)
    summary = str(getattr(result, "summary", "") or "").strip()
    if getattr(result, "status", "") == "ok":
        return _clip("未发现明确 ACC/LCC 退出跳变", 120)
    if summary:
        return _clip(_friendly_function_exit_text(summary), 120)
    return _clip("缺少功能状态或退出原因证据，暂不能判断退出原因。", 120)


def _function_exit_factor(text: str) -> str:
    value = str(text or "")
    lowered = value.lower()
    if "手力抑制" in value or "方向盘转角抑制" in value:
        return "驾驶员手力接管或方向盘转角抑制"
    if "前轮压线" in value:
        return "前轮压线触发 LCC 退出/不可用"
    if "车道过窄" in value:
        return "车道过窄触发 LCC 退出/不可用"
    if "车道线过短" in value or "双侧无线" in value:
        return "车道线质量不足触发 LCC 退出/不可用"
    if "EPS 状态异常" in value or "eps" in lowered:
        return "EPS 状态异常触发 LCC 退出/不可用"
    if "踩制动" in value or "制动踏板" in value or "brake" in lowered:
        return "驾驶员踩制动或制动踏板介入"
    if "踩油门" in value or "油门" in value or "accelerator" in lowered:
        return "驾驶员踩油门接管"
    if "esp" in lowered or "车身稳定系统关闭" in value:
        return "车身稳定系统关闭"
    if "曲率" in value or "弯道" in value or "curve" in lowered:
        return "弯道过急或曲率条件触发"
    if "驾驶员" in value and "接管" in value:
        return "驾驶员接管"
    if "证据不足" in value or "暂不能" in value or "不清晰" in value:
        return "证据不足"
    return ""


def _friendly_function_exit_summary(event: Any) -> str:
    function = str(getattr(event, "function", "") or "功能").strip()
    reason = str(getattr(event, "reason_summary", "") or "")
    reason_text = _friendly_function_exit_text(reason)
    if reason_text:
        if "暂不能" in reason_text or "不清晰" in reason_text or "不足" in reason_text:
            return f"{function} 发生退出，但{reason_text}。"
        return f"{function} 退出主要由{reason_text}。"
    return f"{function} 发生退出，具体原因需要看完整结论中的事件证据。"


def _friendly_function_exit_text(text: str) -> str:
    value = str(text or "")
    lowered = value.lower()
    if "手力抑制" in value or "方向盘转角抑制" in value:
        return "驾驶员手力接管或方向盘转角抑制触发"
    if "前轮压线" in value:
        return "前轮压线触发"
    if "车道过窄" in value:
        return "车道过窄触发"
    if "车道线过短" in value:
        return "车道线过短触发"
    if "双侧无线" in value:
        return "双侧车道线丢失或路口场景触发"
    if "EPS 状态异常" in value or "eps" in lowered:
        return "EPS 状态异常触发"
    if "转速过高" in value:
        return "转速过高条件触发"
    if any(keyword in lowered for keyword in ("brkped", "brake_ped", "brake pedal")) or "制动踏板" in value or "踩制动" in value:
        return "驾驶员踩制动或制动踏板介入触发"
    if "油门" in value or "accelerator" in lowered or "throttle" in lowered:
        return "驾驶员踩油门接管触发"
    if "espoff" in lowered or "esp off" in lowered or "车身稳定系统关闭" in value:
        return "车身稳定系统关闭触发"
    if "弯道半径过小" in value or "曲率" in value or "curve" in lowered:
        return "弯道过急或曲率限速相关条件触发"
    if "驾驶员" in value and ("接管" in value or "override" in lowered):
        return "驾驶员接管触发"
    if "没有" in value and ("原因" in value or "原因码" in value):
        return "退出原因码不清晰，暂不能定因"
    if "缺少" in value or "未匹配" in value:
        return "关键退出状态证据不足，暂不能定因"
    return ""


def _list_records(config: BatchRootCauseConfig) -> list[dict[str, Any]]:
    try:
        return _list_records_openapi(config)
    except Exception as exc:
        if not _allow_user_auth_fallback():
            raise RuntimeError(f"读取飞书多维表格失败：{_friendly_openapi_error(exc)}") from exc

    cli_path = _resolve_lark_cli(config)
    if not cli_path:
        raise RuntimeError("未找到 lark-cli，无法读取飞书多维表格。")
    command = [
        cli_path,
        "base",
        "+record-list",
        "--as",
        config.cli_identity,
        "--base-token",
        config.base_token,
        "--table-id",
        config.table_id,
        "--limit",
        str(_cli_record_scan_limit(config)),
        "--field-id",
        _field_selector(config.problem_field, config.problem_field_id),
        "--field-id",
        _field_selector(config.path_field, config.path_field_id),
        "--field-id",
        _field_selector(config.type_field, config.type_field_id),
        "--field-id",
        _field_selector(config.problem_date_field, config.problem_date_field_id),
        "--field-id",
        _field_selector(config.problem_time_field, config.problem_time_field_id),
        "--field-id",
        _field_selector(config.car_id_field, config.car_id_field_id),
        "--field-id",
        _field_selector(config.car_model_field, config.car_model_field_id),
        "--field-id",
        _field_selector(config.jira_link_field, config.jira_link_field_id),
        "--field-id",
        _field_selector(config.conclusion_field, config.conclusion_field_id),
        "--field-id",
        _field_selector(config.status_field, config.status_field_id),
        "--field-id",
        _field_selector(config.summary_field, config.summary_field_id),
        "--format",
        "json",
    ]
    completed = _run_cli_command(command, timeout=60)
    if completed.returncode != 0:
        fallback_command = [
            cli_path,
            "base",
            "+record-list",
            "--as",
            config.cli_identity,
            "--base-token",
            config.base_token,
        "--table-id",
        config.table_id,
        "--limit",
        str(_cli_record_scan_limit(config)),
            "--format",
            "json",
        ]
        fallback_completed = _run_cli_command(fallback_command, timeout=60)
        if fallback_completed.returncode != 0:
            detail = _cli_failure_detail(completed)
            fallback_detail = _cli_failure_detail(fallback_completed)
            if detail == fallback_detail:
                raise RuntimeError(f"读取飞书多维表格失败：{detail}")
            raise RuntimeError(f"读取飞书多维表格失败：{detail}；全字段重试失败：{fallback_detail}")
        completed = fallback_completed
    payload = _load_cli_json(completed.stdout)
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


def _cli_record_scan_limit(config: BatchRootCauseConfig) -> int:
    scan_limit = int(config.scan_max_rows or 0)
    if scan_limit > 0:
        return scan_limit
    # lark-cli requires an explicit limit; use a large bounded default when
    # OpenAPI fallback is used with unlimited scan configured.
    return 10000


def _update_record_fields(config: BatchRootCauseConfig, record_id: str, fields: dict[str, Any]) -> None:
    try:
        _update_record_fields_openapi(config, record_id, fields)
        return
    except Exception as exc:
        if not _allow_user_auth_fallback():
            raise BatchFeishuWritebackError(f"回写分析结论失败：{_friendly_openapi_error(exc)}") from exc

    cli_path = _resolve_lark_cli(config)
    if not cli_path:
        raise BatchFeishuWritebackError("未找到 lark-cli，无法回写飞书多维表格。")
    payload_dir = get_cli_payload_dir()
    safe_record_id = safe_runtime_name(record_id or "record", "record", max_length=80)
    payload_path = ensure_writable_file_path(
        payload_dir / f"batch_record_{safe_record_id}_{int(time.time() * 1000)}.json",
        f"batch_record_{safe_record_id}.json",
    )
    payload_path.write_text(json.dumps(_record_write_payload(config, fields), ensure_ascii=True), encoding="utf-8")
    command = [
        cli_path,
        "base",
        "+record-upsert",
        "--as",
        config.cli_identity,
        "--base-token",
        config.base_token,
        "--table-id",
        config.table_id,
        "--record-id",
        record_id,
        "--json",
        f"@{payload_path.name}",
        "--format",
        "json",
    ]
    try:
        completed = _run_cli_command(command, timeout=60, cwd=payload_path.parent)
    finally:
        payload_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        detail = _cli_failure_detail(completed)
        raise BatchFeishuWritebackError(f"回写分析结论失败：{detail}")
    payload = _load_cli_json(completed.stdout)
    if payload and payload.get("ok") is False:
        raise BatchFeishuWritebackError(f"回写分析结论失败：{_payload_failure_detail(payload)}")


def _list_records_openapi(config: BatchRootCauseConfig) -> list[dict[str, Any]]:
    token = _tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    scan_limit = int(config.scan_max_rows or 0)
    params: dict[str, Any] = {
        "page_size": 500 if scan_limit <= 0 else min(max(scan_limit, 1), 500),
    }
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{config.base_token}"
        f"/tables/{config.table_id}/records"
    )
    records: list[dict[str, Any]] = []
    page_token = ""
    while True:
        request_params = dict(params)
        if page_token:
            request_params["page_token"] = page_token
        response = _openapi_request("GET", url, headers=headers, params=request_params, timeout=60)
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(_base_openapi_error_message(body))
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        items = data.get("items") or []
        for item in items:
            if isinstance(item, dict):
                records.append(item)
                if scan_limit > 0 and len(records) >= scan_limit:
                    return records
        if not data.get("has_more"):
            return records
        page_token = str(data.get("page_token") or "")
        if not page_token:
            return records


def _openapi_request(method: str, url: str, **kwargs: Any) -> requests.Response:
    transient_statuses = {429, 500, 502, 503, 504}
    retryable_errors = (
        requests.exceptions.SSLError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )
    attempts = max(1, int(os.getenv("BATCH_ROOT_CAUSE_FEISHU_RETRY_ATTEMPTS", "3").strip() or "3"))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code in transient_statuses and attempt < attempts:
                time.sleep(min(6.0, 0.8 * attempt))
                continue
            return response
        except retryable_errors as exc:
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(min(6.0, 0.8 * attempt))
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Feishu OpenAPI request failed without response: {method} {url}")


def _update_record_fields_openapi(config: BatchRootCauseConfig, record_id: str, fields: dict[str, Any]) -> None:
    token = _tenant_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{config.base_token}"
        f"/tables/{config.table_id}/records/{record_id}"
    )
    response = _openapi_request(
        "PUT",
        url,
        headers=headers,
        json={"fields": _record_write_payload(config, fields, use_field_ids=False)},
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 0:
        raise RuntimeError(_base_openapi_error_message(body))


def _upload_attachments(config: BatchRootCauseConfig, record_id: str, image_paths: list[Path]) -> int:
    if not config.attachment_field or not record_id:
        return 0
    try:
        uploaded = _upload_attachments_openapi(config, record_id, image_paths)
        if uploaded:
            return uploaded
    except Exception as exc:
        print(f"batch root cause OpenAPI attachment upload failed: {_friendly_openapi_error(exc)}", flush=True)
        if not _allow_user_auth_fallback():
            return 0

    cli_path = _resolve_lark_cli(config)
    if not cli_path:
        return 0
    uploaded = 0
    for image_path in image_paths:
        if not image_path.exists() or image_path.stat().st_size <= 0:
            continue
        staged_path: Path | None = None
        command = [
            cli_path,
            "base",
            "+record-upload-attachment",
            "--as",
            config.cli_identity,
            "--base-token",
            config.base_token,
            "--table-id",
            config.table_id,
            "--record-id",
            record_id,
            "--field-id",
            _field_selector(config.attachment_field, config.attachment_field_id),
            "--file",
            "",
            "--format",
            "json",
        ]
        try:
            staged_path = _stage_cli_attachment(image_path)
            command[command.index("--file") + 1] = staged_path.name
            completed = _run_cli_command(command, timeout=90, cwd=staged_path.parent)
            if completed.returncode == 0:
                uploaded += 1
            else:
                print(
                    "batch root cause attachment upload failed: "
                    f"{image_path} | {_cli_failure_detail(completed)}",
                    flush=True,
                )
        finally:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
    return uploaded


def _upload_attachments_openapi(config: BatchRootCauseConfig, record_id: str, image_paths: list[Path]) -> int:
    valid_paths = [path for path in image_paths if path.exists() and path.stat().st_size > 0]
    if not valid_paths:
        return 0
    token = _tenant_access_token()

    file_tokens: list[str] = []
    for image_path in valid_paths:
        file_token = _upload_bitable_media(token, config.base_token, image_path)
        if file_token:
            file_tokens.append(file_token)
        time.sleep(0.25)
    if not file_tokens:
        return 0

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{config.base_token}"
        f"/tables/{config.table_id}/records/{record_id}"
    )
    response = _openapi_request(
        "PUT",
        url,
        headers=headers,
        json={"fields": {config.attachment_field: [{"file_token": item} for item in file_tokens]}},
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 0:
        raise RuntimeError(_base_openapi_error_message(body))
    return len(file_tokens)


def _upload_bitable_media(token: str, base_token: str, image_path: Path) -> str:
    mime_type = mimetypes.guess_type(str(image_path))[0] or "application/octet-stream"
    parent_type = "bitable_image" if mime_type.startswith("image/") else "bitable_file"
    with image_path.open("rb") as file_obj:
        response = _openapi_request(
            "POST",
            "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all",
            headers={"Authorization": f"Bearer {token}"},
            data={
                "file_name": image_path.name,
                "parent_type": parent_type,
                "parent_node": base_token,
                "size": str(image_path.stat().st_size),
            },
            files={"file": (image_path.name, file_obj, mime_type)},
            timeout=120,
        )
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 0:
        raise RuntimeError(_base_openapi_error_message(body))
    return str((body.get("data") or {}).get("file_token") or "")


def _tenant_access_token() -> str:
    from feishu_config import get_feishu_credentials

    cached_token = str(_TENANT_ACCESS_TOKEN_CACHE.get("token") or "")
    cached_expires_at = float(_TENANT_ACCESS_TOKEN_CACHE.get("expires_at") or 0.0)
    if cached_token and time.time() < cached_expires_at:
        return cached_token

    app_id, app_secret = get_feishu_credentials()
    token_response = _openapi_request(
        "POST",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=20,
    )
    token_response.raise_for_status()
    token_body = token_response.json()
    if token_body.get("code") != 0:
        raise RuntimeError(_base_openapi_error_message(token_body))
    token = str(token_body.get("tenant_access_token") or "")
    if not token:
        raise RuntimeError("Feishu tenant_access_token is empty")
    try:
        expires_in = int(token_body.get("expire") or 7200)
    except (TypeError, ValueError):
        expires_in = 7200
    _TENANT_ACCESS_TOKEN_CACHE["token"] = token
    _TENANT_ACCESS_TOKEN_CACHE["expires_at"] = time.time() + max(60, expires_in - 300)
    return token


def _use_field_ids(config: BatchRootCauseConfig) -> bool:
    return any(
        bool(value)
        for value in (
            config.problem_field_id,
            config.path_field_id,
            config.type_field_id,
            config.conclusion_field_id,
            config.attachment_field_id,
            config.status_field_id,
            config.summary_field_id,
        )
    )


def _allow_user_auth_fallback() -> bool:
    return os.getenv("BATCH_ROOT_CAUSE_ALLOW_USER_AUTH_FALLBACK", "0").strip() == "1"


def _base_openapi_error_message(body: dict[str, Any]) -> str:
    code = body.get("code")
    message = str(body.get("msg") or body.get("message") or "").strip()
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    permission_violations = error.get("permission_violations")
    missing_scopes = error.get("missing_scopes")
    parts = [f"Feishu OpenAPI code={code}"]
    if message:
        parts.append(message)
    if isinstance(permission_violations, list) and permission_violations:
        scopes = []
        for item in permission_violations:
            if isinstance(item, dict):
                scopes.append(str(item.get("scope") or item.get("name") or item))
            else:
                scopes.append(str(item))
        parts.append("缺少应用权限：" + "、".join(scopes))
    if isinstance(missing_scopes, list) and missing_scopes:
        parts.append("缺少应用权限：" + "、".join(str(item) for item in missing_scopes))
    return "；".join(parts)


def _friendly_openapi_error(exc: Exception) -> str:
    text = str(exc)
    lower = text.lower()
    if (
        "ssleoferror" in lower
        or "ssl" in lower
        or "connectionpool" in lower
        or "max retries exceeded" in lower
        or "connection aborted" in lower
        or "remote end closed" in lower
        or "connection reset" in lower
    ):
        return (
            f"{text}。这是飞书 OpenAPI HTTPS 连接被中途断开或网络出口异常，不是模型接口失败；"
            "请检查 Ubuntu/本机到 open.feishu.cn:443 的网络、代理/VPN/网关策略后重试。"
        )
    if "permission" in lower or "scope" in lower or "99991663" in text:
        return (
            f"{text}。当前默认不申请用户授权；请只在飞书开放平台给应用开启批量分析必需的 bot/app 权限："
            "base:record:read、base:record:write、base:field:read、base:field:write，"
            "如需回写信号图示附件，再开启多维表格附件/素材上传相关权限。"
        )
    return text


def _friendly_batch_analysis_failure(exc: Exception) -> tuple[str, str, str]:
    raw = str(exc).strip() or repr(exc)
    lower = raw.lower()
    if "回写分析结论失败" in raw or "open.feishu.cn" in lower or "feishu openapi" in lower:
        detail = _clip(raw.replace("\n", " "), 260)
        conclusion = (
            "分析结果写回飞书多维表格失败，模型侧并未报告调用失败。\n\n"
            f"写回错误：{detail}\n"
            "建议处理：检查当前运行机器到 open.feishu.cn:443 的网络、代理/VPN/网关策略；"
            "网络恢复后将状态改回“待分析”再重跑。"
        )
        return conclusion, "失败：飞书多维表格写回失败", "飞书多维表格写回失败"
    if (
        "object visitation failed" in lower
        or "addr overflow" in lower
        or "bad object header" in lower
        or "file signature not found" in lower
        or "unable to open file" in lower
    ):
        conclusion = (
            "分析失败：H5 文件结构异常或解析不完整，无法读取本问题所需的信号。\n\n"
            "可能原因：数据解析未完成、H5 文件损坏、文件被截断，或该段数据在拷贝过程中不完整。\n"
            "建议处理：重新解析原始数据，或换用覆盖同一问题时间段的完整 H5 后，将状态改回“待分析”再重跑。\n"
            "内部错误类别：H5 对象遍历失败。"
        )
        return conclusion, "失败：H5文件结构异常或解析不完整，需重新解析", "H5 文件结构异常或解析不完整"
    if "timeout" in lower or "timed out" in lower or "read timed out" in lower:
        conclusion = (
            "分析失败：模型或数据读取等待超时，本条没有生成可信结论。\n\n"
            "建议处理：稍后重试；如果同一文件反复超时，可以先缩小问题时间范围或单条分析确认。"
        )
        return conclusion, "失败：分析超时，未生成可信结论", "分析超时"
    if "h5 file not found" in lower or "no such file" in lower or "系统找不到指定的文件" in raw:
        conclusion = (
            "分析失败：没有找到对应的 H5 数据文件。\n\n"
            "建议处理：检查 Agent_h5数据路径 是否为当前运行机器可访问的绝对路径；"
            "如果希望自动定位，请确认 Agent_车号、Agent_车型、Agent_问题日期、Agent_问题时间 已填写。"
        )
        return conclusion, "失败：未找到H5数据文件", "未找到 H5 数据文件"
    if "deepseek" in lower or "llm" in lower or "api" in lower:
        conclusion = (
            "分析失败：模型接口调用失败，本条没有生成可信结论。\n\n"
            f"接口返回：{_clip(raw.replace(chr(10), ' '), 240)}"
        )
        return conclusion, "失败：模型接口调用失败", "模型接口调用失败"

    short = _clip(raw.replace("\n", " "), 160)
    conclusion = (
        "分析失败：本条未生成可信结论。\n\n"
        f"失败摘要：{short}\n"
        "建议处理：确认 H5 文件、问题类型和问题时间是否匹配后，将状态改回“待分析”再重跑。"
    )
    return conclusion, _clip(f"失败：{short}", 120), short


def _stage_cli_attachment(image_path: Path) -> Path:
    attachment_dir = get_cli_attachment_dir()
    safe_stem = safe_runtime_name(image_path.stem, "image", max_length=80)
    suffix = image_path.suffix or ".png"
    target = ensure_writable_file_path(
        attachment_dir / f"{time.time_ns()}_{safe_stem}{suffix}",
        f"{safe_stem}{suffix}",
    )
    shutil.copy2(image_path, target)
    return target


def _collect_generated_image_paths(
    performance_result: Any,
    actuator_result: Any,
    root_cause_report: Any = None,
) -> list[Path]:
    paths: list[Path] = []
    for evidence_plot in getattr(root_cause_report, "evidence_plots", []) or []:
        path = Path(getattr(evidence_plot, "path", evidence_plot))
        if path.exists() and path not in paths:
            paths.append(path)
    for issue in getattr(performance_result, "problems", []) or []:
        for image_path in getattr(issue, "image_paths", []) or []:
            path = Path(image_path)
            if path.exists() and path not in paths:
                paths.append(path)
    for image_path in getattr(actuator_result, "image_paths", []) or []:
        path = Path(image_path)
        if path.exists() and path not in paths:
            paths.append(path)
    return paths


def _cleanup_generated_paths(paths: list[Path]) -> None:
    safe_roots = [
        get_analysis_results_dir().resolve(),
        get_batch_runtime_dir().resolve(),
        get_performance_result_dir().resolve(),
    ]
    for path in paths:
        try:
            resolved = Path(path).resolve()
            owning_root = next((root for root in safe_roots if _is_relative_to(resolved, root)), None)
            if owning_root is None:
                continue
            if resolved.exists():
                resolved.unlink()
            _remove_empty_artifact_parents(resolved.parent, owning_root)
        except Exception as exc:
            print(f"batch root cause image cleanup failed: {path} | {exc}", flush=True)


def _remove_empty_artifact_parents(start: Path, stop_root: Path) -> None:
    current = start
    while current != stop_root and _is_relative_to(current, stop_root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _cleanup_empty_runtime_roots() -> None:
    for root in (get_batch_artifact_dir(), get_batch_runtime_dir()):
        try:
            root.rmdir()
        except OSError:
            pass
        except Exception as exc:
            print(f"batch root cause empty runtime cleanup failed: {root} | {exc}", flush=True)
    for root in (get_cli_attachment_dir(), get_cli_payload_dir()):
        try:
            root.rmdir()
        except OSError:
            pass
        except Exception as exc:
            print(f"batch root cause cli payload cleanup failed: {root} | {exc}", flush=True)


def _field_selector(name: str, field_id: str = "") -> str:
    return str(field_id or name).strip()


def _record_write_payload(
    config: BatchRootCauseConfig,
    fields: dict[str, Any],
    *,
    use_field_ids: bool = True,
) -> dict[str, Any]:
    key_map = {
        config.path_field: config.path_field_id,
        config.conclusion_field: config.conclusion_field_id,
        config.summary_field: config.summary_field_id,
        config.status_field: config.status_field_id,
    }
    payload: dict[str, Any] = {}
    for key, value in fields.items():
        payload_key = _field_selector(str(key), key_map.get(str(key), "")) if use_field_ids else str(key)
        payload[payload_key] = value
    return payload


def _field_text(fields: dict[str, Any], *names: str) -> str:
    value = None
    lowered = {str(key).lower(): key for key in fields}
    for name in names:
        if not name:
            continue
        value = fields.get(name)
        if value is None:
            key = lowered.get(str(name).lower())
            value = fields.get(key) if key else None
        if value is not None:
            break
    return _value_text(value)


def _field_text_with_legacy(
    fields: dict[str, Any],
    name: str,
    field_id: str = "",
    legacy_name: str = "",
) -> str:
    names = [name, field_id]
    if legacy_name and legacy_name not in names:
        names.append(legacy_name)
    return _field_text(fields, *names)


def _value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value).strip()
    if isinstance(value, list):
        parts = [_value_text(item) for item in value]
        return " ".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "name", "value", "url", "link"):
            if key in value:
                text = _value_text(value.get(key))
                if text:
                    return text
        if "elements" in value:
            return _value_text(value.get("elements"))
        return " ".join(_value_text(item) for item in value.values()).strip()
    return str(value).strip()


def _latest_database_name() -> str:
    if not DATABASE_DIR.exists():
        return ""
    files = [path for path in DATABASE_DIR.iterdir() if path.is_file() and path.suffix.lower() == ".arxml"]
    if not files:
        return ""
    return max(files, key=lambda path: _database_priority(path.name)).name


def _database_priority(name: str) -> tuple[tuple[int, ...], int, str]:
    import re

    version_match = re.search(r"V(\d+(?:\.\d+)*)", name, re.IGNORECASE)
    version = tuple(int(part) for part in version_match.group(1).split(".")) if version_match else (0,)
    tier_match = re.search(r"_(HC|MC|LC)_", name, re.IGNORECASE)
    tier_rank = {"HC": 3, "MC": 2, "LC": 1}.get((tier_match.group(1).upper() if tier_match else ""), 0)
    return version, tier_rank, name.lower()


def _resolve_lark_cli(config: BatchRootCauseConfig) -> str | None:
    return shutil.which(config.lark_cli) or shutil.which("lark-cli.cmd") or shutil.which("lark-cli")


def _run_cli_command(
    command: list[str],
    *,
    timeout: int,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    last_completed: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, 4):
        completed = subprocess.run(
            command,
            cwd=str(cwd or ROOT),
            env=get_runtime_subprocess_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        last_completed = completed
        if completed.returncode == 0 or not _is_retryable_cli_failure(completed):
            return completed
        time.sleep(min(2 * attempt, 5))
    return last_completed  # type: ignore[return-value]


def _is_retryable_cli_failure(completed: subprocess.CompletedProcess[str]) -> bool:
    text = f"{completed.stderr}\n{completed.stdout}".lower()
    return any(marker in text for marker in ("eof", "transport", "timeout", "temporar", "connection reset"))


def _load_cli_json(text: str) -> dict[str, Any]:
    value = (text or "").strip()
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"无法解析 lark-cli 返回 JSON：{exc}: {value[:500]}") from exc
    return payload if isinstance(payload, dict) else {}


def _cli_failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    raw = (completed.stderr or completed.stdout or "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(payload, dict):
            return _payload_failure_detail(payload)
    return f"exit={completed.returncode}"


def _payload_failure_detail(payload: dict[str, Any]) -> str:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    if not error:
        return json.dumps(payload, ensure_ascii=False)[:800]

    auth_hint = _friendly_lark_user_auth_error(payload, error)
    if auth_hint:
        return auth_hint

    parts: list[str] = []
    message = str(error.get("message") or "").strip()
    if message:
        parts.append(message)
    missing_scopes = error.get("missing_scopes")
    if isinstance(missing_scopes, list) and missing_scopes:
        parts.append("缺少权限：" + "、".join(str(scope) for scope in missing_scopes))
    hint = str(error.get("hint") or "").strip()
    if hint:
        parts.append("处理建议：" + "；".join(part.strip() for part in hint.splitlines() if part.strip()))
    subtype = str(error.get("subtype") or "").strip()
    if subtype and not parts:
        parts.append(subtype)
    return "；".join(parts) or json.dumps(error, ensure_ascii=False)[:800]


def _friendly_lark_user_auth_error(payload: dict[str, Any], error: dict[str, Any]) -> str:
    text = json.dumps({"identity": payload.get("identity"), "error": error}, ensure_ascii=False).lower()
    if not any(
        marker in text
        for marker in (
            "need_user_authorization",
            "token_missing",
            "base:record:read",
            "base:record:write",
            "base:field:read",
            "base:field:write",
        )
    ):
        return ""
    if "need_user_authorization" not in text and "token_missing" not in text:
        return ""
    if os.name == "nt":
        auth_command = "`lark-cli.cmd auth login --domain base`"
        helper_note = "如果你的 lark-cli 命令不是 `.cmd` 形式，也可以执行 `lark-cli auth login --domain base`。"
    else:
        auth_command = "`blu-agent-lark-auth`"
        helper_note = "如果不是 Ubuntu 一键部署环境，也可以执行 `lark-cli auth login --domain base`。"
    return (
        "当前运行布鲁的这台机器还没有完成飞书多维表格用户授权。"
        f"请在启动布鲁的同一个用户终端执行 {auth_command}，"
        f"{helper_note}"
        "按提示扫码或打开链接授权后，再重新发送 `@布鲁 分析飞书多维表格中的问题`。"
        "这一步只是在新机器、换用户或授权过期时需要做，不是 App ID / App Secret 配错。"
    )


def _relative_cli_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        try:
            return str(path.resolve().relative_to(Path.cwd().resolve()))
        except ValueError:
            return str(path)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _notify(callback: Callable[[str, str], None] | None, stage: str, detail: str) -> None:
    if callback is None:
        return
    try:
        callback(stage, detail)
    except BatchRootCauseCancelled:
        raise
    except Exception as exc:
        print(f"batch root cause progress callback failed: {exc}", flush=True)


def _raise_if_cancelled(cancellation_check: Callable[[], bool] | None) -> None:
    if cancellation_check is None:
        return
    try:
        cancelled = bool(cancellation_check())
    except BatchRootCauseCancelled:
        raise
    except Exception as exc:
        print(f"batch root cause cancellation check failed: {exc}", flush=True)
        return
    if cancelled:
        raise BatchRootCauseCancelled("用户已取消当前批量行车问题原因分析。")


def _clip(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...(已截断)"
