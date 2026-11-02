from __future__ import annotations

import re
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from openpyxl import load_workbook


ROOT = Path(__file__).resolve().parent
FAULT_DOC_DIR = ROOT / "fault_tables"
LEGACY_FAULT_DOC_DIR = (
    Path(os.environ["BLU_LEGACY_FAULT_DOC_DIR"]).expanduser()
    if os.environ.get("BLU_LEGACY_FAULT_DOC_DIR")
    else None
)


def resolve_fault_table_path(file_name: str) -> Path:
    project_path = FAULT_DOC_DIR / file_name
    if project_path.exists():
        return project_path
    if LEGACY_FAULT_DOC_DIR is not None:
        return LEGACY_FAULT_DOC_DIR / file_name
    return project_path


FDA3_PATH = resolve_fault_table_path("FDA3故障.xlsx")
FDA7_PATH = resolve_fault_table_path("FDA7故障.xlsx")


@dataclass
class FaultEntry:
    source_table: str
    index: int
    code: str
    name: str
    category: str = ""
    fault_type: str = ""
    definition: str = ""
    impact: str = ""
    reaction: str = ""
    domain_l1: str = ""
    domain_l2: str = ""
    maturity: str = ""
    function_impacts: dict[str, str] = field(default_factory=dict)


@dataclass
class FaultFinding:
    source: str
    code: str
    name: str
    state: str
    time_text: str = ""
    summary: str = ""
    impact: str = ""
    raw_line: str = ""


@dataclass
class FaultDiagnosisResult:
    status: str
    summary: str
    findings: list[FaultFinding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    matched_codes: list[str] = field(default_factory=list)


@dataclass
class FaultReferenceMatch:
    table: str
    code: str
    name: str
    summary: str
    impact: str
    score: int


FAULT_TABLE_LABELS = {
    "FDA7": "FDA7 应用层故障",
    "FDA3": "FDA3 底软故障",
}


def diagnose_fault_file(file_path: Path) -> FaultDiagnosisResult:
    text = read_text_file(file_path)
    result = diagnose_fault_text(text, source_name=file_path.name)
    return result


def diagnose_fault_text(text: str, source_name: str = "inline_text") -> FaultDiagnosisResult:
    value = (text or "").strip()
    if not value:
        return FaultDiagnosisResult(status="error", summary="输入内容为空。")

    findings: list[FaultFinding] = []
    warnings: list[str] = []
    source_types: set[str] = set()

    slog_findings = parse_slog_fault_events(value)
    if slog_findings:
        findings.extend(slog_findings)
        source_types.add("slog")

    did_findings = parse_did_faults(value)
    if did_findings:
        findings.extend(did_findings)
        source_types.add("did")

    if not findings:
        direct_findings, direct_warnings = parse_direct_query(value)
        findings.extend(direct_findings)
        warnings.extend(direct_warnings)
        if direct_findings:
            source_types.add("direct_query")

    if not findings:
        warnings.append("未在输入中识别到 fault_event、62 FD A3/62 FD A7，或明确的故障码查询格式。")
        return FaultDiagnosisResult(
            status="no_match",
            summary="本次未识别到可解析的故障诊断内容。",
            warnings=warnings,
            source_types=sorted(source_types),
        )

    summary = build_fault_summary(findings, source_types, source_name)
    return FaultDiagnosisResult(
        status="ok",
        summary=summary,
        findings=findings,
        warnings=warnings,
        source_types=sorted(source_types),
        matched_codes=sorted({item.code for item in findings}),
    )


def answer_fault_reference_question(question: str, context_hint: str = "") -> str | None:
    question = normalize_fault_reference_aliases(question)
    context_hint = normalize_fault_reference_aliases(context_hint)
    summary_reply = answer_fault_table_summary_question(question, context_hint)
    if summary_reply:
        return summary_reply

    metadata_reply = answer_fault_table_metadata_question(question, context_hint)
    if metadata_reply:
        return metadata_reply

    bus_miss_reply = answer_bus_miss_question(question, context_hint)
    if bus_miss_reply:
        return bus_miss_reply

    scope = detect_fault_reference_scope(question, context_hint)
    if not scope:
        return None

    merged_question = merge_fault_reference_context(question, context_hint)
    table_order = ["FDA3", "FDA7"]
    lowered_question = merged_question.lower()
    if scope == "both" and any(token in lowered_question for token in ["apa", "rpa"]):
        table_order = ["FDA7", "FDA3"]

    tables = []
    if scope in {"fda3", "both"} and "FDA3" in table_order:
        tables.append(("FDA3", search_fda3_reference(merged_question)))
    if scope in {"fda7", "both"} and "FDA7" in table_order:
        insert_at = 0 if table_order[0] == "FDA7" else len(tables)
        tables.insert(insert_at, ("FDA7", search_fda7_reference(merged_question)))

    matched = [(table, rows) for table, rows in tables if rows]
    if not matched:
        return None

    lines = ["我先按现有故障表帮你梳理重点关注项。"]
    for table, rows in matched:
        lines.append("")
        lines.append(f"**{FAULT_TABLE_LABELS.get(table, table)}，建议优先关注**")
        for item in rows[:6]:
            lines.append(f"- `{item.code}` {item.name}")
            if item.summary:
                lines.append(f"  说明：{item.summary}")
            if item.impact:
                lines.append(f"  影响：{item.impact}")
    lines.append("")
    lines.append("说明：FDA7 对应应用层故障，FDA3 对应底软故障。")
    lines.append("如果你愿意，我下一步可以继续按这个功能细分成“必须先看”“影响功能大”“底软关联项”三类。")
    return "\n".join(lines)


def answer_fault_table_metadata_question(question: str, context_hint: str = "") -> str | None:
    text = merge_fault_reference_context(normalize_fault_reference_aliases(question), normalize_fault_reference_aliases(context_hint))
    lowered = text.lower()
    if "fda3" not in lowered and "fda7" not in lowered:
        return None

    asks_count = any(token in text for token in ["多少", "几项", "几条", "数量", "总数", "一共", "共有", "条目数", "表项"])
    asks_source = any(token in text for token in ["根据什么", "依据", "来源", "哪个文档", "哪份文档", "什么文档", "哪个表", "哪张表", "判断"])
    asks_definition = any(token in text for token in ["是什么", "对应", "区别", "分别", "含义"])
    if not (asks_count or asks_source or asks_definition):
        return None

    tables: list[str] = []
    if "fda3" in lowered:
        tables.append("FDA3")
    if "fda7" in lowered:
        tables.append("FDA7")
    if not tables:
        tables = ["FDA3", "FDA7"]

    lines: list[str] = []
    if asks_count:
        lines.append("**故障表数量**")
        for table in tables:
            lines.append(f"- {table}：{fault_table_entry_count(table)} 项")
        lines.append("- 统计口径：项目内故障表中可解析的有效 byte/bit 条目。")
        lines.append("")

    if asks_source or asks_definition:
        lines.append("**资料来源与判断依据**" if asks_source else "**FDA3 / FDA7 含义**")
        for table in tables:
            lines.extend(fault_table_source_lines(table))
        lines.append("")

    if "FDA7" in tables and (asks_source or asks_definition):
        lines.extend(
            [
                "**FDA7 怎么对应到 DID**",
                "- FDA7 对应应用层故障。",
                "- DID 响应里类似 `62 FD A7 ...`，其中 `62` 是诊断正响应头，`FD A7` 是 DID；后面的数据区才按 FDA7 表的 `Byte + bit` 去映射故障项。",
                "- 例如数据区 byte0 bit2 置位，就对应 `FDA7[2]`，也就是 FDA7 表里 Byte=0、bit=2 的那一项。",
            ]
        )
    if "FDA3" in tables and (asks_source or asks_definition):
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(
            [
                "**FDA3 怎么对应**",
                "- FDA3 对应底软故障。",
                "- `fault_event` 日志主要按 FDA3 表解释；DID 响应里 `62 FD A3 ...` 的数据区也按 FDA3 表的 byte/bit 映射。",
            ]
        )

    return "\n".join(lines).strip()


def answer_fault_table_summary_question(question: str, context_hint: str = "") -> str | None:
    text = merge_fault_reference_context(normalize_fault_reference_aliases(question), normalize_fault_reference_aliases(context_hint))
    lowered = text.lower()
    if "fda3" not in lowered and "fda7" not in lowered:
        return None

    asks_summary = any(
        token in text
        for token in ["总结", "概括", "归纳", "分类", "大致", "整体", "全部", "所有", "这136项", "这些", "这几项"]
    )
    if not asks_summary:
        return None

    tables: list[str] = []
    if "fda7" in lowered:
        tables.append("FDA7")
    if "fda3" in lowered:
        tables.append("FDA3")
    if not tables:
        return None

    lines: list[str] = []
    for table in tables:
        if table == "FDA7":
            lines.extend(build_fda7_table_summary_lines())
        elif table == "FDA3":
            if lines:
                lines.append("")
            lines.extend(build_fda3_table_summary_lines())
    return "\n".join(lines).strip()


def build_fda7_table_summary_lines() -> list[str]:
    rows = list(load_fda7_map().values())
    domain_l1 = Counter(entry.domain_l1 or "未分类" for entry in rows)
    domain_l2 = Counter(entry.domain_l2 or "未分类" for entry in rows)
    function_counter: Counter[str] = Counter()
    impact_counter: Counter[str] = Counter()
    for entry in rows:
        for function_name, impact in entry.function_impacts.items():
            if impact:
                function_counter[function_name] += 1
                impact_counter[impact] += 1

    return [
        "**FDA7 应用层故障概览**",
        f"- 当前可解析条目：**{len(rows)} 项**",
        f"- 来源：`{FDA7_PATH.relative_to(ROOT).as_posix()}`",
        "- 本质：应用层/功能层故障表，DID `62 FD A7 ...` 的数据区按 byte + bit 映射到这些条目。",
        "",
        "**主要故障域**",
        *[f"- {name}：{count} 项" for name, count in domain_l1.most_common(8)],
        "",
        "**细分方向 Top 项**",
        *[f"- {name}：{count} 项" for name, count in domain_l2.most_common(8)],
        "",
        "**功能影响覆盖较多的方向**",
        *[f"- {name}：{count} 项" for name, count in function_counter.most_common(8)],
        "",
        "**影响表述高频项**",
        *[f"- {name}：{count} 项" for name, count in impact_counter.most_common(6)],
        "",
        "**怎么用这张表**",
        "- 看到 DID `62 FD A7 ...` 时，`62` 是正响应头，`FD A7` 是 DID，后面的数据区才是故障位图。",
        "- 某个 bit 置位后，用 `byte * 8 + bit` 找 `FDA7[index]`，再结合故障名称、成熟条件和功能影响判断风险。",
    ]


def build_fda3_table_summary_lines() -> list[str]:
    rows = list(load_fda3_map().values())
    categories = Counter(entry.category or "未分类" for entry in rows)
    fault_types = Counter(entry.fault_type or "未分类" for entry in rows)
    reactions = Counter(entry.reaction or "未注明" for entry in rows)
    return [
        "**FDA3 底软故障概览**",
        f"- 当前可解析条目：**{len(rows)} 项**",
        f"- 来源：`{FDA3_PATH.relative_to(ROOT).as_posix()}`",
        "- 本质：底软/基础软件故障表，`fault_event` 日志和 DID `62 FD A3 ...` 的数据区都可以按 byte + bit 映射到这些条目。",
        "",
        "**主要故障类别**",
        *[f"- {name}：{count} 项" for name, count in categories.most_common(8)],
        "",
        "**故障类型 Top 项**",
        *[f"- {name}：{count} 项" for name, count in fault_types.most_common(8)],
        "",
        "**常见处理/反应逻辑**",
        *[f"- {name}：{count} 项" for name, count in reactions.most_common(6)],
        "",
        "**怎么用这张表**",
        "- 看到 `fault_event` 关键字时，通常优先按 FDA3 表解释。",
        "- 看到 DID `62 FD A3 ...` 时，`62` 是正响应头，`FD A3` 是 DID，后面的数据区按 FDA3 的 byte + bit 映射。",
    ]


def fault_table_entry_count(table: str) -> int:
    table = table.upper()
    if table == "FDA7":
        return len(load_fda7_map())
    if table == "FDA3":
        return len(load_fda3_map())
    return 0


def fault_table_source_lines(table: str) -> list[str]:
    table = table.upper()
    if table == "FDA7":
        return [
            f"- FDA7：来自项目内 `{FDA7_PATH.relative_to(ROOT).as_posix()}`。",
            "  这张表是应用层故障定义表，核心字段是 Byte、bit、故障名称、故障成熟条件、一级/二级故障域，以及 APA/RPA/ACC/LCC/AEB 等功能影响。",
        ]
    if table == "FDA3":
        return [
            f"- FDA3：来自项目内 `{FDA3_PATH.relative_to(ROOT).as_posix()}`。",
            "  这张表是底软故障定义表，核心字段是 Record Byte.Bit、Fault Name、Fault Definition、Fault Active、Reaction 逻辑、DTC 追溯等。",
        ]
    return []


def answer_bus_miss_question(question: str, context_hint: str = "") -> str | None:
    merged_question = merge_fault_reference_context(question, context_hint)
    if not is_bus_miss_question(merged_question):
        return None

    ids = extract_bus_ids(merged_question)
    miss_entries = search_bus_miss_entries(ids)
    related_entries = search_bus_related_entries(ids)
    if not ids and not miss_entries:
        return None

    lines = [
        "这类问题我按“总线报文/信号组 Miss”来理解：`PB_xxx_..._Miss_Fault` 通常表示 ADM 没有在期望周期内收到对应 CAN 报文或信号组。",
        "",
    ]

    if miss_entries:
        lines.append("**故障表中命中的 Miss 项**")
        for entry in miss_entries[:12]:
            frame_id = extract_frame_id_from_entry(entry)
            frame_text = f"0x{display_frame_id(frame_id)}" if frame_id else "对应报文"
            lines.append(f"- `{entry.source_table}[{entry.index}]` {entry.name}")
            lines.append(f"  含义：{frame_text} miss / 报文丢失。{entry.definition or ''}".rstrip())
            if entry.impact:
                lines.append(f"  影响：{entry.impact}")
    else:
        lines.append("**故障表中未直接命中 Miss 项**")
        if ids:
            lines.append(f"- 当前 FDA3/FDA7 表里没有找到这些 ID 的 `*_Miss_Fault`：{', '.join('0x' + item for item in ids)}")

    if ids:
        hit_ids = {normalize_frame_id(extract_frame_id_from_entry(entry)) for entry in miss_entries}
        missing_ids = [item for item in ids if normalize_frame_id(item) not in hit_ids]
        if missing_ids:
            lines.extend(
                [
                    "",
                    "**未直接命中的 ID**",
                    f"- {', '.join('0x' + item for item in missing_ids)}：当前故障表未找到同名 `Miss_Fault`。可能是故障表未配置、车型/项目差异、ID 名称不是按十六进制写入，或这些 ID 只是原始报文 ID。",
                ]
            )

    if related_entries:
        lines.extend(["", "**同 ID 相关的非 Miss 通信故障**"])
        for entry in related_entries[:8]:
            lines.append(f"- `{entry.source_table}[{entry.index}]` {entry.name}：{entry.definition or entry.fault_type or entry.category}")

    lines.extend(
        [
            "",
            "**常见原因排查方向**",
            "- 发送端 ECU 未发送：发送端掉电、休眠、复位、DTC 限制发送、软件任务异常。",
            "- 总线链路异常：CAN 线束/接插件/终端电阻/网关路由异常，或对应网段离线。",
            "- 通信配置不一致：数据库版本不匹配、报文 ID/通道/周期/DBC-ARXML 配置和实车不一致。",
            "- 超时监控触发：报文周期抖动过大、短时丢帧、负载过高，导致 ADM 超过超时阈值未收到。",
            "- 如果同时出现同一报文的 `MAC_Fault` / `ARC_Fault`，更偏向通信校验或滚动计数异常；`Miss_Fault` 更偏向报文本身没收到。",
            "",
            "说明：这类 `PB_xxx_..._Miss_Fault` 当前主要在 FDA3 底软故障表中；FDA7 是应用层故障，FDA3 是底软故障。",
        ]
    )
    return "\n".join(lines)


def is_bus_miss_question(text: str) -> bool:
    value = (text or "").lower()
    has_bus_id = bool(extract_bus_ids(text))
    miss_tokens = ["miss", "丢失", "丢帧", "收不到", "未收到", "报文", "总线", "信号"]
    cause_tokens = ["原因", "导致", "为什么", "可能", "排查", "是什么"]
    return has_bus_id and any(token in value for token in miss_tokens) and any(token in value for token in cause_tokens + miss_tokens)


def extract_bus_ids(text: str) -> list[str]:
    candidates = re.findall(r"(?<![0-9A-Fa-f])(?:0x)?([0-9A-Fa-f]{3})(?![0-9A-Fa-f])", text or "")
    return list(dict.fromkeys(item.upper() for item in candidates))


def normalize_frame_id(frame_id: str) -> str:
    return (frame_id or "").upper().lstrip("0") or "0"


def display_frame_id(frame_id: str) -> str:
    normalized = normalize_frame_id(frame_id)
    return normalized.zfill(3) if len(normalized) <= 3 else normalized


def search_bus_miss_entries(ids: list[str]) -> list[FaultEntry]:
    if not ids:
        return []
    rows: list[FaultEntry] = []
    for entry in list(load_fda3_map().values()) + list(load_fda7_map().values()):
        haystack = " ".join([entry.name, entry.definition, entry.category, entry.fault_type]).upper()
        if "MISS" not in haystack and "丢失" not in haystack:
            continue
        if any(matches_frame_id(haystack, frame_id) for frame_id in ids):
            rows.append(entry)
    return rows


def search_bus_related_entries(ids: list[str]) -> list[FaultEntry]:
    if not ids:
        return []
    rows: list[FaultEntry] = []
    for entry in list(load_fda3_map().values()) + list(load_fda7_map().values()):
        name_haystack = (entry.name or "").upper()
        detail_haystack = " ".join([entry.definition, entry.category, entry.fault_type]).upper()
        if not any(matches_frame_id(name_haystack, frame_id) for frame_id in ids):
            continue
        if any(token in name_haystack + " " + detail_haystack for token in ["MISS", "丢失"]):
            continue
        if any(token in name_haystack + " " + detail_haystack for token in ["MAC", "ARC", "校验", "无效", "INVALID", "FAULT"]):
            rows.append(entry)
    return rows


def matches_frame_id(haystack: str, frame_id: str) -> bool:
    frame_id = normalize_frame_id(frame_id)
    if not frame_id:
        return False
    patterns = [
        rf"0X0*{re.escape(frame_id)}(?![0-9A-F])",
        rf"[_\s]0*{re.escape(frame_id)}(?:[_\s]|$)",
        rf"\b0*{re.escape(frame_id)}\b",
    ]
    return any(re.search(pattern, haystack) for pattern in patterns)


def extract_frame_id_from_entry(entry: FaultEntry) -> str:
    text = " ".join([entry.name, entry.definition]).upper()
    match = re.search(r"0X([0-9A-F]{3,8})", text)
    if match:
        value = match.group(1).lstrip("0") or "0"
        return value.upper()
    match = re.search(r"(?:^|[_\s])([0-9A-F]{3})(?:[_\s]|$)", text)
    if match:
        return match.group(1).upper()
    return ""


def format_fault_markdown(open_id: str, task_label: str, input_name: str, result: FaultDiagnosisResult) -> str:
    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        f"**任务类型**：**{task_label}**",
        f"**输入来源**：`{input_name}`",
        "",
        f"**重点结论**\n{result.summary}",
    ]
    if result.source_types:
        lines.extend(["", f"**解析来源**\n- {'、'.join(result.source_types)}"])
    if result.findings:
        lines.append("")
        lines.append("**识别结果**")
        for item in result.findings[:20]:
            line = f"- **{item.name or item.code}**"
            details = []
            if item.state:
                details.append(item.state)
            if item.time_text:
                details.append(item.time_text)
            if item.code:
                details.append(item.code)
            if item.source:
                details.append(item.source)
            if details:
                line += f"：{' | '.join(details)}"
            if item.summary:
                line += f"\n  {item.summary}"
            if item.impact:
                line += f"\n  影响：{item.impact}"
            lines.append(line)
    if result.warnings:
        lines.append("")
        lines.append("**说明**")
        for warning in result.warnings:
            lines.append(f"- {warning}")
    return "\n".join(lines)


def build_fault_summary(findings: list[FaultFinding], source_types: set[str], source_name: str) -> str:
    triggered = [item for item in findings if item.state == "触发"]
    recovered = [item for item in findings if item.state == "恢复"]
    active = [item for item in findings if item.state == "激活"]
    unique_names = list(dict.fromkeys(item.name for item in findings if item.name))
    names = "、".join(unique_names[:4]) if unique_names else ""
    parts = [f"已解析 `{source_name}`。"]
    if triggered:
        parts.append(f"识别到 {len(triggered)} 条故障触发事件。")
    if recovered:
        parts.append(f"其中 {len(recovered)} 条为恢复事件。")
    if active:
        parts.append(f"当前快照里有 {len(active)} 个激活故障位。")
    if not triggered and not recovered and not active:
        parts.append(f"共识别到 {len(findings)} 条故障相关记录。")
    if names:
        parts.append(f"重点涉及：{names}。")
    if "slog" in source_types and "did" in source_types:
        parts.append("本次同时结合了过程事件和当前故障快照。")
    elif "slog" in source_types:
        parts.append("本次结论主要来自 slog 中的 fault_event 过程记录。")
    elif "did" in source_types:
        parts.append("本次结论主要来自 DID 返回的数据区快照。")
    return "".join(parts)


def parse_slog_fault_events(text: str) -> list[FaultFinding]:
    findings: list[FaultFinding] = []
    mapping = load_fda3_map()
    pattern = re.compile(
        r"(?P<time>[A-Z][a-z]{2}\s+\d+\s+\d+:\d+:\d+\.\d+).*?\[Fault_Event\].*?"
        r"(?P<table>FDA\d)\[(?P<index>\d+)\]\s+0x(?P<old>[0-9A-Fa-f]+)\s+to\s+0x(?P<new>[0-9A-Fa-f]+)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        table = match.group("table").upper()
        if table != "FDA3":
            continue
        index = int(match.group("index"))
        entry = mapping.get(index)
        if not entry:
            continue
        old_value = int(match.group("old"), 16)
        new_value = int(match.group("new"), 16)
        if old_value == 0 and new_value != 0:
            state = "触发"
        elif old_value != 0 and new_value == 0:
            state = "恢复"
        else:
            state = "变化"
        findings.append(
            FaultFinding(
                source="slog/FDA3",
                code=f"FDA3[{index}]",
                name=entry.name or entry.code,
                state=state,
                time_text=match.group("time"),
                summary=entry.definition or entry.category or entry.fault_type,
                impact=entry.impact or entry.reaction,
                raw_line=match.group(0),
            )
        )
    return findings


def parse_did_faults(text: str) -> list[FaultFinding]:
    findings: list[FaultFinding] = []
    pattern = re.compile(r"62\s+FD\s+(A3|A7)\s+(?P<payload>(?:[0-9A-Fa-f]{2}\s*){4,})", re.IGNORECASE)
    matches = list(pattern.finditer(text))
    for match in matches:
        did = match.group(1).upper()
        payload = hex_tokens(match.group("payload"))
        if did == "A3":
            findings.extend(expand_active_bits(payload, load_fda3_map(), "did/FDA3", "FDA3"))
        elif did == "A7":
            findings.extend(expand_active_bits(payload, load_fda7_map(), "did/FDA7", "FDA7"))
    return findings


def parse_direct_query(text: str) -> tuple[list[FaultFinding], list[str]]:
    warnings: list[str] = []
    findings: list[FaultFinding] = []

    for match in re.finditer(r"FDA3\[(\d+)\]", text, re.IGNORECASE):
        index = int(match.group(1))
        entry = load_fda3_map().get(index)
        if entry:
            findings.append(entry_to_query_finding(entry, f"FDA3[{index}]"))

    for match in re.finditer(r"FDA7\[(\d+)\]", text, re.IGNORECASE):
        index = int(match.group(1))
        entry = load_fda7_map().get(index)
        if entry:
            findings.append(entry_to_query_finding(entry, f"FDA7[{index}]"))

    byte_bit_match = re.search(r"byte\s*(\d+)\s*bit\s*(\d+)", text, re.IGNORECASE)
    if byte_bit_match:
        byte_index = int(byte_bit_match.group(1))
        bit_index = int(byte_bit_match.group(2))
        index = byte_index * 8 + bit_index
        entry = load_fda7_map().get(index)
        if entry:
            findings.append(entry_to_query_finding(entry, f"FDA7 byte {byte_index} bit {bit_index}"))

    if findings:
        return dedupe_findings(findings), warnings

    hinted_payload = parse_inline_payload_with_hint(text)
    if hinted_payload:
        did_name, payload = hinted_payload
        mapping = load_fda3_map() if did_name == "FDA3" else load_fda7_map()
        findings.extend(expand_active_bits(payload, mapping, f"direct/{did_name}", did_name))
        return dedupe_findings(findings), warnings

    if len(hex_tokens(text)) >= 16:
        warnings.append("检测到较长十六进制字节串，但未明确是 FDA3 还是 FDA7。可在前面补 `FDA3` 或 `FDA7` 再查询。")
    return findings, warnings


def parse_inline_payload_with_hint(text: str) -> tuple[str, list[int]] | None:
    upper = text.upper()
    if "FDA3" in upper or "FD A3" in upper:
        payload = extract_payload_after_hint(text, ("FDA3", "FD A3", "62 FD A3"))
        if payload:
            return "FDA3", payload
    if "FDA7" in upper or "FD A7" in upper:
        payload = extract_payload_after_hint(text, ("FDA7", "FD A7", "62 FD A7"))
        if payload:
            return "FDA7", payload
    return None


def extract_payload_after_hint(text: str, hints: Iterable[str]) -> list[int]:
    upper = text.upper()
    for hint in hints:
        idx = upper.find(hint)
        if idx >= 0:
            suffix = text[idx + len(hint) :]
            tokens = hex_tokens(suffix)
            if tokens:
                return tokens
    return []


def expand_active_bits(payload: list[int], mapping: dict[int, FaultEntry], source: str, code_prefix: str) -> list[FaultFinding]:
    findings: list[FaultFinding] = []
    for byte_index, value in enumerate(payload):
        if value == 0:
            continue
        for bit_index in range(8):
            if value & (1 << bit_index):
                entry_index = byte_index * 8 + bit_index
                entry = mapping.get(entry_index)
                if not entry:
                    continue
                summary_parts = [part for part in [entry.category, entry.fault_type, entry.definition, entry.maturity] if part]
                impact = entry.impact
                if not impact and entry.function_impacts:
                    impact = summarize_function_impacts(entry.function_impacts)
                findings.append(
                    FaultFinding(
                        source=source,
                        code=f"{code_prefix}[{entry_index}]",
                        name=entry.name or entry.code,
                        state="激活",
                        summary="；".join(summary_parts[:2]) if summary_parts else "",
                        impact=impact,
                    )
                )
    return dedupe_findings(findings)


def summarize_function_impacts(function_impacts: dict[str, str]) -> str:
    items = [f"{key}:{value}" for key, value in function_impacts.items() if value]
    return "；".join(items[:6])


def entry_to_query_finding(entry: FaultEntry, code: str) -> FaultFinding:
    summary_parts = [part for part in [entry.category, entry.fault_type, entry.definition, entry.maturity] if part]
    impact = entry.impact or summarize_function_impacts(entry.function_impacts)
    return FaultFinding(
        source=f"query/{entry.source_table}",
        code=code,
        name=entry.name or entry.code,
        state="查询命中",
        summary="；".join(summary_parts[:2]) if summary_parts else "",
        impact=impact,
    )


def dedupe_findings(findings: list[FaultFinding]) -> list[FaultFinding]:
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[FaultFinding] = []
    for item in findings:
        key = (item.source, item.code, item.name, item.state)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def normalize_fault_reference_aliases(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    lowered = value.lower()
    if "fda7" not in lowered and any(token in value for token in ["应用层故障", "应用层", "应用故障"]):
        value = f"{value} FDA7"
    lowered = value.lower()
    if "fda3" not in lowered and any(token in value for token in ["底软故障", "底软", "底层软件故障", "基础软件故障", "BSW故障", "bsw故障"]):
        value = f"{value} FDA3"
    return value


def is_fault_reference_followup(question: str) -> bool:
    value = str(question or "").strip()
    if not value:
        return False
    followup_tokens = [
        "这",
        "这些",
        "上述",
        "上面",
        "刚才",
        "前面",
        "它们",
        "那些",
        "这几项",
        "那几项",
        "这136项",
        "136项",
        "总结",
        "概括",
        "归纳",
        "分类",
        "大致",
        "展开",
        "更具体",
    ]
    return any(token in value for token in followup_tokens)


def merge_fault_reference_context(question: str, context_hint: str = "") -> str:
    question = normalize_fault_reference_aliases(question)
    context_hint = normalize_fault_reference_aliases(context_hint)
    if not context_hint:
        return question

    lowered = question.lower()
    if any(
        token in lowered
        for token in ["fda3", "fda7", "环视", "相机", "鱼眼", "前视", "后视", "左视", "右视", "apa", "rpa", "svs", "camera"]
    ):
        return question

    if is_fault_reference_followup(question) or any(token in question for token in ["故障码", "故障类型", "哪些故障", "哪些码", "重点看", "重点关注", "说错了"]):
        return f"{context_hint} {question}".strip()
    return question


def detect_fault_reference_scope(question: str, context_hint: str = "") -> str | None:
    text = normalize_fault_reference_aliases(merge_fault_reference_context(question, context_hint)).lower()
    has_fault_query = any(token in text for token in ["fda3", "fda7", "故障码", "故障条目", "故障类型", "相关故障", "故障", "关注哪些", "关注哪", "哪些故障"])
    has_module_query = any(token in text for token in ["功能", "模块", "环视", "相机", "泊车", "停车", "前视", "后视", "鱼眼", "apa", "rpa", "acc", "camera"])
    if not (has_fault_query or has_module_query):
        return None
    if "fda3" in text and "fda7" not in text:
        return "fda3"
    if "fda7" in text and "fda3" not in text:
        return "fda7"
    return "both"


def build_fault_query_terms(question: str) -> list[str]:
    question = normalize_fault_reference_aliases(question)
    text = (question or "").lower()
    groups = {
        "环视": ["环视", "鱼眼", "相机", "camera", "泊车", "影子模式", "svs"],
        "前视": ["前视", "fvc", "前摄像头", "fov100", "前环视"],
        "后视": ["后视", "后环视", "rear"],
        "左视": ["左", "左环视", "left"],
        "右视": ["右", "右环视", "right"],
        "dms": ["dms", "驾驶员监控"],
        "uss": ["uss", "超声波"],
        "apa": ["apa", "泊车", "停车", "环视", "鱼眼", "相机"],
        "rpa": ["rpa", "遥控泊车", "泊车", "环视", "鱼眼", "相机"],
        "acc": ["acc", "跟车", "纵向", "制动", "减速", "车速"],
    }
    terms: list[str] = []
    for _, synonyms in groups.items():
        if any(token in text for token in synonyms):
            terms.extend(synonyms)
    if not terms:
        for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", question):
            if len(token) >= 2:
                terms.append(token.lower())
    return list(dict.fromkeys(term.lower() for term in terms))


def score_fault_entry(entry: FaultEntry, terms: list[str]) -> int:
    haystack_parts = [
        entry.name,
        entry.category,
        entry.fault_type,
        entry.definition,
        entry.impact,
        entry.reaction,
        entry.domain_l1,
        entry.domain_l2,
        summarize_function_impacts(entry.function_impacts),
    ]
    haystack = " ".join(part for part in haystack_parts if part).lower()
    score = 0
    for term in terms:
        if term and term in haystack:
            score += 3
        if term and entry.name and term in entry.name.lower():
            score += 4
        if term in {"apa", "rpa", "acc"} and entry.function_impacts.get(term.upper()):
            score += 8
    if "环视" in terms and ("影子模式" in haystack or "泊车功能" in haystack):
        score += 2
    return score


def entry_to_reference_match(entry: FaultEntry, table: str, score: int) -> FaultReferenceMatch:
    summary = "；".join(part for part in [entry.category, entry.fault_type, entry.definition or entry.maturity] if part)
    impact = entry.impact or summarize_function_impacts(entry.function_impacts)
    return FaultReferenceMatch(
        table=table,
        code=f"{table}[{entry.index}]",
        name=entry.name or entry.code,
        summary=summary,
        impact=impact,
        score=score,
    )


def search_fda3_reference(question: str) -> list[FaultReferenceMatch]:
    terms = build_fault_query_terms(question)
    rows: list[FaultReferenceMatch] = []
    for entry in load_fda3_map().values():
        score = score_fault_entry(entry, terms)
        if score > 0:
            rows.append(entry_to_reference_match(entry, "FDA3", score))
    rows.sort(key=lambda item: (-item.score, item.code))
    return rows[:12]


def search_fda7_reference(question: str) -> list[FaultReferenceMatch]:
    terms = build_fault_query_terms(question)
    rows: list[FaultReferenceMatch] = []
    for entry in load_fda7_map().values():
        score = score_fault_entry(entry, terms)
        if score > 0:
            rows.append(entry_to_reference_match(entry, "FDA7", score))
    rows.sort(key=lambda item: (-item.score, item.code))
    return rows[:12]


def read_text_file(file_path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk", "latin-1"):
        try:
            return file_path.read_text(encoding=encoding, errors="strict")
        except Exception:
            continue
    return file_path.read_text(encoding="utf-8", errors="ignore")


def hex_tokens(text: str) -> list[int]:
    return [int(token, 16) for token in re.findall(r"\b[0-9A-Fa-f]{2}\b", text)]


@lru_cache(maxsize=1)
def load_fda3_map() -> dict[int, FaultEntry]:
    workbook = load_workbook(FDA3_PATH, read_only=True, data_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    mapping: dict[int, FaultEntry] = {}
    for row in sheet.iter_rows(min_row=2, values_only=True):
        record = row[0]
        parsed = parse_record_byte_bit(record)
        if parsed is None:
            continue
        byte_index, bit_index = parsed
        index = byte_index * 8 + bit_index
        mapping[index] = FaultEntry(
            source_table="FDA3",
            index=index,
            code=f"{byte_index}.{bit_index}",
            name=normalize_fault_name(clean_text(row[1]), clean_text(row[6]), clean_text(row[2]), f"{byte_index}.{bit_index}"),
            category=clean_text(row[2]),
            fault_type=clean_text(row[3]),
            definition=clean_text(row[6]),
            impact=clean_text(row[7]),
            reaction=clean_text(row[11]),
        )
    return mapping


@lru_cache(maxsize=1)
def load_fda7_map() -> dict[int, FaultEntry]:
    workbook = load_workbook(FDA7_PATH, read_only=True, data_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    mapping: dict[int, FaultEntry] = {}
    current_byte: int | None = None
    headers: list[str] = []
    for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
        if row_index == 1:
            headers = [clean_text(item) for item in row]
            continue
        byte_value = row[0]
        bit_value = row[1]
        if byte_value is not None:
            current_byte = int(byte_value)
        if current_byte is None or bit_value is None:
            continue
        bit_index = int(bit_value)
        index = current_byte * 8 + bit_index
        function_impacts = {}
        for col_index, header in enumerate(headers[6:], start=6):
            header_name = clean_text(header)
            cell_value = clean_text(row[col_index]) if col_index < len(row) else ""
            if header_name and cell_value:
                function_impacts[header_name] = cell_value
        mapping[index] = FaultEntry(
            source_table="FDA7",
            index=index,
            code=f"{current_byte}.{bit_index}",
            name=normalize_fault_name(clean_text(row[2]), clean_text(row[3]), clean_text(row[4]), f"{current_byte}.{bit_index}"),
            definition="",
            maturity=clean_text(row[3]),
            domain_l1=clean_text(row[4]),
            domain_l2=clean_text(row[5]),
            impact=summarize_function_impacts(function_impacts),
            function_impacts=function_impacts,
        )
    return mapping


def parse_record_byte_bit(value: object) -> tuple[int, int] | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or not re.fullmatch(r"[0-9A-Fa-f]+(?:\.\d+)?", text):
        return None
    if "." in text:
        left, right = text.split(".", 1)
        base = 16 if re.search(r"[A-Fa-f]", left) else 10
        return int(left, base), int(right)
    base = 16 if re.search(r"[A-Fa-f]", text) else 10
    return int(text, base), 0


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_fault_name(name: str, fallback1: str, fallback2: str, code: str) -> str:
    if name and not re.fullmatch(r"[0-9A-Fa-f]+(?:\.\d+)?", name):
        return name
    for candidate in (fallback1, fallback2, code):
        if candidate:
            return candidate
    return code
