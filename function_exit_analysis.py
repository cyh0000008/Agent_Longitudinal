from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
import re

from analysis_engine.signal_lib import H5SignalStore


ROOT = Path(__file__).resolve().parent
EXIT_RULE_CONFIG_PATH = ROOT / "config" / "acc_lcc_exit_reason_rules.xlsx"

MATCHED = "已匹配"
MISSING = "未匹配"
ACC_INHIBIT_REASON_ALIASES = [
    "MPC_DebugMsg_Reserved_14.ACC_Inhibit_Reasion",
    "ACC_Inhibit_Reasion",
    "MPC_DebugMsg_Reserved_14.Debug_uint_Reserved_42",
    "MPC_DebugMsg_Reserved_14__Debug_uint_Reserved_42",
    "MPC_DebugMsg_Reserved_14.Debug_uint8_Reserved_42",
    "MPC_DebugMsg_Reserved_14__Debug_uint8_Reserved_42",
    "Debug_uint_Reserved_42",
    "Debug_uint8_Reserved_42",
]
LCC_STATE_ALIASES = [
    "MPC_DebugMsg_Reserved_10.Debug_uint_Reserved_11",
    "MPC_DebugMsg_Reserved_10__Debug_uint_Reserved_11",
    "ch4/Eth_Debug_CAN5/PATAC_Rcore_Debug_Msg_0p8/0x3D9_MPC_DebugMsg_Reserved_10/signals/Debug_uint_Reserved_11",
    "Debug_uint_Reserved_11",
]
LCC_INHIBIT_REASON_ALIASES = [
    "MPC_DebugMsg_Reserved_10.LCC_Inhibit_Reasion",
    "LCC_Inhibit_Reasion",
    "MPC_DebugMsg_Reserved_12.Debug_uint_Reserved_32",
    "MPC_DebugMsg_Reserved_12__Debug_uint_Reserved_32",
    "MPC_DebugMsg_Reserved_12__Debug_uint_Reserved_12",
    "MPC_DebugMsg_Reserved_12.Debug_uint_Reserved_12",
    "Debug_uint_Reserved_12",
]
DISABLED_EXIT_LOGICAL_NAMES = {
    "acc_mrr_state",
    "acc_exit_reason_1",
    "acc_exit_reason_2",
    "acc_exit_reason_3",
    "acc_exit_reason_4",
    "acc_additional_act2psv",
    "acc_standby_to_active_code",
}


@dataclass
class ExitSignalRule:
    function: str
    label: str
    logical_name: str
    aliases: list[str]
    decode_type: str = "enum"
    role: str = ""
    note: str = ""


@dataclass
class ExitSignalMatch:
    function: str
    label: str
    logical_name: str
    signal_name: str
    status: str
    note: str = ""


@dataclass
class ExitEvent:
    function: str
    time_text: str
    from_state: str
    to_state: str
    reason_summary: str
    reason_details: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass
class FunctionExitAnalysisResult:
    file_name: str
    request_text: str
    status: str
    summary: str
    matched_signals: list[ExitSignalMatch] = field(default_factory=list)
    missing_signals: list[ExitSignalMatch] = field(default_factory=list)
    events: list[ExitEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_scope: str = "ACC / LCC"


DEFAULT_SIGNAL_RULES: list[ExitSignalRule] = [
    ExitSignalRule(
        "ACC",
        "ACC 功能状态（debug）",
        "acc_state_debug",
        [
            "MPC_DebugMsg_Reserved_14__Debug_uint8_Reserved_47",
            "MPC_DebugMsg_Reserved_14__Debug_uint_Reserved_47",
            "MPC_DebugMsg_Reserved_14.Debug_uint8_Reserved_47",
            "MPC_DebugMsg_Reserved_14.Debug_uint_Reserved_47",
            "Debug_uint8_Reserved_47",
            "Debug_uint_Reserved_47",
        ],
        "enum",
        "state",
    ),
    ExitSignalRule(
        "ACC",
        "ACC 功能抑制/退出原因",
        "acc_inhibit_reason",
        ACC_INHIBIT_REASON_ALIASES,
        "enum",
        "reason",
    ),
    ExitSignalRule(
        "LCC",
        "LCC 功能状态",
        "lcc_state",
        LCC_STATE_ALIASES,
        "enum",
        "state",
    ),
    ExitSignalRule(
        "LCC",
        "LCC 抑制/退出原因",
        "lcc_inhibit_reason",
        LCC_INHIBIT_REASON_ALIASES,
        "enum",
        "reason",
    ),
]


DEFAULT_DECODES: dict[str, dict[int, str]] = {
    "acc_state_debug": {
        0: "Off",
        1: "Passive（存在抑制项）",
        2: "Standby（未激活）",
        3: "Standwait",
        4: "Standactive",
        5: "Active",
        6: "BrakeOnly",
        7: "Override",
    },
    "acc_inhibit_reason": {
        1: "CDPActive",
        2: "UnexpectGear（档位非预期）",
        3: "DrDoorOpen（主驾车门打开）",
        4: "AnyDoorOpen（其他车门）",
        5: "DrSeatBeltUnlock（主驾安全带）",
        6: "AEBMode",
        7: "ESPOff（ESP系统关闭）",
        8: "DriveUnready",
        9: "TcmFail",
        10: "EPB_Act（EPB激活）",
        11: "CCDUnAvl",
        12: "WhlDirAbnorm（车轮转动方向异常）",
        13: "DTC（FDA7诊断不通过）",
        14: "ABSActive（ABS激活）",
        15: "TCSActive（TCS激活）",
        16: "HDCActive（HDC激活）",
        17: "VDCActive（VDC激活）",
        18: "ESPActive",
        19: "ACC_softoff（ACC软开关关闭）",
        20: "HoodOpen（前机盖）",
        21: "TailDoorOpen（后备箱）",
        22: "ECM bug",
        23: "TrailerMod",
        24: "BCM_IgnitionSts",
        25: "indrivemode（底软非行车模式）",
        26: "brake_ped_0learnd（制动踏板零位学习）",
        27: "eps0_learnd034（EPS零位学习）",
        28: "imu0learnd（IMU标定学习）",
        29: "hiv1_no_link",
        30: "drv_brk_stuts（驱动/制动底盘状态）",
        31: "airbag_deployed（安全气囊激活）",
        32: "drivemode_lanekeep（驾驶模式不支持）",
        33: "seatbelt_10s（安全带）",
        34: "rampbigger 12",
    },
    "lcc_state": {
        0: "OFF",
        1: "Passive",
        2: "Passive",
        3: "Standby",
        4: "Active",
        5: "temp exit",
        6: "reserved",
        7: "DOP（跟车）",
    },
    "lcc_inhibit_reason": {
        1: "转速过高",
        2: "刹车踏板踩下",
        3: "手力抑制",
        4: "前轮压线",
        5: "reserved",
        6: "车道过窄",
        7: "EPS状态异常",
        8: "车道线交叉",
        9: "reserved",
        10: "方向盘转角抑制",
        11: "车速过高",
        12: "弯道半径过小",
        13: "双侧无线（常见于路口）",
        14: "后视镜折叠",
        15: "油箱盖未关/车道线跳变",
        16: "reserved",
        17: "油门override达到2min",
        18: "EPS inactive",
    },
}


ACC_REASON_KEYS = (
    "acc_inhibit_reason",
)
LCC_REASON_KEYS = ("lcc_inhibit_reason",)
FUNCTION_EXIT_CORE_LOGICALS = {
    "ACC": ("acc_state_debug", "acc_inhibit_reason"),
    "LCC": ("lcc_state", "lcc_inhibit_reason"),
}


def is_function_exit_data_file(file_name: str) -> bool:
    return Path(file_name or "").suffix.lower() in {".h5", ".hdf5"}


def run_function_exit_analysis(
    file_path: str | Path,
    request_text: str = "",
    problem_date: str = "",
    problem_time: str = "",
) -> FunctionExitAnalysisResult:
    path = Path(file_path)
    if path.suffix.lower() not in {".h5", ".hdf5"}:
        return FunctionExitAnalysisResult(
            file_name=path.name,
            request_text=request_text,
            status="error",
            summary="ACC/LCC 功能退出原因分析当前需要解析后的 .h5/.hdf5 数据；BLF 需要先解码成 H5 后再分析。",
            warnings=["当前能力不直接解析 BLF。"],
        )

    try:
        rules, decodes = load_exit_rule_config()
        store = H5SignalStore.from_hdf5(path)
        requested = _requested_functions(request_text)
        active_rules = [rule for rule in rules if rule.function in requested]
        matches = _resolve_signal_matches(store, active_rules)
        matched = [item for item in matches.values() if item.status == MATCHED]
        missing = [item for item in matches.values() if item.status != MATCHED]
        complete_functions, core_missing = _function_exit_core_signal_status(requested, matches, rules)
        warnings: list[str] = []
        if core_missing:
            warnings.append("缺少功能退出核心信号：" + "、".join(core_missing))
        if not complete_functions:
            return FunctionExitAnalysisResult(
                file_name=path.name,
                request_text=request_text,
                status="error",
                summary="缺少 ACC/LCC 功能退出核心信号，未生成退出原因结论；需要状态信号和原因码信号同时存在。",
                matched_signals=matched,
                missing_signals=missing,
                warnings=warnings,
                checked_scope=" / ".join(sorted(requested)),
            )

        base_time = _base_timestamp(store, matched)
        target_elapsed = _problem_elapsed_seconds_from_filename(
            path,
            problem_date=problem_date,
            problem_time=problem_time,
        )
        events: list[ExitEvent] = []
        if "ACC" in complete_functions:
            events.extend(_detect_acc_events(store, matches, rules, decodes, base_time, warnings))
        if "LCC" in complete_functions:
            events.extend(_detect_lcc_events(store, matches, rules, decodes, base_time, warnings))

        reason_observations: list[ExitEvent] = []
        if target_elapsed is not None:
            if "ACC" in complete_functions:
                reason_observations.extend(
                    _detect_reason_observation_events(
                        store,
                        "ACC",
                        ACC_REASON_KEYS,
                        matches,
                        rules,
                        decodes,
                        base_time,
                        target_elapsed=target_elapsed,
                    )
                )
            if "LCC" in complete_functions:
                reason_observations.extend(
                    _detect_reason_observation_events(
                        store,
                        "LCC",
                        LCC_REASON_KEYS,
                        matches,
                        rules,
                        decodes,
                        base_time,
                        target_elapsed=target_elapsed,
                    )
                )
            added_observations = _dedupe_reason_observation_events(events, reason_observations)
            if added_observations:
                events.extend(added_observations)
                warnings.append(
                    "已按表格问题时间附近的原因/抑制信号置位补充事件；"
                    "该证据不要求状态信号必须出现 4/7 -> Off 跳变。"
                )
        if target_elapsed is not None and events:
            events.sort(key=lambda event: abs(_seconds_from_time_text(event.time_text) - target_elapsed))
            warnings.append(f"已按表格问题时间优先排序退出事件：H5 内约 {target_elapsed:.1f}s。")

        has_state_signal = any(match.role == "state" for match in _matched_rule_pairs(matches, rules))
        if events:
            status = "issue"
            summary = f"我在这段数据里看到 {len(events)} 次 ACC/LCC 退出或原因置位相关事件。{_top_reason_summary(events)}"
        elif not has_state_signal:
            status = "warning"
            summary = "没有匹配到 ACC/LCC 状态信号，所以还不能判断功能是否退出。需要先确认 H5 里是否包含状态 debug 信号。"
        else:
            status = "ok"
            summary = "这段数据里没有看到 ACC/LCC 从激活态退出的明确跳变；已匹配到的状态信号可用于继续核对具体时间窗。"

        if missing:
            missing_labels = "、".join(f"{item.function}-{item.label}" for item in missing[:6])
            warnings.append(f"仍有 {len(missing)} 个关注信号没有匹配到：{missing_labels}")

        return FunctionExitAnalysisResult(
            file_name=path.name,
            request_text=request_text,
            status=status,
            summary=summary,
            matched_signals=matched,
            missing_signals=missing,
            events=events,
            warnings=warnings,
            checked_scope=" / ".join(sorted(complete_functions)),
        )
    except Exception as exc:
        return FunctionExitAnalysisResult(
            file_name=path.name,
            request_text=request_text,
            status="error",
            summary=f"ACC/LCC 功能退出原因分析失败：{exc}",
            warnings=[str(exc)],
        )


def format_function_exit_result_context(result: FunctionExitAnalysisResult) -> str:
    core_names = {name for names in FUNCTION_EXIT_CORE_LOGICALS.values() for name in names}
    core_matches = [
        item
        for item in result.matched_signals
        if item.logical_name in core_names and item.status == MATCHED
    ]
    core_missing = [
        item
        for item in result.missing_signals
        if item.logical_name in core_names and item.status != MATCHED
    ]
    lines = [
        f"文件：{result.file_name}",
        f"状态：{result.status}",
        f"总结：{result.summary}",
        f"检查范围：{result.checked_scope}",
        "证据范围：仅包含 ACC/LCC 状态信号和退出/抑制原因码信号。",
        f"核心信号匹配：{len(core_matches)}",
        f"核心信号缺失：{len(core_missing)}",
        f"退出/原因置位事件：{len(result.events)}",
    ]
    for item in core_matches[:8]:
        lines.append(f"- 信号：{item.function} | {item.label} | {item.signal_name}")
    for item in core_missing[:8]:
        lines.append(f"- 缺失：{item.function} | {item.label}")
    for event in result.events[:20]:
        lines.append(
            f"- {event.function} | {event.time_text} | {event.from_state}->{event.to_state} | {event.reason_summary}"
        )
        for detail in event.reason_details[:4]:
            lines.append(f"  - 原因码：{detail}")
    return "\n".join(lines)


def load_exit_rule_config() -> tuple[list[ExitSignalRule], dict[str, dict[int, str]]]:
    if not EXIT_RULE_CONFIG_PATH.exists():
        return DEFAULT_SIGNAL_RULES, DEFAULT_DECODES
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(EXIT_RULE_CONFIG_PATH, read_only=True, data_only=True)
        rules = _merge_exit_signal_rules(_load_signal_rules_from_workbook(workbook))
        decodes = _merge_exit_decodes(_load_decodes_from_workbook(workbook))
        return rules, decodes
    except Exception:
        return DEFAULT_SIGNAL_RULES, DEFAULT_DECODES


def _merge_exit_signal_rules(config_rules: list[ExitSignalRule]) -> list[ExitSignalRule]:
    config_rules = [rule for rule in (config_rules or []) if rule.logical_name not in DISABLED_EXIT_LOGICAL_NAMES]
    if not config_rules:
        return [rule for rule in DEFAULT_SIGNAL_RULES if rule.logical_name not in DISABLED_EXIT_LOGICAL_NAMES]
    merged = [
        ExitSignalRule(
            rule.function,
            rule.label,
            rule.logical_name,
            list(rule.aliases),
            rule.decode_type,
            rule.role,
            rule.note,
        )
        for rule in config_rules
    ]
    index_by_key = {(rule.function, rule.logical_name): idx for idx, rule in enumerate(merged)}
    for default in DEFAULT_SIGNAL_RULES:
        if default.logical_name in DISABLED_EXIT_LOGICAL_NAMES:
            continue
        key = (default.function, default.logical_name)
        idx = index_by_key.get(key)
        if idx is None:
            index_by_key[key] = len(merged)
            merged.append(default)
            continue
        rule = merged[idx]
        existing = {alias.lower() for alias in rule.aliases}
        for alias in default.aliases:
            if alias.lower() not in existing:
                rule.aliases.append(alias)
                existing.add(alias.lower())
        if not rule.role:
            rule.role = default.role
        if not rule.decode_type:
            rule.decode_type = default.decode_type
        if not rule.label:
            rule.label = default.label
    return merged


def _merge_exit_decodes(config_decodes: dict[str, dict[int, str]]) -> dict[str, dict[int, str]]:
    merged = {
        name: dict(values)
        for name, values in DEFAULT_DECODES.items()
        if name not in DISABLED_EXIT_LOGICAL_NAMES
    }
    for logical_name, values in (config_decodes or {}).items():
        if logical_name in DISABLED_EXIT_LOGICAL_NAMES:
            continue
        merged.setdefault(logical_name, {}).update(values)
        if logical_name in {"acc_inhibit_reason", "lcc_inhibit_reason"}:
            merged[logical_name].update(DEFAULT_DECODES[logical_name])
    return merged


def query_function_exit_reference(query: str, max_rows: int = 12) -> str:
    """Answer no-file ACC/LCC exit-reason questions from the maintained config workbook."""
    rows = _read_exit_reference_rows()
    if not rows:
        return (
            "我查了一下当前的 ACC/LCC 功能退出原因配置，但配置表暂时不可读。"
            "如果要确认某个退出原因，需要先检查 `config/acc_lcc_exit_reason_rules.xlsx` 是否存在且能打开。"
        )

    value = (query or "").strip()
    normalized = value.lower()
    reverse_query_terms = ["reverse", "反向", "轮速方向", "车轮方向", "轮速", "滚动方向", "rolling", "opposite"]
    reverse_config_terms = [
        *reverse_query_terms,
        "whldirabnorm",
        "whl dir",
        "wheel dir",
        "wheel direction",
        "wheel speed direction",
    ]
    if any(term in normalized for term in reverse_query_terms):
        hits = [
            row
            for row in rows
            if any(term in row["text"].lower() for term in reverse_config_terms)
        ]
        if not hits:
            signal_rows = [
                row
                for row in rows
                if row["sheet"] == "signals"
                and any(term in row["text"].lower() for term in ["acc", "lcc", "退出", "reason", "inhibit"])
            ]
            signal_summary = _format_exit_reference_rows(signal_rows[:5]) if signal_rows else ""
            extra = f"\n\n当前配置里和功能退出分析直接相关的证据主要是：\n{signal_summary}" if signal_summary else ""
            return (
                "我查的是布鲁当前的 ACC/LCC 功能退出原因配置。\n\n"
                "结论：当前没有把“轮速方向反向 / reverse”定义成独立的 ACC/LCC 功能退出原因或退出原因码。\n\n"
                "也就是说，按现在的行车问题原因分析配置，不能直接判定“reverse 导致 ACC/LCC 退出”。"
                "如果你们确实希望覆盖这个场景，需要补充轮速方向反向对应的信号、触发条件，以及它和 ACC/LCC 退出状态或原因码之间的设计关系。"
                f"{extra}"
            )
        return (
            "我查的是布鲁当前的 ACC/LCC 功能退出原因配置。\n\n"
            "结论：有覆盖。当前配置里已经把“轮速/车轮方向异常”维护为 ACC 相关退出或抑制原因，"
            "配置项名称是 WhlDirAbnorm。\n\n"
            "命中的配置项：\n"
            + _format_exit_reference_rows(hits[:max_rows])
            + "\n\n如果要判断某段 H5 是不是因为这个原因退出，还需要同时看 ACC/LCC 状态跳变、退出原因码、"
            "以及对应轮速/方向证据的时序关系。"
        )

    tokens = _function_exit_query_tokens(value)
    if any(term in normalized for term in ["有哪些", "哪些", "列表", "原因码", "退出码", "退出原因", "多少"]):
        reason_rows = [
            row
            for row in rows
            if row["sheet"] == "decode_values"
            and any(term in row["text"].lower() for term in ["reason", "退出", "抑制", "inhibit", "temporary"])
        ]
        if reason_rows:
            return (
                "我按当前配置表整理了一下 ACC/LCC 功能退出相关原因：\n\n"
                + _format_exit_reference_rows(reason_rows[:max_rows])
            )

    scored = sorted(
        (
            (_score_exit_reference_row(row, tokens), row)
            for row in rows
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    hits = [row for score, row in scored if score > 0][:max_rows]
    if not hits:
        return (
            "我查了当前的 ACC/LCC 功能退出原因配置，但没有命中你提到的具体项。\n\n"
            "可以确认的是：如果要判断某个条件是否属于功能退出原因，优先看配置表里的退出原因码、抑制原因和 ACC/LCC 状态信号。"
            "如果你希望把这个条件纳入自动分析，需要补充对应信号、取值含义和退出状态之间的关系。"
        )
    return "我按当前 ACC/LCC 功能退出配置查到这些相关项：\n\n" + _format_exit_reference_rows(hits)


def _read_exit_reference_rows() -> list[dict[str, Any]]:
    if not EXIT_RULE_CONFIG_PATH.exists():
        return []
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(EXIT_RULE_CONFIG_PATH, read_only=True, data_only=True)
    except Exception:
        return []

    result: list[dict[str, Any]] = []
    for sheet_name in ("decode_values", "signals"):
        if sheet_name not in workbook.sheetnames:
            continue
        sheet = workbook[sheet_name]
        rows = list(sheet.iter_rows(values_only=True))
        if len(rows) < 2:
            continue
        header = [_cell_text(value) for value in rows[0]]
        for row in rows[1:]:
            data = {
                (header[index] if index < len(header) and header[index] else f"列{index + 1}"): _cell_text(value)
                for index, value in enumerate(row)
            }
            logical = data.get("逻辑字段") or data.get("logical_name") or ""
            purpose = data.get("用途") or data.get("label") or ""
            if logical in DISABLED_EXIT_LOGICAL_NAMES or "MRR 给 ESC" in purpose:
                continue
            text = " ".join(part for part in data.values() if part)
            if text:
                result.append({"sheet": sheet_name, "data": data, "text": text})
    return result


def _function_exit_query_tokens(query: str) -> list[str]:
    separators = ["，", "。", "、", "；", "：", "(", ")", "（", "）", "/", "\\", ",", ";", ":", "?", "？"]
    value = (query or "").lower()
    for separator in separators:
        value = value.replace(separator, " ")
    tokens = [token.strip() for token in value.split() if len(token.strip()) >= 2]
    for keyword in ["acc", "lcc", "退出", "功能退出", "原因", "原因码", "抑制", "临时退出"]:
        if keyword in (query or "").lower() and keyword not in tokens:
            tokens.append(keyword)
    return tokens


def _score_exit_reference_row(row: dict[str, Any], tokens: list[str]) -> int:
    text = str(row.get("text") or "").lower()
    score = 0
    for token in tokens:
        if token and token in text:
            score += 3 if token in {"acc", "lcc", "功能退出", "退出", "原因码"} else 1
    if row.get("sheet") == "decode_values":
        score += 1
    return score


def _format_exit_reference_rows(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        data = row.get("data") or {}
        function = data.get("功能") or data.get("function") or ""
        purpose = data.get("用途") or data.get("label") or ""
        logical = data.get("逻辑字段") or data.get("logical_name") or ""
        value = data.get("取值/bit") or data.get("value") or ""
        meaning = data.get("含义") or data.get("meaning") or ""
        signal = data.get("真实信号名") or data.get("signal_name") or ""
        note = data.get("说明") or data.get("备注") or data.get("note") or ""

        head_parts = [part for part in [function, purpose] if part]
        head = " / ".join(head_parts) if head_parts else (logical or signal or "配置项")
        details = []
        if value or meaning:
            details.append(f"{value} = {meaning}".strip(" ="))
        if signal:
            details.append(f"信号：{signal}")
        if note:
            details.append(note)
        if not details and logical:
            details.append(logical)
        lines.append(f"- {head}：" + "；".join(details))
    return "\n".join(lines)


def _load_signal_rules_from_workbook(workbook: Any) -> list[ExitSignalRule]:
    if "signals" not in workbook.sheetnames:
        return []
    sheet = workbook["signals"]
    rows = list(sheet.iter_rows(values_only=True))
    if len(rows) < 2:
        return []
    header = [_cell_text(value) for value in rows[0]]
    result: list[ExitSignalRule] = []
    for row in rows[1:]:
        function = _row_text(row, header, "功能", "function")
        logical = _row_text(row, header, "逻辑字段", "logical_name")
        if logical in DISABLED_EXIT_LOGICAL_NAMES:
            continue
        aliases = _split_aliases(_row_text(row, header, "真实信号名", "signal_name"))
        aliases.extend(_split_aliases(_row_text(row, header, "H5数据集路径", "h5_path", "dataset_path")))
        label = _row_text(row, header, "用途", "label") or logical
        if logical == "acc_inhibit_reason":
            aliases = list(ACC_INHIBIT_REASON_ALIASES)
            label = "ACC 功能抑制/退出原因"
        elif logical == "lcc_state":
            aliases = list(LCC_STATE_ALIASES)
        elif logical == "lcc_inhibit_reason":
            aliases = list(LCC_INHIBIT_REASON_ALIASES)
        if not function or not logical or not aliases:
            continue
        result.append(
            ExitSignalRule(
                function=function,
                label=label,
                logical_name=logical,
                aliases=aliases,
                decode_type=_row_text(row, header, "解码方式", "decode_type") or "enum",
                role=_row_text(row, header, "角色", "role"),
                note=_row_text(row, header, "备注", "note"),
            )
        )
    return result


def _load_decodes_from_workbook(workbook: Any) -> dict[str, dict[int, str]]:
    if "decode_values" not in workbook.sheetnames:
        return {}
    sheet = workbook["decode_values"]
    rows = list(sheet.iter_rows(values_only=True))
    if len(rows) < 2:
        return {}
    header = [_cell_text(value) for value in rows[0]]
    decodes: dict[str, dict[int, str]] = {}
    for row in rows[1:]:
        logical = _row_text(row, header, "逻辑字段", "logical_name")
        if logical in DISABLED_EXIT_LOGICAL_NAMES:
            continue
        raw_value = _row_text(row, header, "取值/bit", "value")
        label = _row_text(row, header, "含义", "meaning")
        if not logical or raw_value == "" or not label:
            continue
        value = _int_value(raw_value)
        if value is None:
            continue
        decodes.setdefault(logical, {})[value] = label
    return decodes


def _resolve_signal_matches(store: H5SignalStore, rules: list[ExitSignalRule]) -> dict[str, ExitSignalMatch]:
    available = store.list_signals()
    lower_to_signal = {name.lower(): name for name in available}
    normalized_to_signal = {_normalize_signal_key(name): name for name in available}
    matches: dict[str, ExitSignalMatch] = {}
    for rule in rules:
        signal_name = _resolve_one_signal(rule.aliases, available, lower_to_signal, normalized_to_signal)
        if signal_name:
            matches[rule.logical_name] = ExitSignalMatch(
                rule.function,
                rule.label,
                rule.logical_name,
                signal_name,
                MATCHED,
                rule.note,
            )
        else:
            alias_hint = " / ".join(rule.aliases[:3])
            matches[rule.logical_name] = ExitSignalMatch(
                rule.function,
                rule.label,
                rule.logical_name,
                "",
                MISSING,
                alias_hint,
            )
    return matches


def _resolve_one_signal(
    aliases: list[str],
    available: list[str],
    lower_to_signal: dict[str, str],
    normalized_to_signal: dict[str, str],
) -> str:
    for alias in aliases:
        key = alias.strip().replace("\\", "/").strip("/").lower()
        if not key:
            continue
        if key in lower_to_signal:
            return lower_to_signal[key]
        if alias.strip().lower() in lower_to_signal:
            return lower_to_signal[alias.strip().lower()]
        normalized = _normalize_signal_key(alias)
        if normalized in normalized_to_signal:
            return normalized_to_signal[normalized]
        for signal in available:
            signal_lower = signal.lower().replace("\\", "/").strip("/")
            if signal_lower.endswith("." + key) or signal_lower.endswith("/" + key):
                return signal
    for alias in aliases:
        key = alias.strip().replace("\\", "/").strip("/").lower()
        normalized = _normalize_signal_key(alias)
        for signal in available:
            signal_lower = signal.lower().replace("\\", "/").strip("/")
            if key and key in signal_lower:
                return signal
            if normalized and normalized in _normalize_signal_key(signal):
                return signal
    return ""


def _detect_acc_events(
    store: H5SignalStore,
    matches: dict[str, ExitSignalMatch],
    rules: list[ExitSignalRule],
    decodes: dict[str, dict[int, str]],
    base_time: float,
    warnings: list[str],
) -> list[ExitEvent]:
    state_key = "acc_state_debug"
    if not _matched(matches, state_key):
        warnings.append("没有匹配到 ACC 状态信号，ACC 退出无法自动判断。")
        return []
    strong_active_values = {4, 5, 6, 7}
    active_values = _non_off_state_values(state_key, decodes, fallback=strong_active_values)
    return _detect_exit_events(
        store,
        "ACC",
        state_key,
        active_values,
        strong_active_values,
        ACC_REASON_KEYS,
        matches,
        rules,
        decodes,
        base_time,
    )


def _detect_lcc_events(
    store: H5SignalStore,
    matches: dict[str, ExitSignalMatch],
    rules: list[ExitSignalRule],
    decodes: dict[str, dict[int, str]],
    base_time: float,
    warnings: list[str],
) -> list[ExitEvent]:
    state_key = "lcc_state"
    events: list[ExitEvent] = []
    if _matched(matches, state_key):
        strong_active_values = {4, 7}
        active_values = _non_off_state_values("lcc_state", decodes, fallback=strong_active_values)
        events = _detect_exit_events(
            store,
            "LCC",
            state_key,
            active_values,
            strong_active_values,
            LCC_REASON_KEYS,
            matches,
            rules,
            decodes,
            base_time,
        )
    else:
        warnings.append("没有匹配到 LCC 状态信号，LCC 退出无法自动判断。")
    return events


def _detect_exit_events(
    store: H5SignalStore,
    function: str,
    state_key: str,
    active_values: set[int],
    strong_active_values: set[int],
    reason_keys: tuple[str, ...],
    matches: dict[str, ExitSignalMatch],
    rules: list[ExitSignalRule],
    decodes: dict[str, dict[int, str]],
    base_time: float,
) -> list[ExitEvent]:
    state_signal_name = matches[state_key].signal_name
    signal = store.get_signal(state_signal_name)
    timestamps = [float(item) for item in signal.get("timestamps", [])]
    values = signal.get("values", [])
    if not timestamps or not values:
        return []

    events: list[ExitEvent] = []
    previous = _int_value(values[0])
    if previous is None:
        return events
    for index in range(1, len(values)):
        current = _int_value(values[index])
        if current is None or current == previous:
            continue
        timestamp = timestamps[index]
        exited_from_strong_active = previous in strong_active_values and current not in strong_active_values
        exited_from_non_off = previous in active_values and current not in active_values
        if exited_from_strong_active or exited_from_non_off:
            details = _reason_details_at_timestamp(
                store,
                timestamp,
                reason_keys,
                matches,
                rules,
                decodes,
            )
            if not details:
                details = _reason_details_for_window(
                    store,
                    timestamp - 0.5,
                    timestamp + 1.5,
                    reason_keys,
                    matches,
                    rules,
                    decodes,
                )
            if not details:
                details = _reason_details_for_window(
                    store,
                    timestamp - 8.0,
                    timestamp + 3.0,
                    reason_keys,
                    matches,
                    rules,
                    decodes,
                )
            if not details and previous not in strong_active_values:
                previous = current
                continue
            reason_summary = "；".join(details[:2]) if details else "退出窗口内没有看到明确非零原因码"
            evidence = [
                f"{matches[state_key].label}：{_decode_single(state_key, previous, decodes)} -> {_decode_single(state_key, current, decodes)}",
            ]
            evidence.extend(details[:4])
            events.append(
                ExitEvent(
                    function=function,
                    time_text=f"{timestamp - base_time:.3f}s",
                    from_state=_decode_single(state_key, previous, decodes),
                    to_state=_decode_single(state_key, current, decodes),
                    reason_summary=reason_summary,
                    reason_details=details,
                    evidence=evidence,
                )
            )
        previous = current
        if len(events) >= 50:
            break
    return events


def _detect_reason_observation_events(
    store: H5SignalStore,
    function: str,
    reason_keys: tuple[str, ...],
    matches: dict[str, ExitSignalMatch],
    rules: list[ExitSignalRule],
    decodes: dict[str, dict[int, str]],
    base_time: float,
    *,
    target_elapsed: float,
) -> list[ExitEvent]:
    center_timestamp = base_time + float(target_elapsed)
    observations = _reason_observations_for_window(
        store,
        center_timestamp - 3.0,
        center_timestamp + 3.0,
        center_timestamp,
        reason_keys,
        matches,
        rules,
        decodes,
    )
    scope_text = "问题时间附近 3s 内"
    if not observations:
        observations = _reason_observations_for_window(
            store,
            center_timestamp - 30.0,
            center_timestamp + 30.0,
            center_timestamp,
            reason_keys,
            matches,
            rules,
            decodes,
        )
        scope_text = "问题时间前后 30s 内"
    if not observations:
        return []

    event_time = min(float(item["timestamp"]) for item in observations)
    details = [str(item["detail"]) for item in observations[:6]]
    return [
        ExitEvent(
            function=function,
            time_text=f"{event_time - base_time:.3f}s",
            from_state="未依赖状态跳变",
            to_state="原因信号置位",
            reason_summary="；".join(details[:2]),
            reason_details=details,
            evidence=[f"{scope_text}：{detail}" for detail in details[:4]],
        )
    ]


def _reason_observations_for_window(
    store: H5SignalStore,
    start_timestamp: float,
    end_timestamp: float,
    center_timestamp: float,
    reason_keys: tuple[str, ...],
    matches: dict[str, ExitSignalMatch],
    rules: list[ExitSignalRule],
    decodes: dict[str, dict[int, str]],
) -> list[dict[str, Any]]:
    rule_by_name = {rule.logical_name: rule for rule in rules}
    candidates: list[tuple[float, str, int, dict[str, Any]]] = []
    for key in reason_keys:
        match = matches.get(key)
        if not match or match.status != MATCHED:
            continue
        signal = store.get_signal(match.signal_name)
        timestamps = [float(item) for item in signal.get("timestamps", [])]
        values = signal.get("values", [])
        if not timestamps:
            continue
        left = bisect_left(timestamps, start_timestamp)
        right = bisect_right(timestamps, end_timestamp)
        for index in range(left, min(right, len(timestamps))):
            value = _int_value(values[index])
            if value is None or value == 0:
                continue
            rule = rule_by_name.get(key)
            decoded = _decode_reason_value(key, value, rule.decode_type if rule else "enum", decodes)
            item = {
                "logical_name": key,
                "timestamp": timestamps[index],
                "value": value,
                "detail": f"{match.label}={value}（{decoded}）",
            }
            candidates.append((abs(timestamps[index] - center_timestamp), key, value, item))
    candidates.sort(key=lambda item: item[0])
    seen: set[tuple[str, int]] = set()
    result: list[dict[str, Any]] = []
    for _, key, value, item in candidates:
        dedupe_key = (key, value)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        result.append(item)
        if len(result) >= 8:
            break
    result.sort(key=lambda item: float(item["timestamp"]))
    return result


def _dedupe_reason_observation_events(
    existing_events: list[ExitEvent],
    observation_events: list[ExitEvent],
) -> list[ExitEvent]:
    result: list[ExitEvent] = []
    for observation in observation_events:
        observation_time = _seconds_from_time_text(observation.time_text)
        duplicate = False
        for event in existing_events:
            if event.function != observation.function:
                continue
            if event.reason_summary != observation.reason_summary:
                continue
            if abs(_seconds_from_time_text(event.time_text) - observation_time) <= 1.0:
                duplicate = True
                break
        if not duplicate:
            result.append(observation)
    return result


def _reason_details_at_timestamp(
    store: H5SignalStore,
    timestamp: float,
    reason_keys: tuple[str, ...],
    matches: dict[str, ExitSignalMatch],
    rules: list[ExitSignalRule],
    decodes: dict[str, dict[int, str]],
    max_gap_seconds: float = 0.25,
) -> list[str]:
    rule_by_name = {rule.logical_name: rule for rule in rules}
    details: list[str] = []
    for key in reason_keys:
        match = matches.get(key)
        if not match or match.status != MATCHED:
            continue
        signal = store.get_signal(match.signal_name)
        timestamps = [float(item) for item in signal.get("timestamps", [])]
        values = signal.get("values", [])
        if not timestamps or not values:
            continue
        index = bisect_left(timestamps, timestamp)
        candidates = [idx for idx in (index, index - 1) if 0 <= idx < len(timestamps)]
        if not candidates:
            continue
        best = min(candidates, key=lambda idx: abs(timestamps[idx] - timestamp))
        if abs(timestamps[best] - timestamp) > max_gap_seconds:
            continue
        value = _int_value(values[best])
        if value is None or value == 0:
            continue
        rule = rule_by_name.get(key)
        decoded = _decode_reason_value(key, value, rule.decode_type if rule else "enum", decodes)
        details.append(f"{match.label}={value}（{decoded}）")
    return details


def _reason_details_for_window(
    store: H5SignalStore,
    start_timestamp: float,
    end_timestamp: float,
    reason_keys: tuple[str, ...],
    matches: dict[str, ExitSignalMatch],
    rules: list[ExitSignalRule],
    decodes: dict[str, dict[int, str]],
) -> list[str]:
    rule_by_name = {rule.logical_name: rule for rule in rules}
    details: list[str] = []
    for key in reason_keys:
        match = matches.get(key)
        if not match or match.status != MATCHED:
            continue
        values = _nonzero_values_in_window(store, match.signal_name, start_timestamp, end_timestamp)
        for value in values:
            rule = rule_by_name.get(key)
            decoded = _decode_reason_value(key, value, rule.decode_type if rule else "enum", decodes)
            details.append(f"{match.label}={value}（{decoded}）")
    return details


def _nonzero_values_in_window(store: H5SignalStore, signal_name: str, start_timestamp: float, end_timestamp: float) -> list[int]:
    signal = store.get_signal(signal_name)
    timestamps = [float(item) for item in signal.get("timestamps", [])]
    values = signal.get("values", [])
    if not timestamps:
        return []
    left = bisect_left(timestamps, start_timestamp)
    right = bisect_right(timestamps, end_timestamp)
    seen: list[int] = []
    for value in values[left:right]:
        numeric = _int_value(value)
        if numeric is None or numeric == 0 or numeric in seen:
            continue
        seen.append(numeric)
    return seen


def _decode_reason_value(logical_name: str, value: int, decode_type: str, decodes: dict[str, dict[int, str]]) -> str:
    if decode_type.lower() == "bitmask":
        labels = []
        bit = 1
        rest = int(value)
        while bit <= max(rest, 1):
            if rest & bit:
                labels.append(decodes.get(logical_name, {}).get(bit, f"bit{bit}"))
            bit <<= 1
        return " + ".join(labels) if labels else decodes.get(logical_name, {}).get(value, str(value))
    return decodes.get(logical_name, {}).get(value, f"未定义取值 {value}")


def _decode_single(logical_name: str, value: int, decodes: dict[str, dict[int, str]]) -> str:
    label = decodes.get(logical_name, {}).get(value)
    if label:
        return f"{value}（{label}）"
    return str(value)


def _non_off_state_values(
    logical_name: str,
    decodes: dict[str, dict[int, str]],
    fallback: set[int],
) -> set[int]:
    values = set()
    for value, label in decodes.get(logical_name, {}).items():
        normalized = str(label or "").lower()
        if value == 0 or normalized in {"off", "reserved"} or "reserved" in normalized:
            continue
        values.add(value)
    return values or set(fallback)


def _seconds_from_time_text(text: str) -> float:
    value = str(text or "").strip().lower()
    if value.endswith("s"):
        value = value[:-1]
    try:
        return float(value)
    except Exception:
        return 0.0


def _matched(matches: dict[str, ExitSignalMatch], logical_name: str) -> bool:
    item = matches.get(logical_name)
    return bool(item and item.status == MATCHED and item.signal_name)


def _function_exit_core_signal_status(
    requested: set[str],
    matches: dict[str, ExitSignalMatch],
    rules: list[ExitSignalRule],
) -> tuple[set[str], list[str]]:
    rule_by_logical = {rule.logical_name: rule for rule in rules}
    complete_functions: set[str] = set()
    missing_labels: list[str] = []
    for function in sorted(requested):
        logical_names = FUNCTION_EXIT_CORE_LOGICALS.get(function, ())
        missing_for_function = []
        for logical_name in logical_names:
            if _matched(matches, logical_name):
                continue
            rule = rule_by_logical.get(logical_name)
            label = rule.label if rule else logical_name
            missing_for_function.append(label)
        if not missing_for_function:
            complete_functions.add(function)
        else:
            missing_labels.extend(f"{function}-{label}" for label in missing_for_function)
    return complete_functions, missing_labels


def _matched_rule_pairs(matches: dict[str, ExitSignalMatch], rules: list[ExitSignalRule]) -> list[ExitSignalRule]:
    return [rule for rule in rules if _matched(matches, rule.logical_name)]


def _base_timestamp(store: H5SignalStore, matches: list[ExitSignalMatch]) -> float:
    timestamps = []
    for match in matches:
        try:
            signal = store.get_signal(match.signal_name)
        except Exception:
            continue
        values = signal.get("timestamps", [])
        if values:
            timestamps.append(float(values[0]))
    return min(timestamps) if timestamps else 0.0


def _top_reason_summary(events: list[ExitEvent]) -> str:
    reasons: dict[str, int] = {}
    for event in events:
        key = event.reason_summary
        if not key:
            continue
        reasons[key] = reasons.get(key, 0) + 1
    if not reasons:
        return "但退出窗口内没有读到明确原因码，需要继续确认原因信号是否接入。"
    top = sorted(reasons.items(), key=lambda item: item[1], reverse=True)[0]
    return f"主要能看到的原因线索是：{top[0]}。"


def _requested_functions(request_text: str) -> set[str]:
    value = (request_text or "").lower()
    result: set[str] = set()
    if "acc" in value or "巡航" in value:
        result.add("ACC")
    if "lcc" in value or "车道居中" in value:
        result.add("LCC")
    if not result:
        result = {"ACC", "LCC"}
    return result


def _problem_elapsed_seconds_from_filename(
    file_path: Path,
    *,
    problem_date: str = "",
    problem_time: str = "",
) -> float | None:
    problem_time = str(problem_time or "").strip()
    if not problem_time:
        return None
    h5_start = _parse_h5_start_datetime(file_path.name)
    if h5_start is None:
        return None
    problem_dt = _parse_problem_datetime(
        problem_date=problem_date,
        problem_time=problem_time,
        fallback_date=h5_start,
    )
    if problem_dt is None:
        return None
    return (problem_dt - h5_start).total_seconds()


def _parse_h5_start_datetime(file_name: str) -> datetime | None:
    match = re.search(
        r"(?P<y>\d{4})[-_](?P<m>\d{2})[-_](?P<d>\d{2})[-_](?P<h>\d{2})[-_](?P<mi>\d{2})[-_](?P<s>\d{2})",
        str(file_name or ""),
    )
    if not match:
        return None
    try:
        return datetime(
            int(match.group("y")),
            int(match.group("m")),
            int(match.group("d")),
            int(match.group("h")),
            int(match.group("mi")),
            int(match.group("s")),
        )
    except ValueError:
        return None


def _parse_problem_datetime(
    *,
    problem_date: str,
    problem_time: str,
    fallback_date: datetime,
) -> datetime | None:
    time_numbers = [int(item) for item in re.findall(r"\d+", str(problem_time or ""))]
    date_numbers = [int(item) for item in re.findall(r"\d+", str(problem_date or ""))]
    if len(time_numbers) >= 6:
        values = time_numbers[:6]
        try:
            return datetime(values[0], values[1], values[2], values[3], values[4], values[5])
        except ValueError:
            return None
    if len(date_numbers) >= 3:
        year, month, day = date_numbers[:3]
    else:
        year, month, day = fallback_date.year, fallback_date.month, fallback_date.day
    if len(time_numbers) < 2:
        return None
    hour, minute = time_numbers[:2]
    second = time_numbers[2] if len(time_numbers) >= 3 else 0
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _int_value(value: object) -> int | None:
    try:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            return int(float(text))
        numeric = float(value)  # type: ignore[arg-type]
        if numeric != numeric:
            return None
        return int(round(numeric))
    except Exception:
        return None


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _row_text(row: tuple[object, ...], header: list[str], *names: str) -> str:
    idx = _header_index(header, names)
    if 0 <= idx < len(row):
        return _cell_text(row[idx])
    return ""


def _header_index(header: list[str], names: tuple[str, ...]) -> int:
    header_map = {name: idx for idx, name in enumerate(header)}
    candidates: list[str] = []
    for name in names:
        candidates.append(name)
        candidates.extend(_semantic_header_aliases(name))
    for name in candidates:
        idx = header_map.get(name, -1)
        if idx >= 0:
            return idx

    lowered_names = {name.lower() for name in names}
    if {"h5_path", "dataset_path"} & lowered_names:
        for idx, label in enumerate(header):
            normalized = label.lower()
            if "h5" in normalized:
                return idx
    return -1


def _semantic_header_aliases(name: str) -> tuple[str, ...]:
    aliases = {
        "function": ("功能",),
        "logical_name": ("逻辑字段",),
        "signal_name": ("真实信号名", "信号名"),
        "h5_path": ("H5数据集路径",),
        "dataset_path": ("H5数据集路径",),
        "label": ("用途",),
        "decode_type": ("解码方式",),
        "role": ("角色",),
        "note": ("备注",),
        "value": ("取值/bit", "取值"),
        "meaning": ("含义",),
    }
    return aliases.get(name.lower(), ())


def _split_aliases(value: str) -> list[str]:
    aliases: list[str] = []
    text = str(value or "").replace("，", ";").replace("、", ";").replace(",", ";").replace("\n", ";")
    for item in text.split(";"):
        item = item.strip()
        if item:
            aliases.append(item)
    return aliases


def _normalize_signal_key(value: str) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())
