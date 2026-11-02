from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from openpyxl import load_workbook


ROOT = Path(__file__).resolve().parent
DTC_TABLE_DIR = ROOT / "dtc_tables"


@dataclass(frozen=True)
class DtcSource:
    system: str
    system_label: str
    file_name: str
    preferred_sheets: tuple[str, ...]


@dataclass(frozen=True)
class DtcEntry:
    system: str
    system_label: str
    file_name: str
    sheet_name: str
    row_number: int
    preferred_sheet: bool
    values: dict[str, str]
    primary_code: str
    description: str
    identifier: str
    search_text: str


@dataclass(frozen=True)
class DtcSearchResult:
    question: str
    scope: str
    scope_label: str
    matches: tuple[DtcEntry, ...]
    total_by_system: dict[str, int]
    sheet_counts: dict[str, int]
    query_terms: tuple[str, ...]


DTC_SOURCES: tuple[DtcSource, ...] = (
    DtcSource("drive", "驱动系统", "PS DTC.xlsx", ("DTC List",)),
    DtcSource("brake", "制动系统", "EBCM DTC.xlsx", ("GM Master DTC List",)),
)

SYSTEM_ALIASES = {
    "brake": ("制动", "刹车", "ebcm", "ebooster", "brake"),
    "drive": ("驱动", "动力", "ps", "pdf", "ecp", "ecm", "propulsion", "powertrain"),
}

GENERIC_TERMS = {
    "dtc",
    "故障",
    "故障码",
    "码",
    "查询",
    "查",
    "看看",
    "含义",
    "描述",
    "多少",
    "几项",
    "几条",
    "一共",
    "共有",
    "总数",
    "制动",
    "刹车",
    "驱动",
    "系统",
    "相关",
    "ebcm",
    "ebooster",
    "brake",
    "ps",
    "pdf",
    "ecp",
    "ecm",
    "propulsion",
    "powertrain",
    "里面",
    "有没有",
    "是什么",
    "什么意思",
}

CODE_RE = re.compile(r"\b(?:[PCBU][0-9A-Fa-f]{4}(?:[.\-]?[0-9A-Fa-f]{2})?|0x[0-9A-Fa-f]{2,8})\b")


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return re.sub(r"\s+", " ", text)


def answer_dtc_question(question: str) -> str:
    result = search_dtc_entries(question)
    return format_dtc_search_result(result)


def search_dtc_entries(question: str, limit: int = 12) -> DtcSearchResult:
    question = (question or "").strip()
    scope, scope_label = detect_dtc_scope(question)
    entries = [entry for entry in load_dtc_entries() if scope == "all" or entry.system == scope]
    query_terms = extract_query_terms(question)
    scored: list[tuple[int, DtcEntry]] = []
    for entry in entries:
        score = score_entry(entry, query_terms, question)
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda item: (item[0], item[1].preferred_sheet), reverse=True)

    matches: list[DtcEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for _, entry in scored:
        key = (entry.system, normalize_code(entry.primary_code), entry.description.lower())
        if key in seen:
            continue
        seen.add(key)
        matches.append(entry)
        if len(matches) >= limit:
            break

    total_by_system = count_preferred_entries_by_system()
    sheet_counts = count_entries_by_sheet()
    return DtcSearchResult(
        question=question,
        scope=scope,
        scope_label=scope_label,
        matches=tuple(matches),
        total_by_system=total_by_system,
        sheet_counts=sheet_counts,
        query_terms=query_terms,
    )


def detect_dtc_scope(question: str) -> tuple[str, str]:
    text = (question or "").lower()
    has_brake = any(term in text for term in SYSTEM_ALIASES["brake"])
    has_drive = any(term in text for term in SYSTEM_ALIASES["drive"])
    if has_brake and not has_drive:
        return "brake", "制动系统"
    if has_drive and not has_brake:
        return "drive", "驱动系统"
    return "all", "制动系统 + 驱动系统"


def extract_query_terms(question: str) -> tuple[str, ...]:
    text = (question or "").strip()
    code_terms = [normalize_code(item) for item in CODE_RE.findall(text)]
    word_terms = re.findall(r"[A-Za-z0-9_./-]{2,}|[\u4e00-\u9fff]{2,}", text)
    terms: list[str] = []
    for term in [*code_terms, *word_terms]:
        normalized = normalize_search_term(term)
        if asks_count(question) and any(token in normalized for token in ("多少", "几项", "几条", "一共", "共有", "总数", "数量")):
            continue
        if not normalized or normalized in GENERIC_TERMS:
            continue
        if any(alias in normalized for aliases in SYSTEM_ALIASES.values() for alias in aliases) and len(normalized) <= 8:
            continue
        if normalized not in terms:
            terms.append(normalized)
        for expanded in expand_query_term(normalized):
            if expanded and expanded not in terms:
                terms.append(expanded)
    return tuple(terms)


def expand_query_term(term: str) -> tuple[str, ...]:
    expanded: list[str] = []
    if any(token in term for token in ("轮速", "车轮速度", "wheel speed")):
        expanded.extend(["wss", "wheel", "speed"])
    if "传感器" in term or "sensor" in term:
        expanded.append("sensor")
    if "开路" in term or "断路" in term:
        expanded.extend(["open", "open circuit"])
    if "短路" in term:
        expanded.append("short")
    if "接地" in term or "对地" in term:
        expanded.extend(["gnd", "ground"])
    if "电源" in term or "对电" in term:
        expanded.extend(["batt", "battery", "voltage"])
    if "丢失" in term or "收不到" in term or "miss" in term:
        expanded.extend(["miss", "lost"])
    if "校验" in term:
        expanded.extend(["checksum", "mac", "arc"])
    if "扭矩" in term:
        expanded.append("torque")
    if "制动" in term or "刹车" in term:
        expanded.append("brake")
    if "驱动" in term:
        expanded.extend(["drive", "propulsion"])
    result: list[str] = []
    for item in expanded:
        normalized = normalize_search_term(item)
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def score_entry(entry: DtcEntry, query_terms: Iterable[str], question: str) -> int:
    terms = list(query_terms)
    asks_count_only = asks_count(question) and not terms
    if asks_count_only:
        return 0
    if not terms:
        return 1 if entry.preferred_sheet else 0

    score = 0
    search_text = entry.search_text
    primary_code = normalize_code(entry.primary_code)
    for term in terms:
        if not term:
            continue
        if term == primary_code:
            score += 30
            continue
        if term in search_text:
            score += 8
        if term in normalize_code(entry.values.get("Service DTC", "")):
            score += 20
        if term in normalize_code(entry.values.get("DTC", "")):
            score += 20
        if term in normalize_code(entry.values.get("CustomerDTCID", "")):
            score += 16
        if term in normalize_code(entry.values.get("J1979-2 DTC", "")):
            score += 16
        if term in normalize_code(entry.values.get("InternalDTCID", "")):
            score += 14
    if entry.preferred_sheet and score:
        score += 2
    return score


def asks_count(question: str) -> bool:
    return any(token in (question or "") for token in ("多少", "几项", "几条", "一共", "共有", "总数", "数量"))


def format_dtc_search_result(result: DtcSearchResult) -> str:
    lines: list[str] = []
    if asks_count(result.question):
        lines.append("**DTC 表统计**")
        if result.scope in {"all", "brake"}:
            lines.append(f"- 制动系统 EBCM DTC：{result.total_by_system.get('brake', 0)} 项")
        if result.scope in {"all", "drive"}:
            lines.append(f"- 驱动系统 PS DTC：{result.total_by_system.get('drive', 0)} 项")
        lines.append("- 统计口径：按当前项目内 DTC 表的主列表有效行统计。")
        if not result.query_terms:
            return "\n".join(lines)
        if not result.matches:
            lines.append("- 附加关键词暂未命中更细的筛选项，以上先按系统范围统计。")
            return "\n".join(lines)
        lines.append("")

    if not result.matches:
        scope_text = result.scope_label
        return (
            f"**DTC 查询未直接命中**\n"
            f"- 查询范围：{scope_text}\n"
            f"- 当前可查：制动系统 EBCM DTC、驱动系统 PS DTC\n"
            f"- 建议补充：完整 DTC 码（如 `C0502.00` / `P0001`）、内部 ID、J1979 码，或英文故障描述关键词。"
        )

    lines.append("**DTC 查询结果**")
    lines.append(f"- 查询范围：{result.scope_label}")
    lines.append(f"- 命中条目：{len(result.matches)} 条（最多展示 12 条）")
    lines.append("")
    lines.append("| 系统 | DTC / ID | 描述 | 关键字段 | 来源 |")
    lines.append("|---|---|---|---|---|")
    for entry in result.matches:
        key_fields = summarize_key_fields(entry)
        code = entry.primary_code or entry.identifier or "-"
        description = entry.description or "-"
        source = f"{entry.file_name} / {entry.sheet_name} / row {entry.row_number}"
        lines.append(
            "| "
            + " | ".join(
                sanitize_table_cell(value)
                for value in [entry.system_label, code, description, key_fields, source]
            )
            + " |"
        )
    return "\n".join(lines)


def summarize_key_fields(entry: DtcEntry) -> str:
    candidates = [
        ("Service DTC", entry.values.get("Service DTC", "")),
        ("J1979", entry.values.get("J1979-2 Service DTC", "") or entry.values.get("J1979-2 DTC", "")),
        ("Internal", entry.values.get("InternalDTCID", "")),
        ("Identifier", entry.values.get("DTC Identifier", "")),
        ("Message", entry.values.get("Message", "")),
        ("Fault Type", entry.values.get("Fault Type", "")),
        ("Light", entry.values.get("Light Enable", "")),
    ]
    parts = [f"{label}: {value}" for label, value in candidates if value]
    return "；".join(parts[:4]) or "-"


def sanitize_table_cell(value: str) -> str:
    return str(value or "").replace("|", "/").replace("\n", " ").strip()


@lru_cache(maxsize=1)
def load_dtc_entries() -> tuple[DtcEntry, ...]:
    entries: list[DtcEntry] = []
    for source in DTC_SOURCES:
        path = DTC_TABLE_DIR / source.file_name
        if not path.exists():
            continue
        workbook = load_workbook(path, read_only=True, data_only=True)
        for sheet in workbook.worksheets:
            preferred = sheet.title in source.preferred_sheets
            header_row, header = find_header(sheet.iter_rows(values_only=True))
            if not header_row or not header:
                continue
            for row_number, raw_row in enumerate(sheet.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
                values = row_to_values(header, raw_row)
                if not is_valid_dtc_row(values):
                    continue
                entry = build_entry(source, sheet.title, row_number, preferred, values)
                entries.append(entry)
    return tuple(entries)


def find_header(rows: Iterable[tuple[object, ...]]) -> tuple[int, list[str]]:
    best_row_number = 0
    best_header: list[str] = []
    best_score = 0
    for row_number, row in enumerate(rows, start=1):
        cells = [_cell_text(value) for value in row]
        non_empty = [cell for cell in cells if cell]
        if not non_empty:
            continue
        score = 0
        joined = " ".join(non_empty).lower()
        if "dtc" in joined:
            score += 5
        if "description" in joined or "message" in joined or "monitor name" in joined:
            score += 5
        if "id" in joined:
            score += 2
        score += min(len(non_empty), 20)
        if score > best_score:
            best_score = score
            best_row_number = row_number
            best_header = normalize_headers(cells)
        if best_score >= 20 and ("dtc" in joined or "monitor name" in joined):
            break
    return best_row_number, best_header


def normalize_headers(cells: list[str]) -> list[str]:
    headers: list[str] = []
    seen: dict[str, int] = {}
    for index, cell in enumerate(cells):
        header = cell.replace("\n", " ").strip().lstrip("#").strip()
        if "Monitor Name" in header:
            header = "Monitor Name"
        if not header:
            header = f"Column {index + 1}"
        if header in seen:
            seen[header] += 1
            header = f"{header} {seen[header]}"
        else:
            seen[header] = 1
        headers.append(header)
    return headers


def row_to_values(header: list[str], raw_row: tuple[object, ...]) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, name in enumerate(header):
        text = _cell_text(raw_row[index] if index < len(raw_row) else None)
        if text:
            values[name] = text
    return values


def is_valid_dtc_row(values: dict[str, str]) -> bool:
    if len(values) < 2:
        return False
    first = next(iter(values.values()), "")
    if first.startswith("#") or first.lower().startswith("group "):
        return False
    combined = " ".join(values.values())
    return bool(CODE_RE.search(combined) or values.get("Service DTC") or values.get("CustomerDTCID"))


def build_entry(
    source: DtcSource,
    sheet_name: str,
    row_number: int,
    preferred_sheet: bool,
    values: dict[str, str],
) -> DtcEntry:
    primary_code = first_non_empty(
        values.get("Service DTC"),
        values.get("DTC"),
        values.get("CustomerDTCID"),
        values.get("J1979-2 Service DTC"),
        values.get("J1979-2 DTC"),
    )
    description = first_non_empty(
        values.get("DTC Description"),
        values.get("J1979-2 Description"),
        values.get("Monitor Name"),
        values.get("Message"),
    )
    identifier = first_non_empty(
        values.get("DTC Identifier"),
        values.get("InternalDTCID"),
        values.get("ID hex"),
        values.get("ID"),
        values.get("Message"),
    )
    search_text = normalize_search_term(" ".join(values.values()))
    return DtcEntry(
        system=source.system,
        system_label=source.system_label,
        file_name=source.file_name,
        sheet_name=sheet_name,
        row_number=row_number,
        preferred_sheet=preferred_sheet,
        values=values,
        primary_code=primary_code,
        description=description,
        identifier=identifier,
        search_text=search_text,
    )


def first_non_empty(*values: str | None) -> str:
    for value in values:
        if value:
            return value.strip()
    return ""


def normalize_code(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).lower()


def normalize_search_term(value: str) -> str:
    value = str(value or "").strip().lower()
    value = value.replace("，", " ").replace("。", " ").replace("：", " ")
    return re.sub(r"\s+", " ", value)


@lru_cache(maxsize=1)
def count_preferred_entries_by_system() -> dict[str, int]:
    counts = {"drive": 0, "brake": 0}
    for entry in load_dtc_entries():
        if entry.preferred_sheet:
            counts[entry.system] = counts.get(entry.system, 0) + 1
    return counts


@lru_cache(maxsize=1)
def count_entries_by_sheet() -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in load_dtc_entries():
        key = f"{entry.file_name}/{entry.sheet_name}"
        counts[key] = counts.get(key, 0) + 1
    return counts
