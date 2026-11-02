from __future__ import annotations

import csv
import datetime
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from analysis_engine.signal_lib import FolderSession
from runtime_paths import ensure_writable_dir, get_runtime_subdir


ROOT = Path(__file__).resolve().parent
RULE_DIR = ROOT / "ADAS_interface_machine_readable_rules"
def get_actuator_runtime_dir() -> Path:
    return get_runtime_subdir("actuator_handshake")


ACTUATOR_RUNTIME_DIR = get_actuator_runtime_dir()
RULE_CSV = RULE_DIR / "diagnostic_rules.csv"
SIGNAL_CSV = RULE_DIR / "signal_dictionary.csv"
PARAMETER_CSV = RULE_DIR / "parameters.csv"
STATE_MACHINE_CSV = RULE_DIR / "state_machines.csv"
DOCX_FILES = (
    RULE_DIR / "PS ICD for ADAS_V3.5.6.docx",
    RULE_DIR / "EBCM ICD for ADAS and APA.docx",
    RULE_DIR / "IDS2.5 Electric Power Steering ICD V1.13 2026.6.29.docx",
    RULE_DIR / "PIDS-ICR03 SGM智能驾驶泊车换档系统（ETRS）接口定义功能规范.docx",
)

ACTUATOR_DATA_SUFFIXES = {".blf", ".h5", ".hdf5"}

BRAKE_TERMS = {"制动", "刹车", "brake", "ebcm", "ebooster", "boost", "ibooster", "aeb", "cib"}
PROPULSION_TERMS = {"驱动", "动力", "propulsion", "pdf", "ecp", "ecm", "engine", "tpim"}
STEERING_TERMS = {"转向", "方向盘", "steering", "eps", "swar", "strg", "strng", "角度", "转角", "angle overlay", "torque overlay"}
SHIFT_TERMS = {"换挡", "换档", "挡位", "档位", "gear", "shift", "shifter", "etrs", "transmission range", "range selector"}
HANDSHAKE_TERMS = {"握手", "断握手", "handshake", "响应", "honor", "honored", "request"}
DEGRADE_TERMS = {"降级", "退出", "deny", "denied", "failed", "failure", "limited", "suspend", "abort"}
UPLOAD_TERMS = {"上传", "文件", "数据", "h5", "hdf5", "blf", "日志", "跑一下", "分析这段", "看数据"}
REQUIREMENT_REVIEW_TERMS = {
    "需求", "变更", "合理", "合理性", "可行", "可行性", "实施", "实现", "方案",
    "风险", "风险点", "关注点", "建议", "评审", "设计", "改动", "改成", "新增",
    "兼容", "接口变更", "超时", "500ms", "timer", "timeout",
}
DRIVE_PARK_SWITCH_TERMS = {
    "行泊", "行/泊", "行车", "泊车", "停车", "静止", "运动", "drive", "park",
    "driving", "parking", "switch", "mode switch",
}

_SIGNAL_NAMES: set[str] | None = None
_SIGNAL_TO_MESSAGE: dict[str, str] | None = None
_SIGNAL_METADATA: dict[str, dict[str, str]] | None = None
_STATE_MACHINE_ROWS: list[dict[str, Any]] | None = None
_PARAMETER_ROWS: list[dict[str, str]] | None = None
_DOCX_SEGMENTS: list[dict[str, str]] | None = None


def to_float_or_nan(value: Any) -> float:
    try:
        if value is None:
            return float("nan")
        if isinstance(value, bool):
            return float(int(value))
        return float(value)
    except Exception:
        return float("nan")


def patch_python_can_blf_timestamp(can_module: Any) -> None:
    blf_module = getattr(getattr(can_module, "io", None), "blf", None)
    if blf_module is None or getattr(blf_module, "_feishuagent_timestamp_patch", False):
        return

    def safe_systemtime_to_timestamp(systemtime: Any) -> float:
        try:
            value = datetime.datetime(
                int(systemtime[0]),
                int(systemtime[1]),
                int(systemtime[3]),
                int(systemtime[4]),
                int(systemtime[5]),
                int(systemtime[6]),
                int(systemtime[7]) * 1000,
                tzinfo=datetime.timezone.utc,
            )
            return value.timestamp()
        except Exception:
            return 0.0

    blf_module.systemtime_to_timestamp = safe_systemtime_to_timestamp
    blf_module._feishuagent_timestamp_patch = True


def iter_blf_messages_preserve_fd_payload(blf_file: Path, can_module: Any):
    """Read BLF while preserving CAN FD payload when valid_bytes is recorded as 0."""
    import zlib

    blf_module = can_module.io.blf
    obj_header = blf_module.OBJ_HEADER_BASE_STRUCT
    container_struct = blf_module.LOG_CONTAINER_STRUCT

    with blf_file.open("rb") as file:
        header_data = file.read(blf_module.FILE_HEADER_STRUCT.size)
        header = blf_module.FILE_HEADER_STRUCT.unpack(header_data)
        if header[0] != b"LOGG":
            raise RuntimeError("不是有效的 Vector BLF 文件")
        start_timestamp = blf_module.systemtime_to_timestamp(header[14:22])
        file.read(header[1] - blf_module.FILE_HEADER_STRUCT.size)

        tail = b""
        while True:
            header_data = file.read(obj_header.size)
            if not header_data:
                break
            signature, _, _, obj_size, obj_type = obj_header.unpack(header_data)
            if signature != b"LOBJ":
                raise RuntimeError("BLF 对象头异常")
            obj_data = file.read(obj_size - obj_header.size)
            file.read(obj_size % 4)
            if obj_type != blf_module.LOG_CONTAINER:
                continue

            method, _ = container_struct.unpack_from(obj_data)
            data = obj_data[container_struct.size:]
            if method == blf_module.ZLIB_DEFLATE:
                data = zlib.decompress(data)
            elif method != blf_module.NO_COMPRESSION:
                continue

            data = tail + data
            pos = 0
            max_pos = len(data)
            while True:
                try:
                    pos = data.index(b"LOBJ", pos, min(pos + 8, max_pos))
                except ValueError:
                    break
                if pos + obj_header.size > max_pos:
                    break

                signature, _, header_version, object_size, object_type = obj_header.unpack_from(data, pos)
                if signature != b"LOBJ":
                    raise RuntimeError("BLF 容器对象头异常")
                next_pos = pos + object_size
                if next_pos > max_pos:
                    break

                pos += obj_header.size
                if header_version == 1:
                    flags, _, _, timestamp = blf_module.OBJ_HEADER_V1_STRUCT.unpack_from(data, pos)
                    pos += blf_module.OBJ_HEADER_V1_STRUCT.size
                elif header_version == 2:
                    flags, _, _, timestamp = blf_module.OBJ_HEADER_V2_STRUCT.unpack_from(data, pos)
                    pos += blf_module.OBJ_HEADER_V2_STRUCT.size
                else:
                    pos = next_pos
                    continue

                factor = 1e-5 if flags == 1 else 1e-9
                timestamp = timestamp * factor + start_timestamp

                if object_type in (blf_module.CAN_MESSAGE, blf_module.CAN_MESSAGE2):
                    channel, msg_flags, dlc, can_id, can_data = blf_module.CAN_MSG_STRUCT.unpack_from(data, pos)
                    yield can_module.Message(
                        timestamp=timestamp,
                        arbitration_id=can_id & 0x1FFFFFFF,
                        is_extended_id=bool(can_id & blf_module.CAN_MSG_EXT),
                        is_remote_frame=bool(msg_flags & blf_module.REMOTE_FLAG),
                        is_rx=not bool(msg_flags & blf_module.DIR),
                        dlc=dlc,
                        data=can_data[:dlc],
                        channel=channel - 1,
                    )
                elif object_type == blf_module.CAN_FD_MESSAGE:
                    (
                        channel,
                        msg_flags,
                        dlc,
                        can_id,
                        _frame_length,
                        _bit_count,
                        fd_flags,
                        valid_bytes,
                        can_data,
                    ) = blf_module.CAN_FD_MSG_STRUCT.unpack_from(data, pos)
                    payload_length = valid_bytes or blf_module.dlc2len(dlc)
                    yield can_module.Message(
                        timestamp=timestamp,
                        arbitration_id=can_id & 0x1FFFFFFF,
                        is_extended_id=bool(can_id & blf_module.CAN_MSG_EXT),
                        is_remote_frame=bool(msg_flags & blf_module.REMOTE_FLAG),
                        is_fd=bool(fd_flags & 0x1),
                        is_rx=not bool(msg_flags & blf_module.DIR),
                        bitrate_switch=bool(fd_flags & 0x2),
                        error_state_indicator=bool(fd_flags & 0x4),
                        dlc=blf_module.dlc2len(dlc),
                        data=can_data[:payload_length],
                        channel=channel - 1,
                    )
                elif object_type == blf_module.CAN_FD_MESSAGE_64:
                    (
                        channel,
                        dlc,
                        valid_bytes,
                        _tx_count,
                        can_id,
                        _frame_length,
                        fd_flags,
                        _valid_data_bytes,
                        _bit_count,
                        _direction,
                        *_rest,
                    ) = blf_module.CAN_FD_MSG_64_STRUCT.unpack_from(data, pos)
                    payload_start = pos + blf_module.CAN_FD_MSG_64_STRUCT.size
                    payload_length = valid_bytes or blf_module.dlc2len(dlc)
                    payload = data[payload_start:payload_start + payload_length]
                    yield can_module.Message(
                        timestamp=timestamp,
                        arbitration_id=can_id & 0x1FFFFFFF,
                        is_extended_id=bool(can_id & blf_module.CAN_MSG_EXT),
                        is_remote_frame=bool(fd_flags & 0x0010),
                        is_fd=bool(fd_flags & 0x1000),
                        is_rx=True,
                        bitrate_switch=bool(fd_flags & 0x2000),
                        error_state_indicator=bool(fd_flags & 0x4000),
                        dlc=blf_module.dlc2len(dlc),
                        data=payload,
                        channel=channel - 1,
                    )

                pos = next_pos
            tail = data[pos:]


@dataclass
class Rule:
    rule_id: str
    domain: str
    subsystem: str
    feature: str
    scenario: str
    rule_type: str
    preconditions: list[dict[str, Any]]
    trigger: str
    expected: list[dict[str, Any]]
    time_window_ms: str
    violation: str
    severity: str
    owner_hint: str
    evidence_signals: list[str]
    next_checks: list[str]
    source_doc: str = ""
    notes: str = ""
    enabled: bool = True


@dataclass
class ActuatorViolation:
    rule: Rule
    start_time: float
    end_time: float
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActuatorAnalysisResult:
    status: str
    file_name: str
    checked_rules: int
    violations: list[ActuatorViolation]
    skipped_rules: list[tuple[str, str]]
    missing_signals: dict[str, list[str]]
    warnings: list[str] = field(default_factory=list)
    timeline_lines: list[str] = field(default_factory=list)
    image_paths: list[Path] = field(default_factory=list)


def is_actuator_data_file(file_name: str) -> bool:
    return Path(file_name or "").suffix.lower() in ACTUATOR_DATA_SUFFIXES


def actuator_request_needs_file(text: str) -> bool:
    value = (text or "").lower()
    if any(token in value for token in UPLOAD_TERMS):
        return True
    if "总线" in value and any(token in value for token in ["查", "看", "分析", "诊断"]):
        return True
    return False


def is_actuator_requirement_review_query(text: str) -> bool:
    value = (text or "").lower()
    if not value:
        return False
    actuator_focus = any(term in value for term in (
        BRAKE_TERMS
        | PROPULSION_TERMS
        | STEERING_TERMS
        | SHIFT_TERMS
        | {"执行器", "接口", "acc", "aeb", "apa", "hpp", "srp", "行泊", "行/泊"}
    ))
    review_focus = any(term in value for term in REQUIREMENT_REVIEW_TERMS)
    switch_focus = any(term in value for term in DRIVE_PARK_SWITCH_TERMS)
    return actuator_focus and review_focus and ("需求" in value or switch_focus or "接口" in value)


def is_broad_actuator_consultation(text: str) -> bool:
    value = (text or "").lower()
    if not any(term in value for term in {"执行器", "actuator"}):
        return False
    if not any(term in value for term in {"咨询", "问题", "问一下", "问问", "了解", "可以干啥"}):
        return False

    specific_terms = (
        BRAKE_TERMS
        | PROPULSION_TERMS
        | STEERING_TERMS
        | SHIFT_TERMS
        | HANDSHAKE_TERMS
        | DEGRADE_TERMS
        | {"acc", "aeb", "apa", "srp", "hpp", "信号", "条件", "流程", "状态", "置位", "取值", "请求", "释放", "接收"}
    )
    specific_terms = specific_terms - {"执行器", "接口", "request"}
    return not any(term in value for term in specific_terms)


def _answer_etrs_epb_release_query(user_text: str) -> str:
    value = (user_text or "").lower()
    if not value:
        return ""
    if "etrs" not in value or not any(term in value for term in {"epb", "电子驻车", "驻车"}):
        return ""
    if not any(term in value for term in {"释放", "release", "松开"}):
        return ""
    if not any(term in value for term in {"ebcm", "制动", "brake"}):
        return ""

    return (
        "按当前执行器原始接口文档看，结论是：没有看到 EBCM 直接接收 ETRS 的 EPB 释放请求这一条接口。\n\n"
        "更像下面这种关系：\n"
        "1. ETRS 负责 APA 下的换挡/挡位控制，APA 向 ETRS 发挡位请求，ETRS 回换挡控制状态。\n"
        "2. EPB 释放请求在 APA/SRP 制动接口里，信号是 `APABT_SRPEPBReq`，取值 `2 = Release`，来源是 APA 制动/驱动请求报文，不是 ETRS 给 EBCM 的请求。\n"
        "3. EBCM 侧需要看制动响应，例如 `BrkSysAPABrk_SRPBrkResp` 和 `APABrkStat`，确认制动系统是否进入 Hold、Control 或 Failed。\n\n"
        "所以如果你问的是“APA 状态下是否会释放 EPB”，当前文档支持从 APA/HPP/SRP 制动接口发释放请求；"
        "如果你问的是“ETRS 是否作为 EPB 释放请求源”，当前文档没有支撑，需要补充 ETRS 与 EBCM 之间的接口或仲裁说明。"
    )


def answer_actuator_query(user_text: str) -> str:
    if is_actuator_requirement_review_query(user_text):
        review_context = build_actuator_requirement_review_context(user_text)
        if review_context:
            return review_context

    direct_answer = _answer_etrs_epb_release_query(user_text)
    if direct_answer:
        return direct_answer

    context = build_actuator_reference_context(user_text)
    if context:
        return (
            "我按执行器原始接口文档检索到了这些线索：\n\n"
            f"{context}\n\n"
            "如果要从真实数据里验证这些条件是否满足，可以上传 `.blf`、`.h5` 或 `.hdf5` 总线数据。"
        )

    rules = _rank_rules(user_text, load_rules())
    if not rules:
        if is_broad_actuator_consultation(user_text):
            return (
                "可以，执行器相关问题你直接问就行。\n\n"
                "我现在能帮你查这些：\n"
                "1. 制动、驱动、转向、换挡执行器的请求/响应关系。\n"
                "2. ACC、AEB、APA、SRP/HPP 场景下的握手条件、状态跳转和异常原因。\n"
                "3. 某个信号的取值含义、什么时候应该置位、没响应时该看哪些证据信号。\n"
                "4. 执行器接口需求变更的合理性、风险点和需要补充的设计边界。\n\n"
                "你可以直接这样问：`APA 状态下，EBCM 会接收 ETRS 的 EPB 释放请求吗`，"
                "或者 `ACC 下驱动系统请求被拒绝一般看哪些信号`。"
            )
        return (
            "本地执行器原始接口文档里暂时没有直接命中的条目。可以再补充系统对象"
            "（制动/EBCM/ebooster，或驱动/PDF/ECP/ECM）和功能场景（ACC、AEB、APA、SRP/HPP），我会继续按原始接口文档检索。"
        )

    scope = _infer_scope(user_text)
    lines = [
        "我按执行器接口文档帮你收敛了一下：",
        "",
        f"关注对象：{_scope_label(scope)}",
        f"相关接口要求：{len(rules)} 条，下面列出最相关的 {min(len(rules), 8)} 条",
        "",
    ]
    for index, rule in enumerate(rules[:8], start=1):
        lines.extend(
            [
                f"{index}. {human_rule_title(rule)}",
                f"   - 可能原因：{human_violation(rule)}",
                f"   - 归属倾向：{human_owner(rule.owner_hint)}",
                f"   - 触发条件：{_human_trigger(rule)}",
                f"   - 正常应该是什么样：{_human_expected(rule)}",
                f"   - 关键证据信号：{_format_evidence_signals(rule)}",
            ]
        )
        if rule.source_doc:
            lines.append(f"   - 原始文档：{rule.source_doc}")
    lines.extend(
        [
            "",
            "如果你要让我直接从数据里判断是否发生断握手、降级或未按设计响应，可以直接上传 `.blf`、`.h5` 或 `.hdf5` 总线数据。"
        ]
    )
    return "\n".join(lines)


def build_actuator_reference_context(user_text: str) -> str:
    sections: list[str] = []
    rules = _rank_rules(user_text, load_rules())
    if rules:
        lines = [f"### 接口要求命中（{len(rules)} 条，展示前 {min(len(rules), 6)} 条）"]
        for index, rule in enumerate(rules[:6], start=1):
            lines.extend(
                [
                    f"{index}. {human_rule_title(rule)}",
                    f"   - 场景：{_display_feature(rule.feature)} / {_translate_scenario(rule.scenario)}",
                    f"   - 触发条件：{_human_trigger(rule)}",
                    f"   - 正常应答：{_human_expected(rule)}",
                    f"   - 异常含义：{human_violation(rule)}",
                    f"   - 关键信号：{_format_evidence_signals(rule)}",
                    f"   - 后续排查：{_format_next_checks(rule)}",
                ]
            )
            if rule.source_doc:
                lines.append(f"   - 原始文档：{rule.source_doc}")
        sections.append("\n".join(lines))

    signal_hits = _rank_signal_metadata(user_text)
    if signal_hits:
        lines = [f"### 相关信号（展示前 {len(signal_hits)} 条）"]
        for index, (score, signal, metadata) in enumerate(signal_hits, start=1):
            conversion = _display_text(metadata.get("conversion", ""))
            lines.append(
                f"{index}. {signal}：{_display_text(metadata.get('concept', '')) or '未填写概念'}；"
                f"报文 {metadata.get('message', '') or '未知'} / {metadata.get('can_id', '') or '未知ID'}；"
                f"取值 {conversion or metadata.get('range', '') or '未整理'}"
            )
        sections.append("\n".join(lines))

    parameter_hits = _rank_parameter_rows(user_text)
    if parameter_hits:
        lines = [f"### 相关参数/阈值（展示前 {len(parameter_hits)} 条）"]
        for index, (score, row) in enumerate(parameter_hits, start=1):
            value = row.get("default_value") or "需项目标定提供"
            unit = row.get("unit") or ""
            lines.append(
                f"{index}. {row.get('parameter_id', '')} = {value}{unit}；"
                f"owner={row.get('owner', '') or '未标注'}；说明：{_display_text(row.get('description', ''))}"
            )
        sections.append("\n".join(lines))

    doc_hits = _rank_docx_segments(user_text)
    if doc_hits:
        lines = [f"### 原始文档片段命中（展示前 {len(doc_hits)} 条）"]
        for index, (score, item) in enumerate(doc_hits, start=1):
            lines.append(f"{index}. 来源 {item.get('source', '')}：{_compact_text(item.get('text', ''), 420)}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def build_actuator_requirement_review_context(user_text: str) -> str:
    rules = _rank_rules(user_text, load_rules())
    brake_signals = _rank_signal_metadata(
        f"{user_text} EBCM ACC 行车 制动 扭矩 请求 响应 ACCBSCE ACCBrkngAct BrkSysAutBrkStat",
        limit=8,
    )
    park_signals = _rank_signal_metadata(
        f"{user_text} APA HPP SRP 泊车 制动 扭矩 Hold EPB APABT BrkSysAPABrk APABrkStat",
        limit=8,
    )
    switch_signals = _rank_signal_metadata(
        f"{user_text} 行泊 行车 泊车 switch mode active status APA ACC",
        limit=6,
    )
    parameter_hits = _rank_parameter_rows(f"{user_text} timeout response handshake 500ms", limit=6)
    doc_hits = _rank_docx_segments(
        f"{user_text} EBCM ACC APA HPP SRP brake torque request response EPB timeout mode switch",
        limit=5,
    )

    support_lines: list[str] = []
    if brake_signals:
        support_lines.append(
            "行车制动接口：本地索引能检索到 ACC/行车侧制动请求与 EBCM 响应相关信号，"
            f"例如 {_format_review_signal_names(brake_signals, 5)}。"
        )
    if park_signals:
        support_lines.append(
            "泊车制动接口：本地索引能检索到 APA/HPP/SRP 泊车侧制动扭矩、Hold、EPB 和 EBCM 反馈相关信号，"
            f"例如 {_format_review_signal_names(park_signals, 5)}。"
        )
    if rules:
        support_lines.append(
            "异常处理：现有规则里能检索到制动请求未响应、请求失败、降级/退出等相近接口行为，"
            f"最相关场景包括：{_format_review_rule_titles(rules, 4)}。"
        )
    if parameter_hits:
        support_lines.append(
            "超时机制：本地参数表能检索到一些响应超时/握手超时类参数，"
            f"但是否可直接作为本需求的 500ms 判据，需要看 EBCM 供应商或系统需求是否正式定义。"
        )

    if not support_lines:
        support_lines.append("本地执行器文档索引没有直接命中完整接口链路，只能按相近制动/驱动/泊车接口行为做评审。")

    missing_lines = [
        "行泊切换 flag：当前检索结果没有直接确认一个可作为行/泊切换判据的统一信号；需要定义信号来源、取值、有效条件、去抖和边沿触发。",
        "500ms 未握手判据：当前不能把 500ms 当成已有 EBCM ICD 结论；需要明确计时起点、停止条件、重试策略、超时后由谁判定失败。",
        "降级/刹停/拉 EPB 动作：需要把“降级”“刹停”“拉 EPB”拆成明确状态和触发条件，尤其区分车辆静止、低速、行驶中和驾驶员接管。",
        "接口优先级：ACC/HPP 同时使用行车接口后，需要定义与 APA 泊车接口并存时的仲裁优先级，避免同一时刻两条接口给 EBCM 发送冲突请求。",
    ]

    implementation_lines = [
        "先把接口所有权讲清：ACC、HPP 走行车接口，APA 走泊车接口；每类请求必须有唯一的请求信号、响应信号和失败状态。",
        "再定义行泊切换状态机：进入行车、进入泊车、切换中、切换失败分别是什么信号组合，哪些状态允许发制动扭矩请求。",
        "把 500ms 设计成可配置阈值：不要写死在算法或 EBCM 里，建议形成标定/系统参数，并在日志中记录计时开始、响应到达和超时原因。",
        "对 EBCM 响应做闭环校验：不仅看请求是否发出，还要看 EBCM 是否反馈请求接受、Hold/Control 激活、失败/拒绝、EPB 执行状态。",
        "把异常动作拆成测试用例：行车转泊车、泊车转行车、ACC/HPP/APA 同时请求、驾驶员踩制动/油门接管、总线延迟或丢帧、EBCM 返回失败。",
    ]

    risk_lines = [
        "误触发风险：如果行泊 flag 抖动或状态定义不清，可能把正常接口切换误判成未握手，从而错误降级或拉 EPB。",
        "冲突请求风险：ACC/HPP 改走行车接口后，和 APA 泊车接口同时存在时，EBCM 需要明确仲裁，否则可能出现制动请求互相覆盖。",
        "安全动作过强风险：500ms 后直接刹停或拉 EPB 在行驶状态下风险很高，必须绑定车速、挡位、驾驶员接管和当前功能模式。",
        "兼容风险：旧版 APA/HPP 逻辑仍按泊车接口理解，接口切换后可能影响既有泊车功能、故障处理和诊断报文解释。",
        "可诊断性风险：如果不补充超时原因、请求来源、响应状态等日志，后续问题只能看到降级结果，看不清到底是 ADAS 未发、EBCM 未收、EBCM 拒绝还是总线异常。",
    ]

    checklist_lines = [
        "补齐行泊切换 flag 的 ICD 定义：信号名、取值、发送方、周期、超时、有效位。",
        "补齐 ACC/HPP 行车接口请求和 EBCM 响应矩阵：请求值、期望响应值、失败/拒绝/等待状态。",
        "补齐 APA 泊车接口与行车接口并存时的优先级和互斥条件。",
        "补齐 500ms 计时规范：计时起点、终点、重入、清除条件、异常上报码。",
        "补齐降级/刹停/EPB 的安全门槛：车速、挡位、驾驶员接管、功能状态、EBCM 故障状态。",
    ]

    evidence_lines = _format_review_evidence_lines(brake_signals, park_signals, switch_signals, parameter_hits, doc_hits)

    lines = [
        "执行器需求评审",
        "",
        "结论先行",
        "这个需求方向可以讨论，但不能只靠现有 EBCM ICD 里已有信号直接落地。现有文档能支撑“行车制动接口”和“泊车制动接口”各自的请求/响应闭环；但“行泊切换 flag + 500ms 未握手 + 降级/刹停/拉 EPB”的完整策略更像新增系统需求，需要补充接口定义、超时语义、仲裁优先级和安全动作边界。",
        "",
        "需求理解",
        "- ACC 和 HPP 计划改用行车接口发制动扭矩请求，APA 继续使用泊车接口。",
        "- 行/泊切换时，制动侧需要监控握手是否成功；如果 500ms 内行车扭矩接口未完成握手，需要进入降级、刹车或拉 EPB。",
        "- 你要评估这个需求是否合理，以及实现时应该看哪些信号、时序和风险。",
        "",
        "现有文档能支撑的部分",
        *_prefix_lines(support_lines),
        "",
        "当前缺口",
        *_prefix_lines(missing_lines),
        "",
        "实施关注点",
        *_prefix_lines(implementation_lines),
        "",
        "主要风险点",
        *_prefix_lines(risk_lines),
        "",
        "建议补齐的检查清单",
        *_prefix_lines(checklist_lines),
    ]
    if evidence_lines:
        lines.extend(["", "本地检索到的证据线索", *evidence_lines])
    return "\n".join(lines)


def _prefix_lines(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items if item]


def _format_review_signal_names(hits: list[tuple[int, str, dict[str, str]]], limit: int) -> str:
    names = []
    for _, signal, metadata in hits[:limit]:
        concept = _display_text(metadata.get("concept", ""))
        if concept:
            names.append(f"{signal}（{concept}）")
        else:
            names.append(signal)
    return "、".join(names) or "未命中明确名称"


def _format_review_rule_titles(rules: list[Rule], limit: int) -> str:
    titles = []
    for rule in rules[:limit]:
        title = human_rule_title(rule)
        if title and title not in titles:
            titles.append(title)
    return "、".join(titles) or "未命中明确场景"


def _format_review_evidence_lines(
    brake_signals: list[tuple[int, str, dict[str, str]]],
    park_signals: list[tuple[int, str, dict[str, str]]],
    switch_signals: list[tuple[int, str, dict[str, str]]],
    parameter_hits: list[tuple[int, dict[str, str]]],
    doc_hits: list[tuple[int, dict[str, str]]],
) -> list[str]:
    lines: list[str] = []
    if brake_signals:
        lines.append(f"- 行车制动接口相关信号：{_format_review_signal_names(brake_signals, 6)}")
    if park_signals:
        lines.append(f"- 泊车制动接口相关信号：{_format_review_signal_names(park_signals, 6)}")
    if switch_signals:
        lines.append(f"- 行/泊状态相近信号：{_format_review_signal_names(switch_signals, 4)}")
    if parameter_hits:
        params = []
        for _, row in parameter_hits[:4]:
            value = row.get("default_value") or "需项目定义"
            unit = row.get("unit") or ""
            params.append(f"{row.get('parameter_id', '')}={value}{unit}")
        lines.append(f"- 相近超时/阈值参数：{'、'.join(item for item in params if item)}")
    if doc_hits:
        sources = []
        for _, item in doc_hits:
            source = item.get("source", "")
            if source and source not in sources:
                sources.append(source)
        if sources:
            lines.append(f"- 原始文档命中来源：{'、'.join(sources[:3])}")
    return lines


def run_actuator_handshake_analysis(
    file_path: str | Path,
    user_text: str = "",
    artifact_root: str | Path | None = None,
) -> ActuatorAnalysisResult:
    path = Path(file_path)
    return run_actuator_handshake_analysis_with_database(
        file_path,
        user_text=user_text,
        database_path=None,
        artifact_root=artifact_root,
    )


def run_actuator_handshake_analysis_with_database(
    file_path: str | Path,
    user_text: str = "",
    database_path: str | Path | None = None,
    artifact_root: str | Path | None = None,
) -> ActuatorAnalysisResult:
    path = Path(file_path)
    if path.suffix.lower() not in ACTUATOR_DATA_SUFFIXES:
        raise ValueError("执行器握手诊断当前支持 `.blf`、`.h5`、`.hdf5` 总线数据。")

    rules = _rank_rules(user_text, load_rules())
    if not rules:
        rules = load_rules()

    run_artifact_root = Path(artifact_root) if artifact_root is not None else (
        get_actuator_runtime_dir() / f"{path.stem}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    )
    analysis_path = path
    if path.suffix.lower() == ".blf":
        if database_path is None:
            raise ValueError("执行器握手诊断处理 `.blf` 需要配套 `.arxml` 数据库；请先指定数据库版本，或先上传数据库文件。")
        analysis_path = convert_blf_to_h5_for_actuator(
            path,
            Path(database_path),
            rules,
            output_root=run_artifact_root / "converted_h5",
        )

    session = FolderSession(analysis_path)
    index = SignalIndex(session)
    parameters = load_parameters()
    violations: list[ActuatorViolation] = []
    skipped: list[tuple[str, str]] = []
    missing_by_rule: dict[str, list[str]] = {}

    for rule in rules:
        if not rule.enabled:
            continue
        needed = sorted(_rule_signal_names(rule))
        missing = [name for name in needed if not index.resolve(name)]
        if missing:
            missing_by_rule[rule.rule_id] = missing[:12]
            skipped.append((rule.rule_id, "缺少关键信号：" + ", ".join(missing[:6])))
            continue

        try:
            rule_violations = _evaluate_rule(rule, index, parameters)
        except NotImplementedError as exc:
            skipped.append((rule.rule_id, str(exc)))
            continue
        except Exception as exc:
            skipped.append((rule.rule_id, f"规则执行失败：{exc}"))
            continue
        violations.extend(rule_violations[:5])

    status = "issue" if violations else "ok"
    warnings: list[str] = []
    if skipped:
        warnings.append(f"有 {len(skipped)} 条规则因缺少信号或复杂触发条件未自动判定。")
    timeline_lines = build_actuator_timeline(index, rules, violations)
    image_paths = build_actuator_plots(
        analysis_path,
        index,
        rules,
        violations,
        output_root=run_artifact_root / "plots",
    )
    return ActuatorAnalysisResult(
        status=status,
        file_name=path.name,
        checked_rules=len(rules) - len(skipped),
        violations=violations,
        skipped_rules=skipped,
        missing_signals=missing_by_rule,
        warnings=warnings,
        timeline_lines=timeline_lines,
        image_paths=image_paths,
    )


def build_actuator_timeline(
    index: "SignalIndex",
    rules: list[Rule],
    violations: list[ActuatorViolation],
    limit: int = 16,
) -> list[str]:
    signals = _selected_timeline_signals(rules, violations)
    rows: list[tuple[float, str, Any, Any, str]] = []
    for signal in signals:
        try:
            timestamps, values = index.values(signal)
        except Exception:
            continue
        previous = None
        for timestamp, value in zip(timestamps, values):
            if previous is None:
                previous = value
                continue
            if _value_equals(previous, value):
                continue
            note = _timeline_note(signal, value, rules, violations, float(timestamp))
            rows.append((float(timestamp), signal, previous, value, note))
            previous = value
            if len(rows) >= limit * 4:
                break

    if not rows:
        snapshots: list[str] = []
        for signal in signals:
            try:
                timestamps, values = index.values(signal)
            except Exception:
                continue
            if not timestamps:
                continue
            snapshots.append(
                f"{float(timestamps[0]):.3f}s — {signal} = {_format_signal_value(signal, values[0])} ← 数据段内未发生状态变化"
            )
            if len(snapshots) >= min(limit, 8):
                break
        return snapshots

    rows.sort(key=lambda item: item[0])
    lines = []
    for violation in violations[:5]:
        lines.append(
            f"{violation.start_time:.3f}s - {violation.end_time:.3f}s — 问题窗口："
            f"{_issue_window_detail(violation, index)}"
        )

    if violations:
        window_rows = []
        for row in rows:
            timestamp = row[0]
            if any(violation.start_time - 1.0 <= timestamp <= violation.end_time + 1.0 for violation in violations):
                window_rows.append(row)
        if len(window_rows) < max(6, limit // 2):
            selected: list[tuple[float, str, Any, Any, str]] = []
            for violation in violations:
                nearest = sorted(rows, key=lambda item: (abs(item[0] - violation.start_time), item[0]))[: max(3, limit // max(len(violations), 1))]
                selected.extend(nearest)
            seen = set()
            window_rows = []
            for row in sorted(selected, key=lambda item: item[0]):
                key = (round(row[0], 6), row[1])
                if key in seen:
                    continue
                seen.add(key)
                window_rows.append(row)
        rows = window_rows[: max(0, limit - len(lines))]
    else:
        rows = rows[:limit]

    for timestamp, signal, old_value, new_value, note in rows:
        change = f"{_format_signal_value(signal, old_value)}→{_format_signal_value(signal, new_value)}"
        signal_label = _signal_timeline_label(signal)
        suffix = f" ← {note}" if note else ""
        lines.append(f"{timestamp:.3f}s — {signal}{signal_label} {change}{suffix}")
    return lines


def build_actuator_plots(
    analysis_path: Path,
    index: "SignalIndex",
    rules: list[Rule],
    violations: list[ActuatorViolation],
    output_root: str | Path | None = None,
) -> list[Path]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return []

    signals = _selected_timeline_signals(rules, violations)[:6]
    series: dict[str, tuple[list[float], list[float]]] = {}
    for signal in signals:
        try:
            timestamps, values = index.values(signal)
        except Exception:
            continue
        clean_axis: list[float] = []
        clean_values: list[float] = []
        for timestamp, value in zip(timestamps, values):
            numeric = _plot_float(value)
            if numeric is None:
                continue
            clean_axis.append(float(timestamp))
            clean_values.append(numeric)
        if clean_axis:
            series[signal] = _downsample_series(clean_axis, clean_values)

    if not series:
        return []

    plot_dir = Path(output_root) if output_root is not None else (
        get_actuator_runtime_dir()
        / f"{analysis_path.stem}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        / "plots"
    )
    plot_dir = ensure_writable_dir(plot_dir)
    event_windows = [
        {
            "start": item.start_time,
            "end": item.end_time,
            "title": human_rule_title(item.rule),
            "reason": human_violation(item.rule),
        }
        for item in violations
    ]
    if not event_windows:
        event_windows = [{"start": None, "end": None, "title": "关键接口信号", "reason": "未定位异常点，仅展示状态变化"}]

    paths: list[Path] = []
    for index_no, event in enumerate(event_windows, start=1):
        output_path = plot_dir / f"actuator_timeline_{index_no}.png"
        _draw_actuator_plot(Image, ImageDraw, ImageFont, output_path, series, event)
        paths.append(output_path)
    return paths


def _selected_timeline_signals(rules: list[Rule], violations: list[ActuatorViolation]) -> list[str]:
    ordered: list[str] = []
    source_rules = [item.rule for item in violations] or rules[:3]
    for rule in source_rules:
        for signal in list(rule.evidence_signals) + [str(item.get("signal") or "") for item in rule.preconditions + rule.expected]:
            if signal and signal not in ordered:
                ordered.append(signal)
    return ordered[:10]


def _timeline_note(
    signal: str,
    value: Any,
    rules: list[Rule],
    violations: list[ActuatorViolation],
    timestamp: float,
) -> str:
    for rule in rules[:5]:
        for condition in rule.preconditions:
            if str(condition.get("signal") or "") == signal and _condition_ok(value, condition):
                return f"触发条件满足：{human_rule_title(rule)}"
        for condition in rule.expected:
            if str(condition.get("signal") or "") == signal and _condition_ok(value, condition):
                return f"目标响应出现：{human_rule_title(rule)}"
    for violation in violations:
        if violation.start_time <= timestamp <= violation.end_time and signal in _rule_signal_names(violation.rule):
            return f"问题时间窗：{human_rule_title(violation.rule)}"
    return ""


def _signal_timeline_label(signal: str) -> str:
    metadata = load_signal_metadata().get(signal, {})
    concept = (metadata.get("concept") or metadata.get("signal_long_name") or "").lower()
    bus = metadata.get("bus") or ""
    rx = metadata.get("rx") or ""
    tx = metadata.get("tx") or ""
    if any(token in concept for token in ["brake", "braking", "ebcm"]) or "EBCM" in rx:
        return "（制动请求）"
    if any(token in concept for token in ["propulsion", "axle torque", "engine"]) or any(token in tx for token in ["ECM", "ECP", "PDF"]):
        return "（驱动响应）"
    if any(token in concept for token in ["speed", "wheel"]):
        return "（车速/轮速）"
    if any(token in concept for token in ["pedal"]):
        return "（踏板信号）"
    if bus:
        return f"（{bus}）"
    return ""


def _plot_float(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _downsample_series(axis: list[float], values: list[float], limit: int = 1200) -> tuple[list[float], list[float]]:
    if len(axis) <= limit:
        return axis, values
    step = max(1, len(axis) // limit)
    return axis[::step], values[::step]


def _draw_actuator_plot(
    Image: Any,
    ImageDraw: Any,
    ImageFont: Any,
    output_path: Path,
    series: dict[str, tuple[list[float], list[float]]],
    event: dict[str, Any],
) -> None:
    font = ImageFont.load_default()
    width = 1700
    left = 150
    right = 45
    top = 90
    row_height = 165
    bottom = 55
    height = top + bottom + row_height * len(series)
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)

    all_axis = [timestamp for axis, _values in series.values() for timestamp in axis]
    time_min = min(all_axis)
    time_max = max(all_axis)
    if event.get("start") is not None:
        start = float(event["start"])
        end = float(event["end"])
        time_min = max(time_min, start - 3.0)
        time_max = min(time_max, end + 3.0)
    if time_max <= time_min:
        time_max = time_min + 1.0

    plot_left = left
    plot_right = width - right
    colors = [(38, 111, 214), (224, 128, 31), (38, 150, 96), (170, 77, 210), (210, 70, 85), (80, 80, 80)]

    def map_x(timestamp: float) -> float:
        return plot_left + (timestamp - time_min) / (time_max - time_min) * (plot_right - plot_left)

    draw.text((left, 18), f"执行器接口信号时序：{event.get('title') or '关键状态'}", fill=(30, 30, 30), font=font)
    draw.text((left, 42), str(event.get("reason") or ""), fill=(90, 90, 90), font=font)

    for row_index, (signal, (axis, values)) in enumerate(series.items()):
        row_top = top + row_index * row_height
        plot_top = row_top + 25
        plot_bottom = plot_top + 105
        clipped = [(timestamp, value) for timestamp, value in zip(axis, values) if time_min <= timestamp <= time_max]
        if not clipped:
            continue
        y_values = [item[1] for item in clipped]
        y_min = min(y_values)
        y_max = max(y_values)
        if abs(y_max - y_min) < 1e-9:
            y_min -= 1.0
            y_max += 1.0

        def map_y(value: float) -> float:
            return plot_top + (y_max - value) / (y_max - y_min) * (plot_bottom - plot_top)

        draw.text((plot_left, row_top + 3), signal, fill=(40, 40, 40), font=font)
        for tick in range(5):
            y_value = y_min + (y_max - y_min) * tick / 4
            y = map_y(y_value)
            draw.line((plot_left, y, plot_right, y), fill=(235, 235, 235), width=1)
            draw.text((18, y - 6), f"{y_value:.3g}", fill=(120, 120, 120), font=font)
        for tick in range(6):
            t = time_min + (time_max - time_min) * tick / 5
            x = map_x(t)
            draw.line((x, plot_top, x, plot_bottom), fill=(245, 245, 245), width=1)
            draw.text((x - 18, plot_bottom + 8), f"{t:.1f}s", fill=(120, 120, 120), font=font)

        if event.get("start") is not None:
            x1 = map_x(max(float(event["start"]), time_min))
            x2 = map_x(min(float(event["end"]), time_max))
            draw.rectangle((x1, plot_top, max(x2, x1 + 3), plot_bottom), fill=(255, 225, 225))
            draw.line((x1, plot_top, x1, plot_bottom), fill=(220, 45, 45), width=2)
            draw.line((x2, plot_top, x2, plot_bottom), fill=(220, 45, 45), width=2)

        color = colors[row_index % len(colors)]
        points = [(map_x(timestamp), map_y(value)) for timestamp, value in clipped]
        if len(points) >= 2:
            draw.line(points, fill=color, width=2)
        elif points:
            x, y = points[0]
            draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=color)
        draw.line((plot_left, plot_bottom, plot_right, plot_bottom), fill=(150, 150, 150), width=1)
        draw.line((plot_left, plot_top, plot_left, plot_bottom), fill=(150, 150, 150), width=1)

    image.save(output_path)


def format_actuator_markdown(open_id: str, result: ActuatorAnalysisResult, request_text: str = "") -> str:
    if result.violations:
        headline = (
            f"<font color='red'>**发现 {len(result.violations)} 个执行器接口风险点**</font>\n"
            f"<font color='red'>**核心结论：{_summarize_violations(result.violations)}**</font>"
        )
    elif result.checked_rules > 0:
        headline = (
            "<font color='green'>**未识别到明确的执行器断握手/降级异常**</font>\n"
            "<font color='green'>**核心结论：已跑通可判定规则，当前数据未命中明显违规场景。**</font>"
        )
    else:
        headline = (
            "<font color='grey'>**暂未形成自动诊断结论**</font>\n"
            "<font color='grey'>**核心结论：当前数据没有跑通可自动判定规则，需要补充匹配的接口信号或继续用文字查询设计原因。**</font>"
        )

    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        "**工作类型**：执行器握手诊断",
        f"**上传文件**：`{result.file_name}`",
    ]
    if request_text:
        lines.append(f"**用户需求**：{request_text}")
    lines.extend(["", headline, ""])

    if result.violations:
        lines.extend(
            [
                "**诊断核心结论**",
                f"- 风险点数量：<font color='red'>**{len(result.violations)} 个**</font>",
                f"- 最需要关注：<font color='red'>**{_issue_title(result.violations[0])}**</font>",
                f"- 风险时间窗：{_issue_time_ranges(result.violations)}",
                "",
                "**异常明细表**",
                "| # | 问题 | 时间窗 | 未响应信号 | 实际值 | 期望响应 |",
                "|---|---|---:|---|---|---|",
            ]
        )
        for index, item in enumerate(result.violations[:8], start=1):
            lines.append(
                "| "
                f"{index} | "
                f"<font color='red'>**{_issue_title(item)}**</font> | "
                f"{item.start_time:.3f}s-{item.end_time:.3f}s | "
                f"{_issue_failed_signal_names(item)} | "
                f"{_issue_actual_values(item)} | "
                f"{_human_expected(item.rule)} |"
            )
    else:
        lines.extend(
            [
                "**诊断状态**",
                "| 内容 | 状态 |",
                "|---|---|",
                f"| 自动判定规则 | {result.checked_rules} 条 |",
                "| 覆盖范围 | 制动/EBCM/ebooster、驱动/PDF/ECP/ECM、ACC/APA/SRP 握手和状态响应 |",
            ]
        )

    if result.timeline_lines:
        lines.extend(["", "**关键状态时序**"])
        for line in result.timeline_lines[:16]:
            lines.append(f"- {line}")
    if result.image_paths:
        lines.extend(
            [
                "",
                "**发送内容状态**",
                "| 内容 | 状态 |",
                "|---|---|",
                "| 完整诊断报告 | 已发送 |",
                f"| 关键信号时序图 | 已生成 {len(result.image_paths)} 张，随后发送 |",
                "| 红色阴影 | 标记识别到的问题时间窗 |",
            ]
        )

    user_hints: list[str] = []
    if result.checked_rules <= 0:
        if result.missing_signals:
            user_hints.append(
                "当前数据里缺少这次判断需要的关键接口信号，建议确认 BLF 和 ARXML 是否匹配，"
                "或者补充包含目标接口报文的数据片段。"
            )
        else:
            user_hints.append(
                "当前问题还没有可直接自动判定的数据规则，我已隐藏内部规则明细；"
                "如果你只是想查原因或设计逻辑，可以继续直接用文字问。"
            )
    elif result.missing_signals and len(result.missing_signals) > result.checked_rules:
        user_hints.append("部分扩展规则因为数据里缺少对应接口信号未参与判断，当前结论以已跑通规则为准。")

    if user_hints:
        lines.extend(["", "**补充说明**"])
        for hint in user_hints:
            lines.append(f"- {hint}")

    return "\n".join(lines)


def actuator_result_headline(result: ActuatorAnalysisResult) -> str:
    if result.violations:
        return (
            f"<font color='red'>**发现 {len(result.violations)} 个执行器接口风险点**</font>\n"
            f"<font color='red'>**核心结论：{_summarize_violations(result.violations)}**</font>"
        )
    if result.checked_rules > 0:
        return (
            "<font color='green'>**未识别到明确的执行器断握手/降级异常**</font>\n"
            "<font color='green'>**核心结论：已跑通可判定规则，当前数据未命中明显违规场景。**</font>"
        )
    return (
        "<font color='grey'>**当前数据还不能直接判定执行器响应异常**</font>\n"
        "<font color='grey'>**核心结论：没有在解码后的数据里匹配到足够的执行器接口信号，建议先确认 BLF 与数据库版本、CAN 通道和 AEB/ACC 制动接口报文是否对应。**</font>"
    )


def actuator_result_summary_rows(result: ActuatorAnalysisResult) -> list[tuple[str, str]]:
    if result.violations:
        return [
            ("风险点数量", f"<font color='red'>**{len(result.violations)} 个**</font>"),
            ("最需要关注", f"<font color='red'>**{_issue_title(result.violations[0])}**</font>"),
            ("风险时间窗", _issue_time_ranges(result.violations)),
            ("已判定规则", f"{result.checked_rules} 条"),
        ]
    return [
        ("诊断状态", "<font color='green'>**未发现明确异常**</font>" if result.checked_rules else "缺少可判定信号"),
        ("已跑通检查", f"{result.checked_rules} 条规则" if result.checked_rules else "0 条，当前数据/数据库未匹配到完整信号"),
        ("建议先看", "数据库版本、CAN 通道、AEB/ACC 制动请求与 EBCM 自动制动响应信号"),
    ]


def actuator_issue_rows(result: ActuatorAnalysisResult, limit: int = 8) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for index, item in enumerate(result.violations[:limit], start=1):
        rows.append(
            {
                "index": str(index),
                "issue": _issue_title(item),
                "time_window": f"{item.start_time:.3f}s-{item.end_time:.3f}s",
                "failed_signals": _issue_failed_signal_names(item),
                "actual": _issue_actual_values(item),
                "expected": _human_expected(item.rule),
            }
        )
    return rows


def load_rules() -> list[Rule]:
    rows: list[Rule] = []
    with RULE_CSV.open("r", encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            rows.append(
                Rule(
                    rule_id=row.get("rule_id", ""),
                    domain=row.get("domain", ""),
                    subsystem=row.get("subsystem", ""),
                    feature=row.get("feature", ""),
                    scenario=row.get("scenario", ""),
                    rule_type=row.get("rule_type", ""),
                    preconditions=_loads_list(row.get("preconditions")),
                    trigger=row.get("trigger", ""),
                    expected=_loads_list(row.get("expected")),
                    time_window_ms=row.get("time_window_ms", ""),
                    violation=row.get("violation", ""),
                    severity=row.get("severity", ""),
                    owner_hint=row.get("owner_hint", ""),
                    evidence_signals=_loads_list(row.get("evidence_signals")),
                    next_checks=_loads_list(row.get("next_checks")),
                    source_doc=row.get("source_doc", ""),
                    notes=row.get("notes", ""),
                    enabled=str(row.get("enabled", "true")).strip().lower() != "false",
                )
            )
    return rows


def load_parameters() -> dict[str, float]:
    params: dict[str, float] = {
        "immediate": 50.0,
        "continuous": 500.0,
        "immediate_or_ACC_disengage_time": 500.0,
    }
    with PARAMETER_CSV.open("r", encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            value = str(row.get("default_value") or "").strip()
            if not value:
                continue
            try:
                params[str(row.get("parameter_id"))] = float(value)
            except ValueError:
                continue
    return params


def load_signal_names() -> set[str]:
    global _SIGNAL_NAMES
    if _SIGNAL_NAMES is not None:
        return _SIGNAL_NAMES
    names: set[str] = set()
    with SIGNAL_CSV.open("r", encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            short_name = str(row.get("short_name") or "").strip()
            if short_name:
                names.add(short_name)
    _SIGNAL_NAMES = names
    return names


def load_signal_to_message() -> dict[str, str]:
    global _SIGNAL_TO_MESSAGE
    if _SIGNAL_TO_MESSAGE is not None:
        return _SIGNAL_TO_MESSAGE
    mapping: dict[str, str] = {}
    with SIGNAL_CSV.open("r", encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            short_name = str(row.get("short_name") or "").strip()
            message = str(row.get("message") or "").strip()
            if short_name and message:
                mapping.setdefault(short_name, message)
    _SIGNAL_TO_MESSAGE = mapping
    return mapping


def load_signal_metadata() -> dict[str, dict[str, str]]:
    global _SIGNAL_METADATA
    if _SIGNAL_METADATA is not None:
        return _SIGNAL_METADATA
    metadata: dict[str, dict[str, str]] = {}
    with SIGNAL_CSV.open("r", encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            short_name = str(row.get("short_name") or "").strip()
            if short_name:
                metadata[short_name] = {key: str(value or "").strip() for key, value in row.items()}
    _SIGNAL_METADATA = metadata
    return metadata


def load_state_machines() -> list[dict[str, Any]]:
    global _STATE_MACHINE_ROWS
    if _STATE_MACHINE_ROWS is not None:
        return _STATE_MACHINE_ROWS
    rows: list[dict[str, Any]] = []
    if not STATE_MACHINE_CSV.exists():
        _STATE_MACHINE_ROWS = rows
        return rows
    with STATE_MACHINE_CSV.open("r", encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            states = _loads_list(row.get("states"))
            rows.append(
                {
                    "state_machine_id": str(row.get("state_machine_id") or "").strip(),
                    "states": states,
                }
            )
    _STATE_MACHINE_ROWS = rows
    return rows


def load_parameter_rows() -> list[dict[str, str]]:
    global _PARAMETER_ROWS
    if _PARAMETER_ROWS is not None:
        return _PARAMETER_ROWS
    rows: list[dict[str, str]] = []
    if not PARAMETER_CSV.exists():
        _PARAMETER_ROWS = rows
        return rows
    with PARAMETER_CSV.open("r", encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            rows.append({key: str(value or "").strip() for key, value in row.items()})
    _PARAMETER_ROWS = rows
    return rows


def load_docx_segments() -> list[dict[str, str]]:
    global _DOCX_SEGMENTS
    if _DOCX_SEGMENTS is not None:
        return _DOCX_SEGMENTS
    segments: list[dict[str, str]] = []
    for path in DOCX_FILES:
        if not path.exists():
            continue
        paragraphs = _extract_docx_paragraphs(path)
        segments.extend(_chunk_paragraphs_for_search(path.name, paragraphs))
    _DOCX_SEGMENTS = segments
    return segments


def _extract_docx_paragraphs(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            document_xml = archive.read("word/document.xml")
    except Exception:
        return []

    try:
        root = ET.fromstring(document_xml)
    except Exception:
        return []

    paragraphs: list[str] = []
    for paragraph in root.iter():
        if not paragraph.tag.endswith("}p"):
            continue
        texts = [
            node.text
            for node in paragraph.iter()
            if node.tag.endswith("}t") and node.text
        ]
        text = _compact_text("".join(texts), 2000)
        if len(text) >= 2:
            paragraphs.append(text)
    return paragraphs


def _chunk_paragraphs_for_search(source: str, paragraphs: list[str]) -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        if current and current_len + len(paragraph) > 900:
            chunks.append({"source": source, "text": "\n".join(current)})
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += len(paragraph)
    if current:
        chunks.append({"source": source, "text": "\n".join(current)})
    return chunks


def _rank_signal_metadata(user_text: str, limit: int = 8) -> list[tuple[int, str, dict[str, str]]]:
    tokens = _query_tokens(user_text)
    scope = _infer_scope(user_text)
    hits: list[tuple[int, str, dict[str, str]]] = []
    for signal, metadata in load_signal_metadata().items():
        blob = " ".join([signal, *metadata.values()])
        score = _score_blob(blob, tokens)
        signal_lower = signal.lower()
        if signal_lower in (user_text or "").lower():
            score += 20
        if scope == "brake" and any(token in signal_lower for token in ["brk", "brake", "ebcm", "autbrk", "accbsc"]):
            score += 2
        if scope == "propulsion" and any(token in signal_lower for token in ["accatc", "accatcs", "axl", "trq"]):
            score += 2
        if scope == "steering" and any(token in signal_lower for token in ["strg", "strng", "swar", "eps", "steer", "whl"]):
            score += 3
        if scope == "shift" and any(token in signal_lower for token in ["shift", "shf", "gear", "rng", "range", "etrs", "trans"]):
            score += 3
        if scope == "apa" and any(token in signal_lower for token in ["apa", "srp", "hpp"]):
            score += 2
        if scope == "acc" and "acc" in signal_lower:
            score += 2
        if score > 0:
            hits.append((score, signal, metadata))
    hits.sort(key=lambda item: item[0], reverse=True)
    return hits[:limit]


def _rank_state_machines(user_text: str, limit: int = 4) -> list[tuple[int, dict[str, Any]]]:
    tokens = _query_tokens(user_text)
    hits: list[tuple[int, dict[str, Any]]] = []
    for row in load_state_machines():
        blob = json.dumps(row, ensure_ascii=False)
        score = _score_blob(blob, tokens)
        if score > 0:
            hits.append((score, row))
    hits.sort(key=lambda item: item[0], reverse=True)
    return hits[:limit]


def _rank_parameter_rows(user_text: str, limit: int = 5) -> list[tuple[int, dict[str, str]]]:
    tokens = _query_tokens(user_text)
    hits: list[tuple[int, dict[str, str]]] = []
    for row in load_parameter_rows():
        blob = " ".join(row.values())
        score = _score_blob(blob, tokens)
        if score > 0:
            hits.append((score, row))
    hits.sort(key=lambda item: item[0], reverse=True)
    return hits[:limit]


def _rank_docx_segments(user_text: str, limit: int = 4) -> list[tuple[int, dict[str, str]]]:
    tokens = _query_tokens(user_text)
    scope = _infer_scope(user_text)
    hits: list[tuple[int, dict[str, str]]] = []
    for item in load_docx_segments():
        blob = f"{item.get('source', '')} {item.get('text', '')}"
        score = _score_blob(blob, tokens)
        source = item.get("source", "").lower()
        if scope == "brake" and any(token in source for token in ["ebcm", "brake"]):
            score += 2
        if scope == "propulsion" and any(token in source for token in ["ps icd", "adas"]):
            score += 2
        if scope == "steering" and any(token in source for token in ["steering", "eps", "electric power steering"]):
            score += 4
        if scope == "shift" and any(token in source for token in ["etrs", "shift", "换档", "换挡"]):
            score += 4
        if score > 0:
            hits.append((score, item))
    hits.sort(key=lambda item: item[0], reverse=True)
    return hits[:limit]


def _query_tokens(text: str) -> set[str]:
    value = (text or "").lower()
    tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", value)
        if len(token.strip()) >= 2
    }
    domain_terms = [
        "acc", "fsra", "aeb", "cib", "fca", "apa", "srp", "hpp",
        "ebcm", "ebooster", "pdf", "ecp", "ecm", "tpim",
        "eps", "steering", "swar", "strg", "strng", "etrs", "shift", "gear", "range",
        "驱动", "制动", "刹车", "动力", "握手", "断握手", "响应",
        "条件", "置位", "取值", "信号", "状态", "状态机", "跳转",
        "激活", "退出", "降级", "受限", "失败", "拒绝", "仲裁",
        "扭矩", "减速度", "自动制动", "请求", "文档", "规则",
        "转向", "方向盘", "转角", "角度", "换挡", "换档", "挡位", "档位",
        "需求", "变更", "合理性", "可行性", "实施", "风险", "关注点",
        "行泊", "行/泊", "行车", "泊车", "接口变更", "500ms",
    ]
    for term in domain_terms:
        if term in value:
            tokens.add(term)
    return tokens


def _score_blob(blob: str, tokens: set[str]) -> int:
    value = (blob or "").lower()
    score = 0
    for token in tokens:
        if not token:
            continue
        if token in value:
            score += 6 if "_" in token or len(token) >= 8 else 2
    return score


def _compact_text(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _format_next_checks(rule: Rule) -> str:
    if not rule.next_checks:
        return "原始文档未列出额外排查项"
    rules_by_id = {item.rule_id: item for item in load_rules()}
    readable: list[str] = []
    for item in rule.next_checks[:6]:
        text = _display_text(str(item))
        linked_rule = rules_by_id.get(str(item))
        if linked_rule:
            readable.append(human_rule_title(linked_rule))
            continue
        if text.lower().startswith("signal_timeout:"):
            signals = text.split(":", 1)[1].strip()
            readable.append(f"检查 {signals} 是否存在超时、丢失或无效")
            continue
        if re.fullmatch(r"[A-Z]{3}_[A-Z0-9_]+_\d{3}", text):
            continue
        readable.append(text)
    return "；".join(readable) or "建议结合相关制动/驱动接口条件继续排查"


def convert_blf_to_h5_for_actuator(
    blf_file: Path,
    database_path: Path,
    rules: list[Rule],
    output_root: str | Path | None = None,
) -> Path:
    import can
    import cantools
    import h5py
    import numpy as np

    patch_python_can_blf_timestamp(can)

    signal_to_message = load_signal_to_message()
    target_signals = {signal for rule in rules for signal in _rule_signal_names(rule)}
    target_messages = {signal_to_message[signal] for signal in target_signals if signal in signal_to_message}

    if not blf_file.exists():
        raise FileNotFoundError(f"BLF 文件不存在：{blf_file}")
    if blf_file.stat().st_size <= 0:
        raise ValueError(f"BLF 文件为空：{blf_file.name}")
    if not database_path.exists():
        raise FileNotFoundError(f"ARXML 数据库不存在：{database_path}")

    try:
        database = cantools.database.load_file(str(database_path), strict=False)
    except Exception as exc:
        raise RuntimeError(f"ARXML 数据库加载失败：{database_path.name}；{exc}") from exc
    output_dir = Path(output_root) if output_root is not None else (
        get_actuator_runtime_dir()
        / f"{blf_file.stem}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        / "converted_h5"
    )
    output_dir = ensure_writable_dir(output_dir)
    h5_path = output_dir / "_actuator_can_data.h5"

    rows_by_message: dict[str, list[dict[str, Any]]] = {}
    first_timestamp: float | None = None
    target_frame_count = 0
    empty_payload_count = 0
    target_channels: Counter[str] = Counter()
    target_ids: Counter[str] = Counter()

    try:
        for frame in iter_blf_messages_preserve_fd_payload(blf_file, can):
            if first_timestamp is None:
                first_timestamp = float(frame.timestamp)
            try:
                message = database.get_message_by_frame_id(int(frame.arbitration_id))
            except Exception:
                continue
            if target_messages and message.name not in target_messages:
                continue
            target_frame_count += 1
            target_channels[str(getattr(frame, "channel", ""))] += 1
            target_ids[hex(int(frame.arbitration_id))] += 1
            payload = bytes(frame.data)
            if not payload:
                empty_payload_count += 1
                continue
            try:
                decoded = message.decode(
                    payload,
                    decode_choices=False,
                    scaling=True,
                    allow_truncated=True,
                )
            except TypeError:
                decoded = message.decode(payload, decode_choices=False, scaling=True)
            except Exception:
                continue
            if not decoded:
                continue

            timestamp = float(frame.timestamp) - float(first_timestamp or frame.timestamp)
            row = {"timestamp": timestamp}
            for signal_name, value in decoded.items():
                row[str(signal_name)] = to_float_or_nan(value)
            rows_by_message.setdefault(message.name, []).append(row)
    except OSError as exc:
        raise RuntimeError(
            f"BLF 文件打开/读取失败：{blf_file.name}；{exc}。"
            "请确认这个附件是真正的 Vector BLF 文件，不是压缩包、快捷方式或上传未完成的占位文件。"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"BLF 文件解析失败：{blf_file.name}；{exc}") from exc

    if not rows_by_message:
        if target_frame_count and empty_payload_count == target_frame_count:
            channel_text = ", ".join(
                f"通道 {channel}: {count} 帧"
                for channel, count in target_channels.most_common()
                if channel
            )
            id_text = ", ".join(
                f"{frame_id}: {count} 帧"
                for frame_id, count in target_ids.most_common(8)
            )
            raise RuntimeError(
                "BLF 中找到了执行器诊断需要的 CAN FD 报文头，但这些报文的数据区为空，无法解码信号值。"
                f"目标报文共 {target_frame_count} 帧，空 payload {empty_payload_count} 帧。"
                f"{(' 分布：' + channel_text + '。') if channel_text else ''}"
                f"{(' 主要 ID：' + id_text + '。') if id_text else ''}"
                "这通常不是规则或通道选择问题，更像是采集/导出 BLF 时没有保存 CAN FD 数据区，"
                "或当前 BLF 只保留了报文头/DLC。请用 CANoe/CANalyzer 打开确认这些 FD 帧是否有 Data 字节，"
                "或重新导出包含 CAN FD payload 的 BLF。"
            )
        raise RuntimeError(
            "BLF 已读取，但没有解码出执行器握手诊断所需报文；"
            "请确认 BLF 与当前 ARXML 数据库版本匹配，并且数据里包含 ACC/AEB/APA 执行器接口相关报文。"
        )

    with h5py.File(h5_path, "w") as h5_file:
        channel = h5_file.create_group("CAN/Channel1")
        for message_name, rows in sorted(rows_by_message.items()):
            signal_names = sorted({key for row in rows for key in row.keys() if key != "timestamp"})
            dtype = [("timestamp", "f8")] + [(signal_name, "f8") for signal_name in signal_names]
            data = np.empty(len(rows), dtype=dtype)
            for index, row in enumerate(rows):
                data[index]["timestamp"] = float(row["timestamp"])
                for signal_name in signal_names:
                    data[index][signal_name] = float(row.get(signal_name, np.nan))
            channel.create_dataset(message_name, data=data, compression="gzip")
    return h5_path


def human_rule_title(rule: Rule) -> str:
    if _is_brake_rule(rule):
        prefix = "制动系统"
    elif _is_propulsion_rule(rule):
        prefix = "驱动系统"
    elif _is_steering_rule(rule):
        prefix = "转向系统"
    elif _is_shift_rule(rule):
        prefix = "换挡系统"
    else:
        prefix = rule.subsystem
    scenario = _translate_scenario(rule.scenario)
    feature = _display_feature(rule.feature)
    if feature and scenario.lower().startswith(feature.lower()):
        scenario = scenario[len(feature):].strip(" /-_")
    if feature == "AEB" and scenario.startswith("AEB "):
        scenario = scenario[4:].strip()
    if feature == "ACC" and scenario.startswith("ACC "):
        scenario = scenario[4:].strip()
    return _display_text(f"{prefix} {feature} {scenario}".strip())


def human_violation(rule: Rule) -> str:
    original = str(rule.violation or "")
    text = _display_text(rule.violation)
    replacements = {
        "ACC Brake Per Torque request active but EBCM does not enter automatic braking active/limited.": "ADAS 已发出 ACC Brake Per Torque 制动请求，但 EBCM 没有进入自动制动 Active/Limited，属于制动握手未闭环。",
        "AEB requested brake per torque but automatic braking did not become active/limited.": "AEB 已请求 Brake Per Requested Torque，但 EBCM 没有进入自动制动 Active/Limited。",
        "Brake torque request violates safety limit or causes EBCM Failed.": "制动扭矩请求触发安全限制或导致 EBCM Failed，需要检查请求值、车速和 EBCM 限制原因。",
        "No Braking requested but EBCM keeps automatic braking active beyond release threshold.": "ADAS 已请求退出自动制动，但 EBCM 仍保持制动激活，属于释放不及时。",
        "Accelerator override is true but EBCM does not release ACC automatic braking.": "驾驶员油门接管后 EBCM 没有按要求释放 ACC 自动制动。",
        "Hold requested but EBCM does not report braking/hold active.": "ADAS 请求 Hold，但 EBCM 未反馈制动/Hold 激活。",
        "EBCM reports braking failed/denied, but ACC/propulsion state does not exit/deny.": "EBCM 已上报制动失败或拒绝，但 ACC/驱动侧没有退出或拒绝请求。",
        "ACCATC_ACCAct=True but ACCATCS_RqStat is not Request Honored/Suspended within timeout.": "ACC 驱动请求已激活，但驱动系统没有在超时内反馈 Request Honored 或允许的 Suspended 状态。",
        "Brake pedal condition is active but propulsion does not set Request Denied after delay.": "制动踏板条件已触发，但驱动系统没有按要求拒绝 ACC 请求。",
        "Automatic Braking Available is false/invalid but propulsion does not deny ACC.": "自动制动不可用或无效，但驱动系统没有拒绝 ACC 请求。",
        "Request Status=Lost Arbitration lasts longer than threshold.": "驱动侧 Lost Arbitration 持续超时，可能存在仲裁无法恢复或其他扭矩请求抢占。",
        "SRP activation requested but propulsion response is not Honored or temporary Arbitration Failed.": "APA/SRP 驱动激活请求发出后，驱动系统没有反馈 Honored 或允许的临时 Arbitration Failed。",
        "APA/HPP brake request active but EBCM SRP brake response/status does not enter Active Hold/Active Control.": "APA/HPP 制动请求激活后，EBCM 没有进入 Active Hold/Active Control。",
        "Brake Per Decel/Open Loop Brake observed although this rule pack is configured for Brake Per Torque only.": "数据里出现 Brake Per Decel 或 Open Loop Brake，但当前判定按原始文档的 Brake Per Torque 接口来检查，需要确认项目配置是否一致。",
        "Brakes overheated but ACC remains engaged or auto braking does not phase out.": "制动过热时 ACC 仍保持工作，或自动制动没有按文档要求退出/降级。",
        "ACC/LCC protected signal fault but propulsion does not deny ACC.": "ACC/LCC 受保护信号异常，但驱动系统没有按文档要求拒绝 ACC 请求。",
        "Request Status=Lost Arbitration lasts longer than threshold.": "请求状态持续处于 Lost Arbitration，超过允许时间。",
        "Automatic Braking Available is false/invalid but propulsion does not deny ACC.": "自动制动不可用或无效，但驱动系统没有拒绝 ACC 请求。",
        "Brake pedal condition is active but propulsion does not set Request Denied after delay.": "制动踏板条件已触发，但驱动系统没有在延时后置为 Request Denied。",
        "ACC Brake Per Torque request active but EBCM does not enter automatic braking active/limited.": "ACC 已发出按请求扭矩制动请求，但 EBCM 没有进入自动制动 Active/Limited，属于制动握手未闭环。",
        "AEB requested brake per torque but automatic braking did not become active/limited.": "AEB 已发出按请求扭矩制动请求，但 EBCM 没有进入自动制动 Active/Limited。",
    }
    return _display_text(replacements.get(original, replacements.get(text, text or "规则期望未满足。")))


def human_owner(owner: str) -> str:
    text = _display_text(owner)
    replacements = {
        "Propulsion": "驱动系统",
        "propulsion": "驱动系统",
        "EBCM": "制动系统/EBCM",
        "brake": "制动",
        "Brake": "制动",
        "signal": "信号",
        "Signal": "信号",
        "request integrity": "请求完整性",
        "release": "释放逻辑",
        "hold control": "Hold 控制",
        "supervision": "监控逻辑",
        "integration": "集成状态",
        "arbitration": "仲裁状态",
        "safety limiter": "安全限制",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\bor\b", "或", text)
    return _display_text(text)


class SignalIndex:
    def __init__(self, session: FolderSession) -> None:
        self.session = session
        self.short_to_key: dict[str, str] = {}
        for key in session.get_store().list_signals():
            record = session.get_store().get_signal(key)
            short = str(record.get("signal") or key.rsplit(".", 1)[-1])
            for alias in self._aliases(short):
                self.short_to_key.setdefault(alias, key)
            for alias in self._aliases(key):
                self.short_to_key.setdefault(alias, key)

    @staticmethod
    def _aliases(name: str) -> set[str]:
        value = str(name or "").strip()
        if not value:
            return set()
        aliases = {value}
        tail = value.rsplit(".", 1)[-1]
        aliases.add(tail)
        if tail.startswith("I") and len(tail) > 1 and tail[1].isupper():
            aliases.add(tail[1:])
            aliases.add(value.rsplit(".", 1)[0] + "." + tail[1:] if "." in value else tail[1:])
        else:
            aliases.add("I" + tail)
            aliases.add(value.rsplit(".", 1)[0] + ".I" + tail if "." in value else "I" + tail)
        return aliases

    def resolve(self, short_name: str) -> str | None:
        if not short_name:
            return None
        for alias in self._aliases(short_name):
            if alias in self.short_to_key:
                return self.short_to_key[alias]
        for key in self.short_to_key.values():
            if any(key.endswith("." + alias) for alias in self._aliases(short_name)):
                return key
        return None

    def values(self, short_name: str) -> tuple[list[float], list[Any]]:
        key = self.resolve(short_name)
        if not key:
            raise KeyError(short_name)
        record = self.session.get_store().get_signal(key)
        return list(record["timestamps"]), list(record["values"])

    def value_at(self, short_name: str, timestamp: float) -> Any:
        key = self.resolve(short_name)
        if not key:
            raise KeyError(short_name)
        return self.session.get_store().get_value_at(key, timestamp, method="nearest")["value"]

    def any_satisfies(self, cond: dict[str, Any], start: float, end: float) -> bool:
        key = self.resolve(str(cond.get("signal") or ""))
        if not key:
            return False
        data = self.session.get_store().get_range(key, start, end)
        return any(_condition_ok(value, cond) for value in data["values"])


def _evaluate_rule(rule: Rule, index: SignalIndex, parameters: dict[str, float]) -> list[ActuatorViolation]:
    trigger_events = _find_trigger_events(rule, index)
    if not trigger_events:
        return []
    window_s = _resolve_window_seconds(rule.time_window_ms, parameters)
    violations: list[ActuatorViolation] = []
    for t_start in trigger_events[:80]:
        if not _preconditions_ok(rule.preconditions, index, t_start):
            continue
        t_end = t_start + max(window_s, 0.05)
        if rule.expected and all(index.any_satisfies(cond, t_start, t_end) for cond in rule.expected):
            continue
        evidence = {}
        for signal in rule.evidence_signals[:8]:
            try:
                evidence[signal] = index.value_at(signal, t_start)
            except Exception:
                pass
        violations.append(
            ActuatorViolation(
                rule=rule,
                start_time=t_start,
                end_time=t_end,
                reason=rule.violation,
                evidence=evidence,
            )
        )
        if len(violations) >= 3:
            break
    return violations


def _find_trigger_events(rule: Rule, index: SignalIndex) -> list[float]:
    trigger = rule.trigger or ""
    match = re.search(r"rising_edge\(([^,]+),\s*([^,]+),\s*([^)]+)\)", trigger)
    if match:
        signal, old_text, new_text = [item.strip() for item in match.groups()]
        timestamps, values = index.values(signal)
        old_value = _coerce_value(old_text, signal)
        new_value = _coerce_value(new_text, signal)
        events = []
        for idx in range(1, len(values)):
            if _value_equals(values[idx - 1], old_value) and _value_equals(values[idx], new_value):
                events.append(float(timestamps[idx]))
        return events

    conditions = _parse_inline_conditions(trigger)
    if conditions:
        timeline = sorted(
            {
                float(timestamp)
                for condition in conditions
                for timestamp in index.values(str(condition.get("signal") or ""))[0]
            }
        )
        events = []
        previous = False
        use_or = " OR " in trigger or " or " in trigger
        for timestamp in timeline:
            results = [
                _condition_ok(index.value_at(str(condition.get("signal")), timestamp), condition)
                for condition in conditions
            ]
            current = any(results) if use_or else all(results)
            if current and not previous:
                events.append(float(timestamp))
            previous = current
        return events

    if "requests braking torque" in trigger or "Brake per torque requested" in trigger:
        signal = "ACCBSCE_ACCTrq1" if "ACC" in rule.feature else "APABT_SRPBrkTrqReq"
        timestamps, values = index.values(signal)
        events = []
        previous_active = False
        for timestamp, value in zip(timestamps, values):
            active = _numeric_abs(value) > 1
            if active and not previous_active:
                events.append(float(timestamp))
            previous_active = active
        return events[:20]

    if "Brake Per Requested Torque" in trigger and rule.preconditions:
        return _events_for_condition(index, rule.preconditions[0])

    if "activation false" in trigger:
        return _events_for_condition(index, {"signal": "APAPpTrq_SRPPpSysActReq", "operator": "==", "value": 0})

    raise NotImplementedError("暂不支持该规则的复杂触发表达式")


def _events_for_condition(index: SignalIndex, condition: dict[str, Any]) -> list[float]:
    timestamps, values = index.values(str(condition.get("signal") or ""))
    events = []
    previous = False
    for timestamp, value in zip(timestamps, values):
        current = _condition_ok(value, condition)
        if current and not previous:
            events.append(float(timestamp))
        previous = current
    return events


def _preconditions_ok(preconditions: list[dict[str, Any]], index: SignalIndex, timestamp: float) -> bool:
    for condition in preconditions:
        try:
            if not _condition_ok(index.value_at(str(condition.get("signal")), timestamp), condition):
                return False
        except Exception:
            return False
    return True


def _condition_ok(value: Any, condition: dict[str, Any]) -> bool:
    operator = str(condition.get("operator") or "").strip()
    expected = condition.get("value")
    if operator == "==":
        return _value_equals(value, expected)
    if operator == "!=":
        return not _value_equals(value, expected)
    if operator == "in":
        return any(_value_equals(value, item) for item in (expected or []))
    if operator == ">":
        return _to_float(value) > _to_float(expected)
    if operator == "<":
        return _to_float(value) < _to_float(expected)
    return False


def _parse_inline_conditions(text: str) -> list[dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    for signal, value in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*==\s*(True|False|[0-9_.-]+)", text):
        conditions.append({"signal": signal, "operator": "==", "value": _coerce_value(value, signal)})
    for signal, values in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s+in\s+\[([^\]]+)\]", text):
        parsed = [_coerce_value(item.strip(), signal) for item in values.split(",")]
        conditions.append({"signal": signal, "operator": "in", "value": parsed})
    return conditions


def _rule_signal_names(rule: Rule) -> set[str]:
    known_signals = load_signal_names()
    names = set(rule.evidence_signals)
    for group in (rule.preconditions, rule.expected):
        for condition in group:
            signal = condition.get("signal")
            if signal and signal != "DIAGNOSTIC_FLAG":
                names.add(str(signal))
    names.update(
        token for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", rule.trigger)
        if token in known_signals
    )
    ignored = {"OR", "AND", "True", "False", "continuously", "rising_edge", "in"}
    return {name for name in names if name in known_signals and name not in ignored and not name.startswith("K_")}


def _rank_rules(user_text: str, rules: list[Rule]) -> list[Rule]:
    value = (user_text or "").lower()
    scope = _infer_scope(user_text)
    scored: list[tuple[int, Rule]] = []
    for rule in rules:
        if not rule.enabled:
            continue
        blob = " ".join(
            [
                rule.rule_id,
                rule.domain,
                rule.subsystem,
                rule.feature,
                rule.scenario,
                rule.rule_type,
                rule.violation,
                rule.owner_hint,
                rule.time_window_ms,
                rule.notes,
                " ".join(rule.evidence_signals),
            ]
        ).lower()
        score = 0
        if scope == "brake":
            if rule.domain.lower() == "brake" or "ebcm" in rule.subsystem.lower():
                score += 8
            elif _is_brake_rule(rule):
                score += 3
        if scope == "propulsion":
            if rule.domain.lower() == "driver" or "propulsion" in rule.subsystem.lower() or "tpim" in rule.subsystem.lower():
                score += 8
            elif _is_propulsion_rule(rule):
                score += 3
        if scope == "steering" and _is_steering_rule(rule):
            score += 8
        if scope == "shift" and _is_shift_rule(rule):
            score += 8
        if scope == "apa" and "apa" in blob:
            score += 4
        if scope == "acc" and "acc" in blob:
            score += 4
        if any(token in value for token in STEERING_TERMS):
            if any(token in blob for token in ["steering", "eps", "strg", "strng", "swar", "wheel", "angle overlay", "torque overlay"]):
                score += 5
            if any(token in value for token in ["apa", "hpp", "泊车", "swar"]) and any(token in blob for token in ["apa", "hpp", "park", "swar"]):
                score += 4
        if any(token in value for token in SHIFT_TERMS):
            if any(token in blob for token in ["etrs", "shift", "transmission", "range", "gear", "apatransrng", "elecshf"]):
                score += 5
            if any(token in value for token in ["apa", "泊车"]) and any(token in blob for token in ["apa", "park"]):
                score += 4
            if any(token in value for token in ["目标挡位", "挡位请求", "档位请求", "range request"]):
                if "shift_request_response" in rule.rule_id.lower() or "range request" in blob:
                    score += 12
            if any(token in value for token in ["激活", "activation", "active"]):
                if "handshake" in rule.rule_id.lower() or "activation" in blob:
                    score += 6
        if any(token in value for token in ["超时", "timeout", "t1max", "时间窗", "多久"]):
            if any(token in blob for token in ["timeout", "t1max", "time", "duration", "k_"]):
                score += 6
            if "timeout" in rule.rule_id.lower() or "timeout" in rule.scenario.lower():
                score += 14
        if any(token in value for token in ["重刹", "急刹", "制动", "刹车", "brake", "braking"]):
            if any(token in blob for token in ["brake", "braking", "brktrq", "autbrk", "brksys", "accbsce", "fcacbsc"]):
                score += 5
            if any(token in value for token in ["重刹", "急刹"]) and any(token in blob for token in ["brake per torque", "brake torque", "accbsce_acctrq", "autbrk"]):
                score += 6
            if any(token in value for token in ["重刹", "急刹"]) and any(token in blob for token in ["brake per torque activation", "brake torque safety limit", "accbsce_acctrq1"]):
                score += 10
            if any(token in value for token in ["重刹", "急刹"]) and any(token in blob for token in ["automatic braking unavailable", "interface mode validation"]):
                score -= 8
            if any(token in value for token in ["重刹", "急刹"]) and any(token in blob for token in ["brake pedal", "brkped"]):
                score -= 5
            if any(token in blob for token in ["propulsion handshake", "protected signal", "lost arbitration"]):
                score -= 2
        if any(token in value for token in ["aeb", "fca"]):
            if any(token in blob for token in ["aeb", "fca", "fcacbsc"]):
                score += 8
            else:
                score -= 7
        if any(token in value for token in HANDSHAKE_TERMS) and rule.rule_type in {"state_response", "disengagement", "fault"}:
            score += 3
        if any(token in value for token in DEGRADE_TERMS) and rule.rule_type in {"disengagement", "fault", "duration", "range_limit"}:
            score += 3
        for token in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", user_text or ""):
            token_l = token.lower()
            if len(token_l) >= 2 and token_l in blob:
                score += 1
        if score > 0:
            scored.append((score, rule))
    scored.sort(key=lambda item: (item[0], _severity_rank(item[1].severity)), reverse=True)
    if scope == "steering":
        focused = [item for item in scored if _is_steering_rule(item[1])]
        if focused:
            scored = focused
    if scope == "shift":
        focused = [item for item in scored if _is_shift_rule(item[1])]
        if focused:
            scored = focused
    if any(token in value for token in ["aeb", "fca"]):
        focused = [
            item for item in scored
            if any(token in " ".join([
                item[1].feature,
                item[1].scenario,
                item[1].violation,
                " ".join(item[1].evidence_signals),
            ]).lower() for token in ["aeb", "fca", "fcacbsc"])
        ]
        if focused:
            scored = focused
    return [rule for _, rule in scored]


def _infer_scope(text: str) -> str:
    value = (text or "").lower()
    if any(term in value for term in STEERING_TERMS):
        return "steering"
    if any(term in value for term in SHIFT_TERMS):
        return "shift"
    if any(term in value for term in BRAKE_TERMS):
        return "brake"
    if any(term in value for term in PROPULSION_TERMS):
        return "propulsion"
    if "apa" in value or "泊车" in value or "srp" in value or "hpp" in value:
        return "apa"
    if "acc" in value or "跟车" in value or "纵向" in value:
        return "acc"
    return "all"


def _scope_label(scope: str) -> str:
    return {
        "brake": "制动系统（EBCM / ebooster）",
        "propulsion": "驱动系统（PDF / ECP / ECM）",
        "steering": "转向系统（EPS / 方向盘）",
        "shift": "换挡系统（ETRS / 挡位）",
        "apa": "APA / SRP / HPP",
        "acc": "ACC",
    }.get(scope, "全部执行器接口规则")


def _is_brake_rule(rule: Rule) -> bool:
    blob = f"{rule.domain} {rule.subsystem} {rule.owner_hint}".lower()
    return "brake" in blob or "ebcm" in blob


def _is_propulsion_rule(rule: Rule) -> bool:
    blob = f"{rule.domain} {rule.subsystem} {rule.owner_hint}".lower()
    return rule.domain.lower() == "driver" or "propulsion" in blob or "tpim" in blob


def _is_steering_rule(rule: Rule) -> bool:
    blob = f"{rule.domain} {rule.subsystem} {rule.feature} {rule.scenario} {rule.owner_hint} {' '.join(rule.evidence_signals)}".lower()
    return any(token in blob for token in ["steering", "eps", "strg", "strng", "swar", "wheel", "angle overlay", "torque overlay"])


def _is_shift_rule(rule: Rule) -> bool:
    blob = f"{rule.domain} {rule.subsystem} {rule.feature} {rule.scenario} {rule.owner_hint} {' '.join(rule.evidence_signals)}".lower()
    return any(token in blob for token in ["etrs", "shift", "transmission", "range", "gear", "apatransrng", "elecshf"])


def _translate_scenario(text: str) -> str:
    mapping = {
        "ACC propulsion handshake": "驱动握手",
        "Brake disengagement": "制动踏板退出",
        "Automatic braking unavailable": "自动制动不可用",
        "Brake Per Torque activation": "制动扭矩握手",
        "Brake torque safety limit": "制动扭矩限值",
        "Automatic braking release": "自动制动释放",
        "Accelerator override brake release": "油门接管释放",
        "Hold activation": "Hold 握手",
        "AEB brake per requested torque": "Brake Per Requested Torque",
        "SRP propulsion handshake": "SRP 驱动握手",
        "SRP brake response availability": "SRP 制动握手",
        "AEB brake per requested torque": "按请求扭矩制动",
        "EBCM braking denial/failure": "EBCM 制动拒绝/失败",
        "Brake overheat": "制动过热",
        "APA/HPP steering angle handshake": "APA/HPP 转向角度握手",
        "ADAS steering request status": "ADAS 转向请求状态",
        "EPS driver steering override": "驾驶员转向接管",
        "EPS steering availability": "EPS 转向可用性",
        "ETRS APA activation handshake": "换挡激活握手",
        "ETRS APA exit": "ETRS APA 退出",
        "ETRS shift request response": "目标挡位请求响应",
        "ETRS shift timeout": "换挡超时",
        "ETRS manual override": "手动接管",
    }
    return _display_text(mapping.get(text, text))


def _display_feature(text: str) -> str:
    value = _display_text(text or "")
    replacements = {
        "ACC/FSRA": "ACC",
        "FSRA/ACC": "ACC",
        "LCC/FSRA": "LCC/ACC",
        "FSRA": "ACC Stop&Go",
        "CIB": "AEB",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return value


def _display_text(text: str) -> str:
    value = str(text or "")
    replacements = {
        "Full Speed Range Adaptive Cruise Control": "ACC Stop&Go",
        "Full Speed Range ACC": "ACC Stop&Go",
        "ACC Stop&Go booster": "ACC Stop&Go with booster",
        "ACC Stop&Go LCC": "ACC Stop&Go with LCC",
        "CIB/FCA": "AEB/FCA",
        "CIB": "AEB",
        "Collision Imminent Braking": "Automatic Emergency Braking",
        "FSRA/ACC": "ACC",
        "ACC/FSRA": "ACC",
        "Request Status": "请求状态",
        "Request Honored": "请求已接受",
        "Request Denied": "请求被拒绝",
        "Lost Arbitration": "仲裁丢失",
        "Request Suspended Until Driver Input": "等待驾驶员输入",
        "Request Suspended Till Driver Input": "等待驾驶员输入",
        "is valid in ACC Stop&Go standstill": "在 ACC Stop&Go 静止等待场景下允许",
        "No Request": "无请求",
        "Not active": "未激活",
        "Active": "激活",
        "Limited": "受限",
        "Failed": "失败",
        "Unavailable": "不可用",
        "Available": "可用",
        "Active Hold": "主动保持",
        "Active Control": "主动控制",
        "No Braking": "无自动制动",
        "Brake Per Requested Torque": "按请求扭矩制动",
        "Brake Per Torque Request": "按请求扭矩制动",
        "Brake Per Decel Request": "按请求减速度制动",
        "Open Loop Brake to Stop": "开环制动到停",
        "Brake system automatic braking failed": "制动系统自动制动失败",
        "Brake system automatic braking status": "制动系统自动制动状态",
        "Automatic braking active": "自动制动激活",
        "EBCM automatic braking available": "EBCM 自动制动可用",
        "ACC automatic braking type": "ACC 自动制动类型",
        "ACC brake system command active": "ACC 制动系统命令激活",
        "ACC total brake torque request": "ACC 总制动扭矩请求",
        "ACC acceleration/deceleration request used as safety monitor with torque interface": "ACC 加减速度请求，作为扭矩接口的安全监控",
        "ACC braking active": "ACC 制动激活",
        "ACC braking request denied": "ACC 制动请求被拒绝",
        "Propulsion accelerator pedal override active": "驱动系统油门踏板接管激活",
        "Vehicle speed average driven": "车辆平均驱动轮速",
        "Brake pedal applied": "制动踏板已踩下",
        "Brake pedal initial travel achieved": "制动踏板初始行程已达到",
        "Cruise switch bank 1 status": "巡航开关组状态",
        "Propulsion limiting status for ACC axle torque command": "驱动系统对 ACC 轴扭矩命令的限制状态",
        "ADAS requested propulsion axle torque": "ADAS 请求的驱动轴扭矩",
        "Actual axle torque": "实际轴扭矩",
        "Adaptive Cruise Hold active expected after pressure built when signal available": "如果该信号可用，建压后应进入 Adaptive Cruise Hold 激活",
        "Except Park/Neutral range if gear available": "如果挡位信号可用，P/N 挡除外",
        "ACC should disengage/propulsion may deny if EBCM cannot support auto braking": "如果 EBCM 不能支持自动制动，ACC 应退出或驱动系统应拒绝请求",
        "immediate 请求被拒绝 expected": "应立即进入请求被拒绝",
        "Should clear or be treated as disengagement": "应恢复正常，否则按退出/断握手处理",
        "ACC type": "ACC 类型",
        "ADAS requests ACC propulsion control active": "ADAS 请求 ACC 驱动控制激活",
        "Brake per torque requested with corresponding acceleration safety check": "存在按请求扭矩制动请求，同时需要结合减速度安全限值检查",
        "受限 is permitted if safety/thermal limit reached": "如果达到安全或热保护限制，允许进入受限",
        "APA/HPP requests brake per torque or hold": "APA/HPP 请求按扭矩制动或 Hold",
        "SRP friction brake torque request": "SRP 摩擦制动扭矩请求",
        "SRP friction brake torque request type": "SRP 摩擦制动扭矩请求类型",
        "Brake Per Torque": "按请求扭矩制动",
        "APA brake/propulsion torque alive rolling count": "APA 制动/驱动扭矩请求滚动计数",
        "Electric Power Steering": "EPS 转向系统",
        "Steering System to Active Safety Information": "EPS 给主动安全的转向状态反馈",
        "Active Safety Steering Request": "主动安全转向请求",
        "Park Assist Parallel": "泊车辅助转向请求",
        "Steering Request": "转向请求",
        "Steering Angle Overlay": "转角叠加",
        "Steering Torque Overlay": "转向扭矩叠加",
        "EPS Override": "EPS/驾驶员接管",
        "Driver steering interference": "驾驶员转向干预",
        "Available for control": "可接受控制",
        "Permanently failed": "永久失败",
        "Temporarily Limited": "临时受限",
        "Limited System Capability": "系统能力受限",
        "Advanced Park Assist Transmission Range Activation Requested": "APA 换挡控制激活请求",
        "Advanced Park Assist Transmission Range Request": "APA 目标挡位请求",
        "Electronic Shift Advanced Park Assist Control Status Active": "ETRS APA 控制激活状态",
        "Electronic Shift Advanced Park Assist Control Status": "ETRS APA 控制状态",
        "Shift In Progress": "换挡进行中",
        "Manual Override": "驾驶员手动干预",
        "Stand by": "待命",
        "No_Request": "无请求",
        "No Request": "无请求",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = value.replace("可用 for control", "可接受控制")
    value = value.replace("驾驶员转向干预 detected", "驾驶员转向干预检测")
    value = value.replace("驾驶员转向干预 detected validity", "驾驶员转向干预检测有效位")
    value = re.sub(r"\bFSRA\b", "ACC Stop&Go", value)
    value = value.replace("Suspended", "等待驾驶员输入")
    value = value.replace("ACC Stop&Go booster", "ACC Stop&Go with booster")
    value = value.replace("ACC Stop&Go LCC", "ACC Stop&Go with LCC")
    return value


def _human_trigger(rule: Rule) -> str:
    trigger = _display_text(rule.trigger or "")
    trigger_replacements = {
        "ACCBSCE_ACCTrq1 requests braking torque": "ACCBSCE_ACCTrq1 存在制动扭矩请求",
        "Brake per torque requested": "存在按请求扭矩制动请求",
        "ACC automatic braking type becomes No Braking": "ACCBSCE_AutBrkTp1 变为 $1=无自动制动",
        "ACC 自动制动类型 becomes 无自动制动": "ACCBSCE_AutBrkTp1 变为 $1=无自动制动",
        "driver accelerator override while automatic braking active": "驾驶员油门接管且自动制动仍处于激活状态",
        "ACC requests Hold": "ACC 请求 Hold",
        "Forward Collision Alert/AEB requests 按请求扭矩制动": "AEB/FCA 请求按请求扭矩制动",
        "Forward Collision Alert/AEB requests Brake Per Requested Torque": "AEB/FCA 请求按请求扭矩制动",
    }
    if trigger in trigger_replacements:
        return trigger_replacements[trigger]
    if "rising_edge" in trigger:
        match = re.search(r"rising_edge\(([^,]+),\s*([^,]+),\s*([^)]+)\)", trigger)
        if match:
            signal, old_value, new_value = [item.strip() for item in match.groups()]
            return f"{signal} 从 {_format_signal_value(signal, old_value)} 变为 {_format_signal_value(signal, new_value)}"
    inline = _parse_inline_conditions(trigger)
    if inline:
        connector = " 或 " if (" OR " in trigger or " or " in trigger) else " 且 "
        return connector.join(_format_condition(item) for item in inline)
    text_match = re.match(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*==\s*(.+?)\s+continuously\b", trigger)
    if text_match:
        signal, value = text_match.groups()
        return f"{signal} 持续为 {_display_text(value)}"
    return trigger or "见原始文档触发条件"


def _human_expected(rule: Rule) -> str:
    if not rule.expected:
        return "这条规则需要结合原始文档里的状态流转一起看"
    return "；".join(_format_condition(item) for item in rule.expected)


def _format_evidence_signals(rule: Rule) -> str:
    if not rule.evidence_signals:
        return "规则未列出"
    condition_by_signal: dict[str, list[str]] = {}
    for label, conditions in [
        ("前提", rule.preconditions),
        ("触发", _parse_inline_conditions(rule.trigger)),
        ("正常应答", rule.expected),
    ]:
        for condition in conditions:
            signal = str(condition.get("signal") or "")
            if signal:
                condition_by_signal.setdefault(signal, []).append(f"{label}: {_format_condition(condition)}")

    parts = []
    for signal in rule.evidence_signals[:8]:
        detail = " / ".join(condition_by_signal.get(signal, []))
        if not detail:
            concept = load_signal_metadata().get(signal, {}).get("concept", "")
            detail = _display_text(concept) if concept else "辅助证据"
        parts.append(f"{signal}（{detail}）")
    return "；".join(parts)


def _format_actual_evidence(violation: ActuatorViolation) -> str:
    parts = []
    for signal, value in list(violation.evidence.items())[:8]:
        parts.append(f"{signal}={_format_signal_value(signal, value)}")
    return "；".join(parts)


def _issue_time_ranges(violations: list[ActuatorViolation]) -> str:
    if not violations:
        return "无"
    ranges = [f"{item.start_time:.3f}s-{item.end_time:.3f}s" for item in violations[:5]]
    if len(violations) > 5:
        ranges.append(f"等 {len(violations)} 个窗口")
    return "；".join(ranges)


def _issue_failed_signal_names(violation: ActuatorViolation) -> str:
    signals = [
        str(condition.get("signal") or "").strip()
        for condition in violation.rule.expected
        if str(condition.get("signal") or "").strip()
    ]
    if signals:
        return "、".join(signals[:4])
    return "、".join(list(violation.evidence.keys())[:4]) or "未定位"


def _issue_actual_values(violation: ActuatorViolation) -> str:
    parts: list[str] = []
    for condition in violation.rule.expected:
        signal = str(condition.get("signal") or "").strip()
        if not signal:
            continue
        if signal in violation.evidence:
            parts.append(f"{signal}={_format_signal_value(signal, violation.evidence[signal])}")
    if parts:
        return "；".join(parts[:4])
    return _format_actual_evidence(violation)


def _issue_title(violation: ActuatorViolation) -> str:
    failed = []
    for condition in violation.rule.expected:
        signal = str(condition.get("signal") or "")
        if not signal:
            continue
        actual = violation.evidence.get(signal)
        if actual is not None:
            failed.append(f"{signal}仍为{_format_signal_value(signal, actual)}")
        else:
            failed.append(f"{signal}未达到{_expected_short_text(condition)}")
    if failed:
        if len(failed) == 1:
            return failed[0]
        return "，".join(failed[:2])
    return human_violation(violation.rule)


def _issue_window_detail(violation: ActuatorViolation, index: "SignalIndex" | None = None) -> str:
    failed_parts: list[str] = []
    for condition in violation.rule.expected:
        signal = str(condition.get("signal") or "")
        if not signal:
            continue
        expected_text = _expected_short_text(condition)
        actual_text = ""
        if index is not None:
            actual_text = _actual_window_values(index, signal, violation.start_time, violation.end_time)
        if not actual_text and signal in violation.evidence:
            actual_text = _format_signal_value(signal, violation.evidence[signal])
        if actual_text:
            failed_parts.append(f"{signal} 未响应到 {expected_text}，窗口内实际 {actual_text}")
        else:
            failed_parts.append(f"{signal} 未响应到 {expected_text}")
    if failed_parts:
        return "；".join(failed_parts[:3])
    return f"{human_rule_title(violation.rule)}：{human_violation(violation.rule)}"


def _expected_short_text(condition: dict[str, Any]) -> str:
    signal = str(condition.get("signal") or "")
    operator = str(condition.get("operator") or "").strip()
    value = condition.get("value")
    if operator == "in" and isinstance(value, list):
        return " / ".join(_format_signal_value(signal, item) for item in value)
    if operator == "==":
        return _format_signal_value(signal, value)
    if operator == "!=":
        return f"非 {_format_signal_value(signal, value)}"
    if operator in {">", "<"}:
        return f"{operator} {_format_signal_value(signal, value)}"
    return _format_signal_value(signal, value)


def _actual_window_values(index: "SignalIndex", signal: str, start_time: float, end_time: float) -> str:
    try:
        timestamps, values = index.values(signal)
    except Exception:
        return ""
    seen: list[Any] = []
    for timestamp, value in zip(timestamps, values):
        if start_time <= float(timestamp) <= end_time and not any(_value_equals(value, item) for item in seen):
            seen.append(value)
        if len(seen) >= 4:
            break
    if not seen:
        try:
            seen.append(index.value_at(signal, start_time))
        except Exception:
            return ""
    return " / ".join(_format_signal_value(signal, item) for item in seen[:4])


def _format_condition(condition: dict[str, Any]) -> str:
    signal = str(condition.get("signal") or "")
    operator = str(condition.get("operator") or "").strip()
    value = condition.get("value")
    note = _display_text(str(condition.get("note") or "").strip())
    if operator == "in" and isinstance(value, list):
        values = " / ".join(_format_signal_value(signal, item) for item in value)
        text = f"{signal} 应为 {values}"
    elif operator == "==":
        text = f"{signal} 应为 {_format_signal_value(signal, value)}"
    elif operator == "!=":
        text = f"{signal} 不应为 {_format_signal_value(signal, value)}"
    elif operator in {">", "<"}:
        text = f"{signal} {operator} {_format_signal_value(signal, value)}"
    else:
        text = f"{signal} {operator} {_format_signal_value(signal, value)}".strip()
    if note:
        text += f"（{note}）"
    return _display_text(text)


def _format_signal_value(signal: str, value: Any) -> str:
    metadata = load_signal_metadata().get(signal, {})
    conversion = metadata.get("conversion", "")
    raw = str(value)
    label = _enum_label(conversion, raw)
    if label:
        return f"{raw}（{_display_text(label)}）"
    return _display_text(raw)


def _enum_label(conversion: str, raw_value: str) -> str:
    if not conversion or "=" not in conversion:
        return ""
    raw_norm = raw_value.strip().lower()
    for part in re.split(r";|/|\n", conversion):
        item = part.strip()
        match = re.match(r"\$?([0-9A-Fa-f.]+)\s*=\s*(.+)", item)
        if not match:
            continue
        key, label = match.groups()
        key_norm = key.strip().lower()
        if key_norm == raw_norm:
            return label.strip()
        try:
            if float(key_norm) == float(raw_norm):
                return label.strip()
        except Exception:
            pass
    return ""


def _human_next_check(item: str) -> str:
    return (
        item.replace("check_", "检查 ")
        .replace("check:", "检查 ")
        .replace("signal_timeout:", "检查信号超时 ")
        .replace("signal_integrity:", "检查信号完整性 ")
        .replace("find_", "定位 ")
        .replace("_", " ")
    )


def _summarize_violations(violations: list[ActuatorViolation]) -> str:
    titles = []
    for violation in violations[:3]:
        title = _issue_title(violation)
        if title not in titles:
            titles.append(title)
    return "、".join(titles) + f"，共 {len(violations)} 个风险时间点。"


def _loads_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _resolve_window_seconds(value: str, parameters: dict[str, float]) -> float:
    value = str(value or "").strip()
    if not value:
        return 0.5
    try:
        return max(0.05, float(value) / 1000.0)
    except ValueError:
        return max(0.05, parameters.get(value, 500.0) / 1000.0)


def _coerce_value(value: Any, signal: str = "") -> Any:
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if text.lower() in {"true", "false"}:
        return 1 if text.lower() == "true" else 0
    try:
        if "." in text:
            return float(text)
        return int(text, 0)
    except ValueError:
        return text


def _value_equals(left: Any, right: Any) -> bool:
    try:
        return float(left) == float(right)
    except Exception:
        return str(left).strip().lower() == str(right).strip().lower()


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _numeric_abs(value: Any) -> float:
    try:
        return abs(float(value))
    except Exception:
        return 0.0


def _severity_rank(severity: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get((severity or "").lower(), 0)
