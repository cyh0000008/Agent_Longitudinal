from __future__ import annotations

import json
import os
import re
import time
import uuid
import shutil
import atexit
import contextvars
import zipfile
import threading
from collections import defaultdict
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateFileRequest,
    CreateFileRequestBody,
    GetChatMembersRequest,
    GetMessageRequest,
    ListMessageRequest,
    P2ImMessageReceiveV1,
)
from lark_oapi.api.cardkit.v1 import (
    Card,
    CreateCardRequest,
    CreateCardRequestBody,
    IdConvertCardRequest,
    IdConvertCardRequestBody,
    UpdateCardRequest,
    UpdateCardRequestBody,
)
from lark_oapi.api.contact.v3 import GetUserRequest

from agent_skills import (
    SKILL_ACTUATOR_HANDSHAKE_ANALYSIS,
    SKILL_ACTUATOR_REFERENCE_SEARCH,
    SKILL_BATCH_ROOT_CAUSE_ANALYSIS,
    SKILL_KPI_REPORT_ANALYSIS,
    SKILL_BLF_TO_MDF_CONVERSION,
    SKILL_AXLE_TORQUE_ACCURACY_ANALYSIS,
    SKILL_FEEDBACK_REPORT,
    SKILL_DTC_CODE_SEARCH,
    SKILL_FAULT_DIAGNOSIS,
    SKILL_FAULT_TABLE_SEARCH,
    SKILL_PERFORMANCE_ANALYSIS_SETUP,
    SKILL_PERFORMANCE_CAPABILITY_SEARCH,
    SKILL_REQUIREMENT_INTAKE,
    SKILL_RESULT_TRANSFORMER,
    SKILL_ROOT_CAUSE_ANALYSIS,
    SKILL_FUNCTION_EXIT_ANALYSIS,
    SKILL_SMALLTALK_CHAT,
    TASK_ROOT_CAUSE_ANALYSIS,
    TASK_BATCH_ROOT_CAUSE_ANALYSIS,
    TASK_KPI_REPORT_ANALYSIS,
    TASK_BLF_TO_MDF_CONVERSION,
    TASK_AXLE_TORQUE_ACCURACY,
    TASK_FUNCTION_EXIT_ANALYSIS,
    is_skill_enabled,
    task_type_options,
)
from feishu_client import build_client
from feishu_config import get_feishu_credentials
from actuator_handshake import (
    actuator_request_needs_file,
    actuator_issue_rows,
    actuator_result_headline,
    actuator_result_summary_rows,
    answer_actuator_query,
    format_actuator_markdown,
    human_rule_title,
    is_broad_actuator_consultation,
    is_actuator_data_file,
    run_actuator_handshake_analysis_with_database,
)
from fault_diagnosis import (
    answer_fault_reference_question,
    diagnose_fault_file,
    diagnose_fault_text,
    format_fault_markdown,
)
from dtc_query import answer_dtc_question
from llm_service import (
    analyze_session_message,
    analyze_workflow_request,
    batch_root_cause_near_miss_reply,
    kpi_report_near_miss_reply,
    clear_history,
    decide_agent_action,
    generate_group_reply,
    generate_tool_fallback_reply,
    get_llm_api_unavailable_message,
    polish_actuator_reference_answer,
    polish_dtc_reference_answer,
    polish_fault_reference_answer,
    polish_performance_capability_answer,
    polish_analysis_summary,
    build_capability_reply,
    build_result_transform_plan,
    is_actuator_reference_query_text,
    is_batch_root_cause_analysis_intent,
    is_batch_root_cause_analysis_near_miss,
    is_kpi_report_analysis_intent,
    is_kpi_report_analysis_near_miss,
)
from performance_analysis import (
    answer_performance_capability_question,
    cleanup_runtime_artifacts,
    cleanup_analysis_artifacts,
    download_uploaded_file,
    format_analysis_markdown_clean,
    format_problem_group_scope,
    get_upload_dir,
    list_problem_groups,
    metric_title,
    run_performance_analysis,
)
from result_transformer import (
    GeneratedArtifact,
    render_transform_artifacts,
)
from root_cause_analysis import (
    build_root_cause_evidence_plots,
    build_root_cause_report,
    infer_root_cause_problem_groups,
    root_cause_report_context,
)
from batch_root_cause_analysis import (
    BatchDeepSeekAPIUnavailable,
    BatchRootCauseCancelled,
    BatchRootCauseResult,
    allowed_problem_types,
    run_batch_root_cause_analysis,
    run_single_batch_analysis,
)
from kpi_report_analysis import (
    KpiReportCancelled,
    run_kpi_report_analysis,
)
from function_exit_analysis import (
    format_function_exit_result_context,
    is_function_exit_data_file,
    query_function_exit_reference,
    run_function_exit_analysis,
)
from blf_mdf_converter import (
    DEFAULT_BLF_MDF_DATABASE_NAME,
    BlfMdfConversionResult,
    run_blf_to_mdf_conversion,
)
from axle_torque_accuracy_analysis import (
    AxleTorqueResult,
    VEHICLE_FINAL_DRIVE_RATIOS,
    VEHICLE_SELECTION_NUMBERS,
    detect_vehicle_config,
    find_default_ecm_arxml,
    format_axle_torque_markdown,
    vehicle_final_drive_ratio,
    run_axle_torque_accuracy_analysis,
)
from feedback_reporter import (
    create_feedback_record,
    feedback_context_summary,
    load_feedback_config,
    render_feedback_snapshot,
)
from runtime_paths import (
    ensure_writable_dir,
    ensure_writable_file_path,
    get_runtime_root,
    get_runtime_subdir,
    safe_runtime_name,
    sanitize_runtime_environment,
)


sanitize_runtime_environment()
APP_RUNTIME_ROOT = get_runtime_root()


BOT_NAME = "布鲁"
BOT_ALIASES = {BOT_NAME, "布鲁Mate", "行车分析助手", "ADAS行车分析小助手", "_user_1"}
BOT_OPEN_ID = "ou_9fdc53df9ab2f6e41cab01ef67c7e4d3"
BATCH_ROOT_CAUSE_CANCEL_COMMAND = "取消当前问题分析"
BATCH_ROOT_CAUSE_CANCEL_DISPLAY_COMMAND = f"@布鲁Mate {BATCH_ROOT_CAUSE_CANCEL_COMMAND}"
BATCH_ROOT_CAUSE_CANCEL_HINT = f"发送 `{BATCH_ROOT_CAUSE_CANCEL_DISPLAY_COMMAND}` 可取消当前进程。"
KPI_REPORT_CANCEL_COMMAND = "取消当前KPI分析"
KPI_REPORT_CANCEL_DISPLAY_COMMAND = f"@布鲁Mate {KPI_REPORT_CANCEL_COMMAND}"
KPI_REPORT_CANCEL_HINT = f"发送 `{KPI_REPORT_CANCEL_DISPLAY_COMMAND}` 可取消当前进程。"
DATABASE_DIR = Path(__file__).resolve().parent / "database"
LOCK_FILE = Path(__file__).resolve().parent / "logs" / "feishuagent.lock"


def get_transform_runtime_dir() -> Path:
    return get_runtime_subdir("result_transformer")


TRANSFORM_RUNTIME_DIR = get_transform_runtime_dir()


def env_int(name, default, minimum=1):
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


TASK_TYPES = task_type_options()
COMBINED_ANALYSIS_TASK = TASK_ROOT_CAUSE_ANALYSIS
BATCH_ROOT_CAUSE_ANALYSIS_TASK = TASK_BATCH_ROOT_CAUSE_ANALYSIS
KPI_REPORT_ANALYSIS_TASK = TASK_KPI_REPORT_ANALYSIS
FUNCTION_EXIT_ANALYSIS_TASK = TASK_FUNCTION_EXIT_ANALYSIS
BLF_TO_MDF_CONVERSION_TASK = TASK_BLF_TO_MDF_CONVERSION
AXLE_TORQUE_ACCURACY_TASK = TASK_AXLE_TORQUE_ACCURACY
LAST_SKILL_RESULT_TTL_SECONDS = env_int("LAST_SKILL_RESULT_TTL_SECONDS", 1800, 60)
LAST_SKILL_RESULT_SWEEP_INTERVAL_SECONDS = env_int("LAST_SKILL_RESULT_SWEEP_INTERVAL_SECONDS", 300, 30)
LAST_SKILL_RESULT_MAX_FOLLOWUP_MESSAGES = env_int("LAST_SKILL_RESULT_MAX_FOLLOWUP_MESSAGES", 3, 1)
RECENT_CONTEXT_MAX_TURNS = env_int("RECENT_CONTEXT_MAX_TURNS", 12, 4)
FEEDBACK_BUFFER_MAX_EVENTS = env_int("FEEDBACK_BUFFER_MAX_EVENTS", 30, 10)
FEEDBACK_CONTEXT_TURNS = env_int("FEEDBACK_CONTEXT_TURNS", 8, 3)
BLF_MDF_DIRECT_UPLOAD_MAX_BYTES = env_int("BLF_MDF_DIRECT_UPLOAD_MAX_MB", 100, 1) * 1024 * 1024

rest_client = build_client()
sessions = {}
processed_message_ids = set()
sent_cardkit_card_ids = {}
active_batch_root_cause_tasks = {}
active_batch_root_cause_lock = threading.Lock()
active_kpi_report_tasks = {}
active_kpi_report_lock = threading.Lock()
started_at_ms = int(time.time() * 1000)
last_skill_result_sweep_ms = 0
current_reply_message_id = contextvars.ContextVar("current_reply_message_id", default="")
current_reply_open_id = contextvars.ContextVar("current_reply_open_id", default="")
current_reply_chat_id = contextvars.ContextVar("current_reply_chat_id", default="")
feedback_event_buffers = defaultdict(list)
user_display_name_cache = {}
WORKFLOW_KEYWORDS = {
    "求助",
    "故障",
    "降级",
    "执行器",
    "握手",
    "性能",
    "分析",
    "诊断",
    "数据库",
    "上传",
    "文件",
    "blf",
    "h5",
    "hdf5",
    "确认上传",
}
WAKEUP_HINT_KEYWORDS = {
    "你好",
    "在吗",
    "帮忙",
    "帮我",
    "请问",
    "分析",
    "诊断",
    "看看",
    "处理",
    "问题",
    "求助",
}


def database_options():
    if not DATABASE_DIR.exists():
        return []

    files = [path for path in DATABASE_DIR.iterdir() if path.is_file()]
    files.sort(key=lambda path: path.name.lower())
    return [(path.name, path.name) for path in files]


def parse_database_priority(name):
    version_match = re.search(r"V(\d+(?:\.\d+)*)", name, re.IGNORECASE)
    version = tuple(int(part) for part in version_match.group(1).split(".")) if version_match else (0,)
    tier_match = re.search(r"_(HC|MC|LC)_", name, re.IGNORECASE)
    tier = (tier_match.group(1).upper() if tier_match else "")
    tier_rank = {"HC": 3, "MC": 2, "LC": 1}.get(tier, 0)
    return version, tier_rank, name.lower()


def latest_database_name():
    options = database_options()
    if not options:
        return None
    names = [label for label, _ in options]
    return max(names, key=parse_database_priority)


def blf_to_mdf_default_database_name():
    preferred = DATABASE_DIR / DEFAULT_BLF_MDF_DATABASE_NAME
    if preferred.exists():
        return DEFAULT_BLF_MDF_DATABASE_NAME
    return latest_database_name()


def user_mentions_database_preference(text):
    value = (text or "").lower()
    return any(token in value for token in ["数据库", "arxml", "v27", "v28", "hc", "mc", "lc"])


def _file_suffix(file_name: str) -> str:
    return Path(file_name or "").suffix.lower()


def _is_database_file(file_name: str) -> bool:
    return _file_suffix(file_name) == ".arxml"


def _is_supported_analysis_file(file_name: str) -> bool:
    return _file_suffix(file_name) in {".blf", ".h5", ".hdf5"}


def _is_root_cause_data_file(file_name: str) -> bool:
    return _file_suffix(file_name) == ".h5"


def _is_blf_file(file_name: str) -> bool:
    return _file_suffix(file_name) == ".blf"


def format_exception_for_card(exc) -> str:
    text = str(exc).strip() or repr(exc)
    text = text.replace("`", "'")
    lower = text.lower()
    if (
        "object visitation failed" in lower
        or "addr overflow" in lower
        or "bad object header" in lower
        or "file signature not found" in lower
        or "unable to open file" in lower
    ):
        return (
            "H5 文件结构异常或解析不完整，无法读取本问题所需的信号。\n"
            "建议重新解析原始数据，或换用覆盖同一问题时间段的完整 H5 后再重跑。"
        )
    if "h5 file not found" in lower or "no such file" in lower or "系统找不到指定的文件" in text:
        return "没有找到对应的 H5 数据文件，请确认路径是当前运行机器可访问的绝对路径。"
    if "timeout" in lower or "timed out" in lower or "read timed out" in lower:
        return "分析等待超时，本条没有生成可信结论；可以稍后重试或缩小问题时间范围后再跑。"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "未知错误"
    if len(lines) == 1:
        return lines[0]
    return "\n".join(f"- {line}" for line in lines)


def save_database_file_from_session(session):
    session_token = f"database_{uuid.uuid4().hex[:12]}"
    temp_file = download_uploaded_file(rest_client, session, session_token)
    target_name = Path(session.get("file_name") or temp_file.name).name
    if Path(target_name).suffix.lower() != ".arxml":
        temp_file.unlink(missing_ok=True)
        shutil.rmtree(temp_file.parent, ignore_errors=True)
        raise ValueError("数据库文件必须是 .arxml 格式。")

    target_path = ensure_writable_file_path(DATABASE_DIR / target_name, target_name)
    shutil.copy2(temp_file, target_path)
    try:
        temp_file.unlink(missing_ok=True)
        shutil.rmtree(temp_file.parent, ignore_errors=True)
    except Exception:
        pass
    return target_path.name


def resolve_axle_torque_arxml_from_session(session) -> Path:
    candidates: list[Path] = []
    raw_value = session.get("axle_torque_arxml") or session.get("database") or ""
    if raw_value:
        candidate = Path(str(raw_value)).expanduser()
        if not candidate.is_absolute():
            candidate = DATABASE_DIR / candidate
        candidates.append(candidate)

    default_arxml = find_default_ecm_arxml()
    if default_arxml is not None:
        candidates.append(Path(default_arxml))

    seen: set[str] = set()
    for candidate in candidates:
        marker = str(candidate).lower()
        if marker in seen:
            continue
        seen.add(marker)
        if candidate.exists() and candidate.suffix.lower() == ".arxml" and candidate.stat().st_size > 0:
            session["axle_torque_arxml"] = str(candidate)
            session["database"] = candidate.name
            return candidate

    raise FileNotFoundError(
        "当前没有可用 ECM `.arxml` 数据库。请先上传 ECM ARXML，"
        "或设置 AXLE_TORQUE_ARXML 指向包含 0CB 涡轮转速信号的数据库。"
    )


def set_default_axle_torque_arxml(session) -> str:
    arxml = find_default_ecm_arxml()
    if arxml is None:
        return ""
    session["axle_torque_arxml"] = str(arxml)
    session["database"] = Path(arxml).name
    return Path(arxml).name


def axle_torque_vehicle_options_text() -> str:
    known_options = [
        f"{number}={label}（主减速比 {VEHICLE_FINAL_DRIVE_RATIOS[label]:g}）"
        for number, label in VEHICLE_SELECTION_NUMBERS.items()
    ]
    known_options.append("4=其他车型（需输入 Final Ratio）")
    return "；".join(known_options)


def axle_torque_vehicle_selection_lines() -> list[str]:
    return [
        "**请先选择车型编号**",
        f"- `1`：358-2（Final Ratio `{VEHICLE_FINAL_DRIVE_RATIOS['358-2']:g}`）",
        f"- `2`：C1UL（Final Ratio `{VEHICLE_FINAL_DRIVE_RATIOS['C1UL']:g}`）",
        f"- `3`：E2LB-2（Final Ratio `{VEHICLE_FINAL_DRIVE_RATIOS['E2LB-2']:g}`）",
        "- `4`：其他车型（请同时输入 Final Ratio，例如 `4 3.25`）",
        "- 车型确认后，我会提示你上传 `.blf` 文件。",
    ]


def is_axle_torque_custom_vehicle_choice(text: str | None) -> bool:
    if not text:
        return False
    normalized = " ".join(str(text).strip().lower().split())
    if normalized in {"4", "4.", "4、", "车型4", "车型 4", "其他", "其它", "其他车型", "其它车型"}:
        return True
    return bool(re.match(r"^(?:选|选择|车型|车号|编号)?\s*4[\s:：,，、.)）]", normalized))


def parse_axle_torque_custom_final_drive(text: str | None) -> float | None:
    if not text:
        return None
    normalized = str(text).strip()
    candidate = re.sub(r"^(?:选|选择|车型|车号|编号)?\s*4[\s:：,，、.)）-]*", "", normalized, flags=re.IGNORECASE)
    match = re.search(r"(?<![\d.])([1-9](?:\.\d+)?)(?![\d.])", candidate)
    if not match:
        return None
    ratio = float(match.group(1))
    if 1.0 <= ratio <= 10.0:
        return ratio
    return None


def apply_axle_torque_custom_final_drive(session, final_drive_ratio: float, vehicle_name: str = "其他车型") -> str:
    session["axle_torque_vehicle_config"] = vehicle_name
    session["axle_torque_final_drive_ratio"] = float(final_drive_ratio)
    return vehicle_name


def clear_axle_torque_vehicle_selection(session) -> None:
    session.pop("axle_torque_vehicle_config", None)
    session.pop("axle_torque_final_drive_ratio", None)


def apply_axle_torque_vehicle_config(session, text_or_config: str | None) -> str:
    vehicle_config = detect_vehicle_config(text_or_config)
    if not vehicle_config:
        return ""
    session["axle_torque_vehicle_config"] = vehicle_config
    session["axle_torque_final_drive_ratio"] = vehicle_final_drive_ratio(vehicle_config)
    return vehicle_config


def require_axle_torque_vehicle_config(session) -> tuple[str, float]:
    vehicle_config = session.get("axle_torque_vehicle_config") or ""
    final_drive_ratio = session.get("axle_torque_final_drive_ratio")
    if not vehicle_config or final_drive_ratio is None:
        raise ValueError(f"请先选择轴端扭矩校验车型：{axle_torque_vehicle_options_text()}。")
    return str(vehicle_config), float(final_drive_ratio)


def task_label(value):
    if value == COMBINED_ANALYSIS_TASK:
        return "行车问题原因分析"
    if value == BATCH_ROOT_CAUSE_ANALYSIS_TASK:
        return "批量行车问题原因分析"
    if value == KPI_REPORT_ANALYSIS_TASK:
        return "KPI 报告 AI 分析"
    if value == FUNCTION_EXIT_ANALYSIS_TASK:
        return "ACC/LCC功能退出原因分析"
    if value == BLF_TO_MDF_CONVERSION_TASK:
        return "BLF 转 MDF 转换"
    if value == AXLE_TORQUE_ACCURACY_TASK:
        return "轴端扭矩准确性校验"
    return dict(TASK_TYPES).get(value, value)


def _response_message_id(response):
    data = getattr(response, "data", None)
    return str(getattr(data, "message_id", "") or "")


def _message_response_error(prefix, response):
    log_id = response.get_log_id() if hasattr(response, "get_log_id") else ""
    troubleshooter = ""
    if hasattr(response, "get_troubleshooter"):
        try:
            troubleshooter = response.get_troubleshooter()
        except Exception:
            troubleshooter = ""
    return (
        f"{prefix}: code={getattr(response, 'code', '')}, "
        f"msg={getattr(response, 'msg', '')}, log_id={log_id}, "
        f"troubleshooter={troubleshooter}"
    )


def remember_feedback_event(open_id, chat_id, role, text, message_type="", message_id=""):
    if not open_id or not chat_id:
        return
    key = state_key(open_id, chat_id)
    events = feedback_event_buffers[key]
    events.append(
        {
            "role": str(role or "")[:20],
            "text": str(text or "")[:2500],
            "message_type": str(message_type or "")[:40],
            "message_id": str(message_id or "")[:120],
            "created_at_ms": int(time.time() * 1000),
        }
    )
    if len(events) > FEEDBACK_BUFFER_MAX_EVENTS:
        del events[: len(events) - FEEDBACK_BUFFER_MAX_EVENTS]


def recent_feedback_events(open_id, chat_id, limit=None):
    key = state_key(open_id, chat_id)
    events = list(feedback_event_buffers.get(key) or [])
    limit = limit or FEEDBACK_CONTEXT_TURNS
    return events[-limit:]


def extract_feedback_text(text):
    value = (text or "").strip()
    match = re.match(r"^(问题反馈|反馈|报错|bug|BUG|纠错|吐槽)\s*[:：\s]*(.*)$", value, flags=re.I | re.S)
    if match:
        return (match.group(2) or match.group(1)).strip()
    match = re.search(r"(?:记录|提交|保存).{0,12}(?:问题反馈|反馈|bug|报错)\s*[:：\s]*(.*)$", value, flags=re.I | re.S)
    if match:
        return (match.group(1) or value).strip()
    return ""


def is_feedback_request_text(text):
    return bool(extract_feedback_text(text))


def summarize_outgoing_message(msg_type, content, fallback_summary=""):
    if fallback_summary:
        return fallback_summary
    if msg_type == "text":
        return str((content or {}).get("text") or "")[:2000]
    if msg_type == "image":
        return "[图片] 已发送图示"
    if msg_type != "interactive":
        return f"[{msg_type}]"
    if isinstance(content, dict) and content.get("type") == "card":
        return "[卡片消息] 已发送 CardKit 卡片"
    return summarize_card_for_feedback(content)


def summarize_card_for_feedback(card):
    if not isinstance(card, dict):
        return "[卡片消息]"
    title = ""
    header = card.get("header") or {}
    if isinstance(header, dict):
        title_obj = header.get("title") or {}
        if isinstance(title_obj, dict):
            title = str(title_obj.get("content") or title_obj.get("text") or "")
    fragments = []

    def collect(value):
        if len(fragments) >= 18:
            return
        if isinstance(value, dict):
            tag = value.get("tag")
            if tag in {"markdown", "plain_text", "lark_md"}:
                current = value.get("content") or value.get("text")
                if current:
                    fragments.append(str(current))
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(card.get("elements") or card.get("body") or card)
    body = "\n".join(fragment.strip() for fragment in fragments if fragment.strip())
    summary = "\n".join(part for part in [title, body] if part).strip()
    return summary[:2000] if summary else "[卡片消息]"


def remember_outgoing_feedback(chat_id, msg_type, content, response, feedback_summary=""):
    open_id = current_reply_open_id.get()
    active_chat_id = current_reply_chat_id.get() or chat_id
    if not open_id or not active_chat_id:
        return
    message_id = _response_message_id(response)
    remember_feedback_event(
        open_id,
        active_chat_id,
        "bot",
        summarize_outgoing_message(msg_type, content, feedback_summary),
        msg_type,
        message_id,
    )


def resolve_user_display_name(open_id, chat_id=""):
    open_id = str(open_id or "").strip()
    if not open_id:
        return ""
    if open_id in user_display_name_cache:
        return user_display_name_cache[open_id]

    display_name = _resolve_chat_member_display_name(open_id, chat_id)
    try:
        if not display_name:
            request = (
                GetUserRequest.builder()
                .user_id_type("open_id")
                .user_id(open_id)
                .build()
            )
            response = rest_client.contact.v3.user.get(request)
            if response.success():
                data = getattr(response, "data", None)
                user = getattr(data, "user", None)
                display_name = str(
                    getattr(user, "name", "")
                    or getattr(user, "nickname", "")
                    or getattr(user, "en_name", "")
                    or ""
                ).strip()
            else:
                print(_message_response_error("resolve user display name failed", response), flush=True)
    except Exception as exc:
        print(f"resolve user display name failed: open_id={open_id}, error={exc}", flush=True)

    if not display_name:
        display_name = open_id
    user_display_name_cache[open_id] = display_name
    return display_name


def _resolve_chat_member_display_name(open_id, chat_id):
    open_id = str(open_id or "").strip()
    chat_id = str(chat_id or "").strip()
    if not open_id or not chat_id:
        return ""
    page_token = ""
    for _ in range(5):
        try:
            builder = (
                GetChatMembersRequest.builder()
                .chat_id(chat_id)
                .member_id_type("open_id")
                .page_size(100)
            )
            if page_token:
                builder.page_token(page_token)
            response = rest_client.im.v1.chat_members.get(builder.build())
        except Exception as exc:
            print(f"resolve chat member name failed: chat_id={chat_id}, open_id={open_id}, error={exc}", flush=True)
            return ""
        if not response.success():
            print(_message_response_error("resolve chat member name failed", response), flush=True)
            return ""

        data = getattr(response, "data", None)
        for item in getattr(data, "items", None) or []:
            member_id = str(getattr(item, "member_id", "") or "")
            if member_id != open_id:
                continue
            return str(getattr(item, "name", "") or "").strip()

        has_more = bool(getattr(data, "has_more", False))
        page_token = str(getattr(data, "page_token", "") or "")
        if not has_more or not page_token:
            break
    return ""


def _cardkit_response_error(prefix, response):
    log_id = response.get_log_id() if hasattr(response, "get_log_id") else ""
    troubleshooter = ""
    if hasattr(response, "get_troubleshooter"):
        try:
            troubleshooter = response.get_troubleshooter()
        except Exception:
            troubleshooter = ""
    return (
        f"{prefix}: code={getattr(response, 'code', '')}, "
        f"msg={getattr(response, 'msg', '')}, log_id={log_id}, "
        f"troubleshooter={troubleshooter}"
    )


def _cardkit_v2_card(card):
    if not isinstance(card, dict):
        return card
    if card.get("schema") == "2.0" or "body" in card:
        return card

    config = {"summary": {"content": ""}}
    old_config = card.get("config") or {}
    if isinstance(old_config, dict):
        config.update({key: value for key, value in old_config.items() if key != "wide_screen_mode"})

    converted = {
        "schema": "2.0",
        "config": config,
        "body": {"elements": list(card.get("elements") or [])},
    }
    if isinstance(card.get("header"), dict):
        converted["header"] = card["header"]
        title = card["header"].get("title") or {}
        if isinstance(title, dict) and title.get("content"):
            converted["config"]["summary"] = {"content": str(title["content"])}
    return converted


def _cardkit_create_card(card):
    request_body = (
        CreateCardRequestBody.builder()
        .type("card_json")
        .data(json.dumps(_cardkit_v2_card(card), ensure_ascii=False))
        .build()
    )
    response = rest_client.cardkit.v1.card.create(
        CreateCardRequest.builder().request_body(request_body).build()
    )
    if not response.success():
        raise RuntimeError(_cardkit_response_error("Create CardKit card failed", response))
    card_id = str(getattr(response.data, "card_id", "") or "")
    if not card_id:
        raise RuntimeError("Create CardKit card failed: missing card_id")
    return card_id


def _cardkit_update_card(card_id, card, sequence):
    request_body = (
        UpdateCardRequestBody.builder()
        .card(
            Card.builder()
            .type("card_json")
            .data(json.dumps(_cardkit_v2_card(card), ensure_ascii=False))
            .build()
        )
        .sequence(int(sequence))
        .uuid(str(uuid.uuid4()))
        .build()
    )
    response = rest_client.cardkit.v1.card.update(
        UpdateCardRequest.builder()
        .card_id(str(card_id))
        .request_body(request_body)
        .build()
    )
    if not response.success():
        raise RuntimeError(_cardkit_response_error("Update CardKit card failed", response))
    return response


def _cardkit_id_convert(message_id):
    if not message_id:
        return ""
    request_body = IdConvertCardRequestBody.builder().message_id(message_id).build()
    response = rest_client.cardkit.v1.card.id_convert(
        IdConvertCardRequest.builder().request_body(request_body).build()
    )
    if not response.success():
        raise RuntimeError(_cardkit_response_error("Convert CardKit message failed", response))
    return str(getattr(response.data, "card_id", "") or "")


def _remember_sent_cardkit(response, card_id):
    message_id = _response_message_id(response)
    if message_id and card_id:
        sent_cardkit_card_ids[message_id] = str(card_id)
    return message_id


def send_cardkit_card(chat_id, card, reply_to_message_id=None):
    card_id = _cardkit_create_card(card)
    response = send_message(
        chat_id,
        "interactive",
        {"type": "card", "data": {"card_id": card_id}},
        reply_to_message_id=reply_to_message_id,
        feedback_summary=summarize_card_for_feedback(card),
    )
    _remember_sent_cardkit(response, card_id)
    return response


def _send_reply_message(reply_to_message_id, msg_type, content):
    request = (
        ReplyMessageRequest.builder()
        .message_id(str(reply_to_message_id))
        .request_body(
            ReplyMessageRequestBody.builder()
            .msg_type(msg_type)
            .content(json.dumps(content, ensure_ascii=False))
            .reply_in_thread(False)
            .uuid(str(uuid.uuid4()))
            .build()
        )
        .build()
    )

    last_error = None
    for attempt in range(3):
        try:
            response = rest_client.im.v1.message.reply(request)
            if not response.success():
                raise RuntimeError(_message_response_error("Reply message failed", response))
            return response
        except Exception as exc:
            last_error = exc
            print(
                f"reply_message failed attempt={attempt + 1}/3 message_id={reply_to_message_id} msg_type={msg_type} err={exc}",
                flush=True,
            )
            if attempt < 2:
                time.sleep(attempt + 1)
    raise last_error


def send_message(chat_id, msg_type, content, reply_to_message_id=None, feedback_summary=""):
    if reply_to_message_id is None:
        reply_to_message_id = current_reply_message_id.get()
    if reply_to_message_id:
        try:
            response = _send_reply_message(reply_to_message_id, msg_type, content)
            remember_outgoing_feedback(chat_id, msg_type, content, response, feedback_summary)
            return response
        except Exception as exc:
            print(
                f"reply_message fallback to create chat_id={chat_id} message_id={reply_to_message_id} msg_type={msg_type} err={exc}",
                flush=True,
            )

    request = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type(msg_type)
            .content(json.dumps(content, ensure_ascii=False))
            .uuid(str(uuid.uuid4()))
            .build()
        )
        .build()
    )

    last_error = None
    for attempt in range(3):
        try:
            response = rest_client.im.v1.message.create(request)
            if not response.success():
                raise RuntimeError(_message_response_error("Send message failed", response))
            remember_outgoing_feedback(chat_id, msg_type, content, response, feedback_summary)
            return response
        except Exception as exc:
            last_error = exc
            print(
                f"send_message failed attempt={attempt + 1}/3 chat_id={chat_id} msg_type={msg_type} err={exc}",
                flush=True,
            )
            if attempt < 2:
                time.sleep(attempt + 1)

    raise last_error


def send_text(chat_id, text, reply_to_message_id=None):
    return send_message(chat_id, "text", {"text": text}, reply_to_message_id=reply_to_message_id)


def send_card(chat_id, card, reply_to_message_id=None):
    try:
        return send_cardkit_card(chat_id, card, reply_to_message_id=reply_to_message_id)
    except Exception as exc:
        print(f"CardKit send fallback triggered: {exc}", flush=True)
        return send_message(chat_id, "interactive", card, reply_to_message_id=reply_to_message_id)


def send_card_with_fallback(chat_id, card, fallback_card, reply_to_message_id=None):
    try:
        return send_card(chat_id, card, reply_to_message_id=reply_to_message_id)
    except Exception as exc:
        print(f"send_card fallback triggered: {exc}", flush=True)
        return send_card(chat_id, fallback_card, reply_to_message_id=reply_to_message_id)


def send_image(chat_id, image_path, reply_to_message_id=None):
    image_key = upload_image_key(image_path)
    return send_message(
        chat_id,
        "image",
        {"image_key": image_key},
        reply_to_message_id=reply_to_message_id,
        feedback_summary=f"[图片] {Path(image_path).name}",
    )


def upload_image_key(image_path):
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"image file not found: {image_path}")
    if image_path.stat().st_size <= 0:
        raise ValueError(f"image file is empty: {image_path}")
    with image_path.open("rb") as image_file:
        request = (
            CreateImageRequest.builder()
            .request_body(
                CreateImageRequestBody.builder()
                .image_type("message")
                .image(image_file)
                .build()
            )
            .build()
        )
        response = rest_client.im.v1.image.create(request)

    if not response.success():
        raise RuntimeError(
            "Upload image failed: "
            f"code={response.code}, msg={response.msg}, log_id={response.get_log_id()}"
        )

    return response.data.image_key


def send_file(chat_id, file_path, reply_to_message_id=None):
    file_path = Path(file_path)
    file_key = upload_file_key(file_path)
    return send_message(
        chat_id,
        "file",
        {"file_key": file_key},
        reply_to_message_id=reply_to_message_id,
        feedback_summary=f"[文件] {file_path.name}",
    )


def zip_file_for_transfer(file_path: str | Path) -> Path:
    file_path = Path(file_path)
    delivery_dir = ensure_writable_dir(get_runtime_subdir("file_delivery"))
    safe_name = safe_runtime_name(file_path.name, "file")
    zip_name = f"{safe_name}.zip"
    zip_path = ensure_writable_file_path(delivery_dir / zip_name, zip_name)
    if zip_path.exists():
        if zip_path.is_file():
            zip_path.unlink()
        else:
            zip_path = ensure_writable_file_path(
                delivery_dir / f"{safe_runtime_name(file_path.stem, 'file')}_{uuid.uuid4().hex[:8]}.zip",
                "file.zip",
            )
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(file_path, arcname=safe_name)
    if not zip_path.exists() or zip_path.stat().st_size <= 0:
        raise RuntimeError(f"压缩回传文件失败：{zip_path}")
    return zip_path


def send_file_with_zip_fallback(chat_id, file_path, reply_to_message_id=None) -> tuple[Path, bool]:
    file_path = Path(file_path)
    direct_error = None
    if file_path.stat().st_size <= BLF_MDF_DIRECT_UPLOAD_MAX_BYTES:
        try:
            send_file(chat_id, file_path, reply_to_message_id=reply_to_message_id)
            return file_path, False
        except Exception as exc:
            direct_error = exc

    zip_path = zip_file_for_transfer(file_path)
    try:
        send_file(chat_id, zip_path, reply_to_message_id=reply_to_message_id)
        return zip_path, True
    except Exception as zip_exc:
        details = []
        if direct_error is not None:
            details.append(f"直接回传失败：{format_exception_for_card(direct_error)}")
        details.append(
            f"zip 回传失败：{format_exception_for_card(zip_exc)}；"
            f"未压缩 MDF 已生成，但没有成功回传到飞书"
        )
        raise RuntimeError("；".join(details)) from zip_exc


def upload_file_key(file_path):
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"file not found: {file_path}")
    if file_path.stat().st_size <= 0:
        raise ValueError(f"file is empty: {file_path}")
    with file_path.open("rb") as file_obj:
        request = (
            CreateFileRequest.builder()
            .request_body(
                CreateFileRequestBody.builder()
                .file_type("stream")
                .file_name(file_path.name)
                .file(file_obj)
                .build()
            )
            .build()
        )
        response = rest_client.im.v1.file.create(request)

    if not response.success():
        raise RuntimeError(
            "Upload file failed: "
            f"code={response.code}, msg={response.msg}, log_id={response.get_log_id()}"
        )

    return response.data.file_key


def base_card(title, template="blue", elements=None):
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements or [],
    }


def markdown_card(title, content, template="blue"):
    return base_card(
        title,
        template,
        feishu_markdown_elements(content),
    )


def clean_plain_reply(text):
    value = str(text or "")
    value = re.sub(r"\*\*(.*?)\*\*", r"\1", value)
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = value.replace("\\\"", "\"")
    return value.strip()


def _is_markdown_table_separator(line):
    cells = parse_markdown_table_row(line)
    if len(cells) < 2:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells if cell)


def parse_markdown_table_row(line):
    value = str(line or "").strip()
    if "|" not in value:
        return []
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [clean_plain_reply(cell.strip()).replace("|", "｜") for cell in value.split("|")]


def _looks_like_markdown_table(lines, index):
    if index + 1 >= len(lines):
        return False
    return "|" in lines[index] and _is_markdown_table_separator(lines[index + 1])


def normalize_feishu_markdown_text(text):
    lines = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if heading:
            title = clean_plain_reply(heading.group(1))
            lines.append(f"<font color='blue'>**{title}**</font>")
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _append_markdown_blocks(elements, buffered_lines):
    text = "\n".join(buffered_lines).strip()
    if not text:
        return
    for section in [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]:
        normalized = normalize_feishu_markdown_text(section)
        if normalized:
            elements.append(_card_markdown(normalized))


def _append_markdown_table(elements, header, rows):
    if not header or not rows:
        return
    columns = [
        {
            "name": f"col_{index}",
            "display_name": str(name or f"列{index + 1}")[:40],
            "data_type": "text",
        }
        for index, name in enumerate(header)
    ]
    table_rows = []
    for row in rows[:50]:
        table_rows.append(
            {
                f"col_{index}": str(row[index]).replace("\n", " ").strip() if index < len(row) else ""
                for index in range(len(columns))
            }
        )
    elements.append(_card_markdown("**📋 表格内容**\n可左右滑动查看完整列。"))
    elements.append(_card_table(columns, table_rows, page_size=min(max(len(table_rows), 1), 8)))


def feishu_markdown_elements(content):
    value = clean_plain_reply(content)
    if not value:
        return []

    lines = value.splitlines()
    elements = []
    buffer = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if re.fullmatch(r"\s*-{3,}\s*", line):
            _append_markdown_blocks(elements, buffer)
            buffer = []
            if elements and elements[-1].get("tag") != "hr":
                elements.append(_card_hr())
            index += 1
            continue

        if _looks_like_markdown_table(lines, index):
            _append_markdown_blocks(elements, buffer)
            buffer = []
            header = parse_markdown_table_row(lines[index])
            index += 2
            rows = []
            while index < len(lines) and "|" in lines[index].strip():
                if not _is_markdown_table_separator(lines[index]):
                    rows.append(parse_markdown_table_row(lines[index]))
                index += 1
            _append_markdown_table(elements, header, rows)
            continue

        buffer.append(line)
        index += 1

    _append_markdown_blocks(elements, buffer)
    return elements or [_card_markdown(value)]


def assistant_reply_card(open_id, reply_text, title="布鲁", template="blue"):
    elements = [_card_markdown(f"**👤 处理对象**：<at id={open_id}></at>")]
    body_elements = feishu_markdown_elements(reply_text)
    if body_elements:
        elements.append(_card_hr())
    for element in body_elements:
        elements.append(element)
    return base_card(title, template, elements)


def assistant_reply_with_artifacts_card(
    open_id,
    reply_text,
    title="布鲁",
    template="blue",
    transform_plan=None,
    generated_artifacts: list[GeneratedArtifact] | None = None,
    uploaded_artifacts=None,
):
    generated_artifacts = generated_artifacts or []
    uploaded_artifacts = uploaded_artifacts or []
    elements = [_card_markdown(f"**👤 处理对象**：<at id={open_id}></at>")]
    body_elements = feishu_markdown_elements(reply_text)
    if body_elements:
        elements.append(_card_hr())
        elements.extend(body_elements)

    if transform_plan:
        summary = str(transform_plan.get("summary") or "").strip()
        sections = transform_plan.get("card_sections") or []
        if summary or sections:
            elements.append(_card_hr())
            if summary:
                elements.append(_card_markdown(f"**🧠 整理说明**\n{summary}"))
            for section in sections:
                section_title = str(section.get("title") or "").strip()
                bullets = [str(item).strip() for item in section.get("bullets") or [] if str(item).strip()]
                if section_title:
                    elements.append(_card_markdown(f"**{section_title}**"))
                for bullet in bullets[:8]:
                    elements.append(_card_markdown(f"- {bullet}"))

    text_artifacts = [artifact for artifact in generated_artifacts if artifact.card_text and not artifact.is_uploadable_image]
    if text_artifacts:
        elements.append(_card_hr())
        elements.append(_card_markdown("**📎 生成内容**"))
        for artifact in text_artifacts:
            elements.append(_card_markdown(f"**{artifact.title}**"))
            parsed_elements = feishu_markdown_elements(artifact.card_text)
            if parsed_elements:
                elements.extend(parsed_elements)
            else:
                elements.append(_card_markdown(artifact.card_text))

    if uploaded_artifacts:
        elements.append(_card_hr())
        elements.append(_card_markdown("**🖼️ 相关图示**"))
        for index, item in enumerate(uploaded_artifacts, start=1):
            artifact_title = item.get("title") or f"图示 {index}"
            description = item.get("description") or ""
            caption = f"**图示 {index}：{artifact_title}**"
            if description:
                caption += f"\n{description}"
            elements.append(_card_markdown(caption))
            elements.append(_card_image(item["image_key"], artifact_title))

    return base_card(title, template, elements)


def upload_transform_artifacts(generated_artifacts: list[GeneratedArtifact]):
    uploaded = []
    for artifact in generated_artifacts:
        if not artifact.is_uploadable_image:
            continue
        try:
            uploaded.append(
                {
                    "title": artifact.title,
                    "description": artifact.description,
                    "image_key": upload_image_key(artifact.path),
                }
            )
        except Exception as exc:
            print(f"上传结果展示产物失败 {artifact.path} | {exc}", flush=True)
            artifact.card_text = f"图示生成完成，但上传飞书失败：{exc}"
    return uploaded


def prepare_result_transform_artifacts(chat_id, open_id, session, user_text, source_skill, local_context, base_reply):
    plan = build_result_transform_plan(user_text, source_skill, local_context, base_reply)
    if not plan.get("should_transform"):
        return None, [], []

    update_progress_card(
        chat_id,
        open_id,
        session,
        stage="整理结果中",
        template="blue",
        detail="正在根据你的展示要求整理结果。",
    )

    generated_artifacts = render_transform_artifacts(plan, get_transform_runtime_dir())
    uploaded_artifacts = upload_transform_artifacts(generated_artifacts)
    return plan, generated_artifacts, uploaded_artifacts


def session_message_count(session):
    try:
        return int(session.get("message_count") or 0)
    except (TypeError, ValueError):
        return 0


def bump_session_message_count(session, chat_id=None, open_id=None):
    if chat_id:
        session["chat_id"] = chat_id
    if open_id:
        session["open_id"] = open_id
    next_count = session_message_count(session) + 1
    session["message_count"] = next_count
    return next_count


def clear_last_skill_result(session):
    session.pop("last_skill_result", None)


def remember_skill_result(session, source_skill, user_text, local_context, reply_text, title, template="blue"):
    now_ms = int(time.time() * 1000)
    open_id = session.get("open_id") or ""
    chat_id = session.get("chat_id") or ""
    message_count = session_message_count(session)
    session["last_skill_result"] = {
        "open_id": open_id,
        "chat_id": chat_id,
        "session_key": state_key(open_id, chat_id) if open_id and chat_id else "",
        "source_skill": source_skill,
        "user_text": str(user_text or "")[:2000],
        "local_context": str(local_context or "")[:16000],
        "reply_text": str(reply_text or "")[:8000],
        "title": title,
        "template": template,
        "created_at_ms": now_ms,
        "expires_at_ms": now_ms + LAST_SKILL_RESULT_TTL_SECONDS * 1000,
        "message_count_at_created": message_count,
    }


def remember_direct_reply_result(session, source_skill, user_text, reply_text, title="布鲁", template="blue"):
    reply = str(reply_text or "").strip()
    if len(reply) < 40:
        return
    remember_skill_result(
        session,
        source_skill or SKILL_SMALLTALK_CHAT,
        user_text,
        reply,
        reply,
        title,
        template,
    )


def prune_last_skill_result(session, now_ms=None):
    last_result = session.get("last_skill_result")
    if not last_result:
        return False
    now_ms = now_ms or int(time.time() * 1000)
    owner_open_id = last_result.get("open_id")
    owner_chat_id = last_result.get("chat_id")
    current_open_id = session.get("open_id")
    current_chat_id = session.get("chat_id")
    if (owner_open_id and current_open_id and owner_open_id != current_open_id) or (
        owner_chat_id and current_chat_id and owner_chat_id != current_chat_id
    ):
        session.pop("last_skill_result", None)
        return True
    expires_at_ms = int(last_result.get("expires_at_ms") or 0)
    created_at_ms = int(last_result.get("created_at_ms") or 0)
    if not expires_at_ms and created_at_ms:
        expires_at_ms = created_at_ms + LAST_SKILL_RESULT_TTL_SECONDS * 1000
    if not expires_at_ms or expires_at_ms <= now_ms:
        session.pop("last_skill_result", None)
        return True
    created_message_count = last_result.get("message_count_at_created")
    try:
        created_message_count = int(created_message_count)
    except (TypeError, ValueError):
        created_message_count = None
    if created_message_count is not None:
        current_message_count = session_message_count(session)
        if current_message_count - created_message_count > LAST_SKILL_RESULT_MAX_FOLLOWUP_MESSAGES:
            session.pop("last_skill_result", None)
            return True
    return False


def recent_skill_result(session):
    prune_last_skill_result(session)
    return session.get("last_skill_result") or {}


def remember_conversation_turn(session, role, text, tool="", action=""):
    turns = session.get("recent_turns")
    if not isinstance(turns, list):
        turns = []
    turns.append(
        {
            "role": str(role or "")[:20],
            "text": str(text or "")[:2000],
            "tool": str(tool or "")[:80],
            "action": str(action or "")[:40],
            "created_at_ms": int(time.time() * 1000),
        }
    )
    session["recent_turns"] = turns[-RECENT_CONTEXT_MAX_TURNS:]


def is_contextual_followup_request(text):
    value = (text or "").strip().lower()
    if not value:
        return False
    reference_terms = [
        "上面",
        "刚才",
        "上一条",
        "前面",
        "上述",
        "这个",
        "这些",
        "这几个",
        "这几项",
        "它",
        "它们",
        "那",
        "那些",
        "该流程",
        "该状态",
        "该问题",
        "这136项",
        "136项",
        "继续",
    ]
    followup_terms = [
        "总结",
        "概括",
        "归纳",
        "分类",
        "大致",
        "展开",
        "更具体",
        "为什么",
        "原因",
        "依据",
        "根据什么",
        "画成",
        "流程图",
        "图示",
        "表格",
        "整理成",
        "生成",
    ]
    if any(term in value for term in reference_terms):
        return True
    return len(value) <= 40 and any(term in value for term in followup_terms)


def contextualize_skill_query(session, source_skill, query):
    query = str(query or "").strip()
    last_result = recent_skill_result(session)
    if not query or not last_result or not is_contextual_followup_request(query):
        return query
    previous_question = str(last_result.get("user_text") or "").strip()
    previous_reply = str(last_result.get("reply_text") or "").strip()
    previous_context = str(last_result.get("local_context") or "").strip()
    chunks = [
        f"上一轮能力：{last_result.get('source_skill') or source_skill or ''}",
        f"上一轮问题：{previous_question}",
    ]
    if previous_reply:
        chunks.append(f"上一轮回复摘要：{previous_reply[:3000]}")
    if previous_context:
        chunks.append(f"上一轮本地结果：{previous_context[:5000]}")
    chunks.append(f"用户追问：{query}")
    return "\n".join(chunk for chunk in chunks if chunk.strip())


def should_remember_fault_reference_context(query, local_context=""):
    value = f"{query or ''}\n{local_context or ''}".lower()
    tokens = [
        "fda3",
        "fda7",
        "应用层故障",
        "应用层",
        "底软故障",
        "底软",
        "环视",
        "相机",
        "鱼眼",
        "前视",
        "后视",
        "左视",
        "右视",
        "camera",
        "svs",
        "apa",
        "rpa",
        "acc",
        "故障表",
        "故障码",
    ]
    return any(token.lower() in value for token in tokens)


def cleanup_expired_last_skill_results(force=False):
    global last_skill_result_sweep_ms
    now_ms = int(time.time() * 1000)
    if not force and now_ms - last_skill_result_sweep_ms < LAST_SKILL_RESULT_SWEEP_INTERVAL_SECONDS * 1000:
        return 0
    last_skill_result_sweep_ms = now_ms
    removed = 0
    for session in list(sessions.values()):
        if prune_last_skill_result(session, now_ms):
            removed += 1
    if removed:
        print(f"已清理过期上一轮结果缓存: {removed}", flush=True)
    return removed


def is_result_transform_followup(text):
    value = (text or "").strip().lower()
    if not value:
        return False
    reference_terms = ["上面", "刚才", "上一条", "前面", "上述", "这个", "这段", "这些", "它"]
    transform_terms = [
        "画成",
        "画一个",
        "画一下",
        "画图",
        "图片",
        "图片形式",
        "流程图",
        "状态图",
        "结构图",
        "架构图",
        "图示",
        "用图",
        "可视化",
        "表格",
        "报告",
        "生成文件",
        "导出文件",
        "导出成",
        "gif",
        "动图",
        "时序图",
        "整理成",
    ]
    if any(term in value for term in reference_terms) and any(term in value for term in transform_terms):
        return True
    transform_actions = ["生成", "画", "做成", "转成", "形式", "输出", "可视化", "整理成", "导出"]
    return (
        len(value) <= 40
        and any(term in value for term in transform_terms)
        and any(term in value for term in transform_actions)
    )


def handle_result_transform_followup(chat_id, open_id, session, text):
    last_result = recent_skill_result(session)
    if not last_result or not is_result_transform_followup(text):
        return False

    session["active_skill"] = SKILL_RESULT_TRANSFORMER
    session["request_text"] = text
    update_progress_card(
        chat_id,
        open_id,
        session,
        stage="整理结果中",
        template="blue",
        detail="正在基于上一轮结果整理你要的展示形式。",
    )
    transform_request = (
        f"用户当前要求：{text}\n"
        f"上一轮问题：{last_result.get('user_text') or ''}\n"
        "请只基于上一轮结果生成展示内容。"
    )
    transform_plan, generated_artifacts, uploaded_artifacts = prepare_result_transform_artifacts(
        chat_id,
        open_id,
        session,
        transform_request,
        last_result.get("source_skill") or "",
        last_result.get("local_context") or "",
        last_result.get("reply_text") or "",
    )
    if not transform_plan:
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="回传结果中",
            template="orange",
            detail="上一轮结果不足以直接生成图示，正在给出说明。",
        )
        send_or_update_result_card(
            chat_id,
            session,
            assistant_reply_card(
                open_id,
                "我知道你想基于上一轮内容做展示，但上一轮结果里还缺少能直接成图的结构信息。你可以直接说要画哪几个状态/条件，我会继续整理。",
                last_result.get("title") or "布鲁",
                "orange",
            ),
        )
        return True

    update_progress_card(chat_id, open_id, session, stage="回传结果中", template="blue", detail="已生成展示内容，正在回传。")
    try:
        send_or_update_result_card(
            chat_id,
            session,
            assistant_reply_with_artifacts_card(
                open_id,
                last_result.get("reply_text") or "",
                last_result.get("title") or "布鲁",
                last_result.get("template") or "blue",
                transform_plan,
                generated_artifacts,
                uploaded_artifacts,
            ),
        )
    finally:
        cleanup_transform_artifacts(generated_artifacts)
    return True


def cleanup_transform_artifacts(generated_artifacts: list[GeneratedArtifact]) -> None:
    roots = set()
    runtime_root = get_transform_runtime_dir().resolve()
    for artifact in generated_artifacts or []:
        if not artifact.path:
            continue
        try:
            parent = artifact.path.resolve().parent
        except Exception:
            continue
        try:
            parent.relative_to(runtime_root)
        except ValueError:
            continue
        roots.add(parent)
    for root in roots:
        shutil.rmtree(root, ignore_errors=True)


def send_assistant_reply(chat_id, open_id, reply_text, title="布鲁", template="blue"):
    return send_card(chat_id, assistant_reply_card(open_id, reply_text, title, template))


def _card_markdown(content):
    return {"tag": "markdown", "content": content}


def _card_hr():
    return {"tag": "hr"}


def _card_column_row(cells, weights=None, header=False):
    weights = weights or [1] * len(cells)
    columns = []
    for cell, weight in zip(cells, weights):
        content = str(cell or "").replace("|", "｜")
        if header:
            content = f"**{content}**"
        columns.append(
            {
                "tag": "column",
                "width": "weighted",
                "weight": weight,
                "elements": [_card_markdown(content)],
            }
        )
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "default",
        "columns": columns,
    }


_TABLE_COLUMN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_TABLE_RESERVED_COLUMN_NAMES = {"name"}


def _safe_table_column_name(raw_name, index, used_names):
    base = str(raw_name or "").strip()
    if not _TABLE_COLUMN_NAME_RE.fullmatch(base) or base.lower() in _TABLE_RESERVED_COLUMN_NAMES:
        base = f"col_{index}"
    candidate = base
    suffix = 1
    while candidate in used_names:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used_names.add(candidate)
    return candidate


def _card_table(columns, rows, page_size=6):
    normalized_columns = []
    name_map = {}
    used_names = set()
    for index, column in enumerate(columns or []):
        if isinstance(column, dict):
            original_name = str(column.get("name") or f"col_{index}")
            normalized = dict(column)
        else:
            original_name = f"col_{index}"
            normalized = {"display_name": str(column or original_name)}
        safe_name = _safe_table_column_name(original_name, index, used_names)
        name_map[original_name] = safe_name
        normalized["name"] = safe_name
        normalized.setdefault("display_name", original_name)
        normalized.setdefault("data_type", "text")
        normalized_columns.append(normalized)

    normalized_rows = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        normalized_row = {}
        for original_name, safe_name in name_map.items():
            normalized_row[safe_name] = row.get(original_name, "")
        for column in normalized_columns:
            normalized_row.setdefault(column["name"], "")
        normalized_rows.append(normalized_row)

    return {
        "tag": "table",
        "columns": normalized_columns,
        "rows": normalized_rows,
        "page_size": page_size,
    }


def _card_image(image_key, alt_text):
    return {
        "tag": "img",
        "img_key": image_key,
        "alt": {"tag": "plain_text", "content": alt_text or "图示"},
        "mode": "fit_horizontal",
    }


def progress_status_card(
    open_id,
    *,
    title,
    stage,
    template="blue",
    task_type="",
    file_name="",
    database="",
    request_text="",
    detail="",
):
    facts = [
        f"**处理对象：** <at id={open_id}></at>",
        f"**当前阶段：** <font color='blue'>**{stage}**</font>",
    ]
    if task_type:
        facts.append(f"**工作类型：** {task_type}")
    if database:
        facts.append(f"**数据库：** `{database}`")
    if file_name:
        facts.append(f"**文件：** `{file_name}`")
    if request_text:
        facts.append(f"**用户需求：** {request_text}")
    if task_type == "批量行车问题原因分析":
        if stage == "取消中":
            facts.append("**取消状态：** 已收到取消指令，正在停止当前进程。")
        elif stage not in {"已取消", "已完成", "完成", "回传结果中", "分析失败", "诊断失败"}:
            facts.append(f"**取消方式：** {BATCH_ROOT_CAUSE_CANCEL_HINT}")

    steps = [
        ("接收请求", stage not in {"等待上传文件"}),
        ("获取文件", stage not in {"等待上传文件", "已收到文件", "已收到输入"}),
        ("执行分析", stage in {"正在分析", "生成图示", "已完成", "诊断失败", "分析失败"}),
        ("整理结果", stage in {"生成图示", "已完成"}),
    ]
    stage_rank_map = {
        "分析需求中": 0,
        "等待上传文件": 0,
        "等待故障诊断输入": 0,
        "已收到文件": 1,
        "已收到输入": 1,
        "查询中": 1,
        "下载文件中": 1,
        "正在分析": 2,
        "问题分析中": 2,
        "生成图示": 3,
        "回传结果中": 3,
        "整理结果中": 3,
        "取消中": 2,
        "已取消": 4,
        "已完成": 4,
        "完成": 4,
        "诊断失败": 4,
        "分析失败": 4,
    }
    rank = stage_rank_map.get(stage, 0)
    steps = [
        ("分析需求", rank >= 0),
        ("查询/取数", rank >= 1),
        ("问题分析", rank >= 2),
        ("回传结果", rank >= 3),
        ("完成", rank >= 4),
    ]
    step_text = "\n".join(f"{'✅' if done else '▫️'} {name}" for name, done in steps)
    elements = [
        {"tag": "markdown", "content": "\n".join(facts)},
        {"tag": "hr"},
        {"tag": "markdown", "content": f"**处理进度**\n{step_text}"},
    ]
    if detail:
        elements.extend(
            [
                {"tag": "hr"},
                {"tag": "markdown", "content": f"**当前动作**\n{detail}"},
            ]
        )

    return {
        "schema": "2.0",
        "config": {"summary": {"content": title}},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {"elements": elements},
    }


def _session_progress_title(session):
    task_type = session.get("task_type") or ""
    if task_type == COMBINED_ANALYSIS_TASK:
        return "行车问题原因分析"
    if task_type == BATCH_ROOT_CAUSE_ANALYSIS_TASK:
        return "批量行车问题原因分析"
    if task_type == KPI_REPORT_ANALYSIS_TASK:
        return "KPI 报告 AI 分析"
    if task_type == FUNCTION_EXIT_ANALYSIS_TASK:
        return "ACC/LCC功能退出原因分析"
    if task_type == BLF_TO_MDF_CONVERSION_TASK:
        return "BLF 转 MDF"
    if task_type == AXLE_TORQUE_ACCURACY_TASK:
        return "轴端扭矩准确性校验"
    if task_type == "performance_analysis":
        return "性能问题分析"
    if task_type == "actuator_handshake":
        return "执行器握手诊断"
    if task_type == "fault_degradation":
        return "故障降级诊断"
    return "布鲁"


def reset_progress_card_state(session):
    session.pop("progress_message_id", None)
    session.pop("progress_card_id", None)
    session.pop("progress_card_sequence", None)


def start_progress_card(chat_id, open_id, session, *, stage, template="blue", detail="", force_new=False):
    if force_new:
        reset_progress_card_state(session)
    if session.get("progress_card_id") or session.get("progress_message_id"):
        update_progress_card(chat_id, open_id, session, stage=stage, template=template, detail=detail)
        return None

    card = progress_status_card(
        open_id,
        title=_session_progress_title(session),
        stage=stage,
        template=template,
        task_type=task_label(session.get("task_type")),
        file_name=session.get("file_name") or "",
        database=session.get("database") or "",
        request_text=session.get("request_text") or "",
        detail=detail,
    )
    try:
        response = send_card(chat_id, card)
    except Exception as exc:
        print(f"CardKit progress start failed: {exc}", flush=True)
        session["progress_message_id"] = ""
        session["progress_card_id"] = ""
        session["progress_card_sequence"] = 0
        return None
    message_id = _response_message_id(response)
    card_id = sent_cardkit_card_ids.get(message_id, "")
    if message_id and not card_id:
        try:
            card_id = _cardkit_id_convert(message_id)
        except Exception as exc:
            print(f"CardKit id_convert failed: {exc}", flush=True)
    session["progress_message_id"] = message_id
    session["progress_card_id"] = card_id
    session["progress_card_sequence"] = 0
    return response


def update_progress_card(chat_id, open_id, session, *, stage, template="blue", detail=""):
    card_id = session.get("progress_card_id")
    if not card_id and session.get("progress_message_id"):
        try:
            card_id = _cardkit_id_convert(session["progress_message_id"])
            session["progress_card_id"] = card_id
        except Exception as exc:
            print(f"CardKit id_convert failed: {exc}", flush=True)
    if not card_id:
        return False

    card = progress_status_card(
        open_id,
        title=_session_progress_title(session),
        stage=stage,
        template=template,
        task_type=task_label(session.get("task_type")),
        file_name=session.get("file_name") or "",
        database=session.get("database") or "",
        request_text=session.get("request_text") or "",
        detail=detail,
    )
    sequence = int(session.get("progress_card_sequence") or 0) + 1
    try:
        _cardkit_update_card(card_id, card, sequence)
    except Exception as exc:
        print(f"CardKit progress update failed: {exc}", flush=True)
        return False
    session["progress_card_sequence"] = sequence
    return True


def make_download_progress_callback(chat_id, open_id, session, *, template="blue"):
    last_update = {"time": 0.0, "stage": "", "detail": ""}

    def callback(stage, detail):
        now = time.time()
        if (
            stage == last_update["stage"]
            and detail == last_update["detail"]
            and now - last_update["time"] < 10
        ):
            return
        last_update["time"] = now
        last_update["stage"] = stage
        last_update["detail"] = detail
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage=stage,
            template=template,
            detail=detail,
        )

    return callback


def make_progress_callback(chat_id, open_id, session, *, template="blue"):
    last_update = {"time": 0.0, "stage": "", "detail": ""}

    def callback(stage, detail=""):
        if session.get("batch_cancel_requested"):
            return
        stage_text = str(stage or "处理中")
        detail_text = str(detail or "")
        now = time.time()
        if (
            stage_text == last_update["stage"]
            and detail_text == last_update["detail"]
            and now - last_update["time"] < 3
        ):
            return
        last_update["time"] = now
        last_update["stage"] = stage_text
        last_update["detail"] = detail_text
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage=stage_text,
            template=template,
            detail=detail_text,
        )

    return callback


def make_axle_torque_analysis_progress_callback(chat_id, open_id, session, *, template="blue"):
    base_callback = make_progress_callback(chat_id, open_id, session, template=template)

    def count_text(value):
        try:
            return f"{int(value):,}"
        except Exception:
            return "0"

    def percent_text(value):
        try:
            if value is None:
                return ""
            return f"{float(value):.1f}%"
        except Exception:
            return ""

    def callback(payload):
        if not isinstance(payload, dict):
            return
        stage = str(payload.get("stage") or "")
        percent = percent_text(payload.get("percent"))
        total = count_text(payload.get("total_frames"))
        candidate = count_text(payload.get("candidate_frames"))
        matched = count_text(payload.get("matched_frames"))

        if stage == "scan":
            detail = f"扫描 BLF 路由：{percent}｜已读 {total} 帧" if percent else f"扫描 BLF 路由：已读 {total} 帧"
            base_callback("正在扫描 BLF", detail)
            return
        if stage == "convert":
            detail = (
                f"解析目标报文：{percent}｜读 {total} 帧｜候选 {candidate}｜匹配 {matched}"
                if percent
                else f"解析目标报文：读 {total} 帧｜候选 {candidate}｜匹配 {matched}"
            )
            base_callback("正在解析 BLF", detail)
            return
        if stage == "write":
            base_callback("正在整理目标信号", f"目标报文匹配 {matched} 帧，正在整理轴扭复算信号。")
            return
        if stage == "extract":
            base_callback("正在读取信号", "目标报文已解码，正在读取轴扭复算所需信号。")
            return
        if stage == "compute":
            base_callback("正在复算轴扭", "正在按当前 ECM 数据库复算轴端扭矩并对齐实测值。")
            return
        if stage == "plot":
            base_callback("正在生成图示", "结论已计算，正在生成轴端扭矩对比图。")
            return
        if stage == "done":
            base_callback("分析完成", "轴端扭矩校验已完成，正在组织结果。")

    return callback


def send_or_update_result_card(chat_id, session, card, fallback_card=None):
    card_id = session.get("progress_card_id")
    if card_id:
        sequence = int(session.get("progress_card_sequence") or 0) + 1
        try:
            _cardkit_update_card(card_id, card, sequence)
            session["progress_card_sequence"] = sequence
            return None
        except Exception as exc:
            print(f"CardKit final update failed: {exc}", flush=True)
    if fallback_card is not None:
        return send_card_with_fallback(chat_id, card, fallback_card)
    return send_card(chat_id, card)


def actuator_result_card(open_id, result, request_text="", template="blue", image_items=None):
    image_items = image_items or []
    issues = actuator_issue_rows(result)
    table_columns = [
        {"name": "idx", "display_name": "#", "data_type": "text"},
        {"name": "issue", "display_name": "问题", "data_type": "text"},
        {"name": "time", "display_name": "时间窗", "data_type": "text"},
        {"name": "signal", "display_name": "未响应信号", "data_type": "text"},
        {"name": "actual", "display_name": "实际值", "data_type": "text"},
        {"name": "expected", "display_name": "期望响应", "data_type": "text"},
    ]
    table_rows = [
        {
            "idx": item["index"],
            "issue": item["issue"],
            "time": item["time_window"],
            "signal": item["failed_signals"],
            "actual": item["actual"],
            "expected": item["expected"],
        }
        for item in issues
    ]

    elements = [
        _card_markdown(
            "\n".join(
                [
                    f"**👤 处理对象**：<at id={open_id}></at>",
                    "**🧩 工作类型**：执行器握手诊断",
                    f"**📎 上传文件**：`{result.file_name}`",
                    f"**🎯 用户需求**：{request_text}" if request_text else "",
                ]
            ).strip()
        ),
        _card_hr(),
        _card_markdown(actuator_result_headline(result)),
        _card_hr(),
        _card_markdown("**📌 诊断核心结论**"),
        _card_column_row(["内容", "状态"], [2, 5], header=True),
    ]

    for label, value in actuator_result_summary_rows(result):
        elements.append(_card_column_row([label, value], [2, 5]))

    if issues:
        elements.extend(
            [
                _card_hr(),
                _card_markdown("**📊 异常明细表**\n可左右滑动查看完整列。"),
                _card_table(table_columns, table_rows),
            ]
        )

    if result.timeline_lines:
        elements.extend([_card_hr(), _card_markdown("**🕒 关键状态时序**")])
        for line in result.timeline_lines[:12]:
            elements.append(_card_markdown(f"- {line}"))

    if image_items:
        elements.extend(
            [
                _card_hr(),
                _card_markdown("**🖼️ 相关信号图示**\n红色阴影表示识别到的问题时间窗。"),
            ]
        )
        for index, item in enumerate(image_items, start=1):
            title = item.get("title") or f"图示 {index}"
            elements.append(_card_markdown(f"**图示 {index}：{title}**"))
            elements.append(_card_image(item["image_key"], title))

    return base_card("执行器握手诊断结果", template, elements)


def actuator_result_fallback_card(open_id, result, request_text="", template="blue", image_items=None):
    image_items = image_items or []
    lines = [
        f"**👤 处理对象**：<at id={open_id}></at>",
        "**🧩 工作类型**：执行器握手诊断",
        f"**📎 上传文件**：`{result.file_name}`",
    ]
    if request_text:
        lines.append(f"**🎯 用户需求**：{request_text}")
    lines.extend(["", actuator_result_headline(result), "", "**📌 诊断核心结论**"])
    for label, value in actuator_result_summary_rows(result):
        lines.append(f"- **{label}**：{value}")
    issues = actuator_issue_rows(result)
    if issues:
        lines.extend(["", "**📊 异常明细**"])
        for item in issues:
            lines.extend(
                [
                    f"{item['index']}. <font color='red'>**{item['issue']}**</font>",
                    f"   - 时间窗：{item['time_window']}",
                    f"   - 未响应信号：{item['failed_signals']}",
                    f"   - 实际值：{item['actual']}",
                    f"   - 期望响应：{item['expected']}",
                ]
            )
    if result.timeline_lines:
        lines.extend(["", "**🕒 关键状态时序**"])
        for line in result.timeline_lines[:12]:
            lines.append(f"- {line}")
    elements = [{"tag": "markdown", "content": "\n".join(lines)}]
    if image_items:
        elements.append(_card_hr())
        elements.append(_card_markdown("**🖼️ 相关信号图示**\n红色阴影表示识别到的问题时间窗。"))
        for index, item in enumerate(image_items, start=1):
            title = item.get("title") or f"图示 {index}"
            elements.append(_card_markdown(f"**图示 {index}：{title}**"))
            elements.append(_card_image(item["image_key"], title))
    return base_card("执行器握手诊断结果", template, elements)


def _upload_card_images(image_items):
    uploaded = []
    for title, image_path in image_items:
        try:
            uploaded.append({"title": title, "image_key": upload_image_key(image_path)})
        except Exception as image_exc:
            print(f"上传图示失败 {image_path} | {image_exc}", flush=True)
    return uploaded


def performance_result_card(open_id, task_name, database, file_name, result, image_items=None, template="blue"):
    image_items = image_items or []
    problems = sorted(
        result.problems,
        key=lambda issue: (
            int(float(issue.statistics.get("Bad Case", 0) or 0)),
            float(issue.statistics.get("Severity Score", 0.0) or 0.0),
            len(issue.events),
        ),
        reverse=True,
    )
    problem_count = len(result.problems)
    event_count = sum(len(issue.events) for issue in result.problems)
    image_count = sum(len(issue.image_paths) for issue in result.problems)
    display_summary = result.polished_summary or result.summary
    if result.status != "ok":
        headline = f"<font color='red'>**⚠️ 分析未完成：{display_summary}**</font>"
    elif problem_count:
        headline = f"<font color='red'>**⚠️ 发现 {problem_count} 类问题，共 {event_count} 个问题时间点**</font>"
    else:
        headline = "<font color='green'>**✅ 未发现当前规则覆盖范围内的性能问题**</font>"

    elements = [
        _card_markdown(
            "\n".join(
                [
                    f"**👤 处理对象**：<at id={open_id}></at>",
                    f"**🧩 工作类型**：{task_name}",
                    f"**🗃️ 数据库**：`{database}`",
                    f"**📎 上传文件**：`{file_name}`",
                ]
            )
        ),
        _card_hr(),
        _card_markdown(headline),
        _card_markdown(f"**📌 总结**\n{display_summary}"),
        _card_hr(),
        _card_markdown("**📊 重点概览**"),
        _card_column_row(["内容", "状态"], [2, 5], header=True),
        _card_column_row(["问题类型", f"{problem_count} 类"], [2, 5]),
        _card_column_row(["问题时间点", f"{event_count} 个"], [2, 5]),
        _card_column_row(["信号图示", f"{image_count} 张"], [2, 5]),
    ]
    if problems:
        rows = []
        for index, issue in enumerate(problems[:20], start=1):
            bad_case = int(float(issue.statistics.get("Bad Case", 0) or 0))
            severity = float(issue.statistics.get("Severity Score", 0.0) or 0.0)
            first_event = issue.events[0] if issue.events else {}
            time_text = (
                f"{first_event.get('start', 0):.3f}s-{first_event.get('end', 0):.3f}s"
                if first_event
                else "未返回"
            )
            rows.append(
                {
                    "idx": str(index),
                    "issue": metric_title(issue.metric_name),
                    "bad_case": str(bad_case),
                    "severity": f"{severity:.3f}",
                    "events": str(len(issue.events)),
                    "first_time": time_text,
                }
            )
        elements.extend(
            [
                _card_hr(),
                _card_markdown("**📋 出现的问题**\n可左右滑动查看完整列。"),
                _card_table(
                    [
                        {"name": "idx", "display_name": "#", "data_type": "text"},
                        {"name": "issue", "display_name": "问题", "data_type": "text"},
                        {"name": "bad_case", "display_name": "触发次数", "data_type": "text"},
                        {"name": "severity", "display_name": "严重度", "data_type": "text"},
                        {"name": "events", "display_name": "时间点", "data_type": "text"},
                        {"name": "first_time", "display_name": "首个时间窗", "data_type": "text"},
                    ],
                    rows,
                    page_size=8,
                ),
            ]
        )
    if result.warnings:
        elements.extend([_card_hr(), _card_markdown("**💡 处理提示**")])
        for warning in result.warnings[:6]:
            elements.append(_card_markdown(f"- {warning}"))
    if image_items:
        elements.extend([_card_hr(), _card_markdown("**🖼️ 相关总线信号图示**")])
        for index, item in enumerate(image_items, start=1):
            title = item.get("title") or f"图示 {index}"
            elements.append(_card_markdown(f"**图示 {index}：{title}**"))
            elements.append(_card_image(item["image_key"], title))
    return base_card("性能问题分析结果", template, elements)


def performance_result_fallback_card(open_id, task_name, database, file_name, result, image_items=None, template="blue"):
    image_items = image_items or []
    content = format_analysis_markdown_clean(open_id, task_name, database, file_name, result)
    elements = [{"tag": "markdown", "content": content}]
    if image_items:
        elements.append(_card_hr())
        elements.append(_card_markdown("**🖼️ 相关总线信号图示**"))
        for index, item in enumerate(image_items, start=1):
            title = item.get("title") or f"图示 {index}"
            elements.append(_card_markdown(f"**图示 {index}：{title}**"))
            elements.append(_card_image(item["image_key"], title))
    return base_card("性能问题分析结果", template, elements)


def combined_analysis_result_card(
    open_id,
    database,
    file_name,
    request_text,
    performance_result=None,
    performance_error=None,
    actuator_result=None,
    actuator_error=None,
    root_cause_report=None,
    image_items=None,
    template="blue",
):
    image_items = image_items or []
    elements = [
        _card_markdown(
            "\n".join(
                [
                    f"**👤 处理对象**：<at id={open_id}></at>",
                    "**🧩 工作类型**：行车问题原因分析",
                    f"**🗃️ 数据库**：`{database}`" if database else "",
                    f"**📎 上传文件**：`{file_name}`",
                    f"**🎯 用户需求**：{request_text}" if request_text else "",
                ]
            ).strip()
        ),
        _card_hr(),
    ]

    perf_issue_count = len(getattr(performance_result, "problems", []) or []) if performance_result else 0
    perf_event_count = (
        sum(len(issue.events) for issue in performance_result.problems)
        if performance_result
        else 0
    )
    actuator_issue_count = len(getattr(actuator_result, "violations", []) or []) if actuator_result else 0
    if root_cause_report:
        headline_color = "red" if root_cause_report.status in {"issue", "error"} else "green"
        elements.append(
            _card_markdown(
                f"<font color='{headline_color}'>**🎯 归因结论：{root_cause_report.cause_side}**</font>\n"
                f"<font color='{headline_color}'>**{root_cause_report.headline}**</font>"
            )
        )
        elements.extend(
            [
                _card_markdown("**🧭 归因链路**"),
            ]
        )
        if getattr(root_cause_report, "diagnosis_steps", None):
            elements.append(
                _card_table(
                    [
                        {"name": "step", "display_name": "环节", "data_type": "text"},
                        {"name": "result", "display_name": "判断", "data_type": "text"},
                    ],
                    [
                        {
                            "step": row.get("step", ""),
                            "result": row.get("result", ""),
                        }
                        for row in root_cause_report.diagnosis_steps
                    ],
                    page_size=6,
                )
            )
        elements.extend(
            [
                _card_markdown("**📌 覆盖状态**"),
                _card_column_row(["内容", "状态"], [2, 5], header=True),
                _card_column_row(["关注现象", root_cause_report.problem_scope], [2, 5]),
                _card_column_row(["性能侧命中", f"{root_cause_report.performance_issue_count} 类 / {root_cause_report.performance_event_count} 个时间点"], [2, 5]),
                _card_column_row(["执行器侧命中", f"{root_cause_report.actuator_issue_count} 个风险点"], [2, 5]),
                _card_column_row(
                    [
                        "现象层信号",
                        f"匹配 {getattr(root_cause_report, 'phenomenon_signal_count', 0)} 个"
                        f" / 缺失 {len(getattr(root_cause_report, 'missing_phenomenon_signals', []) or [])} 个",
                    ],
                    [2, 5],
                ),
                _card_column_row(
                    [
                        "请求侧信号",
                        f"匹配 {getattr(root_cause_report, 'request_signal_count', 0)} 个"
                        f" / 缺失 {len(getattr(root_cause_report, 'missing_request_signals', []) or [])} 个",
                    ],
                    [2, 5],
                ),
                _card_column_row(
                    [
                        "诱因侧信号",
                        f"匹配 {getattr(root_cause_report, 'algorithm_cause_signal_count', 0)} 个"
                        f" / 缺失 {len(getattr(root_cause_report, 'missing_algorithm_cause_signals', []) or [])} 个",
                    ],
                    [2, 5],
                ),
                _card_column_row(
                    [
                        "响应侧信号",
                        f"匹配 {getattr(root_cause_report, 'response_signal_count', 0)} 个"
                        f" / 缺失 {len(getattr(root_cause_report, 'missing_response_signals', []) or [])} 个",
                    ],
                    [2, 5],
                ),
            ]
        )
        if root_cause_report.evidence_rows:
            elements.extend(
                [
                    _card_markdown("**关键证据**\n可左右滑动查看完整列。"),
                    _card_table(
                        [
                            {"name": "idx", "display_name": "#", "data_type": "text"},
                            {"name": "side", "display_name": "归因方向", "data_type": "text"},
                            {"name": "issue", "display_name": "问题/现象", "data_type": "text"},
                            {"name": "time", "display_name": "时间窗", "data_type": "text"},
                            {"name": "evidence", "display_name": "关键证据", "data_type": "text"},
                            {"name": "expected", "display_name": "期望/判定口径", "data_type": "text"},
                        ],
                        [
                            {
                                "idx": str(index),
                                "side": row.get("side", ""),
                                "issue": row.get("issue", ""),
                                "time": row.get("time", ""),
                                "evidence": row.get("evidence", ""),
                                "expected": row.get("expected", ""),
                            }
                            for index, row in enumerate(root_cause_report.evidence_rows, start=1)
                        ],
                        page_size=8,
                    ),
                ]
            )
        if root_cause_report.next_checks:
            elements.extend([_card_markdown("**下一步建议**")])
            for item in root_cause_report.next_checks[:4]:
                elements.append(_card_markdown(f"- {item}"))
    else:
        if performance_error or actuator_error:
            headline = "<font color='red'>**⚠️ 行车问题原因分析未完全完成，请看下面分项结果**</font>"
        elif perf_issue_count or actuator_issue_count:
            headline = (
                f"<font color='red'>**⚠️ 发现性能问题 {perf_issue_count} 类 / "
                f"执行器风险点 {actuator_issue_count} 个**</font>"
            )
        else:
            headline = "<font color='green'>**✅ 当前性能规则和执行器规则均未发现明确问题**</font>"
        elements.append(_card_markdown(headline))

    elements.extend([_card_hr(), _card_markdown("**📈 性能问题分析**")])
    if performance_error:
        elements.append(_card_markdown(f"<font color='red'>分析失败：`{performance_error}`</font>"))
    elif performance_result:
        display_summary = performance_result.polished_summary or performance_result.summary
        elements.append(_card_markdown(f"**结论**：{display_summary}"))
        elements.append(_card_column_row(["内容", "状态"], [2, 5], header=True))
        elements.append(_card_column_row(["问题类型", f"{perf_issue_count} 类"], [2, 5]))
        elements.append(_card_column_row(["问题时间点", f"{perf_event_count} 个"], [2, 5]))
        if performance_result.problems:
            rows = []
            for index, issue in enumerate(performance_result.problems[:20], start=1):
                first_event = issue.events[0] if issue.events else {}
                rows.append(
                    {
                        "idx": str(index),
                        "issue": metric_title(issue.metric_name),
                        "events": str(len(issue.events)),
                        "first_time": (
                            f"{first_event.get('start', 0):.3f}s-{first_event.get('end', 0):.3f}s"
                            if first_event
                            else "未返回"
                        ),
                    }
                )
            elements.extend(
                [
                    _card_markdown("**性能问题明细**\n可左右滑动查看完整列。"),
                    _card_table(
                        [
                            {"name": "idx", "display_name": "#", "data_type": "text"},
                            {"name": "issue", "display_name": "问题", "data_type": "text"},
                            {"name": "events", "display_name": "时间点", "data_type": "text"},
                            {"name": "first_time", "display_name": "首个时间窗", "data_type": "text"},
                        ],
                        rows,
                        page_size=8,
                    ),
                ]
            )
    else:
        elements.append(_card_markdown("未执行。"))

    elements.extend([_card_hr(), _card_markdown("**🧷 执行器分析**")])
    if actuator_error:
        elements.append(_card_markdown(f"<font color='red'>分析失败：`{actuator_error}`</font>"))
    elif actuator_result:
        elements.append(_card_markdown(actuator_result_headline(actuator_result)))
        elements.append(_card_column_row(["内容", "状态"], [2, 5], header=True))
        for label, value in actuator_result_summary_rows(actuator_result):
            elements.append(_card_column_row([label, value], [2, 5]))
        issues = actuator_issue_rows(actuator_result)
        if issues:
            elements.extend(
                [
                    _card_markdown("**执行器异常明细**\n可左右滑动查看完整列。"),
                    _card_table(
                        [
                            {"name": "idx", "display_name": "#", "data_type": "text"},
                            {"name": "issue", "display_name": "问题", "data_type": "text"},
                            {"name": "time", "display_name": "时间窗", "data_type": "text"},
                            {"name": "signal", "display_name": "未响应信号", "data_type": "text"},
                            {"name": "actual", "display_name": "实际值", "data_type": "text"},
                            {"name": "expected", "display_name": "期望响应", "data_type": "text"},
                        ],
                        [
                            {
                                "idx": item["index"],
                                "issue": item["issue"],
                                "time": item["time_window"],
                                "signal": item["failed_signals"],
                                "actual": item["actual"],
                                "expected": item["expected"],
                            }
                            for item in issues
                        ],
                        page_size=8,
                    ),
                ]
            )
    else:
        elements.append(_card_markdown("未执行。"))

    if image_items:
        elements.extend([_card_hr(), _card_markdown("**🖼️ 相关信号图示**")])
        for index, item in enumerate(image_items, start=1):
            title = item.get("title") or f"图示 {index}"
            elements.append(_card_markdown(f"**图示 {index}：{title}**"))
            elements.append(_card_image(item["image_key"], title))

    return base_card("行车问题原因分析结果", template, elements)


def single_root_cause_analysis_result_card(
    open_id,
    *,
    database,
    file_name,
    request_text,
    problem_type,
    result,
    image_items=None,
    template="blue",
):
    image_items = image_items or []
    status = str(getattr(result, "status", "") or "")
    headline_color = "red" if status in {"issue", "error", "failed"} else "green"
    summary = _clip_card_text(getattr(result, "summary", "") or "分析完成。", 500)
    conclusion = _clip_card_text(getattr(result, "conclusion", "") or "", 6500)
    key_signals = _clip_card_text(getattr(result, "key_signals", "") or "", 1800)

    elements = [
        _card_markdown(
            "\n".join(
                [
                    f"**👤 处理对象**：<at id={open_id}></at>",
                    "**🧩 工作类型**：行车问题原因分析",
                    f"**📌 问题类型**：`{problem_type}`",
                    f"**🗃️ 数据库**：`{database}`" if database else "",
                    f"**📎 上传文件**：`{file_name}`",
                    f"**🎯 用户需求**：{request_text}" if request_text else "",
                ]
            ).strip()
        ),
        _card_hr(),
        _card_markdown(f"<font color='{headline_color}'>**🎯 结论概要：{summary}**</font>"),
    ]
    if key_signals:
        elements.extend(
            [
                _card_hr(),
                _card_markdown(f"**🔎 关键分析信号**\n{key_signals}"),
            ]
        )
    if conclusion:
        elements.extend(
            [
                _card_hr(),
                _card_markdown(f"**🧭 完整分析**\n{conclusion}"),
            ]
        )
    if image_items:
        elements.extend([_card_hr(), _card_markdown("**🖼️ 分析信号图示**")])
        for index, item in enumerate(image_items, start=1):
            title = item.get("title") or f"图示 {index}"
            elements.append(_card_markdown(f"**图示 {index}：{title}**"))
            elements.append(_card_image(item["image_key"], title))

    return base_card("行车问题原因分析结果", template, elements)


def _clip_card_text(value, limit):
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + "\n\n...（内容过长，已截断）"


def _cleanup_single_root_cause_artifacts(result) -> None:
    if result is None:
        return
    roots = []
    for name in ("batch_root_cause", "root_cause_evidence"):
        try:
            roots.append(get_runtime_subdir(name).resolve())
        except Exception:
            pass
    targets: set[Path] = set()
    for image_path in getattr(result, "image_paths", []) or []:
        try:
            parent = Path(image_path).resolve().parent
        except Exception:
            continue
        for root in roots:
            try:
                parent.relative_to(root)
            except Exception:
                continue
            if parent != root:
                targets.add(parent)
    for target in sorted(targets, key=lambda item: len(str(item)), reverse=True):
        shutil.rmtree(target, ignore_errors=True)


def batch_root_cause_result_card(open_id, result: BatchRootCauseResult, template="green"):
    def short_text(value, limit=90):
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) <= limit:
            return text
        return text[:limit] + "..."

    headline_color = "red" if result.failed_count else "green"
    visible_rows = [row for row in result.rows if row.status != "skipped"]
    waiting_data_count = sum(1 for row in visible_rows if row.status == "pending_h5_not_found")
    pending_count = result.pending_count or result.processed_count
    planned_count = result.planned_count or result.processed_count
    headline_parts = [
        f"发现待分析 {pending_count} 条",
        f"本轮处理 {result.processed_count}/{planned_count} 条",
        f"成功 {result.success_count} 条",
        f"失败 {result.failed_count} 条",
    ]
    if waiting_data_count:
        headline_parts.append(f"待数据 {waiting_data_count} 条")
    elements = [
        _card_markdown(f"**👤 处理对象**：<at id={open_id}></at>"),
        _card_hr(),
        _card_markdown(f"<font color='{headline_color}'>**批量分析完成：{' / '.join(headline_parts)}**</font>"),
        _card_column_row(["内容", "状态"], [2, 5], header=True),
        _card_column_row(["读取记录", f"{result.total_rows} 条"], [2, 5]),
        _card_column_row(["发现待分析", f"{pending_count} 条"], [2, 5]),
        _card_column_row(["本轮计划处理", f"{planned_count} 条"], [2, 5]),
        _card_column_row(["本轮实际处理", f"{result.processed_count} 条"], [2, 5]),
        _card_column_row(["目标表格", f"{result.base_token} / {result.table_id}"], [2, 5]),
    ]
    if visible_rows:
        rows = []
        has_writeback_failed = any(str(row.status).startswith("writeback_failed") for row in visible_rows)
        status_map = {
            "success": "已完成",
            "failed": "失败",
            "pending_h5_not_found": "待数据",
            "writeback_failed_after_analysis": "写回失败",
            "writeback_failed_before_analysis": "写回失败",
        }
        for row in visible_rows[:20]:
            rows.append(
                {
                    "idx": str(row.row_index),
                    "problem_type": row.problem_type or "-",
                    "file_name": Path(row.h5_path).name if row.h5_path else "-",
                    "status": status_map.get(row.status, row.status),
                    "images": str(row.uploaded_images),
                    "conclusion": short_text(row.summary or row.conclusion or row.error, 120),
                }
            )
        elements.extend(
            [
                _card_hr(),
                _card_markdown(
                    "**📋 本轮待分析明细**\n"
                    "仅展示本次开始时状态为“待分析”的行。可左右滑动查看完整列。"
                    + ("存在飞书写回失败行，完整结论未必已写入多维表格。" if has_writeback_failed else "完整结论和图示已回写到多维表格。")
                ),
                _card_table(
                    [
                        {"name": "idx", "display_name": "行号", "data_type": "text"},
                        {"name": "problem_type", "display_name": "问题类型", "data_type": "text"},
                        {"name": "file_name", "display_name": "数据文件", "data_type": "text"},
                        {"name": "status", "display_name": "状态", "data_type": "text"},
                        {"name": "images", "display_name": "图示", "data_type": "text"},
                        {"name": "conclusion", "display_name": "摘要", "data_type": "text"},
                    ],
                    rows,
                    page_size=8,
                ),
            ]
        )
        if len(visible_rows) > 20:
            elements.append(_card_markdown(f"另有 {len(visible_rows) - 20} 条本轮待分析明细未在群卡片展开，可在多维表格查看。"))
    else:
        elements.extend(
            [
                _card_hr(),
                _card_markdown(
                    "没有找到需要处理的行。请确认表格里已填写 Agent_问题描述、Agent_问题类型，且 Agent_问题状态 为“待分析”。"
                    "Agent_h5数据路径可以留空，但留空时需要填写 Agent_车号、Agent_车型、Agent_问题日期、Agent_问题时间用于自动定位 H5。"
                ),
            ]
        )
    return base_card("批量行车问题原因分析结果", template, elements)


def fault_result_card(open_id, task_name, input_name, result, template="blue"):
    elements = [
        _card_markdown(
            "\n".join(
                [
                    f"**👤 处理对象**：<at id={open_id}></at>",
                    f"**🧩 任务类型**：{task_name}",
                    f"**📎 输入来源**：`{input_name}`",
                ]
            )
        ),
        _card_hr(),
        _card_markdown(f"**📌 重点结论**\n{result.summary}"),
    ]
    if result.source_types:
        elements.append(_card_markdown(f"**🔎 解析来源**：{'、'.join(result.source_types)}"))
    if result.findings:
        rows = []
        for index, item in enumerate(result.findings[:50], start=1):
            rows.append(
                {
                    "idx": str(index),
                    "fault": item.name or item.code,
                    "state": item.state or "-",
                    "time": item.time_text or "-",
                    "code": item.code or "-",
                    "source": item.source or "-",
                    "impact": item.impact or item.summary or "-",
                }
            )
        elements.extend(
            [
                _card_hr(),
                _card_markdown("**📋 识别结果**\n可左右滑动查看完整列。"),
                _card_table(
                    [
                        {"name": "idx", "display_name": "#", "data_type": "text"},
                        {"name": "fault", "display_name": "故障", "data_type": "text"},
                        {"name": "state", "display_name": "状态", "data_type": "text"},
                        {"name": "time", "display_name": "时间", "data_type": "text"},
                        {"name": "code", "display_name": "故障码", "data_type": "text"},
                        {"name": "source", "display_name": "来源", "data_type": "text"},
                        {"name": "impact", "display_name": "说明/影响", "data_type": "text"},
                    ],
                    rows,
                    page_size=10,
                ),
            ]
        )
    if result.warnings:
        elements.extend([_card_hr(), _card_markdown("**💡 说明**")])
        for warning in result.warnings[:8]:
            elements.append(_card_markdown(f"- {warning}"))
    return base_card("故障降级诊断结果", template, elements)


def fault_result_fallback_card(open_id, task_name, input_name, result, template="blue"):
    return markdown_card("故障降级诊断结果", format_fault_markdown(open_id, task_name, input_name, result), template)


def state_key(open_id, chat_id):
    return f"{open_id}:{chat_id}"


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def release_instance_lock() -> None:
    try:
        if not LOCK_FILE.exists():
            return
        content = LOCK_FILE.read_text(encoding="utf-8").strip()
        if content == str(os.getpid()):
            LOCK_FILE.unlink(missing_ok=True)
    except Exception as exc:
        print(f"释放实例锁失败: {exc}", flush=True)


def acquire_instance_lock() -> bool:
    global LOCK_FILE
    try:
        LOCK_FILE = ensure_writable_file_path(LOCK_FILE, "feishuagent.lock")
        if LOCK_FILE.exists():
            content = LOCK_FILE.read_text(encoding="utf-8").strip()
            if content.isdigit() and is_pid_running(int(content)):
                print(f"检测到已有机器人实例在运行，当前进程退出: pid={content}", flush=True)
                return False
            LOCK_FILE.unlink(missing_ok=True)

        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(release_instance_lock)
        return True
    except Exception as exc:
        print(f"实例锁检查失败，保守退出: {exc}", flush=True)
        return False


def sender_open_id(event):
    sender = event.event.sender
    if not sender or not sender.sender_id:
        return ""
    return sender.sender_id.open_id or ""


def extract_text(content):
    if not content:
        return ""

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content.strip()

    text = payload.get("text", "").strip()
    text = re.sub(r"@_user_\d+", "", text).strip()
    text = text.replace(f"@{BOT_NAME}", "").strip()
    return text


def remember_message(message_id):
    if not message_id:
        return True

    if message_id in processed_message_ids:
        print(f"忽略重复消息: message_id={message_id}", flush=True)
        return False

    processed_message_ids.add(message_id)
    if len(processed_message_ids) > 1000:
        processed_message_ids.clear()
        processed_message_ids.add(message_id)
    return True


def release_message(message_id):
    if not message_id:
        return
    processed_message_ids.discard(message_id)


def is_bot_mentioned(message):
    for mention in message.mentions or []:
        mention_name = mention.name or ""
        mention_open_id = mention.id.open_id if mention.id else ""
        if mention_name in BOT_ALIASES or mention_open_id == BOT_OPEN_ID:
            return True
    return False


def is_own_message(open_id):
    return bool(open_id and open_id == BOT_OPEN_ID)


def has_actionable_text(text):
    value = (text or "").strip()
    if not value:
        return False
    return bool(re.search(r"[\w\u4e00-\u9fff]", value))


def is_plain_greeting_text(text):
    value = re.sub(r"[\s，。！？!?,、~～.]+", "", text or "").lower()
    if not value:
        return False
    greetings = {
        "你好",
        "您好",
        "在吗",
        "在不在",
        "hello",
        "hi",
        "hey",
        "哈喽",
        "嗨",
    }
    return value in greetings


def plain_greeting_reply(open_id):
    return (
        f"**👤 处理对象**：<at id={open_id}></at>\n\n"
        "你好，我在。你可以直接说要查什么或分析什么，例如：\n"
        "- 查执行器握手条件或某个信号该怎么置位\n"
        "- 分析性能问题，上传 `.blf` / `.h5` / `.hdf5`\n"
        "- 查 FDA3/FDA7 故障条目或解析故障码"
    )


def mention_debug(message):
    details = []
    for mention in message.mentions or []:
        mention_open_id = mention.id.open_id if mention.id else ""
        details.append(f"name={mention.name}, open_id={mention_open_id}, type={mention.mentioned_type}")
    return "; ".join(details) if details else "none"


def parse_index(text, max_count):
    match = re.search(r"\d+", text or "")
    if not match:
        return None

    index = int(match.group(0))
    if index < 1 or index > max_count:
        return None
    return index


def is_cancel(text):
    return (text or "").strip() in {"取消", "退出", "cancel", "Cancel", "CANCEL"}


def is_batch_root_cause_cancel_intent(text):
    return re.sub(r"\s+", "", text or "") == BATCH_ROOT_CAUSE_CANCEL_COMMAND


def is_kpi_report_cancel_intent(text):
    return re.sub(r"\s+", "", text or "") == KPI_REPORT_CANCEL_COMMAND


def is_help_request(text):
    return "求助" in (text or "")


def is_workflow_related(text):
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return any(keyword in normalized for keyword in WORKFLOW_KEYWORDS)


def is_possible_wakeup_attempt(text):
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    if is_workflow_related(normalized):
        return True
    return any(keyword in normalized for keyword in WAKEUP_HINT_KEYWORDS)


def is_capability_intro_request(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    keywords = [
        "介绍一下你的能力",
        "介绍你的能力",
        "能力介绍",
        "能力说明",
        "你现在有哪些能力",
        "你有哪些能力",
        "有哪些能力",
        "现在有哪些能力",
        "你有什么能力",
        "你可以干哪些事",
        "你可以干什么",
        "你可以干啥",
        "你可以帮我干啥",
        "你可以帮我做什么",
        "你能干什么",
        "你能干啥",
        "你会干什么",
        "你会干啥",
        "你有什么功能",
        "有哪些功能",
        "能做什么",
        "能做啥",
        "怎么用你",
        "我该怎么用你",
        "如何使用",
        "使用说明",
        "怎么使用",
        "支持哪些",
        "帮我干啥",
    ]
    return any(keyword in normalized for keyword in keywords)


def is_explicit_upload_check(text: str, action: str = "") -> bool:
    value = (text or "").strip().lower()
    if action == "check_upload":
        return True
    if not value:
        return False
    phrases = [
        "刚上传",
        "我刚上传",
        "已经上传",
        "已上传",
        "传好了",
        "上传了",
        "用刚才",
        "用上面",
        "用这个文件",
        "用这个附件",
        "检查上传",
        "确认上传",
    ]
    return any(phrase in value for phrase in phrases)


def task_menu_card(open_id):
    capability_text = clean_plain_reply(build_capability_reply([], None))
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "",
        capability_text,
        "",
        "**怎么继续**",
        "- 直接描述你要查的问题、要分析的现象，或上传对应数据。",
        "- 我会按当前能力配置自动判断是否能处理；配置表关闭或隐藏的能力不会展示。",
        "",
        "**其他操作**：`@布鲁 取消`",
    ]
    return markdown_card("布鲁", "\n".join(lines), "blue")


def database_menu_card(open_id, task_type):
    db_options = database_options()
    if not db_options:
        return markdown_card(
            "未找到数据库",
            f"<at id={open_id}></at> `database` 目录下没有可用数据库文件。",
            "red",
        )

    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "<font color='blue'>**● 步骤 2/4：选择数据库**</font>",
        "<font color='green'>**当前状态：工作类型已确认**</font>",
        "",
        "**▌当前配置**",
        f"- 工作类型：**{task_label(task_type)}**",
        "",
        "**▌数据库版本**",
        "请选择解析需要的数据库版本：",
        "",
    ]
    for index, (label, _) in enumerate(db_options, start=1):
        lines.append(f"**【{index}】** `{label}`")
    lines.extend([
        "",
        "**▌下一步**",
        "<font color='green'>**回复数据库编号，例如：`@布鲁 1`**</font>",
        "",
        "**▌其他操作**",
        "- `@布鲁 返回` 重新选择工作类型",
        "- `@布鲁 取消` 取消本次请求",
    ])
    return markdown_card("选择数据库", "\n".join(lines), "blue")


def upload_instruction_card(open_id, task_type, database):
    content = (
        f"**处理对象**：<at id={open_id}></at>\n"
        "<font color='blue'>**● 步骤 3/4：上传文件**</font>\n"
        "<font color='orange'>**当前状态：等待上传文件附件**</font>\n\n"
        "**▌当前配置**\n"
        f"- 工作类型：**{task_label(task_type)}**\n"
        f"- 数据库：`{database}`\n\n"
        "**▌上传要求**\n"
        "- 请在当前群聊直接上传文件附件\n"
        "- 性能问题分析支持：`.blf`、`.h5`、`.hdf5`\n"
        "- 不要发送云文档链接、图片或压缩包\n\n"
        "**▌下一步**\n"
        "<font color='green'>**上传完成后回复：`@布鲁 确认上传`**</font>\n\n"
        "**▌其他操作**\n"
        "- `@布鲁 返回` 重新选择数据库\n"
        "- `@布鲁 取消` 取消本次请求"
    )
    return markdown_card("请上传文件", content, "blue")


def file_received_card(open_id, task_type, database, file_name):
    content = (
        f"**处理对象**：<at id={open_id}></at>\n"
        "<font color='blue'>**● 步骤 4/4：确认上传并开始分析**</font>\n"
        "<font color='green'>**当前状态：已识别上传文件**</font>\n\n"
        "**▌待分析配置**\n"
        f"- 工作类型：**{task_label(task_type)}**\n"
        f"- 数据库：`{database}`\n"
        f"- 上传文件：`{file_name}`\n\n"
        "**▌下一步**\n"
        "<font color='green'>**确认无误后回复：`@布鲁 确认上传`**</font>\n\n"
        "**▌其他操作**\n"
        "- 重新上传：直接再上传一个新文件，然后再次回复 `@布鲁 确认上传`\n"
        "- `@布鲁 返回` 重新选择数据库\n"
        "- `@布鲁 取消` 取消本次请求"
    )
    return markdown_card("确认配置与文件", content, "blue")


def completed_card(open_id, task_type, database, file_name):
    content = (
        f"**处理对象**：<at id={open_id}></at>\n"
        "<font color='green'>**● 已完成**</font>\n"
        "**当前状态**：默认分析流程已结束\n\n"
        "**▌本次配置**\n"
        f"- 工作类型：**{task_label(task_type)}**\n"
        f"- 数据库：`{database}`\n"
        f"- 上传文件：`{file_name}`\n\n"
        "**▌结果**\n"
        "默认分析结果：文件已接收，基础分析流程已完成。"
    )
    return markdown_card("已完成", content, "green")


def wakeup_hint_card(open_id, raw_text):
    suffix = (raw_text or "").strip()
    content = (
        f"**处理对象**：<at id={open_id}></at>\n"
        "<font color='orange'>**● 已识别到你在尝试唤醒机器人**</font>\n\n"
        "**▌正确操作**\n"
        "<font color='green'>**请回复：`@布鲁 求助`**</font>\n"
        "机器人就会进入正式分析流程。\n\n"
        "**▌说明**\n"
        "- `求助` 用于启动一次新的分析请求\n"
        "- 启动后，机器人会继续引导你选择工作类型、数据库和上传文件\n"
        "- 如果你当前有未完成请求，也可以直接按当前步骤继续回复\n"
    )
    if suffix:
        content += f"\n**▌本次识别到的内容**\n- `{suffix}`"
    return markdown_card("唤醒说明", content, "orange")




def cancel_session(chat_id, open_id):
    key = state_key(open_id, chat_id)
    task = _active_batch_task(key)
    if task and task.get("cancel_event"):
        task["cancel_event"].set()
        session = task.get("session") or sessions.get(key)
        if session:
            session["batch_cancel_requested"] = True
            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="取消中",
                template="orange",
                detail="已收到取消请求，正在停止当前批量行车问题原因分析。",
            )
    sessions.pop(key, None)
    clear_history(key)
    send_text(chat_id, f"<at user_id=\"{open_id}\"></at> 已取消你的分析请求。需要重新开始时再次 @我。")








def extract_file_info(content):
    try:
        payload = json.loads(content or "{}")
    except json.JSONDecodeError:
        payload = {}

    file_name = (
        payload.get("file_name")
        or payload.get("name")
        or payload.get("filename")
        or "未命名文件"
    )
    file_key = payload.get("file_key") or payload.get("fileKey") or payload.get("key") or ""
    return {
        "file_name": file_name,
        "file_key": file_key,
        "raw_content": payload,
    }


def extract_list_message_file_info(message):
    content = message.body.content if message.body else ""
    file_info = extract_file_info(content)
    file_info["message_id"] = message.message_id
    file_info["message_type"] = message.msg_type
    return file_info


def referenced_message_ids(message):
    ids = []
    current_id = getattr(message, "message_id", "") or ""
    for attr in ("parent_id", "upper_message_id", "root_id"):
        value = getattr(message, attr, "") or ""
        if value and value != current_id and value not in ids:
            ids.append(value)
    return ids


def get_message_by_id(message_id):
    if not message_id:
        return None
    try:
        request = GetMessageRequest.builder().message_id(message_id).build()
        response = rest_client.im.v1.message.get(request)
    except Exception as exc:
        print(f"读取引用消息失败: message_id={message_id}, err={exc}", flush=True)
        return None

    if not response.success():
        print(
            f"读取引用消息失败: message_id={message_id}, code={response.code}, "
            f"msg={response.msg}, log_id={response.get_log_id()}",
            flush=True,
        )
        return None

    items = response.data.items if response.data and response.data.items else []
    return items[0] if items else None


def message_content(message):
    body = getattr(message, "body", None)
    if body and getattr(body, "content", None):
        return body.content
    return getattr(message, "content", "") or ""


def message_type_of(message):
    return getattr(message, "msg_type", None) or getattr(message, "message_type", None) or ""


def referenced_message_context(message):
    parts = []
    referenced_file = None
    for message_id in referenced_message_ids(message)[:2]:
        ref = get_message_by_id(message_id)
        if not ref:
            continue
        msg_type = message_type_of(ref)
        content = message_content(ref)
        if msg_type == "text":
            text = extract_text(content)
            if text:
                parts.append(f"引用文本：{text}")
        elif msg_type in {"file", "media"}:
            info = extract_file_info(content)
            info["message_id"] = getattr(ref, "message_id", message_id)
            info["message_type"] = msg_type
            if not referenced_file:
                referenced_file = info
            parts.append(f"引用文件：{info.get('file_name')}")
        else:
            text = extract_text(content)
            summary = text or f"type={msg_type}"
            parts.append(f"引用消息：{summary}")
    return "\n".join(parts), referenced_file


def attach_referenced_file(session, file_info):
    if not session or not file_info:
        return
    session["file_name"] = file_info.get("file_name") or "未命名文件"
    session["file_key"] = file_info.get("file_key") or ""
    session["message_id"] = file_info.get("message_id") or ""
    session["message_type"] = file_info.get("message_type") or "file"
    session["raw_file_content"] = file_info.get("raw_content") or {}
    session.pop("pending_referenced_file", None)


def clear_session_file_state(session):
    for key in [
        "file_name",
        "file_key",
        "raw_file_content",
        "message_id",
        "message_type",
        "pending_referenced_file",
    ]:
        session.pop(key, None)


def maybe_process_referenced_file(chat_id, open_id, session, file_info):
    if not session or not file_info:
        return False
    attach_referenced_file(session, file_info)
    stage = session.get("stage")
    task_type = session.get("task_type")
    file_name = session.get("file_name") or ""
    if task_type == "performance_analysis" and stage == "waiting_file" and _is_supported_analysis_file(file_name):
        process_uploaded_file(chat_id, open_id, session)
        return True
    if task_type == "actuator_handshake" and stage == "waiting_file" and is_actuator_data_file(file_name):
        process_uploaded_file(chat_id, open_id, session)
        return True
    if task_type == COMBINED_ANALYSIS_TASK and stage == "waiting_file":
        if _is_root_cause_data_file(file_name):
            process_uploaded_file(chat_id, open_id, session)
        else:
            send_or_update_result_card(
                chat_id,
                session,
                markdown_card(
                    "行车问题原因分析文件格式不支持",
                    (
                        f"**处理对象**：<at id={open_id}></at>\n"
                        "<font color='orange'>**当前阶段：等待 `.h5` 数据**</font>\n\n"
                        f"**收到文件**：`{file_name}`\n\n"
                        "行车问题原因分析当前仅支持标准 `.h5` 数据文件。"
                        "如果手头是 `.blf`、`.hdf5` 或其他格式，请先转换成布鲁可读取的 `.h5` 后再上传。"
                    ),
                    "orange",
                ),
            )
        return True
    if task_type == BLF_TO_MDF_CONVERSION_TASK and stage == "waiting_file":
        if _is_blf_file(file_name):
            process_uploaded_file(chat_id, open_id, session)
        else:
            send_or_update_result_card(
                chat_id,
                session,
                blf_to_mdf_error_card(open_id, file_name, session.get("database") or "", ValueError("BLF 转 MDF 只接受 `.blf` 文件。")),
            )
        return True
    if task_type == "fault_degradation" and stage in {"waiting_fault_input", "collecting_requirements", "completed"}:
        process_uploaded_file(chat_id, open_id, session)
        return True
    return False


def find_recent_file_message(chat_id, open_id, start_time_ms=None):
    try:
        request = (
            ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(chat_id)
            .sort_type("ByCreateTimeDesc")
            .page_size(50)
            .build()
        )
        response = rest_client.im.v1.message.list(request)
    except Exception as exc:
        return None, f"主动读取群消息失败：{exc}"

    if not response.success():
        if response.code == 230027:
            return None, "应用缺少权限 im:message.group_msg，无法读取群消息或识别已上传文件。"
        return None, f"读取群消息失败：code={response.code}, msg={response.msg}, log_id={response.get_log_id()}"

    items = response.data.items if response.data and response.data.items else []
    candidates = []
    for item in items:
        sender_id = item.sender.id if item.sender else ""
        if sender_id != open_id:
            continue
        if start_time_ms and item.create_time and int(item.create_time) < int(start_time_ms):
            continue
        if item.msg_type in {"file", "media"}:
            candidates.append(item)

    if candidates:
        latest = candidates[0]
        latest_info = extract_list_message_file_info(latest)
        if len(candidates) > 1:
            names = []
            for item in candidates[:5]:
                info = extract_list_message_file_info(item)
                names.append(info.get("file_name") or item.message_id)
            print(
                f"检测到多个候选上传文件，默认使用最新文件: open_id={open_id}, "
                f"selected={latest_info.get('file_name')}, candidates={names}",
                flush=True,
            )
        return latest_info, None

    return None, "上传阶段后没有找到你上传的文件消息。"


def find_recent_file_message_flexible(chat_id, open_id, start_time_ms=None, allow_fallback=False):
    recent_file, error = find_recent_file_message(chat_id, open_id, start_time_ms)
    if recent_file:
        return recent_file, None
    if allow_fallback and start_time_ms:
        fallback_file, fallback_error = find_recent_file_message(chat_id, open_id, None)
        if fallback_file:
            return fallback_file, None
        return None, fallback_error or error
    return None, error










def handle_card_action(event):
    print("收到卡片操作，但当前版本不依赖卡片按钮。", flush=True)
    return {"toast": {"type": "info", "content": "请在群里 @布鲁 回复提示里的指令。"}}


def handle_message_reaction_created(event):
    try:
        event_id = getattr(getattr(event, "header", None), "event_id", "")
        print(f"忽略消息表情添加事件: event_id={event_id}", flush=True)
    except Exception:
        pass
    return None


def handle_message_reaction_deleted(event):
    try:
        event_id = getattr(getattr(event, "header", None), "event_id", "")
        print(f"忽略消息表情删除事件: event_id={event_id}", flush=True)
    except Exception:
        pass
    return None


def handle_message_read(event):
    try:
        event_id = getattr(getattr(event, "header", None), "event_id", "")
        print(f"忽略消息已读事件: event_id={event_id}", flush=True)
    except Exception:
        pass
    return None




def handle_message_event(event: P2ImMessageReceiveV1):
    message = event.event.message
    chat_id = message.chat_id
    message_type = message.message_type
    open_id = sender_open_id(event)

    print(
        f"收到消息: message_id={message.message_id}, type={message_type}, "
        f"chat_type={message.chat_type}, chat_id={chat_id}, sender={open_id}",
        flush=True,
    )

    if not remember_message(message.message_id):
        return

    if message.create_time and int(message.create_time) < started_at_ms:
        print(
            f"忽略机器人启动前的历史消息: message_id={message.message_id}, "
            f"create_time={message.create_time}, started_at={started_at_ms}",
            flush=True,
        )
        return

    if message_type != "text":
        handle_file_message(chat_id, open_id, message)
        return

    if not is_bot_mentioned(message):
        print(f"忽略未 @ 机器人的群消息: mentions={mention_debug(message)}", flush=True)
        return

    text = extract_text(message.content)
    quote_text, referenced_file = referenced_message_context(message)
    if quote_text:
        text = f"{text}\n\n引用消息上下文：\n{quote_text}".strip()
        print(f"检测到引用消息上下文: message_id={message.message_id}, context={quote_text!r}", flush=True)
    remember_feedback_event(open_id, chat_id, "user", text, "text", message.message_id)

    key = state_key(open_id, chat_id)
    if is_batch_root_cause_cancel_intent(text):
        cancel_active_batch_root_cause(chat_id, open_id)
        return

    if is_kpi_report_cancel_intent(text):
        cancel_active_kpi_report(chat_id, open_id)
        return

    if is_capability_intro_request(text):
        sessions.pop(key, None)
        clear_history(key)
        send_card(chat_id, capability_reply_card(open_id, "", [], None))
        return
        reply = ensure_substantive_reply(
            text,
            generate_group_reply(key, text),
            None,
            "用户正在询问机器人能力和使用方式。请直接说明当前可以做什么、怎么开始，不要进入任何具体分析流程。",
        )
        send_assistant_reply(chat_id, open_id, reply, "布鲁", "blue")
        return

    if referenced_file and key in sessions:
        sessions[key]["pending_referenced_file"] = referenced_file
        if maybe_process_referenced_file(chat_id, open_id, sessions[key], referenced_file):
            return

    if handle_session_text(chat_id, open_id, text):
        session = sessions.get(key)
        pending_file = session.get("pending_referenced_file") if session else None
        if pending_file and maybe_process_referenced_file(chat_id, open_id, session, pending_file):
            return
        return

    if is_cancel(text):
        cancel_session(chat_id, open_id)
        return

    if is_help_request(text):
        print(f"群聊触发新请求, mentions={mention_debug(message)}", flush=True)
        start_session(chat_id, open_id, text)
        return

    workflow_decision = None
    if False:
        print(
            f"识别到自然流程请求，自动进入会话: text={text!r}, "
            f"intent={workflow_decision.intent_type}, task={workflow_decision.task_type}",
            flush=True,
        )
        start_session(chat_id, open_id, text)
        return

    print(f"群聊 @ 消息进入 DeepSeek 总控: text={text!r}", flush=True)
    start_session(chat_id, open_id, text, referenced_file=referenced_file)


def handle_message(event: P2ImMessageReceiveV1):
    message = event.event.message
    reply_token = current_reply_message_id.set(getattr(message, "message_id", "") or "")
    open_id_token = current_reply_open_id.set(sender_open_id(event))
    chat_id_token = current_reply_chat_id.set(getattr(message, "chat_id", "") or "")
    try:
        cleanup_expired_last_skill_results()
        return handle_message_event(event)
    except Exception:
        try:
            release_message(message.message_id)
        except Exception:
            pass
        raise
    finally:
        current_reply_message_id.reset(reply_token)
        current_reply_open_id.reset(open_id_token)
        current_reply_chat_id.reset(chat_id_token)






def capability_reply_card(open_id, reply_text, problem_groups, database_name=None):
    capability_text = clean_plain_reply(build_capability_reply(problem_groups or [], None))
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "",
        capability_text,
    ]
    if problem_groups:
        lines.extend([
            "",
            "**当前识别到的关注范围**",
            f"- {format_problem_group_scope(problem_groups)}",
        ])
    if database_name:
        lines.extend([
            "",
            "**当前识别到的数据库**",
            f"- `{database_name}`",
        ])
    return markdown_card("布鲁", "\n".join(lines), "blue")

def actuator_upload_card_v2(open_id, request_text="", database_name=""):
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "<font color='blue'>**当前阶段：等待上传执行器握手诊断数据**</font>",
        "",
        "**本次任务**",
        "- 工作类型：**执行器握手诊断**",
        "- 支持对象：制动系统（EBCM / ebooster）、驱动系统（PDF / ECP / ECM）",
    ]
    if request_text:
        lines.append(f"- 用户需求：{request_text}")
    if database_name:
        lines.append(f"- 当前数据库：`{database_name}`")
    lines.extend(
        [
            "",
            "**下一步**",
            "- 请直接在当前群里上传 1 个总线数据附件",
            "- 当前支持：`.blf`、`.h5`、`.hdf5`",
            "- 如果上传的是 `.blf`，我会使用当前数据库解码；如需指定版本，可先上传 `.arxml`",
            "- 上传后我会自动开始检查断握手、功能降级、状态未按设计闭环等问题",
            "",
            "**也可以直接查询**",
            "- 例如：`导致制动断握手的有哪些原因`",
            "- 例如：`驱动系统 ACC 握手失败要看哪些信号`",
            "",
            "**其他操作**",
            "- `@布鲁 取消` 取消本次请求",
        ]
    )
    return markdown_card("执行器握手诊断", "\n".join(lines), "blue")


def format_file_size(size_bytes):
    try:
        value = float(size_bytes)
    except (TypeError, ValueError):
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def blf_to_mdf_upload_card(open_id, request_text="", database_name=""):
    database_name = database_name or blf_to_mdf_default_database_name() or "当前没有可用 .arxml 数据库"
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "<font color='blue'>**当前阶段：等待上传 BLF 文件**</font>",
        "",
        "**本次任务**",
        "- 工作类型：**BLF 转 MDF 转换**",
        f"- 当前数据库：`{database_name}`",
    ]
    if request_text:
        lines.append(f"- 用户需求：{request_text}")
    lines.extend(
        [
            "",
            "**下一步**",
            "- 请直接在当前群里上传 1 个 `.blf` 文件",
            "- 上传后我会用当前数据库解析，并生成未压缩 `.mdf` 文件",
            "- 如果文件较大或飞书直传失败，会自动回传 `.zip`；解压后仍是未压缩 MDF",
            "",
            "**数据库可调整**",
            f"- 不指定时默认使用：`{database_name}`",
            "- 如果要换数据库，可以先上传新的 `.arxml`，或直接说明 Agent 已有数据库版本",
            "",
            "**其他操作**",
            "- `@布鲁 取消` 取消本次请求",
        ]
    )
    return markdown_card("BLF 转 MDF", "\n".join(lines), "blue")


def blf_to_mdf_result_card(
    open_id,
    database_name,
    input_file_name,
    result: BlfMdfConversionResult,
    delivered_path: str | Path | None = None,
    delivered_as_zip: bool = False,
):
    delivered_path = Path(delivered_path) if delivered_path else result.output_path
    finish_text = "转换完成，MDF 文件已回传。"
    if delivered_as_zip:
        finish_text = "转换完成，已回传 zip；解压后即可得到未压缩 MDF。"

    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "<font color='green'>**当前阶段：转换完成**</font>",
        "",
        finish_text,
        "",
        "**本次结果**",
        f"- 输入 BLF：`{input_file_name}`",
        f"- 解析数据库：`{database_name or result.database_path.name}`",
        f"- 输出 MDF：`{result.output_path.name}`",
        f"- MDF 大小：`{format_file_size(result.output_size) or '已生成'}`",
    ]
    if delivered_as_zip:
        lines.extend(
            [
                f"- 回传文件：`{delivered_path.name}`",
                "- 回传说明：zip 仅用于飞书传输，解压后得到的是未压缩 MDF",
            ]
        )
    else:
        lines.append("- 回传方式：未压缩 `.mdf` 文件")
    if result.frame_count is not None or result.decoded_message_count is not None:
        lines.extend(["", "**转换统计**"])
        if result.frame_count is not None:
            lines.append(f"- 读取帧数：`{result.frame_count}`")
        if result.decoded_message_count is not None:
            lines.append(f"- 解码消息：`{result.decoded_message_count}`")
    compatibility_notes = []
    if getattr(result, "decode_incomplete_signals", False):
        compatibility_notes.append("已启用短载荷补齐解码，避免同一报文在不同通道或不同载荷长度下遗漏长信号。")
    if getattr(result, "preserve_channel", False):
        compatibility_notes.append("已按 CAN 通道保留信号，方便在 CANape 里区分不同通道的同名信号。")
    if compatibility_notes:
        lines.extend(["", "**兼容处理**"])
        lines.extend(f"- {note}" for note in compatibility_notes)
        lines.append("- 如果仍有灰色信号，优先检查测量布局里的信号名是否与当前 ARXML 一致。")
    return markdown_card("BLF 转 MDF 完成", "\n".join(lines), "green")


def blf_to_mdf_error_card(open_id, input_file_name, database_name, exc):
    reason = format_exception_for_card(exc)
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "<font color='red'>**当前阶段：转换失败**</font>",
        "",
        f"**输入文件**：`{input_file_name or '未识别'}`",
    ]
    if database_name:
        lines.append(f"**解析数据库**：`{database_name}`")
    lines.extend(
        [
            "",
            "**失败原因**",
            reason,
            "",
            "**你可以这样处理**",
            "- 确认上传的是 `.blf` 文件",
            f"- 不指定数据库时会默认使用 `{DEFAULT_BLF_MDF_DATABASE_NAME}`",
            "- 如果默认数据库不适配，可以先上传新的 `.arxml` 数据库再上传 `.blf`",
        ]
    )
    return markdown_card("BLF 转 MDF 失败", "\n".join(lines), "red")


def axle_torque_default_database_display():
    arxml = find_default_ecm_arxml()
    return Path(arxml).name if arxml else "当前未找到 ECM ARXML"


def axle_torque_upload_card(open_id, request_text="", database_name="", vehicle_config="", final_drive_ratio=None):
    database_name = database_name or axle_torque_default_database_display()
    vehicle_text = (
        f"`{vehicle_config}` / 主减速比 `{float(final_drive_ratio):g}`"
        if vehicle_config and final_drive_ratio is not None
        else "待选择"
    )
    stage_text = "等待上传 BLF 文件" if vehicle_config else "等待选择车型编号"
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        f"<font color='blue'>**当前阶段：{stage_text}**</font>",
        "",
        "**本次任务**",
        "- 工作类型：**轴端扭矩准确性校验**",
        f"- 当前数据库：`{database_name}`",
        f"- 车型/主减速比：{vehicle_text}",
    ]
    if request_text:
        lines.append(f"- 用户需求：{request_text}")
    lines.extend(["", "**下一步**"])
    if not vehicle_config:
        lines.extend(axle_torque_vehicle_selection_lines())
    else:
        lines.extend(
            [
                "- 车型已确认，请直接在当前群里上传 1 个 `.blf` 文件",
                "- 上传后我会按 ECM/CCB ARXML 解出 071/073/0CB/0C8/0D7/076/078/08D 等目标报文",
                "- 复算轴端扭矩并和 `IActAxleTrq` 对比，输出 MAE、P95、通过率和异常窗口",
            ]
        )
    lines.extend(
        [
            "",
            "**数据库可调整**",
            "- 如果默认 ECM ARXML 不适配，可以先上传新的 `.arxml`，或设置 `AXLE_TORQUE_ARXML`",
            "",
            "**其他操作**",
            "- `@布鲁 取消` 取消本次请求",
        ]
    )
    return markdown_card("轴端扭矩准确性校验", "\n".join(lines), "blue")


def axle_torque_custom_ratio_card(open_id, request_text="", database_name=""):
    database_name = database_name or axle_torque_default_database_display()
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "<font color='blue'>**当前阶段：等待输入其他车型 Final Ratio**</font>",
        "",
        "**本次任务**",
        "- 工作类型：**轴端扭矩准确性校验**",
        f"- 当前数据库：`{database_name}`",
    ]
    if request_text:
        lines.append(f"- 用户需求：{request_text}")
    lines.extend(
        [
            "",
            "**已有车型 Final Ratio**",
            f"- `1`：358-2 = `{VEHICLE_FINAL_DRIVE_RATIOS['358-2']:g}`",
            f"- `2`：C1UL = `{VEHICLE_FINAL_DRIVE_RATIOS['C1UL']:g}`",
            f"- `3`：E2LB-2 = `{VEHICLE_FINAL_DRIVE_RATIOS['E2LB-2']:g}`",
            "",
            "**请输入其他车型 Final Ratio**",
            "- 直接回复数值即可，例如 `3.25`",
            "- 也可以回复 `4 3.25`",
            "- 收到 Final Ratio 后，我会提示你上传 `.blf` 文件。",
            "",
            "**其他操作**",
            "- `@布鲁 取消` 取消本次请求",
        ]
    )
    return markdown_card("轴端扭矩车型标定值", "\n".join(lines), "blue")


def axle_torque_result_card(open_id, input_file_name, result: AxleTorqueResult, image_items=None):
    image_items = image_items or []
    template = "green" if result.status == "ok" else ("red" if result.status == "issue" else "orange")
    elements = [_card_markdown(format_axle_torque_markdown(open_id, input_file_name, result))]
    if image_items:
        elements.extend([_card_hr(), _card_markdown("**相关信号图示**")])
        for index, item in enumerate(image_items, start=1):
            title = item.get("title") or f"图示 {index}"
            elements.append(_card_markdown(f"**图示 {index}：{title}**"))
            elements.append(_card_image(item["image_key"], title))
    return base_card("轴端扭矩准确性校验结果", template, elements)


def axle_torque_error_card(open_id, input_file_name, database_name, exc):
    reason = format_exception_for_card(exc)
    raw_reason = str(exc)
    is_conversion_error = "BLF target-message conversion failed" in raw_reason
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "<font color='red'>**当前阶段：分析失败**</font>",
        "",
        f"**输入文件**：`{input_file_name or '未识别'}`",
    ]
    if database_name:
        lines.append(f"**解析数据库**：`{database_name}`")
    lines.extend(
        [
            "",
            "**失败原因**",
            reason,
            "",
            "**你可以这样处理**",
        ]
    )
    if is_conversion_error:
        lines.extend(
            [
                "- 这是 BLF 目标报文解码阶段失败，还没有进入轴端扭矩公式判断。",
                "- 优先确认这份 BLF 和当前 ECM ARXML 是同一车型/同一网络版本；如果数据库不适配，先上传新的 ECM `.arxml` 再上传 `.blf`。",
                "- 新版本会在失败原因里带出具体 traceback / 报文组信息，便于定位是 BLF 对象、ARXML 报文还是信号解码兼容性问题。",
            ]
        )
    else:
        lines.extend(
            [
                "- 确认上传的是 `.blf` 文件",
                "- 确认 ECM ARXML 中包含 `PTEI_Transmission_General_Status_0CB_M.ITrnTrbAngVl`",
                "- 如果当前数据库不适配，可以先上传新的 ECM `.arxml` 数据库",
            ]
        )
    return markdown_card("轴端扭矩准确性校验失败", "\n".join(lines), "red")


def analysis_scope_clarify_card(open_id, request_text=""):
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "<font color='orange'>**当前阶段：需要确认分析范围**</font>",
        "",
        "你这个需求是在让机器人分析数据/问题，但现在还不能稳妥判断应该按哪条能力处理。",
    ]
    if request_text:
        lines.extend(["", "**你刚才的需求**", f"- {request_text}"])
    lines.extend(
        [
            "",
            "**请直接回复其中一种**",
            "- `性能分析`：检查 ACC、APA、稳定性、颠簸/抖动/重刹等性能问题",
            "- `执行器分析`：检查制动/驱动请求响应、断握手、降级、状态不闭环",
            "- `两个都分析`：同一个文件同时跑性能问题分析和执行器分析",
            "",
            "**其他操作**",
            "- `@布鲁 取消` 取消本次请求",
        ]
    )
    return markdown_card("确认分析范围", "\n".join(lines), "orange")


def requirement_intake_card(open_id, reply_text, request_text=""):
    reply_text = clean_plain_reply(reply_text)
    if not reply_text:
        reply_text = (
            "我还需要再确认一下你的真实目标。你可以直接说要查资料、跑性能分析、跑执行器分析、"
            "做故障诊断，或者告诉我“两个都分析”。"
        )
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "<font color='orange'>**当前阶段：需求识别与澄清**</font>",
    ]
    if request_text:
        lines.extend(["", "**当前理解的需求**", f"- {request_text}"])
    lines.extend(["", reply_text])
    return markdown_card("需求识别", "\n".join(lines), "orange")


def combined_upload_card(open_id, request_text="", database_name="", problem_groups=None):
    problem_groups = problem_groups or []
    problem_type = infer_upload_root_cause_problem_type(request_text) or "待识别"
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "<font color='blue'>**当前阶段：等待上传行车问题数据**</font>",
        "",
        "**本次任务**",
        "- 工作类型：**行车问题原因分析**",
        "- 分析目标：基于问题类型、H5 信号和模型调查链路定位根因因素",
        f"- 问题类型：{problem_type}",
        f"- 信号范围：{format_problem_group_scope(problem_groups)}",
    ]
    if database_name:
        lines.append(f"- 当前数据库：`{database_name}`")
    if request_text:
        lines.append(f"- 用户需求：{request_text}")
    lines.extend(
        [
            "",
            "**下一步**",
            "- 请直接在当前群里上传 1 个总线数据附件",
            "- 当前仅支持：`.h5`",
            "- 如果手头是 `.blf`、`.hdf5` 或其他格式，请先转换成布鲁可读取的标准 `.h5`",
            "- 上传后会调用和飞书多维表格批量分析相同的单条分析核心，并回传结论和信号图示",
            "",
            "**数据库可调整**",
            "- 如果要指定数据库，可以先直接说版本，或上传新的 `.arxml` 文件",
            "",
            "**其他操作**",
            "- `@布鲁 返回` 重新说明需求或更换数据库",
            "- `@布鲁 取消` 取消本次请求",
        ]
    )
    return markdown_card("请上传文件", "\n".join(lines), "blue")


def function_exit_upload_card(open_id, request_text=""):
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "<font color='blue'>**当前阶段：等待上传 ACC/LCC 功能退出数据**</font>",
        "",
        "**本次任务**",
        "- 工作类型：**ACC/LCC功能退出原因分析**",
        "- 分析目标：定位 ACC/LCC 从激活态退出或临时退出的时间点、退出原因码和抑制条件",
    ]
    if request_text:
        lines.append(f"- 用户需求：{request_text}")
    lines.extend(
        [
            "",
            "**下一步**",
            "- 请直接在当前群里上传 1 个解析后的 `.h5` 或 `.hdf5` 数据文件",
            "- 如果手里是 `.blf`，需要先解码成 H5 后再分析",
            "- 上传后我会自动匹配状态信号、退出原因信号和抑制条件",
            "",
            "**也可以直接查询**",
            "- 如果只是想了解 ACC/LCC 状态流转、条件或信号置位，直接用文字问就行，不需要上传数据",
            "",
            "**其他操作**",
            "- `@布鲁 取消` 取消本次请求",
        ]
    )
    return markdown_card("请上传文件", "\n".join(lines), "blue")


def function_exit_request_needs_file(request_text: str) -> bool:
    value = (request_text or "").lower()
    file_terms = [
        ".h5",
        ".hdf5",
        "h5",
        "hdf5",
        "上传",
        "附件",
        "文件",
        "这段数据",
        "数据里",
        "数据里面",
        "基于数据",
        "基于这个数据",
        "退出点",
        "退出时刻",
        "定位退出",
        "分析退出",
    ]
    reference_terms = [
        "定义",
        "配置",
        "原因有哪些",
        "有哪些原因",
        "原因码有哪些",
        "退出码有哪些",
        "有没有因为",
        "是否因为",
        "算不算",
        "属于",
    ]
    if any(term in value for term in reference_terms) and not any(term in value for term in [".h5", ".hdf5", "上传", "文件", "这段数据"]):
        return False
    return any(term in value for term in file_terms)


ROOT_CAUSE_UPLOAD_PROBLEM_TYPE_ALIASES = (
    ("横向-猛打", ("横向-猛打", "猛打", "方向盘", "转向请求突变", "车道线突变")),
    ("稳速未达 Setspeed", ("稳速未达", "未达到 set speed", "未达到setspeed", "达不到目标车速", "设速", "setspeed", "set speed")),
    ("不合理低速", ("不合理低速", "低速异常", "车速低", "速度太低", "低于设速", "掉速", "降速")),
    ("跟车距离", ("跟车距离", "车距异常", "距离异常", "时距异常", "headway", "following distance")),
    ("误制动", ("误制动", "误刹", "不该刹", "错误制动", "false braking")),
    ("漏制动", ("漏制动", "没制动", "未制动", "没有制动", "制动没有响应", "制动没响应")),
    ("重制动", ("重制动", "重刹", "急减速", "急刹", "heavy braking")),
    ("顿挫", ("顿挫", "抖动", "颠簸", "jerk", "jerkiness", "加速度抖动")),
    ("溜车", ("溜车", "后溜", "倒溜", "rollback")),
)


def clean_root_cause_request_text(text):
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ""
    cleanup_patterns = [
        r"[，,。；;]?\s*但?目前未上传文件.*?(?:。|$)",
        r"[，,。；;]?\s*需要先等待用户上传\s*\.?h5\s*数据.*?(?:。|$)",
        r"[，,。；;]?\s*等待用户上传\s*\.?h5\s*数据.*?(?:。|$)",
    ]
    for pattern in cleanup_patterns:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE)
    return value.strip(" ，,。；;")


def infer_upload_root_cause_problem_type(request_text):
    value = clean_root_cause_request_text(request_text)
    lowered = value.lower()
    allowed = allowed_problem_types()

    for label in allowed:
        if label and label.lower() in lowered:
            return label

    for canonical, terms in ROOT_CAUSE_UPLOAD_PROBLEM_TYPE_ALIASES:
        if not any(term.lower() in lowered for term in terms):
            continue
        for label in allowed:
            if label == canonical or canonical in label or label in canonical:
                return label
        return canonical
    return ""


def function_exit_result_card(open_id, result, request_text="", template="blue"):
    event_rows = []
    for index, event in enumerate(result.events[:50], start=1):
        details = "；".join(event.reason_details[:3]) if event.reason_details else event.reason_summary
        event_rows.append(
            {
                "idx": str(index),
                "scope": event.function,
                "time": event.time_text,
                "transition": f"{event.from_state} → {event.to_state}",
                "reason": details[:180],
            }
        )

    signal_rows = []
    for item in (result.matched_signals[:20] + result.missing_signals[:20]):
        signal_rows.append(
            {
                "scope": item.function,
                "label": item.label,
                "status": item.status,
                "signal": item.signal_name or item.note or "未匹配到",
            }
        )

    headline_color = "red" if result.status in {"issue", "error"} else ("orange" if result.status == "warning" else "green")
    elements = [
        _card_markdown(
            "\n".join(
                [
                    f"**👤 处理对象：** <at id={open_id}></at>",
                    "**🧩 工作类型：** ACC/LCC功能退出原因分析",
                    f"**📎 上传文件：** `{result.file_name}`",
                    f"**🎯 用户需求：** {request_text}" if request_text else "",
                ]
            ).strip()
        ),
        _card_hr(),
        _card_markdown(f"<font color='{headline_color}'>**🎯 核心结论：{result.summary}**</font>"),
        _card_hr(),
        _card_markdown("**📌 诊断概览**"),
        _card_column_row(["内容", "状态"], [2, 5], header=True),
        _card_column_row(["检查范围", result.checked_scope], [2, 5]),
        _card_column_row(["退出/原因置位事件", f"{len(result.events)} 个"], [2, 5]),
        _card_column_row(["已匹配信号", f"{len(result.matched_signals)} 个"], [2, 5]),
        _card_column_row(["缺失信号", f"{len(result.missing_signals)} 个"], [2, 5]),
    ]

    if event_rows:
        elements.extend(
            [
                _card_hr(),
                _card_markdown("**📋 退出/原因置位事件明细**\n可左右滑动查看完整列。"),
                _card_table(
                    [
                        {"name": "idx", "display_name": "#", "data_type": "text"},
                        {"name": "scope", "display_name": "功能", "data_type": "text"},
                        {"name": "time", "display_name": "时间点", "data_type": "text"},
                        {"name": "transition", "display_name": "状态跳变", "data_type": "text"},
                        {"name": "reason", "display_name": "原因线索", "data_type": "text"},
                    ],
                    event_rows,
                    page_size=min(max(len(event_rows), 1), 8),
                ),
            ]
        )

    if signal_rows:
        elements.extend(
            [
                _card_hr(),
                _card_markdown("**🧷 信号覆盖状态**\n可左右滑动查看完整列。"),
                _card_table(
                    [
                        {"name": "scope", "display_name": "功能", "data_type": "text"},
                        {"name": "label", "display_name": "关注项", "data_type": "text"},
                        {"name": "status", "display_name": "状态", "data_type": "text"},
                        {"name": "signal", "display_name": "匹配信号/候选", "data_type": "text"},
                    ],
                    signal_rows,
                    page_size=min(max(len(signal_rows), 1), 8),
                ),
            ]
        )

    if result.warnings:
        elements.extend([_card_hr(), _card_markdown("**⚠️ 提示**")])
        for warning in result.warnings[:6]:
            elements.append(_card_markdown(f"- {warning}"))

    return base_card("ACC/LCC功能退出原因分析结果", template, elements)


def _contains_any(text, tokens):
    value = (text or "").lower()
    return any(token.lower() in value for token in tokens)


def parse_analysis_scope_choice(text, clarification_mode=False):
    value = (text or "").strip().lower()
    if not value:
        return ""
    both_tokens = [
        "两个都",
        "双分析",
        "性能和执行器",
        "执行器和性能",
        "性能+执行器",
        "执行器+性能",
    ]
    if clarification_mode:
        both_tokens.extend(["都分析", "全都", "一起", "都跑", "同时"])
    if any(token in value for token in both_tokens):
        return "both"

    performance_tokens = [
        "性能",
        "performance",
        "重刹",
        "重制动",
        "急减速",
        "舒适",
        "舒适性",
        "指标",
        "泊车抖动",
    ]
    actuator_tokens = [
        "执行器",
        "握手",
        "断握手",
        "制动响应",
        "驱动响应",
        "没有响应",
        "没响应",
        "不响应",
        "降级",
        "闭环",
        "不闭环",
        "未闭环",
    ]
    has_performance = any(token in value for token in performance_tokens)
    has_actuator = any(token in value for token in actuator_tokens)
    if any(token in value for token in ["不是性能", "不要性能", "不走性能", "别走性能"]):
        has_performance = False
    if any(token in value for token in ["不是执行器", "不要执行器", "不走执行器", "别走执行器"]):
        has_actuator = False
    if has_performance and has_actuator:
        return "both"
    if has_performance:
        return "performance"
    if has_actuator:
        return "actuator"
    return ""


def is_ambiguous_data_analysis_request(text, session=None):
    session = session or {}
    value = (text or "").strip().lower()
    if not value:
        return False
    if parse_analysis_scope_choice(value) == "both":
        return False
    if _contains_any(value, ["fda3", "fda7", "fault_event", "故障码", "62 fd a3", "62 fd a7", "did"]):
        return False
    if _contains_any(value, ["可以做哪些", "有哪些指标", "规则是什么", "阈值", "需要哪些信号", "导致", "原因"]):
        return False
    root_cause_terms = [
        "归因",
        "根因",
        "执行器还是算法",
        "算法还是执行器",
        "控制侧",
        "策略侧",
        "响应侧",
        "综合判断",
        "综合分析",
    ]
    root_cause_phenomena = [
        "顿挫",
        "颠簸",
        "抖动",
        "重制动",
        "重刹",
        "急减速",
        "不合理低速",
        "误制动",
        "漏制动",
        "未达到",
        "setspeed",
        "set speed",
        "设速",
        "溜车",
        "跟车距离",
        "车距异常",
    ]
    if _contains_any(value, root_cause_terms) or (
        _contains_any(value, root_cause_phenomena)
        and _contains_any(value, ["为什么", "原因", "导致", "归因", "根因"])
    ):
        return False

    has_analysis = _contains_any(value, ["分析", "检查", "诊断", "看一下", "看看", "有没有", "识别"])
    has_data_or_issue = _contains_any(
        value,
        [
            "数据",
            "文件",
            "blf",
            "h5",
            "hdf5",
            "这段",
            "下面",
            "问题",
            "异常",
            "颠簸",
            "抖动",
            "顿挫",
        ],
    )
    if not (has_analysis and has_data_or_issue):
        return False

    clear_performance = _contains_any(value, ["性能问题分析", "性能分析", "acc重刹", "acc 重刹", "重制动", "急减速", "舒适性指标"])
    clear_actuator = _contains_any(
        value,
        ["执行器", "握手", "断握手", "制动没有响应", "制动没响应", "驱动没有响应", "驱动没响应", "状态不闭环", "未闭环", "功能降级"],
    )
    if clear_performance and not clear_actuator:
        return False
    if clear_actuator and not clear_performance:
        return False
    return True


def latest_request_segment(text):
    value = str(text or "").strip()
    if "用户更正：" in value:
        return value.rsplit("用户更正：", 1)[-1].strip()
    return value


def has_query_or_reference_intent(text):
    value = (text or "").lower()
    query_tokens = [
        "查询",
        "查一下",
        "帮我查",
        "了解",
        "看文档",
        "查文档",
        "资料",
        "流程",
        "状态跳转",
        "跳转流程",
        "流转",
        "状态流转",
        "条件",
        "置位",
        "取值",
        "应该是多少",
        "设计要求",
        "规则",
        "哪些信号",
        "关键信号",
    ]
    return any(token in value for token in query_tokens)


def has_requirement_mode(text):
    value = (text or "").lower()
    mode_tokens = [
        "分析",
        "查询",
        "查",
        "诊断",
        "了解",
        "看看",
        "检查",
        "上传",
        "性能",
        "执行器",
        "故障",
        "两个都",
        "都分析",
        "不是",
        "不对",
        "换成",
        "改成",
    ]
    return any(token in value for token in mode_tokens)


def has_reference_topic(text):
    value = (text or "").lower()
    topic_tokens = [
        "acc",
        "aeb",
        "apa",
        "cib",
        "srp",
        "hpp",
        "ebcm",
        "ebooster",
        "pdf",
        "ecp",
        "ecm",
        "制动",
        "驱动",
        "执行器",
        "握手",
        "状态",
        "流程",
        "跳转",
        "条件",
        "置位",
        "信号",
        "故障",
        "fda3",
        "fda7",
        "性能",
        "指标",
        "数据",
        "文件",
        "颠簸",
        "抖动",
        "重刹",
        "重制动",
        "异常",
    ]
    return any(token in value for token in topic_tokens)


def is_self_contained_new_request(text):
    value = (text or "").strip()
    if not value:
        return False
    if has_query_or_reference_intent(value):
        return has_reference_topic(value)
    if any(token in value.lower() for token in ["我要", "我想", "帮我", "分析", "诊断", "查询", "了解", "上传"]):
        return has_reference_topic(value) or _contains_any(value, ["数据", "文件", "问题", "故障", "性能", "执行器"])
    return False


def should_carry_previous_request_into_new_request(previous_request, current_text):
    previous = (previous_request or "").strip()
    current = (current_text or "").strip()
    if not previous or not current or previous in current:
        return False
    if is_self_contained_new_request(current):
        return False
    if is_batch_root_cause_analysis_intent(previous) or is_batch_root_cause_analysis_near_miss(previous):
        return False
    return True


def clarification_exit_request_text(session, text):
    current = latest_request_segment(text)
    pending = latest_request_segment(session.get("pending_analysis_request_text") or session.get("request_text") or "")
    if is_self_contained_new_request(current):
        return current
    if pending and has_reference_topic(pending) and has_requirement_mode(current):
        return f"{pending}\n用户补充：{current}"
    return ""


def configure_analysis_scope(chat_id, open_id, session, scope, request_text):
    clear_session_file_state(session)
    session["request_text"] = request_text
    session["request_id"] = uuid.uuid4().hex[:12]
    session["waiting_file_since"] = int(time.time() * 1000)
    session["stage"] = "waiting_file"
    if not session.get("database"):
        session["database"] = latest_database_name()

    if scope == "performance":
        session["task_type"] = "performance_analysis"
        session.pop("analysis_tasks", None)
        update_progress_card(chat_id, open_id, session, stage="回传结果中", template="blue", detail="已确认按性能问题分析处理，等待上传数据文件。")
        send_or_update_result_card(
            chat_id,
            session,
            smart_upload_card(open_id, "performance_analysis", session.get("database") or "", session.get("problem_groups") or []),
        )
        return True

    if scope == "actuator":
        session["task_type"] = "actuator_handshake"
        session.pop("analysis_tasks", None)
        update_progress_card(chat_id, open_id, session, stage="回传结果中", template="blue", detail="已确认按执行器分析处理，等待上传总线数据。")
        send_or_update_result_card(chat_id, session, actuator_upload_card_v2(open_id, request_text, session.get("database") or ""))
        return True

    if scope == "both":
        session["task_type"] = COMBINED_ANALYSIS_TASK
        session["active_skill"] = SKILL_ROOT_CAUSE_ANALYSIS
        session["analysis_tasks"] = ["performance_analysis", "actuator_handshake"]
        if not session.get("problem_groups"):
            session["problem_groups"] = infer_root_cause_problem_groups(request_text)
        update_progress_card(chat_id, open_id, session, stage="回传结果中", template="blue", detail="已确认做行车问题原因分析，等待上传 .h5 数据文件。")
        send_or_update_result_card(
            chat_id,
            session,
            combined_upload_card(open_id, request_text, session.get("database") or "", session.get("problem_groups") or []),
        )
        return True
    return False


def handle_analysis_scope_choice(chat_id, open_id, session, text):
    scope = parse_analysis_scope_choice(text, clarification_mode=True)
    request_text = session.get("pending_analysis_request_text") or session.get("request_text") or text
    if not scope:
        next_text = clarification_exit_request_text(session, text)
        if next_text:
            session.pop("pending_analysis_request_text", None)
            session.pop("analysis_tasks", None)
            session.pop("task_type", None)
            session.pop("active_skill", None)
            session["stage"] = "collecting_requirements"
            session["request_text"] = next_text
            return handle_agent_decision(chat_id, open_id, session, next_text)

    if not scope and is_self_contained_new_request(text):
        session.pop("pending_analysis_request_text", None)
        session.pop("analysis_tasks", None)
        session.pop("task_type", None)
        session.pop("active_skill", None)
        session["stage"] = "collecting_requirements"
        session["request_text"] = text
        return handle_agent_decision(chat_id, open_id, session, text)

    start_progress_card(
        chat_id,
        open_id,
        session,
        stage="分析需求中",
        template="blue",
        detail="正在根据你的选择确认本次要跑的分析范围。",
        force_new=True,
    )
    if scope:
        session.pop("pending_analysis_request_text", None)
        session.pop("active_skill", None)
        return configure_analysis_scope(chat_id, open_id, session, scope, request_text)

    update_progress_card(chat_id, open_id, session, stage="回传结果中", template="orange", detail="还需要你明确选择性能、执行器或两个都分析。")
    send_or_update_result_card(chat_id, session, analysis_scope_clarify_card(open_id, request_text))
    return True




def fault_input_card(open_id):
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "<font color='orange'>**当前阶段：等待故障诊断输入**</font>",
        "",
        "**当前支持范围**",
        "- 故障降级诊断目前仅支持 **J6B** 项目",
        "- **8650** 和 **BMC** 仍在开发中",
        "",
        "**你现在可以这样做**",
        "- 直接上传 `slog` 日志文件",
        "- 直接上传 DID 日志文件",
        "- 直接粘贴 `fault_event` 日志行",
        "- 直接粘贴 `62 FD A3 ...` / `62 FD A7 ...` 的整段字节串",
        "- 直接查询故障码，例如 `FDA3[113]`、`FDA7[8]`、`byte 0 bit 4`",
        "",
        "**其他操作**",
        "- `@布鲁 返回` 返回需求说明",
        "- `@布鲁 取消` 取消本次请求",
    ]
    return markdown_card("故障降级诊断", "\n".join(lines), "orange")

def looks_like_fault_diagnosis_text(text):
    value = (text or "").strip()
    if not value:
        return False
    upper = value.upper()
    if "FAULT_EVENT" in upper or "[FAULT_EVENT]" in upper:
        return True
    if "62 FD A3" in upper or "62 FD A7" in upper or "FDA3[" in upper or "FDA7[" in upper:
        return True
    if re.search(r"\bbyte\s*\d+\s*bit\s*\d+\b", value, flags=re.IGNORECASE):
        return True
    if ("FDA3" in upper or "FDA7" in upper) and len(re.findall(r"\b[0-9A-Fa-f]{2}\b", value)) >= 8:
        return True
    return False


def process_fault_diagnosis_input(chat_id, open_id, session, input_name, text=None, local_file=None, force_new_progress=False):
    session["stage"] = "processing"
    result = None
    if force_new_progress:
        start_progress_card(
            chat_id,
            open_id,
            session,
            stage="已收到输入",
            template="orange",
            detail=f"已收到 `{input_name}`，正在准备故障诊断。",
            force_new=True,
        )
    elif not session.get("progress_card_id"):
        start_progress_card(
            chat_id,
            open_id,
            session,
            stage="已收到输入",
            template="orange",
            detail=f"已收到 `{input_name}`，正在准备故障诊断。",
        )
    else:
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="已收到输入",
            template="orange",
            detail=f"已收到 `{input_name}`，正在准备故障诊断。",
        )
    try:
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="正在分析",
            template="orange",
            detail="正在解析故障码、DID 字节或日志内容，并匹配 FDA3/FDA7 故障表。",
        )
        if local_file is not None:
            result = diagnose_fault_file(local_file)
        else:
            result = diagnose_fault_text(text or "", source_name=input_name)
        template = "green" if result.status == "ok" else "yellow" if result.status == "no_match" else "red"
        send_or_update_result_card(
            chat_id,
            session,
            fault_result_card(open_id, task_label("fault_degradation"), input_name, result, template),
            fault_result_fallback_card(open_id, task_label("fault_degradation"), input_name, result, template),
        )
        remember_skill_result(
            session,
            SKILL_FAULT_DIAGNOSIS,
            text or input_name,
            format_fault_markdown(open_id, task_label("fault_degradation"), input_name, result),
            result.summary,
            "故障降级诊断结果",
            template,
        )
    except Exception as exc:
        send_or_update_result_card(
            chat_id,
            session,
            markdown_card(
                "故障降级诊断失败",
                (
                    f"**处理对象**：<at id={open_id}></at>\n"
                    "<font color='red'>**当前阶段：诊断失败**</font>\n\n"
                    f"**输入来源**：`{input_name}`\n"
                    f"**失败原因**：\n{format_exception_for_card(exc)}"
                ),
                "red",
            ),
        )
    finally:
        session["stage"] = "completed"
        if local_file is not None:
            try:
                local_file.unlink(missing_ok=True)
                shutil.rmtree(local_file.parent, ignore_errors=True)
            except Exception:
                pass


def handle_feedback_report(chat_id, open_id, session, text):
    feedback_text = extract_feedback_text(text) or text
    source_message_id = current_reply_message_id.get()
    reporter_name = resolve_user_display_name(open_id, chat_id)
    events = recent_feedback_events(open_id, chat_id, FEEDBACK_CONTEXT_TURNS)
    if not events:
        events = [
            {
                "role": "user",
                "text": text,
                "message_type": "text",
                "message_id": source_message_id,
                "created_at_ms": int(time.time() * 1000),
            }
        ]

    related_skill = (
        session.get("active_skill")
        or (session.get("last_skill_result") or {}).get("source_skill")
        or session.get("task_type")
        or ""
    )

    try:
        image_path = render_feedback_snapshot(
            events=events,
            reporter_open_id=reporter_name,
            chat_id=chat_id,
            feedback_text=feedback_text,
            source_message_id=source_message_id,
        )
    except Exception as exc:
        send_card(
            chat_id,
            assistant_reply_card(
                open_id,
                f"反馈我收到了，但生成对话快照失败：`{exc}`。你可以先把这段现象留在群里，我会继续按文本记录方向补。",
                "问题反馈",
                "red",
            ),
        )
        return True

    config = load_feedback_config()
    result = create_feedback_record(
        rest_client=rest_client,
        config=config,
        reporter_open_id=reporter_name,
        chat_id=chat_id,
        source_message_id=source_message_id,
        feedback_text=feedback_text,
        related_skill=related_skill,
        context_summary=feedback_context_summary(events),
        image_path=image_path,
    )

    if result.success:
        if result.attachment_uploaded:
            attachment_note = "快照已写入附件字段。"
        elif result.local_snapshot_deleted:
            attachment_note = "记录已写入；附件字段未上传成功或未启用，本地快照已按配置清理。"
        else:
            attachment_note = f"记录已写入；附件字段未上传成功或未启用，本地快照仍保留：`{result.image_path}`。"
        reply = (
            "已记录这条反馈。\n\n"
            f"反馈内容：{feedback_text}\n"
            f"记录 ID：`{result.record_id}`\n"
            f"{attachment_note}"
        )
        send_card(chat_id, assistant_reply_card(open_id, reply, "问题反馈", "green"))
        return True

    if not result.configured:
        reply = (
            "反馈快照已经在本地生成，但还没有配置目标多维表格。\n\n"
            "需要在 `.env` 配置：\n"
            "- `FEEDBACK_BASE_TOKEN`\n"
            "- `FEEDBACK_TABLE_ID`\n"
            "- `FEEDBACK_ATTACHMENT_FIELD`，默认 `对话快照`\n\n"
            "如果你还没有表，可以运行 `scripts/init_feedback_base.ps1` 创建一个，再把输出的 token/table id 写进 `.env`。"
        )
        send_card(chat_id, assistant_reply_card(open_id, reply, "问题反馈", "orange"))
        return True

    send_card(
        chat_id,
        assistant_reply_card(
            open_id,
            f"反馈快照已生成，但写入多维表格失败：`{result.error}`\n\n本地快照：`{result.image_path}`",
            "问题反馈",
            "red",
        ),
    )
    return True


def _active_batch_task(key):
    with active_batch_root_cause_lock:
        return active_batch_root_cause_tasks.get(key)


def _register_active_batch_task(key, task):
    with active_batch_root_cause_lock:
        existing = active_batch_root_cause_tasks.get(key)
        if existing and not existing.get("cancel_event").is_set():
            thread = existing.get("thread")
            if thread is None or thread.is_alive():
                return False
        active_batch_root_cause_tasks[key] = task
        return True


def _clear_active_batch_task(key, cancel_event):
    with active_batch_root_cause_lock:
        current = active_batch_root_cause_tasks.get(key)
        if current and current.get("cancel_event") is cancel_event:
            active_batch_root_cause_tasks.pop(key, None)


def cancel_active_batch_root_cause(chat_id, open_id):
    key = state_key(open_id, chat_id)
    task = _active_batch_task(key)
    if not task:
        send_card(
            chat_id,
            assistant_reply_card(
                open_id,
                "当前没有正在进行的批量行车问题原因分析。",
                "取消当前问题分析",
                "orange",
            ),
        )
        return True

    cancel_event = task.get("cancel_event")
    if cancel_event:
        cancel_event.set()
    session = task.get("session") or sessions.get(key)
    updated = False
    if session:
        session["batch_cancel_requested"] = True
        updated = update_progress_card(
            chat_id,
            open_id,
            session,
            stage="取消中",
            template="orange",
            detail=(
                "已收到固定指令“取消当前问题分析”，正在停止当前进程。最终状态会更新在本卡片；"
                "如果当前正在等待 DeepSeek 或 H5 读取返回，可能会有几秒到几十秒延迟。"
            ),
        )
    if not updated:
        send_card(
            chat_id,
            assistant_reply_card(
                open_id,
                (
                    "已收到取消指令。正在停止当前批量行车问题原因分析；"
                    "如果当前正在等待 DeepSeek 或 H5 读取返回，可能会有几秒到几十秒延迟。"
                ),
                "取消当前问题分析",
                "orange",
            ),
        )
    return True


def _run_batch_root_cause_worker(chat_id, open_id, session, query, cancel_event, task_key):
    progress_callback = make_progress_callback(chat_id, open_id, session, template="blue")

    def cancellation_check():
        return cancel_event.is_set()

    try:
        batch_result = run_batch_root_cause_analysis(
            progress_callback=progress_callback,
            cancellation_check=cancellation_check,
        )
        template = "red" if batch_result.failed_count else "green"
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="回传结果中",
            template=template,
            detail="已完成批量分析，正在回传处理摘要。",
        )
        send_or_update_result_card(
            chat_id,
            session,
            batch_root_cause_result_card(open_id, batch_result, template=template),
        )
        waiting_data_count = sum(1 for row in batch_result.rows if row.status == "pending_h5_not_found")
        pending_count = batch_result.pending_count or batch_result.processed_count
        planned_count = batch_result.planned_count or batch_result.processed_count
        batch_result_summary = (
            f"批量分析完成：发现待分析 {pending_count} 条，本轮处理 {batch_result.processed_count}/{planned_count} 条，"
            f"成功 {batch_result.success_count} 条，失败 {batch_result.failed_count} 条"
            + (f"，待数据 {waiting_data_count} 条" if waiting_data_count else "")
            + "。"
        )
        remember_skill_result(
            session,
            SKILL_BATCH_ROOT_CAUSE_ANALYSIS,
            query,
            "批量行车问题原因分析：" + batch_result_summary,
            batch_result_summary,
            "批量行车问题原因分析",
            template,
        )
        session["stage"] = "completed"
    except BatchDeepSeekAPIUnavailable as exc:
        unavailable_message = str(exc) or get_llm_api_unavailable_message()
        session["stage"] = "completed"
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="已取消",
            template="red",
            detail=unavailable_message,
        )
        send_text(chat_id, f'<at user_id="{open_id}"></at> {unavailable_message}')
    except BatchRootCauseCancelled as exc:
        session["stage"] = "completed"
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="已取消",
            template="orange",
            detail=(
                f"{str(exc) or '已取消当前批量行车问题原因分析。'}\n"
                "最终状态：已取消成功；未完成表格行已恢复为待分析。"
            ),
        )
        send_or_update_result_card(
            chat_id,
            session,
            markdown_card(
                "批量行车问题原因分析已取消",
                (
                    f"**处理对象**：<at id={open_id}></at>\n\n"
                    "**当前状态**：已取消成功\n"
                    f"**取消指令**：`{BATCH_ROOT_CAUSE_CANCEL_DISPLAY_COMMAND}`\n"
                    f"**处理结果**：{format_exception_for_card(exc)}\n\n"
                    "未完成的表格行已恢复为 `待分析`，后续需要继续跑时重新发送批量分析指令即可。"
                ),
                "orange",
            ),
        )
    except Exception as exc:
        session["stage"] = "completed"
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="分析失败",
            template="red",
            detail="批量分析未完成，正在回传失败原因。",
        )
        send_or_update_result_card(
            chat_id,
            session,
            markdown_card(
                "批量行车问题原因分析失败",
                f"**处理对象**：<at id={open_id}></at>\n\n**失败原因**：\n{format_exception_for_card(exc)}",
                "red",
            ),
        )
    finally:
        session.pop("batch_cancel_requested", None)
        _clear_active_batch_task(task_key, cancel_event)


def run_batch_root_cause_command(chat_id, open_id, session, query):
    key = state_key(open_id, chat_id)
    existing = _active_batch_task(key)
    if existing and not existing.get("cancel_event").is_set():
        thread = existing.get("thread")
        if thread is None or thread.is_alive():
            send_card(
                chat_id,
                assistant_reply_card(
                    open_id,
                    f"当前已经有批量行车问题原因分析在运行。需要停止时请发送：`{BATCH_ROOT_CAUSE_CANCEL_DISPLAY_COMMAND}`。",
                    "批量行车问题原因分析",
                    "orange",
                ),
            )
            return True

    session["task_type"] = BATCH_ROOT_CAUSE_ANALYSIS_TASK
    session["active_skill"] = SKILL_BATCH_ROOT_CAUSE_ANALYSIS
    session["request_text"] = query
    session["stage"] = "processing"
    session["request_id"] = uuid.uuid4().hex[:12]
    clear_session_file_state(session)
    start_progress_card(
        chat_id,
        open_id,
        session,
        stage="查询中",
        template="blue",
        detail=f"正在读取批量分析多维表格，准备逐行处理问题。{BATCH_ROOT_CAUSE_CANCEL_HINT}",
        force_new=True,
    )
    cancel_event = threading.Event()
    worker = threading.Thread(
        target=_run_batch_root_cause_worker,
        args=(chat_id, open_id, session, query, cancel_event, key),
        name=f"batch-root-cause-{session['request_id']}",
        daemon=True,
    )
    if not _register_active_batch_task(key, {"cancel_event": cancel_event, "session": session, "thread": worker}):
        session["stage"] = "completed"
        send_card(
            chat_id,
            assistant_reply_card(
                open_id,
                f"当前已经有批量行车问题原因分析在运行。需要停止时请发送：`{BATCH_ROOT_CAUSE_CANCEL_DISPLAY_COMMAND}`。",
                "批量行车问题原因分析",
                "orange",
            ),
        )
        return True
    try:
        worker.start()
    except Exception as exc:
        _clear_active_batch_task(key, cancel_event)
        session["stage"] = "completed"
        send_or_update_result_card(
            chat_id,
            session,
            markdown_card(
                "批量行车问题原因分析启动失败",
                f"**处理对象**：<at id={open_id}></at>\n\n**失败原因**：\n{format_exception_for_card(exc)}",
                "red",
            ),
        )
    return True


def _active_kpi_report_task(key):
    with active_kpi_report_lock:
        return active_kpi_report_tasks.get(key)


def _register_active_kpi_report_task(key, task):
    with active_kpi_report_lock:
        existing = active_kpi_report_tasks.get(key)
        if existing and not existing.get("cancel_event").is_set():
            thread = existing.get("thread")
            if thread is None or thread.is_alive():
                return False
        active_kpi_report_tasks[key] = task
        return True


def _clear_active_kpi_report_task(key, cancel_event):
    with active_kpi_report_lock:
        current = active_kpi_report_tasks.get(key)
        if current and current.get("cancel_event") is cancel_event:
            active_kpi_report_tasks.pop(key, None)


def cancel_active_kpi_report(chat_id, open_id):
    key = state_key(open_id, chat_id)
    task = _active_kpi_report_task(key)
    if not task:
        send_card(chat_id, assistant_reply_card(open_id, "当前没有正在进行的 KPI 报告 AI 分析。", "取消 KPI 分析", "orange"))
        return True
    task["cancel_event"].set()
    session = task.get("session") or sessions.get(key)
    if session:
        update_progress_card(
            chat_id, open_id, session, stage="取消中", template="orange",
            detail="已收到取消指令，正在停止；未完成记录会恢复为待分析。",
        )
    return True


def _run_kpi_report_worker(chat_id, open_id, session, query, cancel_event, task_key):
    progress_callback = make_progress_callback(chat_id, open_id, session, template="blue")
    try:
        result = run_kpi_report_analysis(
            progress_callback=progress_callback,
            cancellation_check=cancel_event.is_set,
        )
        session["stage"] = "completed"
        template = "red" if result.failed_count else "green"
        detail = (
            f"KPI 报告分析完成：扫描 {result.total_records} 条，待分析 {result.pending_count} 条，"
            f"成功回写 {result.success_count} 条，失败 {result.failed_count} 条。分析内容仅写入多维表格。"
        )
        update_progress_card(chat_id, open_id, session, stage="已完成", template=template, detail=detail)
        remember_skill_result(
            session, SKILL_KPI_REPORT_ANALYSIS, query,
            "KPI 报告 AI 分析已完成，详细结果已写入多维表格。", detail,
            "KPI 报告 AI 分析", template,
        )
    except KpiReportCancelled as exc:
        session["stage"] = "completed"
        update_progress_card(
            chat_id, open_id, session, stage="已取消", template="orange",
            detail=f"{str(exc)} 未完成记录已恢复为待分析。",
        )
    except Exception as exc:
        session["stage"] = "completed"
        update_progress_card(
            chat_id, open_id, session, stage="分析失败", template="red",
            detail=f"KPI 报告分析未完成：{format_exception_for_card(exc)}",
        )
    finally:
        _clear_active_kpi_report_task(task_key, cancel_event)


def run_kpi_report_command(chat_id, open_id, session, query):
    key = state_key(open_id, chat_id)
    existing = _active_kpi_report_task(key)
    if existing and not existing.get("cancel_event").is_set():
        send_card(
            chat_id,
            assistant_reply_card(
                open_id,
                f"当前已经有 KPI 报告 AI 分析在运行。需要停止时请发送：`{KPI_REPORT_CANCEL_DISPLAY_COMMAND}`。",
                "KPI 报告 AI 分析",
                "orange",
            ),
        )
        return True

    session["task_type"] = KPI_REPORT_ANALYSIS_TASK
    session["active_skill"] = SKILL_KPI_REPORT_ANALYSIS
    session["request_text"] = query
    session["stage"] = "processing"
    session["request_id"] = uuid.uuid4().hex[:12]
    clear_session_file_state(session)
    start_progress_card(
        chat_id, open_id, session, stage="查询中", template="blue",
        detail=f"正在读取 KPI 报告多维表格，仅分析报告内容并回写对应列。{KPI_REPORT_CANCEL_HINT}",
        force_new=True,
    )
    cancel_event = threading.Event()
    worker = threading.Thread(
        target=_run_kpi_report_worker,
        args=(chat_id, open_id, session, query, cancel_event, key),
        name=f"kpi-report-{session['request_id']}", daemon=True,
    )
    if not _register_active_kpi_report_task(
        key, {"cancel_event": cancel_event, "session": session, "thread": worker}
    ):
        session["stage"] = "completed"
        return True
    try:
        worker.start()
    except Exception as exc:
        _clear_active_kpi_report_task(key, cancel_event)
        session["stage"] = "completed"
        update_progress_card(
            chat_id, open_id, session, stage="启动失败", template="red",
            detail=f"KPI 报告分析启动失败：{format_exception_for_card(exc)}",
        )
    return True


def handle_agent_decision(chat_id, open_id, session, text):
    previous_session_request = session.get("request_text") or ""
    bump_session_message_count(session, chat_id, open_id)
    remember_conversation_turn(session, "user", text)
    session["request_text"] = text
    if is_feedback_request_text(text):
        return handle_feedback_report(chat_id, open_id, session, text)

    if is_batch_root_cause_analysis_intent(text):
        return run_batch_root_cause_command(chat_id, open_id, session, text)

    if is_batch_root_cause_analysis_near_miss(text):
        reset_progress_card_state(session)
        send_card(
            chat_id,
            assistant_reply_card(open_id, batch_root_cause_near_miss_reply(), "多维表格批量分析", "orange"),
        )
        return True

    if is_kpi_report_analysis_intent(text):
        return run_kpi_report_command(chat_id, open_id, session, text)

    if is_kpi_report_analysis_near_miss(text):
        reset_progress_card_state(session)
        send_card(
            chat_id,
            assistant_reply_card(open_id, kpi_report_near_miss_reply(), "KPI 报告 AI 分析", "orange"),
        )
        return True

    start_progress_card(
        chat_id,
        open_id,
        session,
        stage="分析需求中",
        template="blue",
        detail="正在理解你的需求，并判断该走日常沟通、资料查询、诊断分析还是上传文件流程。",
        force_new=True,
    )

    if handle_result_transform_followup(chat_id, open_id, session, text):
        return True

    requested_scope = parse_analysis_scope_choice(text)
    if requested_scope == "both":
        return configure_analysis_scope(chat_id, open_id, session, "both", text)

    if not is_batch_root_cause_analysis_intent(text) and is_ambiguous_data_analysis_request(text, session):
        session["stage"] = "clarify_analysis_scope"
        session["active_skill"] = SKILL_REQUIREMENT_INTAKE
        session["pending_analysis_request_text"] = text
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="回传结果中",
            template="orange",
            detail="需求还不够明确，正在进入需求识别与澄清。",
        )
        send_or_update_result_card(chat_id, session, analysis_scope_clarify_card(open_id, text))
        return True

    db_names = [label for label, _ in database_options()]
    decision = decide_agent_action(text, db_names, session)
    print(
        "[agent-main] "
        f"user={open_id} chat={chat_id} text={text!r} "
        f"action={decision.action} tool={decision.tool} task={decision.task_type} "
        f"db={decision.database_name} groups={decision.problem_groups} "
        f"project={decision.project_type} supported={decision.project_supported}",
        flush=True,
    )

    if decision.action == "cancel":
        key = state_key(open_id, chat_id)
        sessions.pop(key, None)
        clear_history(key)
        send_or_update_result_card(
            chat_id,
            session,
            assistant_reply_card(open_id, "已取消你的分析请求。需要重新开始时，直接重新 @我说需求就行。", "布鲁", "green"),
        )
        return True

    if decision.action == "status":
        update_progress_card(chat_id, open_id, session, stage="完成", template="green", detail="已返回当前会话状态。")
        send_or_update_result_card(
            chat_id,
            session,
            assistant_reply_card(open_id, session_status_text(open_id, session), "布鲁", "blue"),
        )
        return True

    if decision.action == "new_request":
        update_progress_card(chat_id, open_id, session, stage="完成", template="blue", detail="已识别为新的需求，正在重新开始。")
        key = state_key(open_id, chat_id)
        previous_request = previous_session_request
        next_text = text
        if should_carry_previous_request_into_new_request(previous_request, text):
            next_text = f"{previous_request}\n用户更正：{text}"
        sessions.pop(key, None)
        clear_history(key)
        start_session(chat_id, open_id, next_text)
        return True

    if decision.database_name:
        session["database"] = decision.database_name
    if decision.problem_groups:
        session["problem_groups"] = decision.problem_groups
    if decision.project_type:
        session["project_type"] = decision.project_type
    if decision.task_type:
        session["task_type"] = decision.task_type

    tool = decision.tool if decision.action == "call_tool" else None

    if tool and not is_skill_enabled(tool):
        update_progress_card(chat_id, open_id, session, stage="回传结果中", template="orange", detail="该能力已被配置表关闭。")
        send_or_update_result_card(
            chat_id,
            session,
            assistant_reply_card(open_id, f"`{tool}` 当前已在配置表里关闭，暂时不能处理。", "能力已关闭", "orange"),
        )
        return True

    if tool == SKILL_FEEDBACK_REPORT:
        return handle_feedback_report(chat_id, open_id, session, decision.tool_query or text)

    if tool == SKILL_AXLE_TORQUE_ACCURACY_ANALYSIS:
        query = decision.tool_query or text
        session["task_type"] = AXLE_TORQUE_ACCURACY_TASK
        session["active_skill"] = SKILL_AXLE_TORQUE_ACCURACY_ANALYSIS
        session["request_text"] = query
        clear_axle_torque_vehicle_selection(session)
        if decision.database_name:
            session["database"] = decision.database_name
            session.pop("axle_torque_arxml", None)
        if not session.get("database") and not session.get("axle_torque_arxml"):
            set_default_axle_torque_arxml(session)
        session["stage"] = "waiting_axle_torque_vehicle"
        session["request_id"] = session.get("request_id") or uuid.uuid4().hex[:12]
        session["waiting_file_since"] = int(time.time() * 1000)
        clear_session_file_state(session)
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="等待选择车型编号",
            template="blue",
            detail=f"已进入轴端扭矩准确性校验，请先选择车型编号：{axle_torque_vehicle_options_text()}。",
        )
        send_or_update_result_card(
            chat_id,
            session,
            axle_torque_upload_card(
                open_id,
                query,
                session.get("database") or "",
                "",
                None,
            ),
        )
        return True

    if tool == SKILL_BLF_TO_MDF_CONVERSION:
        query = decision.tool_query or text
        session["task_type"] = BLF_TO_MDF_CONVERSION_TASK
        session["active_skill"] = SKILL_BLF_TO_MDF_CONVERSION
        session["request_text"] = query
        if decision.database_name:
            session["database"] = decision.database_name
        if not session.get("database"):
            session["database"] = blf_to_mdf_default_database_name()
        session["stage"] = "waiting_file"
        session["request_id"] = session.get("request_id") or uuid.uuid4().hex[:12]
        session["waiting_file_since"] = int(time.time() * 1000)
        clear_session_file_state(session)
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="等待上传文件",
            template="blue",
            detail="已进入 BLF 转 MDF 转换，等待上传 .blf 文件。",
        )
        send_or_update_result_card(chat_id, session, blf_to_mdf_upload_card(open_id, query, session.get("database") or ""))
        return True

    if tool == SKILL_REQUIREMENT_INTAKE:
        session["stage"] = "clarify_analysis_scope"
        session["active_skill"] = SKILL_REQUIREMENT_INTAKE
        session["pending_analysis_request_text"] = decision.tool_query or text
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="回传结果中",
            template="orange",
            detail="需求还不够明确，先确认真实目标，再切入具体能力。",
        )
        reply = decision.reply or (
            "这个需求可能有多个处理方向。请直接回复：`性能分析`、`执行器分析`、`故障诊断/故障查询`，"
            "或者 `两个都分析`。"
        )
        send_or_update_result_card(chat_id, session, requirement_intake_card(open_id, reply, text))
        return True

    if tool == SKILL_SMALLTALK_CHAT:
        session["stage"] = "collecting_requirements"
        session.pop("task_type", None)
        session.pop("request_id", None)
        session.pop("waiting_file_since", None)
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="回传结果中",
            template="blue",
            detail="这是日常沟通类消息，不调用本地诊断工具。",
        )
        reply = decision.reply or generate_group_reply(state_key(open_id, chat_id), text)
        reply = ensure_substantive_reply(text, reply, SKILL_SMALLTALK_CHAT, "日常沟通消息。")
        remember_direct_reply_result(session, SKILL_SMALLTALK_CHAT, text, reply, "布鲁", "blue")
        send_or_update_result_card(chat_id, session, assistant_reply_card(open_id, reply, "布鲁", "blue"))
        return True

    if tool == SKILL_FAULT_TABLE_SEARCH:
        raw_query = decision.tool_query or text
        query = contextualize_skill_query(session, tool, raw_query)
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="查询中",
            template="orange",
            detail="正在检索本地 FDA3/FDA7 故障表，并准备把结果整理成人话。",
        )
        local_context = answer_fault_reference_question(query, session.get("fault_reference_context", ""))
        if local_context:
            session["stage"] = "collecting_requirements"
            if should_remember_fault_reference_context(query, local_context):
                session["fault_reference_context"] = query
            update_progress_card(chat_id, open_id, session, stage="回传结果中", template="orange", detail="已拿到故障表匹配项，正在组织回复。")
            reference_reply = polish_fault_reference_answer(raw_query, local_context, local_context)
            remember_skill_result(
                session,
                SKILL_FAULT_TABLE_SEARCH,
                raw_query,
                local_context,
                reference_reply,
                "故障表查询",
                "orange",
            )
            transform_plan, generated_artifacts, uploaded_artifacts = prepare_result_transform_artifacts(
                chat_id,
                open_id,
                session,
                raw_query,
                SKILL_FAULT_TABLE_SEARCH,
                local_context,
                reference_reply,
            )
            try:
                send_or_update_result_card(
                    chat_id,
                    session,
                    assistant_reply_with_artifacts_card(
                        open_id,
                        reference_reply,
                        "故障表查询",
                        "orange",
                        transform_plan,
                        generated_artifacts,
                        uploaded_artifacts,
                    ),
                )
            finally:
                cleanup_transform_artifacts(generated_artifacts)
            return True
        reply = ensure_substantive_reply(
            raw_query,
            "",
            SKILL_FAULT_TABLE_SEARCH,
            "本地 FDA3/FDA7 故障表未返回明确匹配项。",
        )
        update_progress_card(chat_id, open_id, session, stage="回传结果中", template="orange", detail="本地未直接命中，正在给出可继续排查的方向。")
        send_or_update_result_card(chat_id, session, assistant_reply_card(open_id, reply, "故障表查询", "orange"))
        return True

    if tool == SKILL_DTC_CODE_SEARCH:
        raw_query = decision.tool_query or text
        query = contextualize_skill_query(session, tool, raw_query)
        session["stage"] = "collecting_requirements"
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="查询中",
            template="orange",
            detail="正在检索本地制动/驱动 DTC 表，并整理匹配结果。",
        )
        local_context = answer_dtc_question(query)
        reply = polish_dtc_reference_answer(raw_query, local_context, local_context)
        reply = ensure_substantive_reply(
            raw_query,
            reply,
            SKILL_DTC_CODE_SEARCH,
            local_context or "本地 DTC 表没有返回明确匹配项。",
        )
        remember_skill_result(
            session,
            SKILL_DTC_CODE_SEARCH,
            raw_query,
            local_context,
            reply,
            "DTC故障码查询",
            "orange",
        )
        transform_plan, generated_artifacts, uploaded_artifacts = prepare_result_transform_artifacts(
            chat_id,
            open_id,
            session,
            raw_query,
            SKILL_DTC_CODE_SEARCH,
            local_context,
            reply,
        )
        update_progress_card(chat_id, open_id, session, stage="回传结果中", template="orange", detail="已整理 DTC 查询结果，正在回传。")
        try:
            send_or_update_result_card(
                chat_id,
                session,
                assistant_reply_with_artifacts_card(
                    open_id,
                    reply,
                    "DTC故障码查询",
                    "orange",
                    transform_plan,
                    generated_artifacts,
                    uploaded_artifacts,
                ),
            )
        finally:
            cleanup_transform_artifacts(generated_artifacts)
        return True

    if tool == SKILL_PERFORMANCE_CAPABILITY_SEARCH:
        raw_query = decision.tool_query or text
        query = contextualize_skill_query(session, tool, raw_query)
        groups = decision.problem_groups or session.get("problem_groups") or []
        session["stage"] = "collecting_requirements"
        if decision.problem_groups:
            session["problem_groups"] = decision.problem_groups
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="查询中",
            template="blue",
            detail="正在读取性能指标说明和指标实现摘要，整理可查询的问题条目。",
        )
        local_context = answer_performance_capability_question(query, groups)
        reply = polish_performance_capability_answer(raw_query, local_context, local_context)
        reply = ensure_substantive_reply(
            raw_query,
            reply,
            SKILL_PERFORMANCE_CAPABILITY_SEARCH,
            local_context or f"本地性能问题组：{format_problem_group_scope(groups)}。",
        )
        remember_skill_result(
            session,
            SKILL_PERFORMANCE_CAPABILITY_SEARCH,
            raw_query,
            local_context,
            reply,
            "性能分析能力",
            "blue",
        )
        transform_plan, generated_artifacts, uploaded_artifacts = prepare_result_transform_artifacts(
            chat_id,
            open_id,
            session,
            raw_query,
            SKILL_PERFORMANCE_CAPABILITY_SEARCH,
            local_context,
            reply,
        )
        update_progress_card(chat_id, open_id, session, stage="回传结果中", template="blue", detail="已整理性能分析能力说明，正在回传。")
        try:
            send_or_update_result_card(
                chat_id,
                session,
                assistant_reply_with_artifacts_card(
                    open_id,
                    reply,
                    "性能分析能力",
                    "blue",
                    transform_plan,
                    generated_artifacts,
                    uploaded_artifacts,
                ),
            )
        finally:
            cleanup_transform_artifacts(generated_artifacts)
        return True

    if tool == SKILL_FAULT_DIAGNOSIS:
        session["task_type"] = "fault_degradation"
        if decision.project_type in {"8650", "BMC"} or decision.project_supported is False:
            session["stage"] = "collecting_requirements"
            reply = ensure_substantive_reply(
                text,
                "故障降级诊断目前只支持 J6B 项目，8650 和 BMC 还在开发中。性能问题分析不受这个限制，可以继续直接分析数据。",
                SKILL_FAULT_DIAGNOSIS,
                "本地故障诊断工具当前仅支持 J6B。",
            )
            update_progress_card(chat_id, open_id, session, stage="回传结果中", template="orange", detail="已确认项目范围，正在说明当前支持边界。")
            send_or_update_result_card(chat_id, session, assistant_reply_card(open_id, reply, "故障降级诊断", "orange"))
            return True
        if looks_like_fault_diagnosis_text(text):
            update_progress_card(chat_id, open_id, session, stage="问题分析中", template="orange", detail="识别到文本故障输入，正在解析故障表。")
            process_fault_diagnosis_input(chat_id, open_id, session, "inline_query", text=text)
            return True
        session["stage"] = "waiting_fault_input"
        session["request_id"] = session.get("request_id") or uuid.uuid4().hex[:12]
        session["waiting_file_since"] = int(time.time() * 1000)
        update_progress_card(chat_id, open_id, session, stage="回传结果中", template="orange", detail="需要补充故障输入，正在给出可上传/可粘贴的格式。")
        send_or_update_result_card(chat_id, session, fault_input_card(open_id))
        return True

    if tool == SKILL_BATCH_ROOT_CAUSE_ANALYSIS:
        query = decision.tool_query or text
        return run_batch_root_cause_command(chat_id, open_id, session, query)

    if tool == SKILL_KPI_REPORT_ANALYSIS:
        query = decision.tool_query or text
        return run_kpi_report_command(chat_id, open_id, session, query)

    if tool == SKILL_ROOT_CAUSE_ANALYSIS:
        query = clean_root_cause_request_text(decision.tool_query or text)
        session["task_type"] = COMBINED_ANALYSIS_TASK
        session["active_skill"] = SKILL_ROOT_CAUSE_ANALYSIS
        session["analysis_tasks"] = ["performance_analysis", "actuator_handshake"]
        session["request_text"] = query
        session["problem_groups"] = infer_root_cause_problem_groups(query, decision.problem_groups or session.get("problem_groups") or [])
        if not session.get("database"):
            session["database"] = latest_database_name()
        session["stage"] = "waiting_file"
        session["request_id"] = session.get("request_id") or uuid.uuid4().hex[:12]
        session["waiting_file_since"] = int(time.time() * 1000)
        clear_session_file_state(session)
        update_progress_card(chat_id, open_id, session, stage="回传结果中", template="blue", detail="已进入行车问题原因分析，等待上传 .h5 数据。")
        send_or_update_result_card(
            chat_id,
            session,
            combined_upload_card(open_id, query, session.get("database") or "", session.get("problem_groups") or []),
        )
        return True

    if tool == SKILL_FUNCTION_EXIT_ANALYSIS:
        query = decision.tool_query or text
        session["task_type"] = FUNCTION_EXIT_ANALYSIS_TASK
        session["active_skill"] = SKILL_FUNCTION_EXIT_ANALYSIS
        session["request_text"] = query
        if not function_exit_request_needs_file(query):
            session["stage"] = "completed"
            session["request_id"] = session.get("request_id") or uuid.uuid4().hex[:12]
            clear_session_file_state(session)
            reply = query_function_exit_reference(query)
            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="回传结果中",
                template="blue",
                detail="已按 ACC/LCC 功能退出原因配置完成查询。",
            )
            send_or_update_result_card(
                chat_id,
                session,
                assistant_reply_card(open_id, reply, "ACC/LCC功能退出原因", "blue"),
            )
            session["last_skill_result"] = {
                "skill": SKILL_FUNCTION_EXIT_ANALYSIS,
                "request": query,
                "text": reply,
            }
            return True
        session["stage"] = "waiting_file"
        session["request_id"] = session.get("request_id") or uuid.uuid4().hex[:12]
        session["waiting_file_since"] = int(time.time() * 1000)
        clear_session_file_state(session)
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="回传结果中",
            template="blue",
            detail="已进入 ACC/LCC 功能退出原因分析，等待上传 H5 数据。",
        )
        send_or_update_result_card(chat_id, session, function_exit_upload_card(open_id, query))
        return True

    if tool == SKILL_PERFORMANCE_ANALYSIS_SETUP:
        session["task_type"] = "performance_analysis"
        session["request_text"] = decision.tool_query or text
        if not session.get("database"):
            session["database"] = latest_database_name()
        session["stage"] = "waiting_file"
        session["request_id"] = session.get("request_id") or uuid.uuid4().hex[:12]
        session["waiting_file_since"] = int(time.time() * 1000)
        clear_session_file_state(session)
        if session.get("database"):
            update_progress_card(chat_id, open_id, session, stage="回传结果中", template="blue", detail="已完成性能分析配置，等待上传数据文件。")
            send_or_update_result_card(
                chat_id,
                session,
                smart_upload_card(open_id, "performance_analysis", session["database"], session.get("problem_groups") or []),
            )
        else:
            reply = ensure_substantive_reply(
                text,
                "我理解你要上传数据做分析，但当前没有可用数据库。你可以先上传 `.arxml` 数据库文件，或确认数据库目录是否已有文件。",
                SKILL_PERFORMANCE_ANALYSIS_SETUP,
                "性能分析需要数据库；本地 database 目录未找到可用 ARXML。",
            )
            update_progress_card(chat_id, open_id, session, stage="回传结果中", template="blue", detail="没有可用数据库，正在提示下一步。")
            send_or_update_result_card(chat_id, session, assistant_reply_card(open_id, reply, "性能问题分析", "blue"))
        return True

    if tool == SKILL_ACTUATOR_REFERENCE_SEARCH:
        session["task_type"] = "actuator_handshake"
        session["active_skill"] = SKILL_ACTUATOR_REFERENCE_SEARCH
        raw_query = decision.tool_query or text
        query = raw_query if is_broad_actuator_consultation(raw_query) else contextualize_skill_query(session, tool, raw_query)
        session["request_text"] = raw_query
        session["request_id"] = session.get("request_id") or uuid.uuid4().hex[:12]
        session["stage"] = "collecting_requirements"
        update_progress_card(
            chat_id,
            open_id,
            session,
            stage="查询中",
            template="blue",
            detail="正在检索执行器原始接口文档和本地索引，提取相关信号、条件和设计要求。",
        )
        local_context = answer_actuator_query(query)
        reply = polish_actuator_reference_answer(raw_query, local_context, local_context)
        reply = ensure_substantive_reply(
            text,
            reply,
            SKILL_ACTUATOR_REFERENCE_SEARCH,
            local_context or "本地执行器知识库没有返回明确匹配项。",
        )
        remember_skill_result(
            session,
            SKILL_ACTUATOR_REFERENCE_SEARCH,
            raw_query,
            local_context,
            reply,
            "执行器查询",
            "blue",
        )
        transform_plan, generated_artifacts, uploaded_artifacts = prepare_result_transform_artifacts(
            chat_id,
            open_id,
            session,
            raw_query,
            SKILL_ACTUATOR_REFERENCE_SEARCH,
            local_context,
            reply,
        )
        update_progress_card(chat_id, open_id, session, stage="回传结果中", template="blue", detail="已整理执行器查询结果，正在回传。")
        try:
            send_or_update_result_card(
                chat_id,
                session,
                assistant_reply_with_artifacts_card(
                    open_id,
                    reply,
                    "执行器查询",
                    "blue",
                    transform_plan,
                    generated_artifacts,
                    uploaded_artifacts,
                ),
            )
        finally:
            cleanup_transform_artifacts(generated_artifacts)
        return True

    if tool == SKILL_ACTUATOR_HANDSHAKE_ANALYSIS:
        session["task_type"] = "actuator_handshake"
        query = decision.tool_query or text
        session["request_text"] = query
        session["request_id"] = session.get("request_id") or uuid.uuid4().hex[:12]
        if not session.get("database"):
            session["database"] = latest_database_name()
        session["stage"] = "waiting_file"
        session["waiting_file_since"] = int(time.time() * 1000)
        clear_session_file_state(session)
        update_progress_card(chat_id, open_id, session, stage="回传结果中", template="blue", detail="已完成执行器分析配置，等待上传总线数据。")
        send_or_update_result_card(chat_id, session, actuator_upload_card_v2(open_id, query, session.get("database") or ""))
        return True

    if decision.action in {"reply", "ask"}:
        update_progress_card(chat_id, open_id, session, stage="回传结果中", template="blue", detail="不需要调用本地诊断工具，正在生成直接回复。")
        reply = decision.reply or generate_group_reply(state_key(open_id, chat_id), text)
        reply = ensure_substantive_reply(
            text,
            reply,
            decision.tool,
            "模型给出了 reply/ask，但没有调用本地工具。",
        )
        remember_direct_reply_result(session, decision.tool or SKILL_SMALLTALK_CHAT, text, reply, "布鲁", "blue")
        send_or_update_result_card(chat_id, session, assistant_reply_card(open_id, reply, "布鲁", "blue"))
        return True

    return False


def ensure_substantive_reply(user_text, reply, tool_name=None, local_context=""):
    if is_empty_progress_reply(reply):
        return generate_tool_fallback_reply(user_text, tool_name, local_context)
    return reply


def is_empty_progress_reply(reply):
    value = (reply or "").strip()
    if not value:
        return True
    compact = re.sub(r"[\s，。,.!！~～]", "", value)
    progress_tokens = [
        "我来查一下",
        "我查一下",
        "稍等",
        "等一下",
        "我帮你查",
        "我先查",
        "我看看",
    ]
    if any(token in value for token in progress_tokens) and len(compact) <= 45:
        return True
    no_content_patterns = [
        "我这边暂时没生成可用回复",
        "没有找到明确结果",
        "没找到足够明确的匹配",
    ]
    return any(pattern in value for pattern in no_content_patterns) and len(compact) <= 60










def session_status_text(open_id, session):
    stage_names = {
        "collecting_requirements": "需求收集中",
        "choose_task": "需求收集中",
        "choose_database": "需求收集中",
        "waiting_file": "等待上传文件",
        "waiting_fault_input": "等待故障诊断输入",
        "processing": "处理中",
        "completed": "已完成",
    }
    lines = [
        f"<at user_id=\"{open_id}\"></at> 当前请求状态：{stage_names.get(session.get('stage'), session.get('stage'))}",
    ]
    if session.get("task_type"):
        lines.append(f"工作类型：{task_label(session['task_type'])}")
    if session.get("database"):
        lines.append(f"数据库：{session['database']}")
    if session.get("file_name"):
        lines.append(f"文件：{session['file_name']}")

    stage = session.get("stage")
    if stage == "waiting_file":
        lines.append("下一步：直接上传 1 个 `.blf`、`.h5` 或 `.hdf5` 文件附件；如果刚上传过，也可以回一句“我刚上传了”。")
    elif stage == "waiting_fault_input":
        lines.append("下一步：直接发 `slog`、DID、`fault_event`、`62 FD A3/A7` 字节串，或像 `FDA3[113]` 这样直接查询。")
    elif stage == "processing":
        lines.append("当前已进入处理阶段，我拿到结果后会直接回你。")

    lines.append("取消当前请求：`@布鲁 取消`")
    return "\n".join(lines)


def is_explicit_session_status_query(text: str) -> bool:
    value = (text or "").strip()
    compact = re.sub(r"\s+", "", value.lower())
    if not compact:
        return False

    exact_status_words = {
        "状态",
        "进度",
        "当前状态",
        "现在状态",
        "处理进度",
        "分析进度",
        "到哪了",
        "跑完了吗",
        "结束了吗",
        "好了吗",
    }
    if compact in exact_status_words:
        return True

    business_context_terms = {
        "apa",
        "acc",
        "aeb",
        "cib",
        "ebcm",
        "ebooster",
        "eps",
        "etrs",
        "epb",
        "pdf",
        "ecp",
        "ecm",
        "制动",
        "刹车",
        "驱动",
        "转向",
        "换挡",
        "换档",
        "挡位",
        "档位",
        "请求",
        "释放",
        "响应",
        "反馈",
        "流程",
        "跳转",
        "流转",
        "条件",
        "置位",
    }
    if any(term in compact for term in business_context_terms):
        return False

    status_patterns = [
        "当前请求状态",
        "本次请求状态",
        "处理到哪",
        "分析到哪",
        "跑到哪",
        "还有多久",
        "结果出来了吗",
        "处理完了吗",
        "分析完了吗",
        "好了没",
        "还在跑吗",
        "卡住了吗",
    ]
    return any(pattern in compact for pattern in status_patterns)


def should_restart_for_clear_actuator_query(session, text: str) -> bool:
    if not is_actuator_reference_query_text(text):
        return False
    if session.get("active_skill") == SKILL_ACTUATOR_REFERENCE_SEARCH:
        return False
    return session.get("stage") in {
        "collecting_requirements",
        "waiting_file",
        "waiting_fault_input",
        "completed",
    }


def processing_followup_reply(open_id, session, text):
    normalized = (text or "").strip().lower()
    task_type = session.get("task_type")
    file_name = session.get("file_name")

    if any(token in normalized for token in ["结果", "结论", "还没好", "多久", "进度", "怎么样了"]):
        if task_type == "fault_degradation":
            return f'<at user_id="{open_id}"></at> 故障诊断已经在跑了。我正在解析 `{file_name or "当前输入"}`，有结果会直接回你。'
        if task_type == "performance_analysis":
            return f'<at user_id="{open_id}"></at> 性能分析已经开始了。我正在处理 `{file_name or "当前文件"}`，结果出来后会直接把重点结论和图示发给你。'

    if any(token in normalized for token in ["收到", "看到了吗", "附件", "文件"]):
        if file_name:
            return f'<at user_id="{open_id}"></at> 已经记录到文件 `{file_name}`，当前正在处理。'

    return f'<at user_id="{open_id}"></at> 正在处理当前请求，请稍等结果返回。'

def smart_request_card(open_id, session):
    current_task = session.get("task_type")
    current_database = session.get("database")
    current_problem_groups = session.get("problem_groups") or []
    capability_text = clean_plain_reply(build_capability_reply(current_problem_groups, current_task))
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "<font color='blue'>**当前阶段：需求说明**</font>",
        "",
        "直接告诉我你想查什么、分析什么，或上传对应数据。我会先理解需求，再按当前能力配置选择合适流程。",
        "",
        capability_text,
    ]
    if current_task or current_database or current_problem_groups:
        lines.extend(["", "**我已经记录到的信息**"])
        if current_task:
            lines.append(f"- 工作类型：**{task_label(current_task)}**")
        if current_database:
            lines.append(f"- 数据库：`{current_database}`")
        if current_problem_groups:
            lines.append(f"- 问题范围：{format_problem_group_scope(current_problem_groups)}")
    lines.extend(["", "**其他操作**", "- `@布鲁 取消` 取消本次请求"])
    return markdown_card("智能引导", "\n".join(lines), "blue")

def smart_upload_card(open_id, task_type, database, problem_groups):
    content = (
        f"**处理对象**：<at id={open_id}></at>\n"
        "<font color='blue'>**当前阶段：等待上传分析文件**</font>\n\n"
        "**本次配置**\n"
        f"- 工作类型：**{task_label(task_type)}**\n"
        f"- 数据库：`{database}`\n"
        f"- 问题范围：{format_problem_group_scope(problem_groups)}\n\n"
        "**下一步**\n"
        "- 请直接在当前群里上传 1 个数据文件附件\n"
        "- 支持：`.blf`、`.h5`、`.hdf5`\n"
        "- 上传后我会自动开始分析，不需要再回复“确认上传”\n\n"
        "**数据库可调整**\n"
        f"- 当前使用：`{database}`\n"
        "- 如果想换数据库，直接重新说明版本，或上传新的 `.arxml` 文件\n\n"
        "**其他操作**\n"
        "- `@布鲁 返回` 重新说明需求或更换数据库\n"
        "- `@布鲁 取消` 取消本次请求"
    )
    return markdown_card("请上传文件", content, "blue")


def actuator_upload_card(open_id, request_text="", database_name=""):
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "<font color='blue'>**当前阶段：等待上传执行器握手诊断数据**</font>",
        "",
        "**本次任务**",
        "- 工作类型：**执行器握手诊断**",
        "- 支持对象：制动系统（EBCM / ebooster）、驱动系统（PDF / ECP / ECM）",
    ]
    if request_text:
        lines.append(f"- 用户需求：{request_text}")
    if database_name:
        lines.append(f"- 当前数据库：`{database_name}`")
    lines.extend(
        [
            "",
            "**下一步**",
            "- 请直接在当前群里上传 1 个总线数据附件",
            "- 当前支持：`.blf`、`.h5`、`.hdf5`",
            "- 如果上传的是 `.blf`，我会使用当前数据库解码；如需指定版本，可先上传 `.arxml`",
            "- 上传后我会自动开始检查断握手、功能降级、状态未按设计闭环等问题",
            "",
            "**也可以直接查询**",
            "- 例如：`导致制动断握手的有哪些原因`",
            "- 例如：`驱动系统 ACC 握手失败要看哪些信号`",
            "",
            "**其他操作**",
            "- `@布鲁 取消` 取消本次请求",
        ]
    )
    return markdown_card("执行器握手诊断", "\n".join(lines), "blue")

def start_session(chat_id, open_id, initial_text="", referenced_file=None):
    key = state_key(open_id, chat_id)
    clear_history(key)
    sessions[key] = {
        "stage": "collecting_requirements",
        "chat_id": chat_id,
        "open_id": open_id,
        "message_count": 0,
        "request_started_ms": int(time.time() * 1000),
    }
    if referenced_file:
        attach_referenced_file(sessions[key], referenced_file)
    if initial_text.strip():
        handle_agent_decision(chat_id, open_id, sessions[key], initial_text)
        maybe_process_referenced_file(chat_id, open_id, sessions[key], referenced_file)
    else:
        send_card(chat_id, smart_request_card(open_id, sessions[key]))


def apply_workflow_decision(chat_id, open_id, session, text):
    db_names = [label for label, _ in database_options()]
    decision = analyze_workflow_request(
        text,
        db_names,
        current_task=session.get("task_type"),
        current_database=session.get("database"),
        current_problem_groups=session.get("problem_groups"),
    )

    if decision.database_name:
        session["database"] = decision.database_name
    if decision.problem_groups:
        session["problem_groups"] = decision.problem_groups
    if decision.project_type:
        session["project_type"] = decision.project_type

    print(
        "[workflow-main] "
        f"user={open_id} chat={chat_id} text={text!r} "
        f"intent={decision.intent_type} task={decision.task_type} "
        f"project={decision.project_type} supported={decision.project_supported} "
        f"db={decision.database_name} groups={decision.problem_groups}",
        flush=True,
    )

    if decision.task_type == "fault_degradation" and decision.intent_type in {"capability_question", "clarify"}:
        local_context = answer_fault_reference_question(text, session.get("fault_reference_context", ""))
        if local_context:
            session["stage"] = "collecting_requirements"
            if any(token in text.lower() for token in ["fda3", "fda7", "环视", "相机", "鱼眼", "前视", "后视", "左视", "右视", "camera", "svs"]):
                session["fault_reference_context"] = text
            reference_reply = polish_fault_reference_answer(text, local_context, local_context)
            send_assistant_reply(chat_id, open_id, reference_reply, "故障表查询", "orange")
            return

    if decision.intent_type == "capability_question":
        session["stage"] = "collecting_requirements"
        session.pop("task_type", None)
        session.pop("request_id", None)
        session.pop("waiting_file_since", None)
        session.pop("file_name", None)
        session.pop("file_key", None)
        session.pop("raw_file_content", None)
        session.pop("message_id", None)
        session.pop("message_type", None)
        send_card(
            chat_id,
            capability_reply_card(
                open_id,
                decision.reply,
                session.get("problem_groups") or [],
                session.get("database"),
            ),
        )
        return

    if decision.task_type:
        session["task_type"] = decision.task_type

    task_type = session.get("task_type")
    database = session.get("database")
    problem_groups = session.get("problem_groups") or []

    if task_type == "performance_analysis" and not database and not user_mentions_database_preference(text):
        database = latest_database_name()
        if database:
            session["database"] = database

    if task_type == "actuator_handshake":
        session["request_text"] = text
        if actuator_request_needs_file(text):
            session["active_skill"] = SKILL_ACTUATOR_HANDSHAKE_ANALYSIS
            if not session.get("database"):
                session["database"] = latest_database_name()
            session["stage"] = "waiting_file"
            session["request_id"] = session.get("request_id") or uuid.uuid4().hex[:12]
            session["waiting_file_since"] = int(time.time() * 1000)
            clear_session_file_state(session)
            send_card(chat_id, actuator_upload_card_v2(open_id, text, session.get("database") or ""))
        else:
            session["active_skill"] = SKILL_ACTUATOR_REFERENCE_SEARCH
            session["stage"] = "collecting_requirements"
            reply = answer_actuator_query(text)
            send_assistant_reply(chat_id, open_id, reply, "执行器查询", "blue")
        return

    if task_type == "fault_degradation" and decision.project_supported is False:
        session["stage"] = "collecting_requirements"
        project_label = decision.project_type or session.get("project_type") or "未识别项目"
        send_card(
            chat_id,
            markdown_card(
                "项目暂不支持",
                (
                    f"**处理对象**：<at id={open_id}></at>\n"
                    "<font color='orange'>**当前阶段：项目暂不支持**</font>\n\n"
                    f"**识别到的项目**：{project_label}\n"
                    "**对应能力**：故障降级诊断\n\n"
                    "目前故障降级诊断只支持 **J6B**，**8650** 和 **BMC** 还在开发中。\n"
                    "你可以继续直接描述真实需求，我会按当前能力配置重新判断可用入口。"
                ),
                "yellow",
            ),
        )
        return

    if decision.intent_type == "dev_request":
        session["stage"] = "collecting_requirements"
        send_assistant_reply(chat_id, open_id, decision.reply)
        return

    if task_type == "fault_degradation":
        if looks_like_fault_diagnosis_text(text):
            process_fault_diagnosis_input(chat_id, open_id, session, "inline_query", text=text)
            return
        session["stage"] = "waiting_fault_input"
        session["request_id"] = uuid.uuid4().hex[:12]
        session["waiting_file_since"] = int(time.time() * 1000)
        session.pop("file_name", None)
        session.pop("file_key", None)
        session.pop("raw_file_content", None)
        session.pop("message_id", None)
        session.pop("message_type", None)
        send_card(chat_id, fault_input_card(open_id))
        return

    if task_type == "performance_analysis" and session.get("database"):
        session["stage"] = "waiting_file"
        session["request_id"] = uuid.uuid4().hex[:12]
        session["waiting_file_since"] = int(time.time() * 1000)
        clear_session_file_state(session)
        send_card(chat_id, smart_upload_card(open_id, task_type, session["database"], problem_groups))
        return

    if task_type == "performance_analysis" and not session.get("database"):
        session["stage"] = "collecting_requirements"
        send_text(
            chat_id,
            f'<at user_id="{open_id}"></at> 这次需要数据分析，但我这边还没有确定数据库。你可以直接说数据库版本；如果现有数据库不满足需求，也可以直接上传新的 `.arxml` 文件。',
        )
        return

    session["stage"] = "collecting_requirements"
    send_assistant_reply(chat_id, open_id, decision.reply)

def process_uploaded_file(chat_id, open_id, session):
    file_name = session.get("file_name")
    if not file_name:
        return

    task_type = session.get("task_type")
    if task_type == "fault_degradation":
        local_file = None
        try:
            start_progress_card(
                chat_id,
                open_id,
                session,
                stage="已收到文件",
                template="orange",
                detail="已收到上传文件，正在下载并准备故障诊断。",
                force_new=True,
            )
            session_token = state_key(open_id, chat_id).replace(":", "_")
            session_token = f"{session_token}_{session.get('request_id', uuid.uuid4().hex[:12])}"
            local_file = download_uploaded_file(
                rest_client,
                session,
                session_token,
                progress_callback=make_download_progress_callback(chat_id, open_id, session, template="blue"),
            )
            process_fault_diagnosis_input(chat_id, open_id, session, file_name, local_file=local_file)
        except Exception as exc:
            send_or_update_result_card(
                chat_id,
                session,
                markdown_card(
                    "故障降级诊断失败",
                    (
                        f"**处理对象**：<at id={open_id}></at>\n"
                        "<font color='red'>**当前阶段：诊断失败**</font>\n\n"
                        f"**输入来源**：`{file_name}`\n"
                        f"**失败原因**：\n{format_exception_for_card(exc)}"
                    ),
                    "red",
                ),
            )
            session["stage"] = "completed"
            if local_file is not None:
                try:
                    local_file.unlink(missing_ok=True)
                    shutil.rmtree(local_file.parent, ignore_errors=True)
                except Exception:
                    pass
        return

    session["stage"] = "processing"
    problem_groups = session.get("problem_groups") or []
    if task_type == AXLE_TORQUE_ACCURACY_TASK:
        local_file = None
        try:
            if not _is_blf_file(file_name):
                raise ValueError("轴端扭矩准确性校验只接受 `.blf` 文件。")

            arxml_path = resolve_axle_torque_arxml_from_session(session)
            database_name = arxml_path.name
            try:
                vehicle_config, final_drive_ratio = require_axle_torque_vehicle_config(session)
            except ValueError:
                session["stage"] = "waiting_axle_torque_vehicle"
                send_or_update_result_card(
                    chat_id,
                    session,
                    axle_torque_upload_card(
                        open_id,
                        session.get("request_text") or "",
                        session.get("database") or database_name,
                        "",
                        None,
                    ),
                )
                return

            start_progress_card(
                chat_id,
                open_id,
                session,
                stage="已收到文件",
                template="blue",
                detail="已收到 BLF 文件，正在下载并准备做轴端扭矩复算。",
                force_new=True,
            )
            session_token = state_key(open_id, chat_id).replace(":", "_")
            session_token = f"{session_token}_{session.get('request_id', uuid.uuid4().hex[:12])}"
            local_file = download_uploaded_file(
                rest_client,
                session,
                session_token,
                progress_callback=make_download_progress_callback(chat_id, open_id, session, template="blue"),
            )
            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="正在分析",
                template="blue",
                detail=f"BLF 已下载完成，车型 {vehicle_config}（主减速比 {final_drive_ratio:g}），正在解码目标报文并复算轴端扭矩。",
            )
            result = run_axle_torque_accuracy_analysis(
                local_file,
                arxml_path=arxml_path,
                vehicle_config=vehicle_config,
                final_drive_ratio=final_drive_ratio,
                progress_callback=make_axle_torque_analysis_progress_callback(chat_id, open_id, session, template="blue"),
            )
            image_items = []
            if result.plot_path and Path(result.plot_path).exists():
                update_progress_card(
                    chat_id,
                    open_id,
                    session,
                    stage="生成图示",
                    template="green" if result.status == "ok" else ("red" if result.status == "issue" else "orange"),
                    detail="轴端扭矩复算已完成，正在上传对比图示。",
                )
                image_items = _upload_card_images([("轴端扭矩复算对比", Path(result.plot_path))])

            session["stage"] = "completed"
            template = "green" if result.status == "ok" else ("red" if result.status == "issue" else "orange")
            remember_skill_result(
                session,
                SKILL_AXLE_TORQUE_ACCURACY_ANALYSIS,
                session.get("request_text") or "",
                format_axle_torque_markdown(open_id, file_name, result),
                result.summary,
                "轴端扭矩准确性校验结果",
                template,
            )
            send_or_update_result_card(
                chat_id,
                session,
                axle_torque_result_card(open_id, file_name, result, image_items=image_items),
            )
            clear_axle_torque_vehicle_selection(session)
        except Exception as exc:
            session["stage"] = "waiting_file"
            send_or_update_result_card(
                chat_id,
                session,
                axle_torque_error_card(open_id, file_name, session.get("database") or "", exc),
            )
        finally:
            if local_file is not None:
                try:
                    upload_root = get_upload_dir().resolve()
                    work_dir = local_file.parent.resolve()
                    if upload_root in work_dir.parents:
                        shutil.rmtree(work_dir, ignore_errors=True)
                    else:
                        local_file.unlink(missing_ok=True)
                except Exception:
                    pass
        return

    if task_type == BLF_TO_MDF_CONVERSION_TASK:
        local_file = None
        file_sent = False
        delivered_path = None
        delivered_as_zip = False
        try:
            if not _is_blf_file(file_name):
                raise ValueError("BLF 转 MDF 只接受 `.blf` 文件。")

            database_name = session.get("database") or blf_to_mdf_default_database_name()
            if not database_name:
                raise RuntimeError("当前没有可用 `.arxml` 数据库，请先上传或指定数据库。")
            session["database"] = database_name
            database_path = Path(database_name)
            if not database_path.is_absolute():
                database_path = DATABASE_DIR / database_name
            if not database_path.exists() or database_path.stat().st_size <= 0:
                raise FileNotFoundError(f"解析数据库不存在或为空：{database_path}")

            start_progress_card(
                chat_id,
                open_id,
                session,
                stage="已收到文件",
                template="blue",
                detail="已收到 BLF 文件，正在下载并准备转换为 MDF。",
                force_new=True,
            )
            session_token = state_key(open_id, chat_id).replace(":", "_")
            session_token = f"{session_token}_{session.get('request_id', uuid.uuid4().hex[:12])}"
            local_file = download_uploaded_file(
                rest_client,
                session,
                session_token,
                progress_callback=make_download_progress_callback(chat_id, open_id, session, template="blue"),
            )
            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="正在分析",
                template="blue",
                detail="BLF 已下载完成，正在调用转换工具生成 MDF。",
            )
            conversion_output_dir = ensure_writable_dir(
                get_runtime_subdir("blf_mdf") / f"{safe_runtime_name(local_file.stem, 'blf')}_{uuid.uuid4().hex[:8]}"
            )
            result = run_blf_to_mdf_conversion(
                local_file,
                database_path,
                output_dir=conversion_output_dir,
            )
            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="回传结果中",
                template="green",
                detail=(
                    f"MDF 已生成，正在回传 `{result.output_path.name}`。"
                    "如果飞书直传受限，会自动改为 zip 回传。"
                ),
            )
            try:
                delivered_path, delivered_as_zip = send_file_with_zip_fallback(chat_id, result.output_path)
                file_sent = True
            except Exception as upload_exc:
                size_text = format_file_size(result.output_size) or f"{result.output_size} B"
                raise RuntimeError(
                    f"MDF 文件已生成（{result.output_path.name}，{size_text}），但回传到飞书失败："
                    f"{format_exception_for_card(upload_exc)}"
                ) from upload_exc
            session["stage"] = "completed"
            delivery_note = f"回传文件：{delivered_path.name}" if delivered_path else "回传文件：已发送"
            if delivered_as_zip:
                delivery_note += "（zip 仅用于传输，解压后是未压缩 MDF）"
            remember_skill_result(
                session,
                SKILL_BLF_TO_MDF_CONVERSION,
                session.get("request_text") or "",
                (
                    f"输入 BLF：{file_name}\n"
                    f"解析数据库：{database_name}\n"
                    f"输出 MDF：{result.output_path.name}\n"
                    f"{delivery_note}"
                ),
                f"BLF 转 MDF 已完成，{delivery_note}",
                "BLF 转 MDF 完成",
                "green",
            )
            try:
                send_or_update_result_card(
                    chat_id,
                    session,
                    blf_to_mdf_result_card(
                        open_id,
                        database_name,
                        file_name,
                        result,
                        delivered_path=delivered_path,
                        delivered_as_zip=delivered_as_zip,
                    ),
                )
            except Exception as card_exc:
                print(f"BLF to MDF completion card failed after file sent: {card_exc}", flush=True)
                try:
                    done_text = "BLF 转 MDF 已完成，MDF 文件已回传。"
                    if delivered_as_zip:
                        done_text = "BLF 转 MDF 已完成，zip 已回传；解压后是未压缩 MDF。"
                    send_message(
                        chat_id,
                        "text",
                        {"text": done_text},
                    )
                except Exception:
                    pass
        except Exception as exc:
            if file_sent:
                session["stage"] = "completed"
                print(f"BLF to MDF post-upload handling failed: {exc}", flush=True)
                return
            session["stage"] = "waiting_file"
            send_or_update_result_card(
                chat_id,
                session,
                blf_to_mdf_error_card(open_id, file_name, session.get("database") or "", exc),
            )
        finally:
            if local_file is not None:
                try:
                    upload_root = get_upload_dir().resolve()
                    work_dir = local_file.parent.resolve()
                    if upload_root in work_dir.parents:
                        shutil.rmtree(work_dir, ignore_errors=True)
                    else:
                        local_file.unlink(missing_ok=True)
                except Exception:
                    pass
        return

    if task_type == COMBINED_ANALYSIS_TASK:
        if not _is_root_cause_data_file(file_name):
            send_or_update_result_card(
                chat_id,
                session,
                markdown_card(
                    "行车问题原因分析文件格式不支持",
                    (
                        f"**处理对象**：<at id={open_id}></at>\n"
                        "<font color='orange'>**当前阶段：等待 `.h5` 数据**</font>\n\n"
                        f"**收到文件**：`{file_name}`\n\n"
                        "行车问题原因分析当前仅支持标准 `.h5` 数据文件。"
                        "如果手头是 `.blf`、`.hdf5` 或其他格式，请先转换成布鲁可读取的 `.h5` 后再上传。"
                    ),
                    "orange",
                ),
            )
            session["stage"] = "waiting_file"
            return

        request_text = clean_root_cause_request_text(session.get("request_text") or "")
        if request_text:
            session["request_text"] = request_text
        problem_type = infer_upload_root_cause_problem_type(request_text)
        start_progress_card(
            chat_id,
            open_id,
            session,
            stage="已收到文件",
            template="blue",
            detail="已收到上传文件，正在下载并准备行车问题原因分析。",
            force_new=True,
        )
        local_file = None
        single_result = None
        try:
            if not problem_type:
                allowed = "、".join(allowed_problem_types())
                raise ValueError(
                    "没有从用户描述中识别出明确的问题类型。"
                    f"请在上传前说明问题属于哪一类，例如：{allowed}。"
                )

            session_token = state_key(open_id, chat_id).replace(":", "_")
            session_token = f"{session_token}_{session.get('request_id', uuid.uuid4().hex[:12])}"
            local_file = download_uploaded_file(
                rest_client,
                session,
                session_token,
                progress_callback=make_download_progress_callback(chat_id, open_id, session, template="blue"),
            )
            if not local_file.exists() or local_file.stat().st_size <= 0:
                raise RuntimeError(f"下载后的文件为空或不存在：{local_file.name}")

            database_name = session.get("database") or latest_database_name() or ""
            if database_name:
                session["database"] = database_name

            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="正在分析",
                template="blue",
                detail=f"已识别问题类型为 `{problem_type}`，正在调用与批量表格一致的单条分析核心。",
            )
            single_result = run_single_batch_analysis(
                data_path=local_file,
                problem_type=problem_type,
                problem_description=request_text,
                database_name=database_name,
            )
            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="回传结果中",
                template="blue",
                detail="分析已完成，正在上传关键信号图示并整理结果卡片。",
            )
            image_items = _upload_card_images(
                [
                    (f"关键信号图 {index}", image_path)
                    for index, image_path in enumerate(single_result.image_paths, start=1)
                ]
            )
            template = "red" if single_result.status in {"issue", "error", "failed"} else "green"
            send_or_update_result_card(
                chat_id,
                session,
                single_root_cause_analysis_result_card(
                    open_id,
                    database=database_name,
                    file_name=file_name,
                    request_text=request_text,
                    problem_type=problem_type,
                    result=single_result,
                    image_items=image_items,
                    template=template,
                ),
            )
            remember_skill_result(
                session,
                SKILL_ROOT_CAUSE_ANALYSIS,
                request_text,
                single_result.context or single_result.conclusion,
                single_result.summary or "行车问题原因分析已完成，结果见上一张卡片。",
                "行车问题原因分析结果",
                template,
            )
        except BatchDeepSeekAPIUnavailable as exc:
            unavailable_message = str(exc) or get_llm_api_unavailable_message()
            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="已取消",
                template="red",
                detail=unavailable_message,
            )
            send_text(chat_id, f'<at user_id="{open_id}"></at> {unavailable_message}')
        except Exception as exc:
            send_or_update_result_card(
                chat_id,
                session,
                markdown_card(
                    "行车问题原因分析失败",
                    (
                        f"**处理对象**：<at id={open_id}></at>\n"
                        "<font color='red'>**当前阶段：分析失败**</font>\n\n"
                        f"**上传文件**：`{file_name}`\n"
                        f"**问题类型**：`{problem_type or '未识别'}`\n"
                        f"**失败原因**：\n{format_exception_for_card(exc)}"
                    ),
                    "red",
                ),
            )
        finally:
            session["stage"] = "completed"
            cleanup_analysis_artifacts(local_file, None)
            _cleanup_single_root_cause_artifacts(single_result)
        return

        start_progress_card(
            chat_id,
            open_id,
            session,
            stage="已收到文件",
            template="blue",
            detail="已收到上传文件，正在下载并准备行车问题原因分析。",
            force_new=True,
        )
        local_file = None
        performance_result = None
        actuator_result = None
        root_cause_report = None
        performance_error = None
        actuator_error = None
        try:
            session_token = state_key(open_id, chat_id).replace(":", "_")
            session_token = f"{session_token}_{session.get('request_id', uuid.uuid4().hex[:12])}"
            local_file = download_uploaded_file(
                rest_client,
                session,
                session_token,
                progress_callback=make_download_progress_callback(chat_id, open_id, session, template="blue"),
            )
            if not local_file.exists() or local_file.stat().st_size <= 0:
                raise RuntimeError(f"下载后的文件为空或不存在：{local_file.name}")

            database_name = session.get("database") or latest_database_name()
            if database_name:
                session["database"] = database_name
            database_path = None
            if database_name:
                candidate = DATABASE_DIR / database_name
                if candidate.exists():
                    database_path = candidate
            problem_groups = infer_root_cause_problem_groups(
                session.get("request_text") or "",
                problem_groups,
            )
            session["problem_groups"] = problem_groups

            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="正在分析",
                template="blue",
                detail="正在运行性能问题分析，先定位顿挫、重制动、溜车、设速等控制表现异常。",
            )
            try:
                if not database_name:
                    raise RuntimeError("性能问题分析需要 `.arxml` 数据库，但当前没有可用数据库。")
                performance_result = run_performance_analysis(
                    local_file,
                    database_name,
                    problem_groups,
                    request_text=session.get("request_text") or "",
                )
                performance_result.polished_summary = polish_analysis_summary(
                    selected_problem_groups=problem_groups,
                    status=performance_result.status,
                    problem_titles=[metric_title(issue.metric_name) for issue in performance_result.problems],
                    event_count=sum(len(issue.events) for issue in performance_result.problems),
                    image_count=sum(len(issue.image_paths) for issue in performance_result.problems),
                    warnings=performance_result.warnings,
                    fallback_summary=performance_result.summary,
                )
            except Exception as exc:
                performance_error = str(exc)

            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="问题分析中",
                template="blue",
                detail="性能侧检查结束，正在继续检查执行器请求/响应、断握手、降级和状态闭环。",
            )
            try:
                if _file_suffix(file_name) == ".blf" and database_path is None:
                    raise RuntimeError("执行器分析处理 `.blf` 需要 `.arxml` 数据库，但当前没有找到可用数据库。")
                safe_data_stem = safe_runtime_name(local_file.stem, "data")
                actuator_artifact_root = ensure_writable_dir(
                    get_runtime_subdir("actuator_handshake") / f"{safe_data_stem}_{uuid.uuid4().hex[:8]}"
                )
                actuator_result = run_actuator_handshake_analysis_with_database(
                    local_file,
                    session.get("request_text") or "",
                    database_path=database_path,
                    artifact_root=actuator_artifact_root,
                )
            except Exception as exc:
                actuator_error = str(exc)

            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="原因分析中",
                template="blue",
                detail="两侧分析已完成，正在汇总证据并判断更像执行器响应侧还是算法控制侧。",
            )
            root_cause_report = build_root_cause_report(
                request_text=session.get("request_text") or "",
                performance_result=performance_result,
                performance_error=performance_error,
                actuator_result=actuator_result,
                actuator_error=actuator_error,
            )
            safe_data_stem = safe_runtime_name(local_file.stem, "data")
            root_cause_image_root = ensure_writable_dir(
                get_runtime_subdir("root_cause_evidence") / f"{safe_data_stem}_{uuid.uuid4().hex[:8]}"
            )
            root_cause_report.evidence_plots = build_root_cause_evidence_plots(
                h5_file=local_file,
                request_text=session.get("request_text") or "",
                performance_result=performance_result,
                actuator_result=actuator_result,
                image_root=root_cause_image_root,
            )
            image_items = []
            for evidence_plot in root_cause_report.evidence_plots:
                try:
                    image_items.append(
                        {
                            "title": f"归因证据：{evidence_plot.title}",
                            "image_key": upload_image_key(evidence_plot.path),
                        }
                    )
                except Exception as image_exc:
                    print(f"上传归因证据图失败 {evidence_plot.path} | {image_exc}", flush=True)
            if performance_result:
                image_items.extend(
                    _upload_card_images(
                        [
                            (f"性能：{metric_title(issue.metric_name)}", image_path)
                            for issue in performance_result.problems
                            for image_path in issue.image_paths
                        ]
                    )
                )
            if actuator_result:
                for image_index, image_path in enumerate(actuator_result.image_paths, start=1):
                    title = "执行器：接口信号时序"
                    if actuator_result.violations and image_index <= len(actuator_result.violations):
                        title = f"执行器：{human_rule_title(actuator_result.violations[image_index - 1].rule)}"
                    try:
                        image_items.append({"title": title, "image_key": upload_image_key(image_path)})
                    except Exception as image_exc:
                        print(f"上传执行器图示失败 {image_path} | {image_exc}", flush=True)

            template = "red" if root_cause_report and root_cause_report.status in {"issue", "error"} else "green"
            send_or_update_result_card(
                chat_id,
                session,
                combined_analysis_result_card(
                    open_id,
                    database_name or "",
                    file_name,
                    session.get("request_text") or "",
                    performance_result=performance_result,
                    performance_error=performance_error,
                    actuator_result=actuator_result,
                    actuator_error=actuator_error,
                    root_cause_report=root_cause_report,
                    image_items=image_items,
                    template=template,
                ),
            )
            remember_skill_result(
                session,
                SKILL_ROOT_CAUSE_ANALYSIS,
                session.get("request_text") or "",
                root_cause_report_context(root_cause_report) if root_cause_report else (
                    f"性能分析错误：{performance_error or '无'}\n"
                    f"执行器分析错误：{actuator_error or '无'}"
                ),
                root_cause_report.headline if root_cause_report else "行车问题原因分析已完成，结果见上一张卡片。",
                "行车问题原因分析结果",
                template,
            )
        except Exception as exc:
            send_or_update_result_card(
                chat_id,
                session,
                markdown_card(
                    "行车问题原因分析失败",
                    (
                        f"**处理对象**：<at id={open_id}></at>\n"
                        "<font color='red'>**当前阶段：分析失败**</font>\n\n"
                        f"**上传文件**：`{file_name}`\n"
                        f"**失败原因**：\n{format_exception_for_card(exc)}"
                    ),
                    "red",
                ),
            )
        finally:
            session["stage"] = "completed"
            cleanup_analysis_artifacts(local_file, performance_result)
        return

    if task_type == "actuator_handshake":
        start_progress_card(
            chat_id,
            open_id,
            session,
            stage="已收到文件",
            template="blue",
            detail="已收到上传文件，正在下载并准备执行器握手诊断。",
            force_new=True,
        )
        local_file = None
        try:
            session_token = state_key(open_id, chat_id).replace(":", "_")
            session_token = f"{session_token}_{session.get('request_id', uuid.uuid4().hex[:12])}"
            try:
                local_file = download_uploaded_file(
                    rest_client,
                    session,
                    session_token,
                    progress_callback=make_download_progress_callback(chat_id, open_id, session, template="blue"),
                )
            except Exception as exc:
                raise RuntimeError(f"下载飞书附件失败：{exc}") from exc

            if not local_file.exists() or local_file.stat().st_size <= 0:
                raise RuntimeError(f"下载后的文件为空或不存在：{local_file.name}")

            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="正在分析",
                template="blue",
                detail="已获取文件，正在匹配数据库、解码总线数据并检查执行器接口规则。",
            )

            database_name = session.get("database") or latest_database_name()
            database_path = None
            if database_name:
                candidate = DATABASE_DIR / database_name
                if candidate.exists():
                    database_path = candidate
            if _file_suffix(file_name) == ".blf" and database_path is None:
                raise RuntimeError("执行器握手诊断处理 `.blf` 需要 `.arxml` 数据库，但当前没有找到可用数据库。")

            actuator_artifact_root = ensure_writable_dir(
                get_runtime_subdir("actuator_handshake")
                / f"{safe_runtime_name(local_file.stem, 'data')}_{uuid.uuid4().hex[:8]}"
            )
            result = run_actuator_handshake_analysis_with_database(
                local_file,
                session.get("request_text") or "",
                database_path=database_path,
                artifact_root=actuator_artifact_root,
            )
            template = "red" if result.status == "issue" else "green"
            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="生成图示",
                template=template,
                detail="已完成接口响应检查，正在上传时序图并整理诊断报告。",
            )
            image_items = []
            for image_index, image_path in enumerate(result.image_paths, start=1):
                title = "执行器接口信号时序"
                if result.violations and image_index <= len(result.violations):
                    title = human_rule_title(result.violations[image_index - 1].rule)
                try:
                    image_items.append(
                        {
                            "title": title,
                            "image_key": upload_image_key(image_path),
                        }
                    )
                except Exception as image_exc:
                    print(f"上传执行器图示失败 {image_path} | {image_exc}", flush=True)
            send_or_update_result_card(
                chat_id,
                session,
                actuator_result_card(open_id, result, session.get("request_text") or "", template, image_items),
                actuator_result_fallback_card(open_id, result, session.get("request_text") or "", template, image_items),
            )
            remember_skill_result(
                session,
                SKILL_ACTUATOR_HANDSHAKE_ANALYSIS,
                session.get("request_text") or "",
                format_actuator_markdown(
                    open_id,
                    result,
                    session.get("request_text") or "",
                ),
                actuator_result_headline(result),
                "执行器握手诊断结果",
                template,
            )
        except Exception as exc:
            send_or_update_result_card(
                chat_id,
                session,
                markdown_card(
                    "执行器握手诊断失败",
                    (
                        f"**处理对象**：<at id={open_id}></at>\n"
                        "<font color='red'>**当前阶段：诊断失败**</font>\n\n"
                        "**本次配置**\n"
                        "- 工作类型：**执行器握手诊断**\n"
                        f"- 上传文件：`{file_name}`\n\n"
                        "**失败原因**\n"
                        f"{format_exception_for_card(exc)}\n\n"
                        "**提示**\n"
                        "- 当前执行器握手诊断支持 `.blf`、`.h5`、`.hdf5` 总线数据。\n"
                        "- 上传 `.blf` 时必须能用当前 `.arxml` 数据库解码；如果数据库版本不匹配，会读取成功但解不出目标报文。\n"
                        "- 如果你只是想查原因，可以直接问：`导致制动断握手的有哪些原因`。"
                    ),
                    "red",
                ),
            )
        finally:
            session["stage"] = "completed"
            if local_file is not None:
                try:
                    local_file.unlink(missing_ok=True)
                    shutil.rmtree(local_file.parent, ignore_errors=True)
                except Exception:
                    pass
        return

    if task_type == "performance_analysis":
        start_progress_card(
            chat_id,
            open_id,
            session,
            stage="已收到文件",
            template="blue",
            detail="已收到上传文件，正在下载并准备性能问题分析。",
            force_new=True,
        )
        local_file = None
        analysis_result = None
        try:
            session_token = state_key(open_id, chat_id).replace(":", "_")
            session_token = f"{session_token}_{session.get('request_id', uuid.uuid4().hex[:12])}"
            local_file = download_uploaded_file(
                rest_client,
                session,
                session_token,
                progress_callback=make_download_progress_callback(chat_id, open_id, session, template="blue"),
            )
            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="正在分析",
                template="blue",
                detail="已获取文件，正在按已选数据库和问题范围运行性能指标检查。",
            )
            analysis_result = run_performance_analysis(
                local_file,
                session["database"],
                problem_groups,
                request_text=session.get("request_text") or "",
            )
            analysis_result.polished_summary = polish_analysis_summary(
                selected_problem_groups=problem_groups,
                status=analysis_result.status,
                problem_titles=[metric_title(issue.metric_name) for issue in analysis_result.problems],
                event_count=sum(len(issue.events) for issue in analysis_result.problems),
                image_count=sum(len(issue.image_paths) for issue in analysis_result.problems),
                warnings=analysis_result.warnings,
                fallback_summary=analysis_result.summary,
            )
            template = "green" if analysis_result.status == "ok" else "red"
            update_progress_card(
                chat_id,
                open_id,
                session,
                stage="生成图示",
                template=template,
                detail="已完成性能指标检查，正在上传图示并整理结果。",
            )
            uploaded_images = _upload_card_images([
                (metric_title(issue.metric_name), image_path)
                for issue in analysis_result.problems
                for image_path in issue.image_paths
            ])
            send_or_update_result_card(
                chat_id,
                session,
                performance_result_card(
                    open_id,
                    task_label(session["task_type"]),
                    session["database"],
                    file_name,
                    analysis_result,
                    uploaded_images,
                    template,
                ),
                performance_result_fallback_card(
                    open_id,
                    task_label(session["task_type"]),
                    session["database"],
                    file_name,
                    analysis_result,
                    uploaded_images,
                    template,
                ),
            )
            remember_skill_result(
                session,
                SKILL_PERFORMANCE_ANALYSIS_SETUP,
                session.get("request_text") or "",
                format_analysis_markdown_clean(open_id, task_label(session["task_type"]), session["database"], file_name, analysis_result),
                analysis_result.polished_summary or analysis_result.summary,
                "性能问题分析结果",
                template,
            )
        except Exception as exc:
            send_or_update_result_card(
                chat_id,
                session,
                markdown_card(
                    "性能问题分析失败",
                    (
                        f"**处理对象**：<at id={open_id}></at>\n"
                        "<font color='red'>**当前阶段：分析失败**</font>\n\n"
                        "**本次配置**\n"
                        f"- 工作类型：**{task_label(session['task_type'])}**\n"
                        f"- 数据库：`{session['database']}`\n"
                        f"- 上传文件：`{file_name}`\n\n"
                        "**失败原因**\n"
                        f"{format_exception_for_card(exc)}"
                    ),
                    "red",
                ),
            )
        finally:
            session["stage"] = "completed"
            cleanup_analysis_artifacts(local_file, analysis_result)
        return

    session["stage"] = "completed"
    send_card(chat_id, completed_card(open_id, session["task_type"], session["database"], file_name))


def handle_file_message(chat_id, open_id, message):
    key = state_key(open_id, chat_id)
    session = sessions.get(key)
    print(
        f"处理非文本消息 key={key}, stage={session.get('stage') if session else None}, "
        f"message_type={message.message_type}, content={message.content}",
        flush=True,
    )
    if not session or session.get("stage") not in {
        "collecting_requirements",
        "waiting_file",
        "waiting_fault_input",
        "waiting_axle_torque_vehicle",
        "waiting_axle_torque_custom_final_ratio",
        "completed",
    }:
        return

    if message.message_type not in {"file", "media"}:
        print(f"忽略非文件上传消息 type={message.message_type}", flush=True)
        return

    file_info = extract_file_info(message.content)
    file_name = file_info["file_name"]
    remember_feedback_event(open_id, chat_id, "user", f"[上传文件] {file_name}", message.message_type, message.message_id)
    session["file_name"] = file_name
    session["file_key"] = file_info["file_key"]
    session["message_id"] = message.message_id
    session["message_type"] = message.message_type
    session["raw_file_content"] = file_info["raw_content"]

    if session.get("task_type") == "fault_degradation":
        if _is_database_file(file_name):
            send_text(chat_id, f"<at user_id=\"{open_id}\"></at> 故障降级诊断暂不需要 `.arxml` 数据库文件。请直接上传 `slog`、DID 日志，或直接粘贴故障码/字节串。")
            return
        if session.get("stage") == "completed":
            session["request_id"] = uuid.uuid4().hex[:12]
            session["waiting_file_since"] = int(time.time() * 1000)
        process_uploaded_file(chat_id, open_id, session)
        return

    if session.get("task_type") == FUNCTION_EXIT_ANALYSIS_TASK:
        if _is_database_file(file_name):
            send_text(chat_id, f"<at user_id=\"{open_id}\"></at> ACC/LCC 功能退出原因分析暂不需要 `.arxml` 数据库。请上传解析后的 `.h5` 或 `.hdf5` 数据。")
            return
        if not is_function_exit_data_file(file_name):
            send_text(chat_id, f"<at user_id=\"{open_id}\"></at> ACC/LCC 功能退出原因分析当前需要解析后的 `.h5` 或 `.hdf5` 数据；收到的是 `{file_name}`。如果你手里是 `.blf`，请先解码成 H5 后再上传。")
            return
        if session.get("stage") == "completed":
            session["request_id"] = uuid.uuid4().hex[:12]
            session["waiting_file_since"] = int(time.time() * 1000)
        process_uploaded_file(chat_id, open_id, session)
        return

    if session.get("task_type") == AXLE_TORQUE_ACCURACY_TASK:
        if session.get("stage") == "completed":
            clear_axle_torque_vehicle_selection(session)
        if _is_database_file(file_name):
            try:
                database_name = save_database_file_from_session(session)
                session["database"] = database_name
                session["axle_torque_arxml"] = str(DATABASE_DIR / database_name)
                session["stage"] = (
                    "waiting_file"
                    if session.get("axle_torque_vehicle_config")
                    else "waiting_axle_torque_vehicle"
                )
                session["request_id"] = session.get("request_id") or uuid.uuid4().hex[:12]
                session["waiting_file_since"] = int(time.time() * 1000)
                clear_session_file_state(session)
                send_or_update_result_card(
                    chat_id,
                    session,
                    axle_torque_upload_card(
                        open_id,
                        session.get("request_text") or "",
                        database_name,
                        session.get("axle_torque_vehicle_config") or "",
                        session.get("axle_torque_final_drive_ratio"),
                    ),
                )
            except Exception as exc:
                send_or_update_result_card(
                    chat_id,
                    session,
                    axle_torque_error_card(open_id, file_name, session.get("database") or "", exc),
                )
            return
        if not session.get("axle_torque_vehicle_config") or session.get("axle_torque_final_drive_ratio") is None:
            if _is_blf_file(file_name):
                clear_session_file_state(session)
                if session.get("stage") == "waiting_axle_torque_custom_final_ratio":
                    send_or_update_result_card(
                        chat_id,
                        session,
                        axle_torque_custom_ratio_card(
                            open_id,
                            session.get("request_text") or "",
                            session.get("database") or "",
                        ),
                    )
                else:
                    session["stage"] = "waiting_axle_torque_vehicle"
                    send_or_update_result_card(
                        chat_id,
                        session,
                        axle_torque_upload_card(
                            open_id,
                            session.get("request_text") or "",
                            session.get("database") or "",
                            "",
                            None,
                        ),
                    )
                return
        if not _is_blf_file(file_name):
            send_or_update_result_card(
                chat_id,
                session,
                axle_torque_error_card(
                    open_id,
                    file_name,
                    session.get("database") or "",
                    ValueError("轴端扭矩准确性校验只接受 `.blf` 文件；如需更换数据库请先上传 `.arxml`。"),
                ),
            )
            session["stage"] = "waiting_file"
            return
        try:
            resolve_axle_torque_arxml_from_session(session)
        except Exception as exc:
            send_or_update_result_card(
                chat_id,
                session,
                axle_torque_error_card(open_id, file_name, session.get("database") or "", exc),
            )
            session["stage"] = "waiting_file"
            return
        if session.get("stage") == "completed":
            session["request_id"] = uuid.uuid4().hex[:12]
            session["waiting_file_since"] = int(time.time() * 1000)
        process_uploaded_file(chat_id, open_id, session)
        return

    if session.get("task_type") == BLF_TO_MDF_CONVERSION_TASK:
        if _is_database_file(file_name):
            try:
                database_name = save_database_file_from_session(session)
                session["database"] = database_name
                session["stage"] = "waiting_file"
                session["request_id"] = session.get("request_id") or uuid.uuid4().hex[:12]
                session["waiting_file_since"] = int(time.time() * 1000)
                clear_session_file_state(session)
                send_or_update_result_card(
                    chat_id,
                    session,
                    blf_to_mdf_upload_card(open_id, session.get("request_text") or "", database_name),
                )
            except Exception as exc:
                send_or_update_result_card(
                    chat_id,
                    session,
                    blf_to_mdf_error_card(open_id, file_name, session.get("database") or "", exc),
                )
            return
        if not _is_blf_file(file_name):
            send_or_update_result_card(
                chat_id,
                session,
                blf_to_mdf_error_card(open_id, file_name, session.get("database") or "", ValueError("BLF 转 MDF 只接受 `.blf` 文件。")),
            )
            session["stage"] = "waiting_file"
            return
        if not session.get("database"):
            default_db = blf_to_mdf_default_database_name()
            if default_db:
                session["database"] = default_db
            else:
                send_or_update_result_card(
                    chat_id,
                    session,
                    blf_to_mdf_error_card(open_id, file_name, "", RuntimeError("当前没有可用 `.arxml` 数据库，请先上传或指定数据库。")),
                )
                session["stage"] = "waiting_file"
                return
        if session.get("stage") == "completed":
            session["request_id"] = uuid.uuid4().hex[:12]
            session["waiting_file_since"] = int(time.time() * 1000)
        process_uploaded_file(chat_id, open_id, session)
        return

    if session.get("task_type") == "actuator_handshake":
        if _is_database_file(file_name):
            try:
                database_name = save_database_file_from_session(session)
                session["database"] = database_name
                session["stage"] = "waiting_file"
                session["request_id"] = session.get("request_id") or uuid.uuid4().hex[:12]
                session["waiting_file_since"] = int(time.time() * 1000)
                clear_session_file_state(session)
                send_text(chat_id, f"<at user_id=\"{open_id}\"></at> 已保存执行器握手诊断数据库 `{database_name}`。接下来请继续上传 `.blf`、`.h5` 或 `.hdf5` 数据文件。")
                send_card(chat_id, actuator_upload_card_v2(open_id, session.get('request_text') or '', database_name))
            except Exception as exc:
                send_text(chat_id, f"<at user_id=\"{open_id}\"></at> 执行器握手诊断数据库处理失败：{exc}")
            return
        if not is_actuator_data_file(file_name):
            send_text(chat_id, f"<at user_id=\"{open_id}\"></at> 执行器握手诊断当前支持 `.blf`、`.h5`、`.hdf5` 总线数据；收到的是 `{file_name}`。如果你是想查规则原因，也可以直接用文字提问。")
            return
        if _file_suffix(file_name) == ".blf" and not session.get("database"):
            default_db = latest_database_name()
            if default_db:
                session["database"] = default_db
            else:
                send_text(chat_id, f"<at user_id=\"{open_id}\"></at> 当前收到的是 `.blf` 数据，但还没有可用的 `.arxml` 数据库。请先上传 1 个 `.arxml` 数据库文件，再继续上传 `.blf`。")
                return
        if _is_database_file(file_name):
            send_text(chat_id, f"<at user_id=\"{open_id}\"></at> 我已识别到这是 `.arxml` 数据库文件。如果你要分析 `.blf`，可以先保存数据库后再上传 `.blf`；如果只是查握手/降级原因，也可以直接文字提问。")
            return
        if not is_actuator_data_file(file_name):
            send_text(chat_id, f"<at user_id=\"{open_id}\"></at> 执行器握手诊断当前支持 `.blf`、`.h5`、`.hdf5` 总线数据；收到的是 `{file_name}`。如果你只是想查规则原因，可以直接用文字提问。")
            return
        if session.get("stage") == "completed":
            session["request_id"] = uuid.uuid4().hex[:12]
            session["waiting_file_since"] = int(time.time() * 1000)
        process_uploaded_file(chat_id, open_id, session)
        return

    if _is_database_file(file_name):
        try:
            database_name = save_database_file_from_session(session)
            session["database"] = database_name
            if session.get("task_type") == "performance_analysis":
                session["stage"] = "waiting_file"
                session["request_id"] = uuid.uuid4().hex[:12]
                session["waiting_file_since"] = int(time.time() * 1000)
                clear_session_file_state(session)
                send_text(chat_id, f"<at user_id=\"{open_id}\"></at> 已保存新数据库 `{database_name}`。接下来请上传分析数据文件。")
                send_card(chat_id, smart_upload_card(open_id, session["task_type"], database_name, session.get("problem_groups") or []))
            elif session.get("task_type") == COMBINED_ANALYSIS_TASK:
                session["stage"] = "waiting_file"
                session["request_id"] = uuid.uuid4().hex[:12]
                session["waiting_file_since"] = int(time.time() * 1000)
                clear_session_file_state(session)
                send_text(chat_id, f"<at user_id=\"{open_id}\"></at> 已保存新数据库 `{database_name}`。接下来请上传 `.h5` 数据文件，我会做行车问题原因分析。")
                send_card(chat_id, combined_upload_card(open_id, session.get("request_text") or "", database_name, session.get("problem_groups") or []))
            else:
                session["stage"] = "collecting_requirements"
                send_text(chat_id, f"<at user_id=\"{open_id}\"></at> 已保存新数据库 `{database_name}`。现在可以继续告诉我要分析什么问题。")
            return
        except Exception as exc:
            send_text(chat_id, f"<at user_id=\"{open_id}\"></at> 数据库文件处理失败：{exc}")
            return

    if session.get("task_type") == COMBINED_ANALYSIS_TASK and not _is_root_cause_data_file(file_name):
        send_or_update_result_card(
            chat_id,
            session,
            markdown_card(
                "行车问题原因分析文件格式不支持",
                (
                    f"**处理对象**：<at id={open_id}></at>\n"
                    "<font color='orange'>**当前阶段：等待 `.h5` 数据**</font>\n\n"
                    f"**收到文件**：`{file_name}`\n\n"
                    "行车问题原因分析当前仅支持标准 `.h5` 数据文件。"
                    "如果手头是 `.blf`、`.hdf5` 或其他格式，请先转换成布鲁可读取的 `.h5` 后再上传。"
                ),
                "orange",
            ),
        )
        session["stage"] = "waiting_file"
        return

    if not _is_supported_analysis_file(file_name):
        send_text(
            chat_id,
            f"<at user_id=\"{open_id}\"></at> 当前只支持上传 `.blf`、`.h5`、`.hdf5` 作为分析数据，或上传 `.arxml` 作为数据库文件。",
        )
        return

    if not session.get("task_type"):
        session["task_type"] = "performance_analysis"
    if not session.get("database"):
        default_db = latest_database_name()
        if default_db:
            session["database"] = default_db

    if not session.get("database"):
        session["stage"] = "collecting_requirements"
        send_text(
            chat_id,
            f"<at user_id=\"{open_id}\"></at> 我已经收到数据文件 `{file_name}`，但当前没有可用数据库。请先说明数据库版本，或上传 `.arxml` 数据库文件。",
        )
        return

    if session.get("stage") == "completed":
        session["request_id"] = uuid.uuid4().hex[:12]
        session["waiting_file_since"] = int(time.time() * 1000)

    process_uploaded_file(chat_id, open_id, session)


def handle_session_text(chat_id, open_id, text):
    key = state_key(open_id, chat_id)
    session = sessions.get(key)
    if not session:
        return False

    normalized = (text or "").strip()
    if not has_actionable_text(normalized):
        print(f"忽略会话内空文本: key={key}", flush=True)
        return True

    if is_batch_root_cause_cancel_intent(normalized):
        return cancel_active_batch_root_cause(chat_id, open_id)

    if is_kpi_report_cancel_intent(normalized):
        return cancel_active_kpi_report(chat_id, open_id)

    if is_feedback_request_text(normalized):
        remember_conversation_turn(session, "user", normalized)
        session["request_text"] = normalized
        return handle_feedback_report(chat_id, open_id, session, normalized)

    if is_batch_root_cause_analysis_intent(normalized):
        session["stage"] = "collecting_requirements"
        session.pop("pending_analysis_request_text", None)
        session.pop("analysis_tasks", None)
        session.pop("active_skill", None)
        return handle_agent_decision(chat_id, open_id, session, normalized)

    if is_batch_root_cause_analysis_near_miss(normalized):
        remember_conversation_turn(session, "user", normalized)
        session["request_text"] = normalized
        session["stage"] = "collecting_requirements"
        session.pop("pending_analysis_request_text", None)
        session.pop("analysis_tasks", None)
        session.pop("active_skill", None)
        reset_progress_card_state(session)
        send_card(
            chat_id,
            assistant_reply_card(open_id, batch_root_cause_near_miss_reply(), "多维表格批量分析", "orange"),
        )
        return True

    if is_kpi_report_analysis_intent(normalized):
        session["stage"] = "collecting_requirements"
        session.pop("pending_analysis_request_text", None)
        session.pop("analysis_tasks", None)
        session.pop("active_skill", None)
        return handle_agent_decision(chat_id, open_id, session, normalized)

    if is_kpi_report_analysis_near_miss(normalized):
        remember_conversation_turn(session, "user", normalized)
        session["request_text"] = normalized
        session["stage"] = "collecting_requirements"
        reset_progress_card_state(session)
        send_card(
            chat_id,
            assistant_reply_card(open_id, kpi_report_near_miss_reply(), "KPI 报告 AI 分析", "orange"),
        )
        return True

    if is_cancel(normalized):
        cancel_session(chat_id, open_id)
        return True

    if is_actuator_reference_query_text(normalized) and not actuator_request_needs_file(normalized):
        sessions.pop(key, None)
        clear_history(key)
        start_session(chat_id, open_id, normalized)
        return True

    session_intent = analyze_session_message(
        normalized,
        current_stage=session.get("stage"),
        current_task=session.get("task_type"),
    )
    session_action = session_intent.action
    if session_action == "status" and not is_explicit_session_status_query(normalized):
        session_action = "continue"

    if session_action == "cancel":
        cancel_session(chat_id, open_id)
        return True

    if session_action == "status" or normalized == "状态":
        send_text(chat_id, session_status_text(open_id, session))
        return True

    stage = session.get("stage")
    if stage == "clarify_analysis_scope":
        return handle_analysis_scope_choice(chat_id, open_id, session, normalized)

    if session.get("task_type") == AXLE_TORQUE_ACCURACY_TASK:
        if stage == "waiting_axle_torque_custom_final_ratio":
            custom_final_drive = parse_axle_torque_custom_final_drive(normalized)
            if custom_final_drive is None:
                send_or_update_result_card(
                    chat_id,
                    session,
                    axle_torque_custom_ratio_card(
                        open_id,
                        session.get("request_text") or normalized,
                        session.get("database") or "",
                    ),
                )
                return True
            vehicle_config = apply_axle_torque_custom_final_drive(session, custom_final_drive)
            session["stage"] = "waiting_file"
            clear_session_file_state(session)
            send_or_update_result_card(
                chat_id,
                session,
                axle_torque_upload_card(
                    open_id,
                    session.get("request_text") or normalized,
                    session.get("database") or "",
                    vehicle_config,
                    custom_final_drive,
                ),
            )
            return True

        if is_axle_torque_custom_vehicle_choice(normalized):
            custom_final_drive = parse_axle_torque_custom_final_drive(normalized)
            if custom_final_drive is None:
                session["stage"] = "waiting_axle_torque_custom_final_ratio"
                send_or_update_result_card(
                    chat_id,
                    session,
                    axle_torque_custom_ratio_card(
                        open_id,
                        session.get("request_text") or normalized,
                        session.get("database") or "",
                    ),
                )
                return True
            vehicle_config = apply_axle_torque_custom_final_drive(session, custom_final_drive)
            session["stage"] = "waiting_file"
            clear_session_file_state(session)
            send_or_update_result_card(
                chat_id,
                session,
                axle_torque_upload_card(
                    open_id,
                    session.get("request_text") or normalized,
                    session.get("database") or "",
                    vehicle_config,
                    custom_final_drive,
                ),
            )
            return True

        vehicle_config = apply_axle_torque_vehicle_config(session, normalized)
        if vehicle_config:
            session["stage"] = "waiting_file"
            clear_session_file_state(session)
            send_or_update_result_card(
                chat_id,
                session,
                axle_torque_upload_card(
                    open_id,
                    session.get("request_text") or normalized,
                    session.get("database") or "",
                    session.get("axle_torque_vehicle_config") or "",
                    session.get("axle_torque_final_drive_ratio"),
                ),
            )
            return True

    if session_action == "new_request":
        previous_request = session.get("request_text") or ""
        next_text = normalized
        if should_carry_previous_request_into_new_request(previous_request, normalized):
            next_text = f"{previous_request}\n用户更正：{normalized}"
        sessions.pop(key, None)
        clear_history(key)
        start_session(chat_id, open_id, next_text)
        return True

    if session_action == "continue" and stage != "processing":
        return handle_agent_decision(chat_id, open_id, session, normalized)

    if stage in {"choose_task", "choose_database", "collecting_requirements"}:
        handle_agent_decision(chat_id, open_id, session, normalized)
    elif stage == "waiting_fault_input":
        if session_action == "change_database":
            session["stage"] = "collecting_requirements"
            send_text(chat_id, f'<at user_id="{open_id}"></at> 好的，你可以直接告诉我想改成哪个数据库，或者上传新的 `.arxml` 数据库文件。')
            send_card(chat_id, smart_request_card(open_id, session))
            return True
        if session_action == "check_upload":
            recent_file, error = find_recent_file_message_flexible(
                chat_id,
                open_id,
                session.get("waiting_file_since"),
                allow_fallback=True,
            )
            if recent_file:
                session.update(recent_file)
                process_uploaded_file(chat_id, open_id, session)
            else:
                send_text(chat_id, f'<at user_id="{open_id}"></at> 我这边还没找到你最近上传的文件。你可以再发一次，或者直接把故障码、`fault_event`、`62 FD A3/A7` 字节串贴给我。')
            return True
        if session_action == "back":
            session["stage"] = "collecting_requirements"
            send_card(chat_id, smart_request_card(open_id, session))
            return True
        if looks_like_fault_diagnosis_text(normalized):
            process_fault_diagnosis_input(chat_id, open_id, session, "inline_query", text=normalized, force_new_progress=True)
        else:
            send_text(chat_id, f'<at user_id="{open_id}"></at> 我这边还在等你的故障诊断输入。你可以直接发 `slog`、DID、`fault_event`、`62 FD A3/A7` 字节串，或者像 `FDA3[113]` 这样直接查。')
    elif stage == "waiting_file":
        if session_action == "change_database":
            session["stage"] = "collecting_requirements"
            session.pop("file_name", None)
            session.pop("file_key", None)
            session.pop("raw_file_content", None)
            send_text(chat_id, f'<at user_id="{open_id}"></at> 好的，我们先改数据库。你可以直接说版本，或者上传新的 `.arxml` 数据库文件。')
            send_card(chat_id, smart_request_card(open_id, session))
            return True
        if session_action == "back" or normalized == "返回":
            session["stage"] = "collecting_requirements"
            session.pop("file_name", None)
            session.pop("file_key", None)
            session.pop("raw_file_content", None)
            send_card(chat_id, smart_request_card(open_id, session))
            return True

        if is_explicit_upload_check(normalized, session_action):
            recent_file, error = find_recent_file_message_flexible(
                chat_id,
                open_id,
                session.get("waiting_file_since"),
                allow_fallback=(session_action == "check_upload"),
            )
            if recent_file:
                session.update(recent_file)
                process_uploaded_file(chat_id, open_id, session)
            else:
                if error:
                    send_text(chat_id, f'<at user_id="{open_id}"></at> {error}')
                else:
                    send_text(chat_id, f'<at user_id="{open_id}"></at> 我这边还没找到你最近上传的文件。请直接在当前群里上传数据文件附件。')
        else:
            send_text(chat_id, f'<at user_id="{open_id}"></at> 当前配置已经记好了，正在等待你上传数据文件。只有你直接上传附件，或明确说“我刚上传了”，我才会开始分析。')
    elif stage == "processing":
        send_text(chat_id, processing_followup_reply(open_id, session, normalized))
    elif stage == "completed":
        if recent_skill_result(session) and is_contextual_followup_request(normalized):
            session["stage"] = "collecting_requirements"
            return handle_agent_decision(chat_id, open_id, session, normalized)
        start_session(chat_id, open_id, normalized)
    else:
        start_session(chat_id, open_id, normalized)
    return True

def build_event_handler():
    return (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handle_message)
        .register_p2_im_message_reaction_created_v1(handle_message_reaction_created)
        .register_p2_im_message_reaction_deleted_v1(handle_message_reaction_deleted)
        .register_p2_im_message_message_read_v1(handle_message_read)
        .register_p2_card_action_trigger(handle_card_action)
        .build()
    )


def main():
    if not acquire_instance_lock():
        return
    cleanup_runtime_artifacts()
    app_id, app_secret = get_feishu_credentials()
    event_handler = build_event_handler()
    ws_client = lark.ws.Client(app_id, app_secret, event_handler=event_handler)

    print("FeishuAgent started. 等待飞书消息事件...", flush=True)
    ws_client.start()


if __name__ == "__main__":
    main()

