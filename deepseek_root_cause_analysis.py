from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import h5py
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from llm_service import call_chat_completion, get_llm_api_unavailable_message, parse_json_object
from performance_analysis import load_performance_problem_presets
from root_cause_analysis import ROOT_CAUSE_SIGNAL_ALIASES


ACC_LCC_FORBIDDEN_INTERFACE_TOKENS = (
    "aeb",
    "daeb",
    "fcacbsc",
    "ifcacbsc",
    "fca",
    "iautobrkngact",
    "autobrkngact",
)

SETSPEED_ACTUAL_ABOVE_MARGIN_KPH = 1.5
SETSPEED_PREFERRED_WINDOW_SECONDS = 6.0
SETSPEED_MIN_ABOVE_SET_DURATION_SECONDS = 0.5
SPEED_BIAS_X = np.asarray([0.0, 40.0, 80.0, 120.0, 200.0, 300.0], dtype=float)
SPEED_BIAS_Y = np.asarray([1.0, 1.075, 1.052, 1.045, 1.039, 1.036], dtype=float)


class DeepSeekAPIUnavailable(RuntimeError):
    """Raised when the configured LLM backend cannot be used for investigation."""


def _is_deepseek_api_unavailable_reply(reply: str) -> bool:
    text = str(reply or "").strip()
    if not text:
        return False
    lower = text.lower()
    if "当前聊天回复模型还没有配置好" in text or "llm_api_key" in lower:
        return True
    if "聊天模型调用失败" not in text:
        return False
    markers = (
        "http 401",
        "http 403",
        "unauthorized",
        "forbidden",
        "invalid",
        "incorrect",
        "authentication",
        "auth",
        "api key",
        "apikey",
        "api_key",
        "bearer",
        "quota",
        "balance",
        "timed out",
        "timeout",
        "connection",
        "connect",
        "urlopen error",
        "dns",
        "name or service not known",
        "temporary failure",
        "proxy",
        "ssl",
    )
    return any(marker in lower for marker in markers) or text.startswith("聊天模型调用失败")


def _raise_if_deepseek_api_unavailable(reply: str) -> None:
    if _is_deepseek_api_unavailable_reply(reply):
        raise DeepSeekAPIUnavailable(get_llm_api_unavailable_message())


def _deepseek_json_call(messages: list[dict[str, str]]) -> str:
    reply = call_chat_completion(
        messages,
        response_format={"type": "json_object"},
        timeout_seconds=_deepseek_timeout_seconds(),
    )
    _raise_if_deepseek_api_unavailable(reply)
    return reply


def _deepseek_timeout_seconds() -> int:
    try:
        from feishu_config import load_env

        load_env()
    except Exception:
        pass
    for key in ("BATCH_ROOT_CAUSE_DEEPSEEK_TIMEOUT_SECONDS", "DEEPSEEK_ROOT_CAUSE_TIMEOUT_SECONDS"):
        value = os.getenv(key, "").strip()
        if not value:
            continue
        try:
            return max(30, int(value))
        except ValueError:
            continue
    value = os.getenv("LLM_TIMEOUT_SECONDS", "").strip()
    try:
        return max(300, int(value))
    except ValueError:
        return 300


def _notify_progress(callback: Callable[[str, str], None] | None, stage: str, detail: str) -> None:
    if callback is None:
        return
    try:
        callback(stage, detail)
    except Exception as exc:
        if exc.__class__.__name__ == "BatchRootCauseCancelled":
            raise
        print(f"deepseek root cause progress callback failed: {exc}", flush=True)


FINAL_SYSTEM_PROMPT = """
你是 ADAS 行车问题原因分析主脑。
本地工具只提供 H5 信号摘要、信号指南和查询计划，你负责根据证据链判断现象、请求、场景诱因、执行器响应之间的关系。
不能声称读取了未提供的文件；不能把没有信号证据的猜测写成根因。
不能自行补充或换算单位；本地证据没有明确单位时，只能写“信号值”或原始数值。
输出必须结论先行：先给出最可能导致问题的具体因素，而不是先铺长证据。
summary 必须是 80 个中文字符以内的一句话，格式尽量为“根因：<具体因素>。<最关键证据>”。
key_signals 只能从本地 compact_evidence 或追加复核证据中挑选，优先选择能支撑根因的现象、请求、诱因、响应信号。
只输出 JSON，不要 Markdown。
""".strip()

WINDOW_SYSTEM_PROMPT = """
你是 ADAS 行车问题候选窗口筛选助手。
本地工具会提供整段 H5 的关键信号摘要、active_segments、采样点和最大变化时间；你负责从这些证据中挑选可能发生问题的候选时间窗口。
只使用 elapsed 秒，不要用 raw_time 拼窗口。
候选窗口要服务于后续根因分析：优先选择现象、请求、诱因、响应之间可能形成证据链的窗口；如果有多个相似问题点，可以都列出。
只输出 JSON，不要 Markdown；不要做最终根因结论。
""".strip()


def _is_setspeed_problem_type(problem_type: str) -> bool:
    text = str(problem_type or "")
    lower = text.lower()
    return "setspeed" in lower or "set speed" in lower or "稳速" in text or "设定" in text


def _model_problem_type_label(problem_type: str, description_clues: dict[str, Any] | None = None) -> str:
    clues = description_clues or {}
    if not _is_setspeed_problem_type(problem_type):
        return str(problem_type or "").strip()
    relation = str(clues.get("reported_speed_relation") or "").strip()
    if relation == "actual_above_set":
        return "稳速偏离 Setspeed（描述方向：实际车速高于设定）"
    if relation == "actual_below_set":
        return "稳速偏离 Setspeed（描述线索：实际车速低于设定/加速不足）"
    if relation == "near_set":
        return "稳速偏离 Setspeed（描述线索：接近设定，需按描述和H5证据判断）"
    return "稳速偏离 Setspeed（描述数值未明确方向，需按描述和H5证据判断）"


def _problem_context_for_llm(
    problem_type: str,
    problem_description: str,
    description_clues: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clues = description_clues or {}
    relation = str(clues.get("reported_speed_relation") or "").strip()
    context: dict[str, Any] = {
        "analysis_type": _model_problem_type_label(problem_type, clues),
        "table_problem_type": problem_type,
        "description": problem_description,
    }
    if relation:
        context["description_speed_relation"] = relation
        context["direction_source"] = "problem_description_speed_values"
    if clues.get("problem_type_name_warning"):
        context["description_direction_note"] = clues.get("problem_type_name_warning")
    elif _is_setspeed_problem_type(problem_type):
        context["description_direction_note"] = "表格问题类型主要用于选择预设信号；现象方向建议结合问题描述和H5证据共同判断。"
    return _drop_empty_json(context)


def _problem_description_clues(problem_type: str, problem_description: str) -> dict[str, Any]:
    text = str(problem_description or "").strip()
    clues: dict[str, Any] = {
        "usage_policy": (
            "问题描述只作为软约束和查询线索：车速数值可能不准，定速不代表速度必须不变，"
            "距离、速度等数值也可能是人工估计或记录误差；不能仅因数值不完全一致排除候选窗口，"
            "也不能忽略明显相反的现象方向。应先依据现象和信号时序判断，再把数值差异记录为不一致线索。"
        ),
        "target_vehicle_constraint_policy": (
            "目标车存在性、目标车距离/速度等描述是宽松约束；允许人工估计误差，"
            "只要目标车时序和问题现象能对上，就保留候选窗口并记录数值差异。"
        ),
        "ego_vehicle_constraint_policy": (
            "自车状态是严格约束；自车实际车速、设定车速关系、ACC/LCC状态、挡位、"
            "踏板/接管状态以及请求/响应时序必须由H5证据确认。自车车速绝对值允许约±3 km/h"
            "的小幅误差，但实际车速与设定车速的高低关系、变化趋势和关键状态不能放宽。"
            "若自车关键状态不匹配，不得仅凭目标车证据给出高置信主结论。"
        ),
    }
    if problem_type:
        clues["problem_type"] = problem_type
    if not text:
        return clues

    set_speed = _first_number_after(
        text,
        (
            r"(?:定速|设定车速|设定速度|setspeed|set\s*speed)[^\d\-+]{0,12}(-?\d+(?:\.\d+)?)",
        ),
    )
    actual_speed = _first_number_after(
        text,
        (
            r"(?:实际最高车速|实际最高|最大车速|实际车速|当前车速)[^\d\-+]{0,12}(-?\d+(?:\.\d+)?)",
            r"(?:车速)[^\d\-+]{0,8}(-?\d+(?:\.\d+)?)\s*(?:kph|km/h|kmh)",
        ),
    )
    if set_speed is not None:
        clues["reported_set_speed_kph_approx"] = _round_float(set_speed, 2)
    if actual_speed is not None:
        clues["reported_actual_speed_kph_approx"] = _round_float(actual_speed, 2)
    if set_speed is not None and actual_speed is not None:
        delta = actual_speed - set_speed
        margin = _setspeed_actual_above_margin_kph()
        if delta >= margin:
            relation = "actual_above_set"
            relation_note = "描述更像实际车速高于设定车速或控速偏高。"
        elif delta <= -margin:
            relation = "actual_below_set"
            relation_note = "描述更像实际车速低于设定车速或动力/加速不足。"
        else:
            relation = "near_set"
            relation_note = "描述车速接近设定值，需结合趋势、坡道、目标车和请求响应判断。"
        clues["reported_speed_delta_kph_approx"] = _round_float(delta, 2)
        clues["reported_speed_relation"] = relation
        clues["reported_speed_relation_note"] = relation_note
        clues["reported_speed_relation_rule"] = f"actual_speed - set_speed >= {margin:g}kph 判定为高于设定；<= -{margin:g}kph 判定为低于设定。"
        if _is_setspeed_problem_type(problem_type) and relation == "actual_above_set" and "未达" in str(problem_type or ""):
            clues["problem_type_name_warning"] = (
                "表格问题类型名包含“未达”，但问题描述数值显示实际车速高于设定；"
                "请优先复核“实际高于设定/控速偏高”这一描述线索是否被H5证据支持，避免仅被类型名带向低于设定或动力不足方向。"
            )

    scene_keywords: list[str] = []
    for keyword in (
        "上坡",
        "下坡",
        "左弯",
        "右弯",
        "弯道",
        "曲率",
        "左一",
        "右一",
        "匝道",
        "护栏",
        "左侧有车",
        "右侧有车",
        "前车",
        "切入",
        "cut-in",
        "cutin",
    ):
        if keyword.lower() in text.lower():
            scene_keywords.append(keyword)
    if scene_keywords:
        clues["scene_keywords"] = scene_keywords

    control_terms: list[str] = []
    for keyword in (
        "override",
        "接管",
        "踩踏板",
        "踏板",
        "踩油门",
        "油门",
        "踩刹车",
        "踩制动",
        "制动踏板",
        "刹车踏板",
        "功能退出",
        "退出",
        "退控",
        "退出原因",
        "acc退出",
        "lcc退出",
    ):
        if keyword.lower() in text.lower():
            control_terms.append(keyword)
    if control_terms:
        clues["explicit_control_override_or_exit_terms"] = list(dict.fromkeys(control_terms))
        clues["mentions_control_override_or_exit"] = True
    else:
        clues["mentions_control_override_or_exit"] = False

    if (
        _is_setspeed_problem_type(problem_type)
        and clues.get("reported_speed_relation") == "actual_above_set"
        and not clues.get("mentions_control_override_or_exit")
    ):
        clues["preferred_window_policy"] = (
            "建议关注实际车速高于SetSpeed、ACC功能active且未override/未踩踏板/未功能退出的时段；"
            "override/踩踏板/功能退出若发生在之后，更适合作为伴随或后续线索，由模型结合时序证据判断其权重。"
        )

    lead_distance = _first_number_after(
        text,
        (
            r"距前车[^\d]{0,8}(\d+(?:\.\d+)?)\s*(?:m|米)",
            r"(?:正前方|前方|前车)[^\d]{0,12}(\d+(?:\.\d+)?)\s*(?:m|米)",
        ),
    )
    if lead_distance is not None:
        clues["reported_lead_distance_m_approx"] = _round_float(lead_distance, 1)
        clues["reported_lead_distance_policy"] = (
            "前车距离仅作为软约束；优先依据前车目标是否存在、目标时序及请求/响应证据选窗口。"
            "H5距离与描述数值不一致时，保留窗口并在 description_alignment 中说明差异。"
        )
    return clues


def _domain_signal_guidance(
    problem_type: str,
    description_clues: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Explain signal semantics to the LLM without deciding the root cause locally."""
    guidance: list[dict[str, Any]] = [
        {
            "signal_family": "VehSpdAvg / IVehSpdAvgDrvn",
            "meaning": "车辆实际速度，用来判断现象方向和速度响应。",
            "possible_implication": "需要和设定车速、限速目标、扭矩请求一起看；速度高/低本身只是现象，不是根因。",
        },
        {
            "signal_family": "IACCDrvrSeltdSpd / SetSpeed",
            "meaning": "驾驶员设定车速或 ACC 目标车速。",
            "possible_implication": "实际车速持续偏离设定值时，需判断是目标变化、算法请求、执行器响应还是功能状态变化导致。",
        },
        {
            "signal_family": "CurvatureLimitSpeed / Debug_uint_Reserved_48 + CurvatureLimitEnable / Debug_uint_Reserved_49",
            "meaning": "曲率/弯道限速目标及其使能状态，表示系统认为当前道路几何可能需要降低目标速度。",
            "possible_implication": "限速低于设定或实际车速时，通常解释主动降速、低速或加速受限；不能单独解释实际车速高于设定，需要结合制动请求和响应判断。",
        },
        {
            "signal_family": "IACCBSCE_ACCTrq1 / IACCATC_AxlTrqRq / AutBrkTp",
            "meaning": "ACC/LCC 算法输出的轴扭矩、制动扭矩或自动制动类型请求。",
            "possible_implication": "若需要减速但请求未发、幅值偏小或持续不足，可能指向算法请求侧；若请求足够但速度未响应，再看执行器响应。",
        },
        {
            "signal_family": "IACCBrkngAct / IBrkSysAutBrkStat / ICSTBATS_TrqVl / IRegenBrkAxTrqAch",
            "meaning": "制动激活、制动系统状态、实际扭矩或能量回收扭矩响应。",
            "possible_implication": "用于判断执行器是否跟随算法请求；请求和响应不一致时可能是执行器响应不足，请求本身不足时不能归因到执行器。",
        },
        {
            "signal_family": "IAccPdlOvrrdAtv / brake override / ACC-LCC state",
            "meaning": "驾驶员踏板覆盖、接管、ACC/LCC 状态和退出状态。",
            "possible_implication": "只有当覆盖/接管/状态变化先于问题且能解释现象时，才适合作为主因；滞后变化更可能是结果或伴随状态。",
        },
    ]
    clues = description_clues or {}
    if (
        _is_setspeed_problem_type(problem_type)
        and str(clues.get("reported_speed_relation") or "") == "actual_above_set"
    ):
        guidance.append(
            {
                "scenario": "稳速偏离 Setspeed / actual_above_set",
                "meaning": "问题描述更像实际车速高于设定车速或控速偏高。",
                "possible_implication": (
                    "曲率限速可作为应当减速的场景证据或排除项，但最终根因应由 AI 结合"
                    "制动扭矩请求、自动制动类型、制动响应、踏板覆盖和 ACC 状态的时序判断。"
                    "若描述没有提到接管/踏板/退出，优先解释接管前、ACC active且未override时为什么车速仍高于设定。"
                ),
            }
        )
    return guidance


def _first_number_after(text: str, patterns: tuple[str, ...]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            return float(match.group(1))
        except (TypeError, ValueError):
            continue
    return None


@dataclass(frozen=True)
class SignalRef:
    dataset_path: str
    field_name: str
    timestamp_path: str = ""

    @property
    def display_name(self) -> str:
        if self.timestamp_path:
            parts = [part for part in self.dataset_path.strip("/").split("/") if part]
            message_name = parts[-3] if len(parts) >= 3 and parts[-2] == "signals" else ""
            if message_name:
                return f"{message_name}.{self.field_name}"
        dataset_name = self.dataset_path.rsplit("/", 1)[-1]
        return f"{dataset_name}.{self.field_name}"


@dataclass
class SignalSummary:
    requested_name: str
    group: str
    reason: str = ""
    resolved_name: str = ""
    dataset_path: str = ""
    field_name: str = ""
    status: str = "missing"
    count: int = 0
    time_start: float | None = None
    time_end: float | None = None
    raw_time_start: float | None = None
    raw_time_end: float | None = None
    start_value: float | None = None
    end_value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    mean_value: float | None = None
    max_delta: float | None = None
    max_delta_time: float | None = None
    raw_max_delta_time: float | None = None
    max_delta_from: float | None = None
    max_delta_to: float | None = None
    max_window_delta: float | None = None
    max_window_delta_time: float | None = None
    raw_max_window_delta_time: float | None = None
    max_window_delta_from: float | None = None
    max_window_delta_to: float | None = None
    changed: bool = False
    active_segments: list[dict[str, float]] = field(default_factory=list)
    samples: list[dict[str, float]] = field(default_factory=list)
    error: str = ""
    timestamp_path: str = ""
    window_start: float | None = None
    window_end: float | None = None
    window_reason: str = ""

    def to_prompt_dict(self, *, compact: bool = False) -> dict[str, Any]:
        if compact:
            data: dict[str, Any] = {
                "requested_name": self.requested_name,
                "group": self.group,
                "reason": _compact_text(self.reason, 100),
                "resolved_name": self.resolved_name,
                "field_name": self.field_name,
                "status": self.status,
            }
            if self.status != "ok":
                data["error"] = _compact_text(self.error, 160)
                return {key: value for key, value in data.items() if value not in ("", None, [], {})}

            data.update(
                {
                    "count": self.count,
                    "time_range": [_round_float(self.time_start), _round_float(self.time_end)],
                    "value": {
                        "start": _round_float(self.start_value),
                        "end": _round_float(self.end_value),
                        "min": _round_float(self.min_value),
                        "max": _round_float(self.max_value),
                        "mean": _round_float(self.mean_value),
                    },
                    "max_step": {
                        "delta": _round_float(self.max_delta),
                        "time": _round_float(self.max_delta_time),
                        "from": _round_float(self.max_delta_from),
                        "to": _round_float(self.max_delta_to),
                    },
                    "max_1s": {
                        "delta": _round_float(self.max_window_delta),
                        "time": _round_float(self.max_window_delta_time),
                        "from": _round_float(self.max_window_delta_from),
                        "to": _round_float(self.max_window_delta_to),
                    },
                    "changed": self.changed,
                }
            )
            if self.active_segments:
                data["active_segments"] = self.active_segments[:3]
            if self.samples:
                data["samples"] = self.samples[:5]
            if self.window_start is not None or self.window_end is not None:
                data["window"] = {
                    "start": _round_float(self.window_start),
                    "end": _round_float(self.window_end),
                    "reason": _compact_text(self.window_reason, 100),
                }
            return {key: value for key, value in data.items() if value not in ("", None, [], {})}

        return {
            "requested_name": self.requested_name,
            "group": self.group,
            "reason": self.reason,
            "resolved_name": self.resolved_name,
            "dataset_path": self.dataset_path,
            "field_name": self.field_name,
            "status": self.status,
            "count": self.count,
            "time_start": _round_float(self.time_start),
            "time_end": _round_float(self.time_end),
            "raw_time_start": _round_float(self.raw_time_start),
            "raw_time_end": _round_float(self.raw_time_end),
            "start_value": _round_float(self.start_value),
            "end_value": _round_float(self.end_value),
            "min_value": _round_float(self.min_value),
            "max_value": _round_float(self.max_value),
            "mean_value": _round_float(self.mean_value),
            "max_delta": _round_float(self.max_delta),
            "max_delta_time": _round_float(self.max_delta_time),
            "raw_max_delta_time": _round_float(self.raw_max_delta_time),
            "max_delta_from": _round_float(self.max_delta_from),
            "max_delta_to": _round_float(self.max_delta_to),
            "max_window_delta": _round_float(self.max_window_delta),
            "max_window_delta_time": _round_float(self.max_window_delta_time),
            "raw_max_window_delta_time": _round_float(self.raw_max_window_delta_time),
            "max_window_delta_from": _round_float(self.max_window_delta_from),
            "max_window_delta_to": _round_float(self.max_window_delta_to),
            "changed": self.changed,
            "active_segments": self.active_segments[:4],
            "samples": self.samples,
            "error": self.error,
            "window_start": _round_float(self.window_start),
            "window_end": _round_float(self.window_end),
            "window_reason": self.window_reason,
        }


@dataclass
class DeepSeekRootCauseInvestigation:
    h5_file: Path
    problem_type: str
    problem_description: str
    plan: dict[str, Any]
    evidence: list[SignalSummary]
    final_json: dict[str, Any]
    conclusion: str
    summary: str
    root_cause_factor: str
    key_signals_text: str
    image_paths: list[Path]
    status: str
    context: str


class H5SignalIndex:
    def __init__(self, h5_file: str | Path):
        self.h5_file = Path(h5_file)
        self.refs: list[SignalRef] = []
        self._normalized: dict[str, list[SignalRef]] = {}
        self._scan()

    def _add_ref(self, ref: SignalRef, *aliases: str) -> None:
        self.refs.append(ref)
        for alias in _name_variants(*aliases):
            self._normalized.setdefault(alias, []).append(ref)

    def _scan(self) -> None:
        with h5py.File(self.h5_file, "r") as h5:
            def visit(name: str, obj: Any) -> None:
                if isinstance(obj, h5py.Group):
                    try:
                        timestamp_dataset = obj.get("timestamp")
                        signals_group = obj.get("signals")
                    except Exception:
                        return
                    if isinstance(timestamp_dataset, h5py.Dataset) and isinstance(signals_group, h5py.Group):
                        message_path = obj.name.strip("/")
                        message_name = message_path.rsplit("/", 1)[-1]
                        try:
                            signal_items = list(signals_group.items())
                        except Exception:
                            return
                        for signal_name, dataset in signal_items:
                            if not isinstance(dataset, h5py.Dataset):
                                continue
                            ref = SignalRef(
                                dataset.name.strip("/"),
                                str(signal_name),
                                timestamp_dataset.name.strip("/"),
                            )
                            message_aliases = _message_aliases(message_name)
                            self._add_ref(
                                ref,
                                ref.display_name,
                                ref.field_name,
                                dataset.name,
                                f"{message_path}/signals/{signal_name}",
                                *(
                                    item
                                    for message_alias in message_aliases
                                    for item in (
                                        f"{message_alias}/{signal_name}",
                                        f"{message_alias}.{signal_name}",
                                    )
                                ),
                            )
                    return
                if not isinstance(obj, h5py.Dataset):
                    return
                try:
                    names = obj.dtype.names or ()
                except Exception:
                    return
                for field_name in names:
                    if field_name == "timestamp":
                        continue
                    ref = SignalRef(name, field_name)
                    self._add_ref(ref, ref.display_name, ref.field_name, name)

            try:
                h5.visititems(visit)
            except Exception:
                # Some parsed H5 files contain damaged objects while useful CAN groups remain readable.
                # Fall back to guarded traversal so one bad node does not fail the whole analysis.
                partial_refs = list(self.refs)
                partial_normalized = {key: list(value) for key, value in self._normalized.items()}
                self.refs.clear()
                self._normalized.clear()
                self._scan_group_safely(h5, visit)
                if not self.refs and partial_refs:
                    self.refs = partial_refs
                    self._normalized = partial_normalized

    def _scan_group_safely(self, group: h5py.Group, visitor: Callable[[str, Any], None]) -> None:
        try:
            items = list(group.items())
        except Exception:
            return
        for name, obj in items:
            try:
                visitor(obj.name.strip("/") or str(name), obj)
            except Exception:
                continue
            if isinstance(obj, h5py.Group):
                self._scan_group_safely(obj, visitor)

    def available_short_names(self, limit: int = 240) -> list[str]:
        names = sorted({ref.field_name for ref in self.refs})
        return names[:limit]

    def resolve(self, requested_name: str) -> SignalRef | None:
        candidates: list[str] = []
        for name in _expanded_signal_names(requested_name):
            candidates.extend(_name_variants(name))

        seen: set[tuple[str, str]] = set()
        for key in candidates:
            matches: list[SignalRef] = []
            for ref in self._normalized.get(key, []):
                identity = (ref.dataset_path, ref.field_name)
                if identity not in seen:
                    seen.add(identity)
                    matches.append(ref)
            if matches:
                return _best_match(requested_name, matches)

        requested_key = _normalize_name(requested_name)
        fuzzy = [
            ref
            for ref in self.refs
            if requested_key
            and (
                requested_key in _normalize_name(ref.field_name)
                or requested_key in _normalize_name(ref.display_name)
                or _strip_leading_i(requested_key) == _strip_leading_i(_normalize_name(ref.field_name))
            )
        ]
        if fuzzy:
            return _best_match(requested_name, fuzzy)
        return None


def run_deepseek_root_cause_investigation(
    *,
    h5_path: str | Path,
    problem_type: str,
    problem_description: str = "",
    problem_time: str = "",
    problem_date: str = "",
    max_signals: int = 32,
    image_root: str | Path | None = None,
    progress_callback: Callable[[str, str], None] | None = None,
) -> DeepSeekRootCauseInvestigation:
    h5_file = Path(h5_path).resolve()
    if not h5_file.is_file():
        raise FileNotFoundError(f"H5 file not found: {h5_file}")

    _notify_progress(progress_callback, "问题分析中", "模型调查 1/7：建立 H5 信号索引。")
    index = H5SignalIndex(h5_file)
    preset_context = _preset_context(problem_type)
    description_clues = _problem_description_clues(problem_type, problem_description)
    _notify_progress(progress_callback, "问题分析中", "本地 2/7：根据问题类型和描述生成信号查询计划。")
    plan = _build_deepseek_query_plan(
        problem_type=problem_type,
        problem_description=problem_description,
        description_clues=description_clues,
        available_signals=index.available_short_names(),
        preset_context=preset_context,
        max_signals=max_signals,
    )
    queries = _queries_from_plan(plan, preset_context, max_signals=max_signals)
    disallowed_resolved_tokens = _disallowed_interface_tokens(problem_type)
    evidence: list[SignalSummary] = []
    query_total = len(queries)
    _notify_progress(
        progress_callback,
        "问题分析中",
        f"模型调查 3/7：提取 {query_total} 个候选信号摘要。",
    )
    for query_index, query in enumerate(queries, start=1):
        if query_index == 1 or query_index == query_total or query_index % 5 == 0:
            _notify_progress(
                progress_callback,
                "问题分析中",
                f"模型调查 3/7：信号摘要 {query_index}/{query_total}：{query.get('signal', '')}",
            )
        evidence.append(
            summarize_signal(
                h5_file=h5_file,
                index=index,
                requested_name=query["signal"],
                group=query.get("group", ""),
                reason=query.get("reason", ""),
                disallowed_resolved_tokens=disallowed_resolved_tokens,
            )
        )
    problem_time_hint = _build_problem_time_hint(h5_file, problem_time=problem_time, problem_date=problem_date)
    if problem_time_hint.get("enabled"):
        _notify_progress(
            progress_callback,
            "问题分析中",
            f"模型调查 4/7：表格问题时间定位到 H5 内 {problem_time_hint.get('target_elapsed_seconds')}s，筛选候选问题窗口。",
        )
    else:
        _notify_progress(progress_callback, "问题分析中", "模型调查 4/7：根据全局摘要筛选候选问题窗口。")
    candidate_windows = _build_deepseek_candidate_windows(
        h5_file=h5_file,
        index=index,
        problem_type=problem_type,
        problem_description=problem_description,
        description_clues=description_clues,
        preset_context=preset_context,
        plan=plan,
        evidence=evidence,
        problem_time_hint=problem_time_hint,
    )
    _notify_progress(
        progress_callback,
        "问题分析中",
        f"模型调查 5/7：按 {len(candidate_windows)} 个候选窗口提取局部证据。",
    )
    window_evidence = _build_window_evidence(
        h5_file=h5_file,
        index=index,
        queries=queries,
        evidence=evidence,
        candidate_windows=candidate_windows,
        disallowed_resolved_tokens=disallowed_resolved_tokens,
    )
    compact_evidence = _build_compact_evidence(
        problem_type=problem_type,
        problem_description=problem_description,
        description_clues=description_clues,
        preset_context=preset_context,
        plan=plan,
        evidence=evidence,
        problem_time_hint=problem_time_hint,
        candidate_windows=candidate_windows,
        window_evidence=window_evidence,
    )
    _notify_progress(progress_callback, "问题分析中", "模型调查 6/7：基于压缩证据生成根因结论。")
    final_json = _build_deepseek_final_analysis(
        problem_type=problem_type,
        problem_description=problem_description,
        description_clues=description_clues,
        preset_context=preset_context,
        plan=plan,
        evidence=evidence,
        problem_time_hint=problem_time_hint,
        candidate_windows=candidate_windows,
        window_evidence=window_evidence,
        compact_evidence=compact_evidence,
    )
    summary = _compact_root_cause_summary(final_json)
    root_cause_factor = _compact_text(str(final_json.get("root_cause_factor") or ""), 80)
    key_signals_text = _format_key_signal_text(final_json, evidence)
    _notify_progress(progress_callback, "问题分析中", "模型调查 7/7：生成候选窗口关键信号图示。")
    image_paths = build_deepseek_signal_plots(
        h5_file=h5_file,
        evidence=evidence,
        final_json=final_json,
        image_root=image_root,
    )
    conclusion = _format_investigation_markdown(
        h5_file=h5_file,
        problem_type=problem_type,
        problem_description=problem_description,
        final_json=final_json,
        evidence=evidence,
        key_signals_text=key_signals_text,
        image_paths=image_paths,
    )
    status = _status_from_final(final_json, evidence)
    return DeepSeekRootCauseInvestigation(
        h5_file=h5_file,
        problem_type=problem_type,
        problem_description=problem_description,
        plan=plan,
        evidence=evidence,
        final_json=final_json,
        conclusion=conclusion,
        summary=summary,
        root_cause_factor=root_cause_factor,
        key_signals_text=key_signals_text,
        image_paths=image_paths,
        status=status,
        context=_build_context(
            plan,
            evidence,
            final_json,
            problem_time_hint=problem_time_hint,
            description_clues=description_clues,
            candidate_windows=candidate_windows,
            window_evidence=window_evidence,
            compact_evidence=compact_evidence,
        ),
    )


def summarize_signal(
    *,
    h5_file: Path,
    index: H5SignalIndex,
    requested_name: str,
    group: str,
    reason: str = "",
    disallowed_resolved_tokens: tuple[str, ...] = (),
    window_start: float | None = None,
    window_end: float | None = None,
    window_reason: str = "",
) -> SignalSummary:
    summary = SignalSummary(requested_name=requested_name, group=group, reason=reason)
    summary.window_start = window_start
    summary.window_end = window_end
    summary.window_reason = window_reason
    ref = index.resolve(requested_name)
    if ref is None:
        summary.error = "signal not found"
        return summary
    if disallowed_resolved_tokens and _contains_forbidden_interface(ref, disallowed_resolved_tokens):
        summary.error = "resolved to forbidden non-problem interface"
        return summary

    summary.resolved_name = ref.display_name
    summary.dataset_path = ref.dataset_path
    summary.field_name = ref.field_name
    summary.timestamp_path = ref.timestamp_path
    try:
        timestamps, values = _read_signal_values(h5_file, ref)
    except Exception as exc:
        summary.error = str(exc)
        return summary

    valid = np.isfinite(timestamps) & np.isfinite(values)
    timestamps = timestamps[valid]
    values = values[valid]
    if len(values) == 0:
        summary.error = "no numeric values"
        return summary

    raw_timestamps = np.asarray(timestamps, dtype=float)
    timestamps = _elapsed_timestamps(raw_timestamps)
    if window_start is not None and window_end is not None:
        start_value = float(window_start)
        end_value = float(window_end)
        if end_value <= start_value:
            end_value = start_value + 1.0
        window_mask = (timestamps >= start_value) & (timestamps <= end_value)
        timestamps = timestamps[window_mask]
        raw_timestamps = raw_timestamps[window_mask]
        values = values[window_mask]
        if len(values) == 0:
            summary.error = f"no numeric values in candidate window {start_value:.3f}-{end_value:.3f}s"
            return summary
    summary.status = "ok"
    summary.count = int(len(values))
    summary.time_start = float(timestamps[0])
    summary.time_end = float(timestamps[-1])
    summary.raw_time_start = float(raw_timestamps[0])
    summary.raw_time_end = float(raw_timestamps[-1])
    summary.start_value = float(values[0])
    summary.end_value = float(values[-1])
    summary.min_value = float(np.nanmin(values))
    summary.max_value = float(np.nanmax(values))
    summary.mean_value = float(np.nanmean(values))

    if len(values) >= 2:
        deltas = np.abs(np.diff(values))
        if len(deltas):
            max_index = int(np.nanargmax(deltas))
            summary.max_delta = float(deltas[max_index])
            summary.max_delta_time = float(timestamps[max_index + 1])
            summary.raw_max_delta_time = float(raw_timestamps[max_index + 1])
            summary.max_delta_from = float(values[max_index])
            summary.max_delta_to = float(values[max_index + 1])
            span = float(summary.max_value - summary.min_value)
            threshold = max(1e-6, span * 0.05)
            (
                summary.max_window_delta,
                summary.max_window_delta_time,
                summary.max_window_delta_from,
                summary.max_window_delta_to,
            ) = _max_window_delta(timestamps, values, window_s=1.0)
            if summary.max_window_delta_time is not None:
                summary.raw_max_window_delta_time = float(summary.max_window_delta_time + raw_timestamps[0])
            summary.changed = bool(
                (summary.max_delta and summary.max_delta >= threshold)
                or (summary.max_window_delta and summary.max_window_delta >= threshold)
            )

    summary.active_segments = _active_segments(timestamps, values)
    summary.samples = _sample_points(timestamps, values)
    return summary


def _build_deepseek_query_plan(
    *,
    problem_type: str,
    problem_description: str,
    description_clues: dict[str, Any] | None = None,
    available_signals: list[str],
    preset_context: dict[str, Any],
    max_signals: int,
) -> dict[str, Any]:
    return _build_local_query_plan(
        problem_type=problem_type,
        problem_description=problem_description,
        description_clues=description_clues,
        preset_context=preset_context,
        max_signals=max_signals,
    )


def _build_local_query_plan(
    *,
    problem_type: str,
    problem_description: str,
    description_clues: dict[str, Any] | None,
    preset_context: dict[str, Any],
    max_signals: int,
) -> dict[str, Any]:
    clues = description_clues or {}
    group_defs = (
        ("phenomenon", "vehicle_signals", "确认用户描述的问题现象是否成立"),
        ("algorithm_request", "algorithm_request_signals", "确认算法请求是否主动变化"),
        ("algorithm_cause", "algorithm_cause_signals", "确认场景输入或功能条件是否能解释算法请求"),
        ("actuator_response", "actuator_response_signals", "确认执行器响应是否跟随请求"),
    )
    queries: list[dict[str, Any]] = []
    remaining_budget = max(1, int(max_signals or 32))
    for group, field_name, why in group_defs:
        signals = [str(item) for item in preset_context.get(field_name) or [] if str(item).strip()]
        ranked = _rank_local_query_signals(signals, group=group, description_clues=clues)
        if not ranked:
            continue
        queries.append({"group": group, "signals": ranked[:remaining_budget], "why": why})
        remaining_budget -= len(ranked[:remaining_budget])
        if remaining_budget <= 0:
            break

    focus = [
        "本地按问题类型预设生成查询计划，不再消耗模型 token 做信号规划。",
        "问题描述只作为信号优先级软约束，最终根因仍必须由 H5 证据支撑。",
    ]
    relation_note = str(clues.get("reported_speed_relation_note") or "").strip()
    if relation_note:
        focus.append(relation_note)
    direction_guard = str(clues.get("problem_type_name_warning") or "").strip()
    if direction_guard:
        focus.append(direction_guard)
    scene_keywords = clues.get("scene_keywords")
    if isinstance(scene_keywords, list) and scene_keywords:
        focus.append("描述场景线索：" + "、".join(str(item) for item in scene_keywords[:6]))

    return {
        "analysis_focus": focus,
        "problem_type": _model_problem_type_label(problem_type, clues),
        "table_problem_type": problem_type,
        "problem_description": problem_description,
        "problem_description_clues": clues,
        "query_source": "local_preset",
        "queries": queries,
        "missing_if_absent": [
            "现象层信号缺失时，不能确认用户描述的现象是否真实发生。",
            "请求层信号缺失时，不能判断是否算法主动触发。",
            "响应层信号缺失时，不能判断执行器是否跟随请求。",
        ],
    }


def _build_deepseek_final_analysis(
    *,
    problem_type: str,
    problem_description: str,
    description_clues: dict[str, Any] | None = None,
    preset_context: dict[str, Any],
    plan: dict[str, Any],
    evidence: list[SignalSummary],
    problem_time_hint: dict[str, Any] | None = None,
    candidate_windows: list[dict[str, Any]] | None = None,
    window_evidence: list[dict[str, Any]] | None = None,
    compact_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    compact_payload = compact_evidence or _build_compact_evidence(
        problem_type=problem_type,
        problem_description=problem_description,
        description_clues=description_clues,
        preset_context=preset_context,
        plan=plan,
        evidence=evidence,
        problem_time_hint=problem_time_hint,
        candidate_windows=candidate_windows,
        window_evidence=window_evidence,
    )
    user_payload = {
        "task": "根据 compact_evidence 完成 H5 行车问题根因分析",
        "compact_evidence": compact_payload,
        "signal_time_note": (
            "compact_evidence 中 time_range/max_*_time/window 均为每个信号从首采样开始的 elapsed 秒；"
            "raw_time_* 保留原始 H5 时间戳。跨信号判断事件窗口优先使用 elapsed 秒。"
        ),
        "signal_scope_note": (
            "本任务只允许使用 compact_evidence 中已经提取出的 ACC/LCC 相关信号。"
            "禁止调用或引用 AEB/FCA/DAeb/*_AEBUsd 接口；这些信号即使 H5 中存在，也不能作为 ACC/LCC 根因证据。"
            "用户可见结论中也不要出现这些接口名，如需说明统一写“非本问题接口”。"
        ),
        "requirements": _final_analysis_requirements(compact_mode=True),
        "output_schema": _final_analysis_output_schema(),
    }

    parsed, raw_replies = _call_final_analysis_json(user_payload)
    parsed = _relax_soft_target_alignment(parsed, compact_payload)
    if _should_retry_final_with_expanded_evidence(parsed, compact_payload):
        retry_payload = dict(user_payload)
        retry_payload["task"] = "compact_evidence 结论证据不足或描述匹配度低，追加窗口扩展证据复核"
        retry_payload["expanded_retry_evidence"] = _build_expanded_retry_evidence(
            evidence=evidence,
            window_evidence=window_evidence,
        )
        retry_payload["requirements"] = [
            *_final_analysis_requirements(compact_mode=False),
            "本轮 expanded_retry_evidence 只用于复核 compact_evidence 没讲清的窗口细节；仍不得引用未提供的信号或做无证据假设。",
        ]
        retry_parsed, retry_raw_replies = _call_final_analysis_json(retry_payload)
        raw_replies.extend(retry_raw_replies)
        retry_parsed = _relax_soft_target_alignment(retry_parsed, compact_payload)
        if _has_usable_final_analysis(retry_parsed):
            parsed = _normalize_final_analysis_payload(retry_parsed)
            parsed["expanded_retry_used"] = True

    parsed = _normalize_final_analysis_payload(parsed)
    if not _has_usable_final_analysis(parsed):
        parsed = _fallback_final(evidence, "\n\n".join(raw_replies))
    if candidate_windows and not parsed.get("candidate_windows"):
        parsed["candidate_windows"] = candidate_windows
    if candidate_windows and not isinstance(parsed.get("event_window"), dict):
        first_window = candidate_windows[0]
        parsed["event_window"] = {
            "start": first_window.get("start"),
            "end": first_window.get("end"),
            "reason": first_window.get("reason", "候选窗口第一优先级"),
        }
    if problem_time_hint and problem_time_hint.get("enabled"):
        parsed["problem_time_hint"] = problem_time_hint
    return _sanitize_public_final_json(parsed)


def _call_final_analysis_json(user_payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    messages = [
        {"role": "system", "content": FINAL_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    raw_replies: list[str] = []
    parsed: dict[str, Any] = {}
    for _ in range(2):
        try:
            raw = _deepseek_json_call(messages)
        except DeepSeekAPIUnavailable:
            raise
        except Exception as exc:
            raw_replies.append(f"LLM final analysis call failed: {exc}")
            break
        raw_replies.append(raw)
        maybe_parsed = parse_json_object(raw)
        parsed = _normalize_final_analysis_payload(maybe_parsed)
        if _has_usable_final_analysis(parsed):
            break
        messages = [
            *messages,
            {"role": "assistant", "content": raw[:1200]},
            {
                "role": "user",
                "content": (
                    "上一轮没有输出可用的最终结论 JSON。请只输出一个简短 JSON object，不要 Markdown，不要解释。"
                    "至少包含 headline、root_cause_factor、cause_side、confidence、summary、description_alignment、"
                    "event_window、key_signals、evidence_chain；每个数组最多 6 项。"
                ),
            },
        ]
    return parsed, raw_replies


def _has_usable_final_analysis(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return any(
        str(value.get(key) or "").strip()
        for key in ("headline", "root_cause_factor", "summary", "conclusion", "final_conclusion")
    )


def _normalize_final_analysis_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = dict(value)
    if not str(result.get("summary") or "").strip():
        result["summary"] = str(result.get("conclusion") or result.get("final_conclusion") or "").strip()
    if not str(result.get("root_cause_factor") or "").strip():
        result["root_cause_factor"] = str(result.get("root_cause") or result.get("primary_cause") or "").strip()
    if not str(result.get("headline") or "").strip():
        result["headline"] = str(result.get("root_cause_factor") or result.get("summary") or "").strip()
    return result


def _final_analysis_requirements(*, compact_mode: bool) -> list[str]:
    evidence_name = "compact_evidence" if compact_mode else "compact_evidence/expanded_retry_evidence"
    return [
        "先判断现象是否有信号证据支撑。",
        "再判断算法请求是否先变化或主动触发。",
        "再判断场景诱因能否解释该请求。",
        "再判断执行器响应是否跟随请求。",
        "compact_evidence.problem.table_problem_type 只是表格分类和信号预设来源，不能覆盖问题描述解析出的现象方向；若 analysis_type 或 problem_description_clues 指明实际车速高于设定，就必须按控速偏高/超设速方向分析。",
        "必须把 problem.description 和 problem_description_clues 当作软约束：车速数值可能不准，定速不代表速度不变，但主结论必须能解释用户描述的现象方向和场景线索。",
        "自车车速绝对值与描述或信号存在约±3 km/h差异时，可视为小幅测量/描述误差；但自车实际车速相对SetSpeed的高低关系、变化趋势和控制时序不一致时，必须记录为冲突。",
        "目标车距离是软线索，没有用于候选筛选的最大距离阈值；即使H5目标距离明显大于描述值或大于50m，也不能据此排除窗口、降低为低匹配、输出“模型结论未生成”或拒绝生成结论。应按H5实际目标距离、目标速度和请求/响应时序继续判断，并把距离差异写入 description_alignment.mismatch_or_uncertain_clues。",
        "对于误制动/重制动/减速类问题，目标距离不是唯一诱因；必须重点检查制动请求前后目标车纵向速度/相对速度是否过低、是否能解释ACC减速请求，并把目标速度与请求/响应时序作为候选根因证据。",
        f"signal_guidance 只解释信号含义和可能导致的问题，不直接指定最终根因；最终根因必须由你基于 {evidence_name} 判断。",
        "对于 problem_description_clues.reported_speed_relation=actual_above_set 的情况，曲率限速/弯道限速通常表示控制系统存在降低目标速度或请求减速的理由；它更常用于解释低速或主动减速，不应孤立地解释车速高于设定。请结合制动扭矩请求、自动制动类型、制动响应、踏板覆盖和 ACC 状态判断实际原因。",
        "对于实际车速高于SetSpeed的问题，root_cause_factor 不应写成“曲率限速触发”或“弯道限速触发”本身；如果曲率限速说明系统应当减速但车速仍偏高，根因应进一步落到请求未发/请求不足/响应不足/覆盖接管等有证据的环节。",
        "对于实际车速高于SetSpeed的问题，前车目标存在、曲率限速、弯道限速、主动限扭、驱动扭矩请求偏低、动力不足、加速不足只能解释车速偏低或加速受限，不能直接解释车速高于设定；若这些信号存在，必须继续判断为什么没有足够制动/减速请求或响应。",
        "对于 problem_description_clues.preferred_window_policy 非空的实际超设速描述，建议重点比较实际车速高于SetSpeed、ACC active且未override/未踩踏板/未功能退出的时段；除非问题描述明确提到override、踩踏板或功能退出，否则不要仅凭这些后续状态直接写成主因。",
        "如果只能在override、踩踏板或功能退出之后找到窗口，但描述没有提到这些控制状态，description_alignment.match_level 必须写“低”或“中”，并说明未覆盖用户真正关心的非接管超设速阶段。",
        "如果证据显示制动请求未发、偏小或未持续，可以将根因归到算法请求；如果请求充足但响应不足，归到执行器响应；如果踏板覆盖/接管先于问题且能解释现象，才归到功能条件/抑制。",
        "必须输出 description_alignment，说明主窗口与问题描述的匹配度、对上的线索和不确定/冲突线索；"
        "若现象和场景线索对得上、但前车距离等数值不同，应写 match_level=中，并明确说明“描述数值与H5不一致，按H5实际值为准”，不能因此判定问题不存在。",
        "description_alignment 中要区分目标车和自车：目标车数值不一致记录为宽松不确定线索；"
        "自车关键状态不一致必须记录为冲突线索，并将 match_level 降为低或中，不能给高置信结论。",
        "如果主窗口与描述现象方向明显相反且无法解释，应将 cause_side 写为“证据不足”、confidence 写“低”，不要给强根因。",
        "没有证据的方向必须写成缺失证据，不要写成根因。",
        "必须给出 root_cause_factor：用 4 到 18 个字说清到底是哪个因素导致，例如“SetSpeed阶跃下降”“前车目标突变”“曲率限速触发”“请求侧未发制动”。",
        "root_cause_factor 不能只写“证据不足”或“场景诱因证据不足”；如果外部诱因没查到但请求侧变化明确，就写请求侧机制，例如“算法主动限扭”“算法负扭矩请求”“算法未请求制动”。",
        "严禁把未观测到的假设因素写成 root_cause_factor，例如“道路坡度未知”“负载未知”“环境阻力未知”“曲率信号缺失”。这些只能写到 missing_evidence 或 recommended_next_queries。",
        "如果没有场景诱因证据，但信号显示驱动扭矩请求偏低或被主动降低，root_cause_factor 应写“驱动扭矩请求偏低”或“算法主动降低驱动扭矩请求”，不要写未知外部因素。",
        "summary 必须 80 个中文字符以内，并以“根因：”开头。",
        "event_window 使用 elapsed 秒填写；如果 raw_time_start 跨信号相差很大，不要直接使用 raw_time_* 拼接事件窗口。",
        "如果候选窗口非空，必须逐个候选窗口分析，并在 window_analyses 中给出每个窗口的结论；最终 headline/root_cause_factor 选择最可信或最严重的窗口作为主结论。",
        "如果多个候选窗口根因一致，summary 中说明是重复出现；如果根因不同，summary 中说明主窗口和其他窗口差异。",
        "如果 problem_time_hint.enabled=true，最终主窗口必须优先来自表格问题时间附近的候选窗口；不要把远离表格问题时间的全文件最大变化误判为主问题。",
        "不要把 AEB/FCA/DAeb/*_AEBUsd 信号、接口状态或抑制原因写入 root_cause_factor、key_signals 或 evidence_chain。",
        "所有用户可见输出字段不得出现 AEB/FCA/DAeb/*_AEBUsd 或其接口名；若需描述，统一写“非本问题接口”。",
        f"key_signals 只列 4 到 8 个最关键证据信号，signal 必须对应 {evidence_name} 里的 requested_name 或 resolved_name。",
    ]


def _final_analysis_output_schema() -> dict[str, Any]:
    return {
        "headline": "string",
        "root_cause_factor": "string",
        "cause_side": "场景/感知目标|算法请求策略|执行器响应|功能条件/抑制|证据不足",
        "confidence": "低|中|高",
        "summary": "根因：<具体因素>。<最关键证据，80字以内>",
        "description_alignment": {
            "match_level": "高|中|低",
            "matched_clues": ["string"],
            "mismatch_or_uncertain_clues": ["string"],
            "selection_note": "why the selected main window matches the problem description; mention uncertainty if needed",
        },
        "event_window": {"start": "number|null", "end": "number|null", "reason": "string"},
        "key_signals": [
            {
                "signal": "requested_name or resolved_name from compact_evidence",
                "role": "现象|请求|诱因|响应|排除项",
                "why": "why this signal is key",
            }
        ],
        "evidence_chain": ["string"],
        "candidate_windows": [
            {
                "id": "string",
                "start": "number",
                "end": "number",
                "reason": "string",
                "description_alignment": "which description clues match or differ",
                "priority": "number",
            }
        ],
        "window_analyses": [
            {
                "window_id": "string",
                "start": "number",
                "end": "number",
                "headline": "string",
                "root_cause_factor": "string",
                "cause_side": "string",
                "confidence": "低|中|高",
                "evidence": ["string"],
            }
        ],
        "missing_evidence": ["string"],
        "recommended_next_queries": ["string"],
    }


def _build_compact_evidence(
    *,
    problem_type: str,
    problem_description: str,
    description_clues: dict[str, Any] | None,
    preset_context: dict[str, Any],
    plan: dict[str, Any],
    evidence: list[SignalSummary],
    problem_time_hint: dict[str, Any] | None,
    candidate_windows: list[dict[str, Any]] | None,
    window_evidence: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    changed = _changed_evidence_by_priority(evidence)
    compact_global = [
        _compact_signal_for_final(item.to_prompt_dict(compact=True))
        for item in _dedupe_evidence_by_resolved_signal(changed)[:12]
    ]
    missing = [_compact_missing_signal(item) for item in evidence if item.status != "ok"]
    coverage = {
        "signals_total": len(evidence),
        "signals_ok": sum(1 for item in evidence if item.status == "ok"),
        "signals_changed": len(changed),
        "signals_missing": sum(1 for item in evidence if item.status != "ok"),
    }
    preset_notes = {
        "query_thinking": _compact_text(str(preset_context.get("query_thinking") or ""), 500),
        "output_hint": _compact_text(str(preset_context.get("output_hint") or ""), 500),
        "note": _compact_text(str(preset_context.get("note") or ""), 300),
        "signal_policy": _compact_text(str(preset_context.get("signal_policy") or ""), 220),
    }
    compact_windows = [_compact_candidate_window(item) for item in (candidate_windows or [])[:3]]
    compact_window_evidence = [_compact_window_evidence_item(item) for item in (window_evidence or [])[:3]]
    return _drop_empty_json(
        {
            "version": "compact_evidence_v1",
            "problem": _problem_context_for_llm(problem_type, problem_description, description_clues),
            "problem_description_clues": description_clues or {},
            "query_source": str(plan.get("query_source") or ""),
            "analysis_focus": [_compact_text(str(item), 180) for item in _as_string_list(plan.get("analysis_focus"))[:6]],
            "problem_time_hint": problem_time_hint or {},
            "signal_guidance": {
                "domain": _domain_signal_guidance(problem_type, description_clues or {}),
                "preset_notes": preset_notes,
                "missing_if_absent": _as_string_list(plan.get("missing_if_absent"))[:5],
            },
            "coverage": coverage,
            "candidate_windows": compact_windows,
            "window_evidence": compact_window_evidence,
            "global_key_evidence": compact_global,
            "missing_evidence": missing[:12],
        }
    )


def _compact_candidate_window(window: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty_json(
        {
            "id": str(window.get("id") or "").strip(),
            "start": _round_float(window.get("start")),
            "end": _round_float(window.get("end")),
            "priority": _round_float(window.get("priority")),
            "reason": _compact_text(str(window.get("reason") or ""), 220),
            "description_alignment": _compact_text(str(window.get("description_alignment") or ""), 220),
            "selection_policy": _compact_text(str(window.get("selection_policy") or ""), 220),
            "focus_signals": _as_string_list(window.get("focus_signals"))[:8],
        }
    )


def _compact_window_evidence_item(window: dict[str, Any], *, max_signals: int = 12) -> dict[str, Any]:
    signal_items = [item for item in window.get("signal_evidence") or [] if isinstance(item, dict)]
    ok_items = [item for item in signal_items if str(item.get("status") or "") == "ok"]
    missing_items = [item for item in signal_items if str(item.get("status") or "") != "ok"]
    group_order = ["phenomenon", "algorithm_request", "algorithm_cause", "actuator_response", "context"]
    group_limits = {
        "phenomenon": 3,
        "algorithm_request": 4,
        "algorithm_cause": 4,
        "actuator_response": 3,
        "context": 2,
    }
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        key = _normalize_name(str(item.get("resolved_name") or item.get("field_name") or item.get("requested_name") or ""))
        if not key or key in seen:
            return
        selected.append(_compact_signal_for_final(item))
        seen.add(key)

    for group in group_order:
        group_items = [item for item in ok_items if str(item.get("group") or "") == group]
        group_items.sort(key=_signal_dict_rank_key)
        for item in group_items[: group_limits.get(group, 2)]:
            add(item)
            if len(selected) >= max_signals:
                break
        if len(selected) >= max_signals:
            break

    if len(selected) < max_signals:
        remaining = sorted(ok_items, key=_signal_dict_rank_key)
        for item in remaining:
            add(item)
            if len(selected) >= max_signals:
                break

    return _drop_empty_json(
        {
            "id": window.get("id"),
            "start": _round_float(window.get("start")),
            "end": _round_float(window.get("end")),
            "reason": _compact_text(str(window.get("reason") or ""), 220),
            "focus_signals": _as_string_list(window.get("focus_signals"))[:8],
            "signals": selected,
            "missing_signals": [_compact_missing_signal_dict(item) for item in missing_items[:8]],
        }
    )


def _compact_signal_for_final(item: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "requested_name": item.get("requested_name"),
        "resolved_name": item.get("resolved_name"),
        "group": item.get("group"),
        "reason": _compact_text(str(item.get("reason") or ""), 120),
        "status": item.get("status"),
        "time_range": item.get("time_range"),
        "value": item.get("value"),
        "max_step": item.get("max_step"),
        "max_1s": item.get("max_1s"),
        "changed": item.get("changed"),
        "active_segments": item.get("active_segments", [])[:2] if isinstance(item.get("active_segments"), list) else [],
        "window": item.get("window") if isinstance(item.get("window"), dict) else {},
    }
    return _drop_empty_json(compact)


def _compact_missing_signal(item: SignalSummary) -> dict[str, Any]:
    return _compact_missing_signal_dict(item.to_prompt_dict(compact=True))


def _compact_missing_signal_dict(item: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty_json(
        {
            "requested_name": item.get("requested_name"),
            "resolved_name": item.get("resolved_name"),
            "group": item.get("group"),
            "reason": _compact_text(str(item.get("reason") or ""), 100),
            "status": item.get("status"),
            "error": _compact_text(str(item.get("error") or ""), 140),
        }
    )


def _signal_dict_rank_key(item: dict[str, Any]) -> tuple[int, float, str]:
    group_order = {
        "phenomenon": 0,
        "algorithm_request": 1,
        "algorithm_cause": 2,
        "actuator_response": 3,
        "context": 4,
    }
    return (
        group_order.get(str(item.get("group") or ""), 9),
        -_signal_dict_change_score(item),
        str(item.get("requested_name") or item.get("resolved_name") or ""),
    )


def _dedupe_evidence_by_resolved_signal(items: list[SignalSummary]) -> list[SignalSummary]:
    result: list[SignalSummary] = []
    seen: set[str] = set()
    for item in items:
        key = _normalize_name(item.resolved_name or item.dataset_path or item.requested_name)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _signal_dict_change_score(item: dict[str, Any]) -> float:
    score = 20.0 if bool(item.get("changed")) else 0.0
    max_1s = item.get("max_1s") if isinstance(item.get("max_1s"), dict) else {}
    max_step = item.get("max_step") if isinstance(item.get("max_step"), dict) else {}
    for block in (max_1s, max_step):
        delta = _float_or_none(block.get("delta"))
        if delta is not None:
            score += min(abs(delta), 10000.0)
    if isinstance(item.get("active_segments"), list) and item.get("active_segments"):
        score += 5.0
    return score


def _should_retry_final_with_expanded_evidence(
    parsed: dict[str, Any],
    compact_evidence: dict[str, Any],
) -> bool:
    if not isinstance(parsed, dict) or not parsed.get("headline"):
        return True
    alignment_level, _, _, uncertain = _description_alignment_details(parsed)
    if alignment_level == "低":
        return True
    factor = str(parsed.get("root_cause_factor") or "").strip()
    if factor in {"", "模型结论未生成", "DeepSeek结论未生成"} or "证据不足" in factor:
        return True
    clues = compact_evidence.get("problem_description_clues") if isinstance(compact_evidence.get("problem_description_clues"), dict) else {}
    if str(clues.get("reported_speed_relation") or "") == "actual_above_set" and any(
        token in factor for token in ("曲率限速", "弯道限速", "曲率限制", "弯道限制")
    ):
        return True
    if str(parsed.get("cause_side") or "").strip() == "证据不足":
        coverage = compact_evidence.get("coverage") if isinstance(compact_evidence.get("coverage"), dict) else {}
        signals_ok = _float_or_none(coverage.get("signals_ok"))
        if signals_ok is not None and signals_ok > 0:
            return True
    window_analyses = parsed.get("window_analyses")
    if compact_evidence.get("candidate_windows") and (not isinstance(window_analyses, list) or not window_analyses):
        return True
    hard_uncertain = [item for item in uncertain if not _is_soft_target_distance_mismatch(item)]
    if len(hard_uncertain) >= 3 and str(parsed.get("confidence") or "").strip() == "低":
        return True
    return False


def _relax_soft_target_alignment(
    parsed: dict[str, Any],
    compact_evidence: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        return {}
    result = dict(parsed)
    alignment = result.get("description_alignment")
    if not isinstance(alignment, dict):
        return result
    uncertain = _as_string_list(alignment.get("mismatch_or_uncertain_clues"))
    if not uncertain or any(not _is_soft_target_distance_mismatch(item) for item in uncertain):
        return result
    if str(alignment.get("match_level") or "").strip() != "低":
        return result
    coverage = compact_evidence.get("coverage") if isinstance(compact_evidence.get("coverage"), dict) else {}
    signals_ok = _float_or_none(coverage.get("signals_ok")) or 0.0
    candidate_windows = compact_evidence.get("candidate_windows")
    if signals_ok <= 0 or not isinstance(candidate_windows, list) or not candidate_windows:
        return result
    adjusted = dict(alignment)
    adjusted["match_level"] = "中"
    note = str(adjusted.get("selection_note") or "").strip()
    soft_note = "目标距离按软线索处理，距离较远或与描述值不同不否决该问题窗口"
    adjusted["selection_note"] = f"{note}；{soft_note}" if note else soft_note
    result["description_alignment"] = adjusted
    return result


def _is_soft_target_distance_mismatch(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    distance_tokens = ("距离", "50m", "50 m", "50米", "distance", "long_range", "long range")
    target_tokens = ("目标", "前车", "车辆", "target", "flt1")
    return any(token in text for token in distance_tokens) and any(token in text for token in target_tokens)


def _build_expanded_retry_evidence(
    *,
    evidence: list[SignalSummary],
    window_evidence: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    expanded_windows: list[dict[str, Any]] = []
    for window in (window_evidence or [])[:3]:
        expanded_windows.append(
            _drop_empty_json(
                {
                    "id": window.get("id"),
                    "start": _round_float(window.get("start")),
                    "end": _round_float(window.get("end")),
                    "reason": _compact_text(str(window.get("reason") or ""), 220),
                    "focus_signals": _as_string_list(window.get("focus_signals"))[:8],
                    "signal_evidence": [
                        _compact_retry_signal_dict(item)
                        for item in (window.get("signal_evidence") or [])
                        if isinstance(item, dict)
                    ][:18],
                }
            )
        )
    return _drop_empty_json(
        {
            "reason": "第一次 compact_evidence 分析结果证据不足、低匹配或未输出有效窗口分析，追加更多窗口信号用于复核。",
            "window_evidence": expanded_windows,
            "global_signal_evidence": [
                item.to_prompt_dict(compact=True)
                for item in _dedupe_evidence_by_resolved_signal(_changed_evidence_by_priority(evidence))[:18]
            ],
        }
    )


def _compact_retry_signal_dict(item: dict[str, Any]) -> dict[str, Any]:
    data = dict(item)
    if isinstance(data.get("samples"), list):
        data["samples"] = data["samples"][:3]
    if isinstance(data.get("active_segments"), list):
        data["active_segments"] = data["active_segments"][:3]
    if "reason" in data:
        data["reason"] = _compact_text(str(data.get("reason") or ""), 120)
    if "error" in data:
        data["error"] = _compact_text(str(data.get("error") or ""), 140)
    return _drop_empty_json(data)


def _drop_empty_json(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {key: _drop_empty_json(item) for key, item in value.items()}
        return {key: item for key, item in cleaned.items() if item not in ("", None, [], {})}
    if isinstance(value, list):
        cleaned_items: list[Any] = []
        for item in value:
            cleaned = _drop_empty_json(item)
            if cleaned not in ("", None, [], {}):
                cleaned_items.append(cleaned)
        return cleaned_items
    return value


def _preset_context(problem_type: str) -> dict[str, Any]:
    presets = load_performance_problem_presets()
    preset = presets.get(problem_type)
    if preset is None:
        for item in presets.values():
            if getattr(item, "label", "") == problem_type:
                preset = item
                break
    if preset is None:
        return {"problem_type": problem_type, "note": "未在预设表中找到该问题类型。"}

    label = getattr(preset, "label", problem_type)
    return {
        "problem_type": label,
        "vehicle_signals": _filter_defined_signal_names(label, list(getattr(preset, "vehicle_signals", ()) or ())),
        "algorithm_request_signals": _filter_defined_signal_names(
            label,
            list(getattr(preset, "algorithm_request_signals", ()) or ()),
        ),
        "algorithm_cause_signals": _filter_defined_signal_names(label, list(getattr(preset, "algorithm_cause_signals", ()) or ())),
        "actuator_response_signals": _filter_defined_signal_names(
            label,
            list(getattr(preset, "actuator_request_signals", ()) or ()),
        ),
        "metrics": list(getattr(preset, "metrics", ()) or ()),
        "query_thinking": str(getattr(preset, "algorithm_cause_logic", "") or ""),
        "output_hint": str(getattr(preset, "root_cause_output_hint", "") or ""),
        "note": str(getattr(preset, "note", "") or ""),
        "signal_policy": "只使用本预设中定义好的 ACC/LCC 信号；禁止使用 AEB/FCA/DAeb/*_AEBUsd 接口。",
    }


def _defined_signal_candidates(preset_context: dict[str, Any]) -> list[str]:
    fields = (
        "vehicle_signals",
        "algorithm_request_signals",
        "algorithm_cause_signals",
        "actuator_response_signals",
    )
    values: list[str] = []
    for field_name in fields:
        for signal in preset_context.get(field_name) or []:
            name = str(signal or "").strip()
            if name:
                values.append(name)
    return list(dict.fromkeys(values))


def _defined_signal_keys(preset_context: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for signal in _defined_signal_candidates(preset_context):
        for variant in _expanded_signal_names(signal):
            for key in _name_variants(variant):
                if key:
                    keys.add(key)
    return keys


def _is_allowed_defined_signal(signal: str, allowed_keys: set[str]) -> bool:
    if not allowed_keys:
        return True
    variants = _name_variants(signal)
    for key in variants:
        if key in allowed_keys:
            return True
        if any(key.endswith(allowed) for allowed in allowed_keys if allowed):
            return True
    return False


def _rank_local_query_signals(
    signals: list[str],
    *,
    group: str,
    description_clues: dict[str, Any],
) -> list[str]:
    clue_text = json.dumps(description_clues or {}, ensure_ascii=False).lower()

    def score_signal(index: int, signal: str) -> tuple[int, int]:
        normalized = _normalize_name(signal)
        score = 0
        if "reported_speed_relation" in description_clues:
            if any(token in normalized for token in ("vehspd", "setspeed", "drvrseltdspd", "imulonacc", "axltrq", "trq")):
                score -= 20
        if any(keyword in clue_text for keyword in ("前车", "目标", "切入", "cut-in", "cutin")):
            if any(token in normalized for token in ("target", "lead", "obj", "object", "longrange", "cutin", "globallong")):
                score -= 30
        if any(keyword in clue_text for keyword in ("弯道", "曲率", "匝道", "左弯", "右弯")):
            if any(token in normalized for token in ("curv", "curve", "radius", "lane", "lanemodel", "c0", "c1")):
                score -= 30
        if any(keyword in clue_text for keyword in ("上坡", "下坡", "坡道", "坡")):
            if any(token in normalized for token in ("slope", "grade", "incline", "pitch", "roadgrade")):
                score -= 30
        if any(keyword in clue_text for keyword in ("接管", "油门", "制动踏板", "刹车")):
            if any(token in normalized for token in ("pedal", "override", "brake", "drvrappl", "accel")):
                score -= 25
        if group == "phenomenon":
            score -= 4
        elif group == "algorithm_request":
            score -= 3
        elif group == "algorithm_cause":
            score -= 2
        elif group == "actuator_response":
            score -= 1
        return score, index

    ranked = sorted(enumerate(signals), key=lambda item: score_signal(item[0], item[1]))
    return [signal for _, signal in ranked]


def _filter_defined_signal_names(problem_type: str, signals: list[str]) -> list[str]:
    disallowed = _disallowed_interface_tokens(problem_type)
    filtered: list[str] = []
    for signal in signals:
        name = str(signal or "").strip()
        if not name:
            continue
        if disallowed and _contains_forbidden_text(name, disallowed):
            continue
        filtered.append(name)
    return filtered


def _disallowed_interface_tokens(problem_type: str) -> tuple[str, ...]:
    return ACC_LCC_FORBIDDEN_INTERFACE_TOKENS


def _contains_forbidden_interface(ref: SignalRef, disallowed_tokens: tuple[str, ...]) -> bool:
    haystack = " ".join((ref.display_name, ref.dataset_path, ref.field_name, ref.timestamp_path))
    return _contains_forbidden_text(haystack, disallowed_tokens)


def _contains_forbidden_text(value: str, disallowed_tokens: tuple[str, ...]) -> bool:
    normalized = _normalize_name(value)
    return any(token in normalized for token in disallowed_tokens)


def _sanitize_public_final_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize_public_final_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_public_final_json(item) for item in value]
    if isinstance(value, str):
        text = value
        replacements = (
            ("AEB/FCA/DAeb/*_AEBUsd", "非本问题接口"),
            ("AEB/FCA", "非本问题接口"),
            ("Forward_Looking_Target_AEB", "非本问题接口"),
            ("IStrWhAng_AEBUsd", "非本问题接口"),
            ("IFCACBSC_AutBrkRqTp1", "非本问题接口"),
            ("IFCACBSC_CtrlTrq1", "非本问题接口"),
            ("IAutoBrkngAct", "非本问题接口"),
            ("FCACBSC", "非本问题接口"),
            ("DAeb", "非本问题接口"),
            ("AEB", "非本问题接口"),
            ("FCA", "非本问题接口"),
        )
        for old, new in replacements:
            text = text.replace(old, new)
        return text
    return value


def _queries_from_plan(
    plan: dict[str, Any],
    preset_context: dict[str, Any],
    *,
    max_signals: int,
) -> list[dict[str, str]]:
    plan_queries: list[dict[str, str]] = []
    allowed_keys = _defined_signal_keys(preset_context)
    for query_group in plan.get("queries") or []:
        if not isinstance(query_group, dict):
            continue
        group = str(query_group.get("group") or "context")
        reason = str(query_group.get("why") or query_group.get("reason") or "")
        for signal in query_group.get("signals") or []:
            name = str(signal or "").strip()
            if name and _is_allowed_defined_signal(name, allowed_keys):
                plan_queries.append({"group": group, "signal": name, "reason": reason})

    baseline_queries = _baseline_signal_queries(preset_context)
    remaining_guide_queries = _all_preset_signal_queries(preset_context)
    queries = [*baseline_queries, *plan_queries, *remaining_guide_queries]

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for query in queries:
        key = _normalize_name(query["signal"])
        if not _is_allowed_defined_signal(query["signal"], allowed_keys):
            continue
        if key and key not in seen:
            seen.add(key)
            deduped.append(query)
        if len(deduped) >= max_signals:
            break
    return deduped


def _build_problem_time_hint(h5_file: Path, *, problem_time: str = "", problem_date: str = "") -> dict[str, Any]:
    time_text = str(problem_time or "").strip()
    date_text = str(problem_date or "").strip()
    if not time_text and not date_text:
        return {"enabled": False}
    h5_start = _parse_h5_start_datetime(h5_file.name)
    if h5_start is None:
        return {
            "enabled": False,
            "problem_time": time_text,
            "problem_date": date_text,
            "reason": "无法从 H5 文件名解析起始时间",
        }
    problem_dt, precision_seconds = _parse_problem_datetime(
        problem_time=time_text,
        problem_date=date_text,
        default_date=h5_start.date(),
    )
    if problem_dt is None:
        return {
            "enabled": False,
            "h5_start_time": h5_start.strftime("%Y-%m-%d %H:%M:%S"),
            "problem_time": time_text,
            "problem_date": date_text,
            "reason": "表格问题时间格式无法解析",
        }
    elapsed = (problem_dt - h5_start).total_seconds()
    if elapsed < -300 and not _problem_date_has_explicit_date(date_text) and not _problem_time_has_explicit_date(time_text):
        problem_dt = problem_dt + timedelta(days=1)
        elapsed = (problem_dt - h5_start).total_seconds()
    if elapsed < -1:
        return {
            "enabled": False,
            "h5_start_time": h5_start.strftime("%Y-%m-%d %H:%M:%S"),
            "problem_datetime": problem_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "problem_time": time_text,
            "problem_date": date_text,
            "target_elapsed_seconds": _round_float(elapsed),
            "reason": "表格问题时间早于 H5 文件名起始时间，未启用时间窗口筛选",
        }
    radius = _problem_time_window_radius(precision_seconds)
    preferred = {
        "start": _round_float(max(0.0, elapsed - radius)),
        "end": _round_float(max(0.0, elapsed + radius)),
        "reason": "根据飞书表格问题时间和 H5 文件名起始时间换算",
    }
    return {
        "enabled": True,
        "h5_start_time": h5_start.strftime("%Y-%m-%d %H:%M:%S"),
        "problem_datetime": problem_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "problem_time": time_text,
        "problem_date": date_text,
        "target_elapsed_seconds": _round_float(elapsed),
        "preferred_window": preferred,
        "window_radius_seconds": radius,
    }


def _parse_h5_start_datetime(file_name: str) -> datetime | None:
    text = str(file_name or "")
    patterns = (
        r"(20\d{2})\D?(\d{2})\D?(\d{2})\D+(\d{2})\D?(\d{2})\D?(\d{2})",
        r"(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            year, month, day, hour, minute, second = (int(part) for part in match.groups())
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            continue
    return None


def _parse_problem_datetime(
    *,
    problem_time: str,
    problem_date: str,
    default_date: Any,
) -> tuple[datetime | None, int]:
    time_dt, time_precision = _parse_datetime_text(problem_time)
    if time_dt and _problem_time_has_explicit_date(problem_time):
        return time_dt, time_precision

    date_dt, _ = _parse_datetime_text(problem_date)
    date_part = date_dt.date() if date_dt else default_date
    if time_dt:
        return datetime.combine(date_part, time_dt.time()), time_precision
    if date_dt:
        return date_dt, 1
    return None, 1


def _parse_datetime_text(value: str) -> tuple[datetime | None, int]:
    text = _normalize_time_text(value)
    if not text:
        return None, 1
    if re.fullmatch(r"\d{10,13}", text):
        try:
            number = int(text)
            if number > 10_000_000_000:
                number = int(number / 1000)
            return datetime.fromtimestamp(number), 1
        except (OSError, OverflowError, ValueError):
            return None, 1
    date_time_patterns = (
        (r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})[日\sT_-]+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", True),
        (r"(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})", True),
    )
    for pattern, _has_date in date_time_patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        groups = match.groups()
        try:
            second = int(groups[5]) if groups[5] is not None else 0
            precision = 1 if groups[5] is not None else 60
            return (
                datetime(int(groups[0]), int(groups[1]), int(groups[2]), int(groups[3]), int(groups[4]), second),
                precision,
            )
        except ValueError:
            continue
    time_match = re.search(r"\b(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?\b", text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        second_text = time_match.group(3)
        second = int(second_text) if second_text is not None else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59:
            return datetime(1900, 1, 1, hour, minute, second), 1 if second_text is not None else 60
    date_match = re.search(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})", text)
    if date_match:
        try:
            return datetime(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))), 86400
        except ValueError:
            return None, 1
    return None, 1


def _normalize_time_text(value: str) -> str:
    text = str(value or "").strip()
    replacements = {
        "：": ":",
        "；": ":",
        "时": ":",
        "点": ":",
        "分": ":",
        "秒": "",
        "年": "-",
        "月": "-",
        "日": " ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r":+$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _problem_time_has_explicit_date(value: str) -> bool:
    text = _normalize_time_text(value)
    return bool(re.search(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", text) or re.search(r"20\d{6}[_-]?\d{6}", text))


def _problem_date_has_explicit_date(value: str) -> bool:
    text = _normalize_time_text(value)
    if re.fullmatch(r"\d{10,13}", text):
        return True
    return bool(re.search(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", text) or re.search(r"20\d{6}", text))


def _problem_time_window_radius(precision_seconds: int) -> float:
    env_value = os.getenv("BATCH_ROOT_CAUSE_PROBLEM_TIME_WINDOW_SECONDS", "").strip()
    if env_value:
        try:
            return max(1.0, float(env_value))
        except ValueError:
            pass
    if precision_seconds >= 60:
        return 30.0
    return 10.0


def _should_prefer_active_non_override_setspeed_window(
    problem_type: str,
    description_clues: dict[str, Any] | None,
) -> bool:
    clues = description_clues or {}
    problem_text = str(problem_type or "").lower()
    is_setspeed_problem = "setspeed" in problem_text or "set speed" in problem_text or "稳速" in problem_text
    return bool(
        is_setspeed_problem
        and clues.get("reported_speed_relation") == "actual_above_set"
        and not clues.get("mentions_control_override_or_exit")
    )


def _preferred_setspeed_actual_above_windows(
    *,
    h5_file: Path,
    index: H5SignalIndex,
    evidence: list[SignalSummary],
    description_clues: dict[str, Any] | None,
    max_windows: int,
) -> list[dict[str, Any]]:
    speed = _read_first_signal_series(h5_file, index, ("VehSpdAvgDrvn_1", "IVehSpdAvgDrvn_1", "VehSpdAvg"))
    set_speed = _read_first_signal_series(h5_file, index, ("IACCDrvrSeltdSpd", "ACCDrvrSeltdSpd", "SetSpeed"))
    acc_state = _read_first_signal_series(h5_file, index, ("IACCAct376", "ACCAct376"))
    override = _read_first_signal_series(h5_file, index, ("IAccPdlOvrrdAtv", "AccPdlOvrrdAtv"))
    if speed is None or set_speed is None or acc_state is None or override is None:
        return []

    raw_origin = min(float(series[0][0]) for series in (speed, set_speed, acc_state, override) if len(series[0]))
    speed_t = speed[0] - raw_origin
    speed_v = speed[1]
    speed_name = speed[2]
    axis = speed_t
    set_v = _sample_nearest_series(set_speed[0] - raw_origin, set_speed[1], axis)
    acc_v = _sample_nearest_series(acc_state[0] - raw_origin, acc_state[1], axis)
    override_v = _sample_nearest_series(override[0] - raw_origin, override[1], axis)

    finite = np.isfinite(axis) & np.isfinite(speed_v) & np.isfinite(set_v) & np.isfinite(acc_v) & np.isfinite(override_v)
    axis = axis[finite]
    speed_v = speed_v[finite]
    set_v = set_v[finite]
    acc_v = acc_v[finite]
    override_v = override_v[finite]
    if len(axis) < 2:
        return []

    margin = _setspeed_actual_above_margin_kph()
    display_speed_v = _display_speed_from_actual_array(speed_v)
    display_set_v = _display_speed_from_actual_array(set_v)
    above_delta = display_speed_v - display_set_v
    mask = (above_delta >= margin) & _truthy_numeric_array(acc_v) & ~_truthy_numeric_array(override_v)
    segments = _segments_from_mask(axis, mask, min_duration=_setspeed_min_above_duration_seconds())
    if not segments and np.any(mask):
        true_indices = np.where(mask)[0]
        peak_index = int(true_indices[int(np.nanargmax(above_delta[true_indices]))])
        segments = [(peak_index, peak_index)]
    if not segments:
        return []

    focus_signals = _focus_signal_names_for_tokens(
        evidence,
        (
            "vehspd",
            "drvrseltdspd",
            "setspeed",
            "iaccact376",
            "accact376",
            "pdlovrrd",
            "override",
            "axltrq",
            "acctrq",
            "autbrk",
            "brkngact",
            "brkped",
            "trqvl",
        ),
    )
    if not focus_signals:
        focus_signals = [speed_name, set_speed[2], acc_state[2], override[2]]

    clues = description_clues or {}
    reported_actual = _float_or_none(clues.get("reported_actual_speed_kph_approx"))
    reported_set = _float_or_none(clues.get("reported_set_speed_kph_approx"))
    ranked: list[tuple[float, float, float, int, int]] = []
    for start_index, end_index in segments:
        segment_delta = above_delta[start_index : end_index + 1]
        if len(segment_delta) == 0:
            continue
        peak_offset = int(np.nanargmax(segment_delta))
        peak_index = start_index + peak_offset
        severity = float(segment_delta[peak_offset])
        alignment_score = 0.0
        if reported_actual is not None:
            alignment_score += abs(float(display_speed_v[peak_index]) - reported_actual)
        if reported_set is not None:
            alignment_score += 0.5 * abs(float(display_set_v[peak_index]) - reported_set)
        ranked.append((alignment_score if reported_actual is not None or reported_set is not None else 0.0, -severity, float(axis[start_index]), start_index, end_index))
    ranked.sort()

    windows: list[dict[str, Any]] = []
    for _, _, _, start_index, end_index in ranked[:max_windows]:
        local_delta = above_delta[start_index : end_index + 1]
        peak_offset = int(np.nanargmax(local_delta)) if len(local_delta) else 0
        peak_index = start_index + peak_offset
        center = float(axis[peak_index])
        segment_start = float(axis[start_index])
        segment_end = float(axis[end_index])
        half_width = max(1.0, SETSPEED_PREFERRED_WINDOW_SECONDS / 2.0)
        window_start = max(segment_start, center - half_width)
        window_end = min(segment_end, center + half_width)
        if window_end - window_start < 1.0:
            window_start = max(0.0, center - 0.5)
            window_end = center + 0.5
        windows.append(
            {
                "id": f"w{len(windows) + 1}",
                "start": _round_float(max(0.0, window_start)),
                "end": _round_float(max(window_end, window_start + 1.0)),
                "priority": len(windows) + 1,
                "reason": (
                    "本地优先窗口：实际车速高于SetSpeed，ACC active且未override；"
                    f"峰值差约 {_round_float(float(above_delta[peak_index]), 2)}，"
                    f"显示车速={_round_float(float(display_speed_v[peak_index]), 2)}，"
                    f"显示SetSpeed={_round_float(float(display_set_v[peak_index]), 2)}。"
                ),
                "description_alignment": (
                    "对齐“实际车速高于设定车速”的描述；描述未提到override/踩踏板/功能退出，"
                    "因此优先选择非接管、功能active时段。"
                ),
                "focus_signals": focus_signals[:12],
                "selection_policy": "prefer_active_non_override_actual_above_setspeed",
            }
        )
    return windows


def _read_first_signal_series(
    h5_file: Path,
    index: H5SignalIndex,
    names: tuple[str, ...],
) -> tuple[np.ndarray, np.ndarray, str] | None:
    for name in names:
        ref = index.resolve(name)
        if ref is None:
            continue
        try:
            timestamps, values = _read_signal_values(h5_file, ref)
        except Exception:
            continue
        timestamps = np.asarray(timestamps, dtype=float)
        values = np.asarray(values, dtype=float)
        finite = np.isfinite(timestamps) & np.isfinite(values)
        timestamps = timestamps[finite]
        values = values[finite]
        if len(timestamps) == 0:
            continue
        order = np.argsort(timestamps)
        return timestamps[order], values[order], ref.display_name
    return None


def _sample_nearest_series(source_t: np.ndarray, source_v: np.ndarray, target_t: np.ndarray) -> np.ndarray:
    if len(source_t) == 0 or len(source_v) == 0 or len(target_t) == 0:
        return np.full(len(target_t), np.nan, dtype=float)
    indices = np.searchsorted(source_t, target_t, side="left")
    right = np.clip(indices, 0, len(source_t) - 1)
    left = np.clip(indices - 1, 0, len(source_t) - 1)
    choose_right = np.abs(source_t[right] - target_t) < np.abs(target_t - source_t[left])
    nearest = np.where(choose_right, right, left)
    return source_v[nearest]


def _display_speed_from_actual_array(values: np.ndarray) -> np.ndarray:
    speed = np.maximum(np.asarray(values, dtype=float), 0.0)
    factors = np.interp(speed, SPEED_BIAS_X, SPEED_BIAS_Y, left=float(SPEED_BIAS_Y[0]), right=float(SPEED_BIAS_Y[-1]))
    return np.floor(speed * factors + 0.5)


def _truthy_numeric_array(values: np.ndarray) -> np.ndarray:
    return np.isfinite(values) & (np.abs(values) > 1e-6)


def _segments_from_mask(
    axis: np.ndarray,
    mask: np.ndarray,
    *,
    min_duration: float,
) -> list[tuple[int, int]]:
    segments: list[tuple[int, int]] = []
    start_index: int | None = None
    for index, is_active in enumerate(mask):
        if bool(is_active) and start_index is None:
            start_index = index
        if (not bool(is_active) or index == len(mask) - 1) and start_index is not None:
            end_index = index if bool(is_active) and index == len(mask) - 1 else max(start_index, index - 1)
            duration = float(axis[end_index] - axis[start_index]) if end_index > start_index else 0.0
            if duration >= min_duration:
                segments.append((start_index, end_index))
            start_index = None
    return segments


def _setspeed_actual_above_margin_kph() -> float:
    value = os.getenv("BATCH_ROOT_CAUSE_ACTUAL_ABOVE_SET_MARGIN_KPH", "").strip()
    if value:
        try:
            return max(0.1, float(value))
        except ValueError:
            pass
    return SETSPEED_ACTUAL_ABOVE_MARGIN_KPH


def _setspeed_min_above_duration_seconds() -> float:
    value = os.getenv("BATCH_ROOT_CAUSE_ACTUAL_ABOVE_SET_MIN_DURATION_SECONDS", "").strip()
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
    return SETSPEED_MIN_ABOVE_SET_DURATION_SECONDS


def _focus_signal_names_for_tokens(evidence: list[SignalSummary], tokens: tuple[str, ...]) -> list[str]:
    focus: list[str] = []
    seen: set[str] = set()
    for item in evidence:
        haystack = _normalize_name(" ".join((item.requested_name, item.resolved_name, item.field_name)))
        if not any(token in haystack for token in tokens):
            continue
        name = item.requested_name or item.resolved_name or item.field_name
        key = _normalize_name(name)
        if key and key not in seen:
            seen.add(key)
            focus.append(name)
    return focus


def _merge_candidate_windows(
    preferred: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
    *,
    max_windows: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[float, float]] = set()
    for item in [*preferred, *secondary]:
        start = _float_or_none(item.get("start"))
        end = _float_or_none(item.get("end"))
        if start is None or end is None:
            continue
        key = (round(float(start), 2), round(float(end), 2))
        if key in seen:
            continue
        seen.add(key)
        merged = dict(item)
        merged["priority"] = len(result) + 1
        result.append(merged)
        if len(result) >= max_windows:
            break
    return result


def _build_deepseek_candidate_windows(
    *,
    h5_file: Path,
    index: H5SignalIndex,
    problem_type: str,
    problem_description: str,
    description_clues: dict[str, Any] | None = None,
    preset_context: dict[str, Any],
    plan: dict[str, Any],
    evidence: list[SignalSummary],
    problem_time_hint: dict[str, Any] | None = None,
    max_windows: int = 3,
) -> list[dict[str, Any]]:
    evidence_payload = [item.to_prompt_dict(compact=True) for item in evidence]
    user_payload = {
        "task": "从整段 H5 信号摘要中筛选候选问题窗口",
        "problem": _problem_context_for_llm(problem_type, problem_description, description_clues),
        "problem_type": _model_problem_type_label(problem_type, description_clues),
        "table_problem_type": problem_type,
        "problem_description": problem_description,
        "problem_description_clues": description_clues or {},
        "local_signal_guide": preset_context,
        "domain_signal_guidance": _domain_signal_guidance(problem_type, description_clues or {}),
        "query_plan": plan,
        "signal_evidence": evidence_payload,
        "problem_time_hint": problem_time_hint or {},
        "max_windows": max_windows,
        "requirements": [
            "只用 elapsed 秒填写 start/end，不要使用 raw_time。",
            "窗口应覆盖可能的问题现象，以及其前后的请求、诱因和响应变化。",
            "如果同一 H5 里有多个相似问题点，返回多个候选窗口，不要只保留一个。",
            "每个窗口尽量 2 到 8 秒；不确定时可以围绕关键变化点前后各 2 秒。",
            "如果 problem_time_hint.enabled=true，候选窗口必须优先围绕 preferred_window；除非信号完全无数据，否则不要选择远离该时间的窗口。",
            "problem_description_clues 是软约束：车速数值可能不准，定速不代表速度不变，不要因数值不完全一致排除窗口。",
            "问题描述中的前车距离、速度等人工数值只是近似值，不能作为候选窗口的硬过滤条件；"
            "先按现象是否发生以及前车目标-算法请求-执行器响应的时序选择窗口，数值不一致只记录为 mismatch_or_uncertain_clues。",
            "目标车约束可以宽松：目标车存在、距离、相对速度等描述数值允许误差，不能单独否决窗口。目标距离没有最大阈值；即使明显大于描述值或大于50m，只要现象和请求/响应时序成立也必须保留。",
            "误制动/重制动/减速类问题不要只看目标距离是否等于描述值；如果目标车纵向速度/相对速度在制动请求前后明显偏低，也应作为高优先级诱因窗口保留。",
            "自车约束必须严格：自车实际车速/设定车速关系、ACC/LCC状态、挡位、踏板接管以及请求-响应时序必须在H5中对上；"
            "自车车速绝对值允许约±3 km/h的小幅误差，但实际车速与设定车速的高低关系、变化趋势和关键状态不能放宽；"
            "若自车关键状态不匹配，应降低窗口优先级或标记为不匹配，不能用目标车证据替代。",
            "若描述给出了实际高于/低于设定、坡道、弯道、前车等线索，窗口 priority 和 reason 应优先体现这些线索是否被信号支持或仍不确定。",
            "priority=1 的候选窗口代表本轮最优先核查窗口；只要窗口有现象或请求-响应证据，必须保留并优先分析，不能因描述中的距离/速度数值不一致而丢弃。",
            "表格问题类型名主要用于选择预设信号；如果它和描述数值方向冲突，请在窗口 reason 中说明冲突并由H5证据决定优先级。",
            "不要只因为某个信号变化最大就选窗口；建议优先选择能解释用户描述现象方向的窗口，同时保留证据更强的相反窗口供后续判断。",
            "对于描述未提到override/踩踏板/功能退出的实际超设速问题，请关注override、踩踏板或功能退出相对速度偏离的先后关系，不要仅凭后续状态直接定因。",
            "focus_signals 只能写 signal_evidence 中出现过的 requested_name 或 resolved_name。",
        ],
        "output_schema": {
            "candidate_windows": [
                {
                    "id": "w1",
                    "start": "number",
                    "end": "number",
                    "priority": "number, 1 is highest",
                    "reason": "why this window may contain the problem",
                    "description_alignment": "which description clues this window matches or cannot verify",
                    "focus_signals": ["requested_name or resolved_name"],
                }
            ]
        },
    }
    try:
        raw = _deepseek_json_call(
            [
                {"role": "system", "content": WINDOW_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ]
        )
        parsed = parse_json_object(raw) or {}
        windows = parsed.get("candidate_windows") if isinstance(parsed, dict) else None
    except DeepSeekAPIUnavailable:
        raise
    except Exception:
        windows = None
    normalized = _normalize_candidate_windows(windows, evidence, max_windows=max_windows)
    normalized = _apply_problem_time_hint_to_windows(normalized, problem_time_hint, max_windows=max_windows)
    if normalized:
        return normalized
    fallback = _fallback_candidate_windows(evidence, max_windows=max_windows)
    fallback = _apply_problem_time_hint_to_windows(fallback, problem_time_hint, max_windows=max_windows)
    for item in fallback:
        item["reason"] = f"{item.get('reason', '')}；模型候选窗口输出不可用，使用本地变化摘要兜底"
    return fallback


def _normalize_candidate_windows(
    windows: Any,
    evidence: list[SignalSummary],
    *,
    max_windows: int,
) -> list[dict[str, Any]]:
    if not isinstance(windows, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[float, float]] = set()
    for index, item in enumerate(windows, start=1):
        if not isinstance(item, dict):
            continue
        start = _float_or_none(item.get("start"))
        end = _float_or_none(item.get("end"))
        if start is None or end is None:
            start, end = _window_from_focus_signals(item.get("focus_signals"), evidence)
        if start is None or end is None:
            continue
        start = max(0.0, float(start))
        end = float(end)
        if end <= start:
            end = start + 2.0
        if end - start < 0.5:
            center = (start + end) / 2.0
            start = max(0.0, center - 0.5)
            end = center + 0.5
        key = (round(start, 2), round(end, 2))
        if key in seen:
            continue
        seen.add(key)
        priority = _float_or_none(item.get("priority"))
        focus_signals = _as_string_list(item.get("focus_signals"))[:12]
        result.append(
            {
                "id": str(item.get("id") or f"w{len(result) + 1}"),
                "start": _round_float(start),
                "end": _round_float(end),
                "priority": int(priority) if priority is not None else len(result) + 1,
                "reason": _compact_text(str(item.get("reason") or "模型筛选的候选问题窗口"), 180),
                "description_alignment": _compact_text(str(item.get("description_alignment") or ""), 180),
                "focus_signals": focus_signals,
            }
        )
    # Sort before truncating so a model-returned priority-1 window cannot be
    # discarded merely because it appeared late in the response.
    return sorted(
        result,
        key=lambda value: (int(value.get("priority") or 99), float(value.get("start") or 0.0)),
    )[:max_windows]


def _apply_problem_time_hint_to_windows(
    windows: list[dict[str, Any]],
    problem_time_hint: dict[str, Any] | None,
    *,
    max_windows: int,
) -> list[dict[str, Any]]:
    if not problem_time_hint or not problem_time_hint.get("enabled"):
        return windows[:max_windows]
    preferred = problem_time_hint.get("preferred_window") if isinstance(problem_time_hint.get("preferred_window"), dict) else {}
    preferred_start = _float_or_none(preferred.get("start"))
    preferred_end = _float_or_none(preferred.get("end"))
    target = _float_or_none(problem_time_hint.get("target_elapsed_seconds"))
    if preferred_start is None or preferred_end is None:
        return windows[:max_windows]
    if preferred_end <= preferred_start:
        preferred_end = preferred_start + 1.0

    filtered: list[dict[str, Any]] = []
    seen: set[tuple[float, float]] = set()
    for item in windows:
        start = _float_or_none(item.get("start"))
        end = _float_or_none(item.get("end"))
        if start is None or end is None:
            continue
        if end < preferred_start or start > preferred_end:
            continue
        adjusted = dict(item)
        adjusted["start"] = _round_float(max(0.0, min(start, preferred_start)))
        adjusted["end"] = _round_float(max(end, preferred_end))
        key = (
            round(float(adjusted["start"] or 0.0), 2),
            round(float(adjusted["end"] or 0.0), 2),
        )
        if key in seen:
            continue
        seen.add(key)
        adjusted["priority"] = len(filtered) + 1
        adjusted["reason"] = _compact_text(
            f"{adjusted.get('reason', '')}；已按表格问题时间 {problem_time_hint.get('problem_datetime')} "
            f"(H5内 {problem_time_hint.get('target_elapsed_seconds')}s) 筛选",
            220,
        )
        filtered.append(adjusted)
        if len(filtered) >= max_windows:
            break
    if filtered:
        return filtered

    start = preferred_start
    end = preferred_end
    if target is not None:
        start = min(start, target)
        end = max(end, target)
    return [
        {
            "id": "w1",
            "start": _round_float(max(0.0, start)),
            "end": _round_float(max(end, start + 1.0)),
            "priority": 1,
            "reason": (
                "根据飞书表格问题时间强制定位候选窗口："
                f"{problem_time_hint.get('problem_datetime')}，H5内 {problem_time_hint.get('target_elapsed_seconds')}s"
            ),
            "focus_signals": [],
        }
    ]


def _window_from_focus_signals(
    focus_signals: Any,
    evidence: list[SignalSummary],
) -> tuple[float | None, float | None]:
    lookup = _evidence_lookup(evidence)
    times: list[float] = []
    for signal in _as_string_list(focus_signals):
        item = lookup.get(_normalize_name(signal))
        if not item:
            continue
        time_value = item.max_window_delta_time if item.max_window_delta_time is not None else item.max_delta_time
        if time_value is not None and math.isfinite(float(time_value)):
            times.append(float(time_value))
    if not times:
        return None, None
    return max(0.0, min(times) - 2.0), max(times) + 2.0


def _fallback_candidate_windows(evidence: list[SignalSummary], *, max_windows: int) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    seen_centers: list[float] = []
    for item in _changed_evidence_by_priority(evidence):
        time_value = item.max_window_delta_time if item.max_window_delta_time is not None else item.max_delta_time
        if time_value is None or not math.isfinite(float(time_value)):
            continue
        center = float(time_value)
        if any(abs(center - existing) < 1.0 for existing in seen_centers):
            continue
        seen_centers.append(center)
        windows.append(
            {
                "id": f"w{len(windows) + 1}",
                "start": _round_float(max(0.0, center - 2.0)),
                "end": _round_float(center + 2.0),
                "priority": len(windows) + 1,
                "reason": f"围绕 {item.requested_name} 最大变化时间 {_round_float(center)}s",
                "focus_signals": [item.requested_name],
            }
        )
        if len(windows) >= max_windows:
            break
    if windows:
        return windows
    starts = [item.time_start for item in evidence if item.time_start is not None]
    ends = [item.time_end for item in evidence if item.time_end is not None]
    if not starts or not ends:
        return []
    return [
        {
            "id": "w1",
            "start": _round_float(min(starts)),
            "end": _round_float(min(max(ends), min(starts) + 10.0)),
            "priority": 1,
            "reason": "未发现明显变化点，使用文件开头可用信号窗口兜底",
            "focus_signals": [],
        }
    ]


def _build_window_evidence(
    *,
    h5_file: Path,
    index: H5SignalIndex,
    queries: list[dict[str, str]],
    evidence: list[SignalSummary],
    candidate_windows: list[dict[str, Any]],
    disallowed_resolved_tokens: tuple[str, ...],
    max_signals_per_window: int = 18,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for window in candidate_windows:
        start = _float_or_none(window.get("start"))
        end = _float_or_none(window.get("end"))
        if start is None or end is None:
            continue
        selected_queries = _select_window_queries(
            queries=queries,
            evidence=evidence,
            focus_signals=_as_string_list(window.get("focus_signals")),
            max_signals=max_signals_per_window,
        )
        summaries = [
            summarize_signal(
                h5_file=h5_file,
                index=index,
                requested_name=query["signal"],
                group=query.get("group", ""),
                reason=query.get("reason", ""),
                disallowed_resolved_tokens=disallowed_resolved_tokens,
                window_start=float(start),
                window_end=float(end),
                window_reason=str(window.get("reason") or ""),
            )
            for query in selected_queries
        ]
        result.append(
            {
                "id": window.get("id"),
                "start": _round_float(start),
                "end": _round_float(end),
                "reason": window.get("reason", ""),
                "focus_signals": window.get("focus_signals", []),
                "signal_evidence": [item.to_prompt_dict(compact=True) for item in summaries],
            }
        )
    return result


def _select_window_queries(
    *,
    queries: list[dict[str, str]],
    evidence: list[SignalSummary],
    focus_signals: list[str],
    max_signals: int,
) -> list[dict[str, str]]:
    query_by_key = {_normalize_name(query["signal"]): query for query in queries if query.get("signal")}
    evidence_by_key = _evidence_lookup(evidence)
    selected: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_signal(name: str) -> None:
        key = _normalize_name(name)
        if not key or key in seen:
            return
        query = query_by_key.get(key)
        if query is None:
            item = evidence_by_key.get(key)
            if item is not None:
                query = {"group": item.group or "context", "signal": item.requested_name, "reason": item.reason or "候选窗口关注信号"}
        if query is None:
            return
        selected.append(query)
        seen.add(_normalize_name(query["signal"]))

    for signal in focus_signals:
        add_signal(signal)
    for item in _changed_evidence_by_priority(evidence):
        add_signal(item.requested_name)
        if len(selected) >= max_signals:
            break
    group_counts = {"phenomenon": 0, "algorithm_request": 0, "algorithm_cause": 0, "actuator_response": 0}
    for query in selected:
        group = query.get("group", "")
        if group in group_counts:
            group_counts[group] += 1
    for query in queries:
        group = query.get("group", "")
        if group in group_counts and group_counts[group] == 0:
            add_signal(query["signal"])
            group_counts[group] += 1
        if len(selected) >= max_signals:
            break
    for query in queries:
        add_signal(query["signal"])
        if len(selected) >= max_signals:
            break
    return selected[:max_signals]


def _baseline_signal_queries(preset_context: dict[str, Any]) -> list[dict[str, str]]:
    limits = {
        "vehicle_signals": 4,
        "algorithm_request_signals": 5,
        "algorithm_cause_signals": 8,
        "actuator_response_signals": 6,
    }
    groups = {
        "vehicle_signals": "phenomenon",
        "algorithm_request_signals": "algorithm_request",
        "algorithm_cause_signals": "algorithm_cause",
        "actuator_response_signals": "actuator_response",
    }
    queries: list[dict[str, str]] = []
    for field_name, limit in limits.items():
        for signal in (preset_context.get(field_name) or [])[:limit]:
            queries.append(
                {
                    "group": groups[field_name],
                    "signal": str(signal),
                    "reason": "本地问题类型信号指南的最低证据基线",
                }
            )
    return queries


def _all_preset_signal_queries(preset_context: dict[str, Any]) -> list[dict[str, str]]:
    pairs = (
        ("phenomenon", "vehicle_signals"),
        ("algorithm_request", "algorithm_request_signals"),
        ("algorithm_cause", "algorithm_cause_signals"),
        ("actuator_response", "actuator_response_signals"),
    )
    queries: list[dict[str, str]] = []
    for group, field_name in pairs:
        for signal in preset_context.get(field_name) or []:
            queries.append({"group": group, "signal": str(signal), "reason": "来自本地问题类型信号指南"})
    return queries


def _fallback_plan(problem_type: str, problem_description: str, preset_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "analysis_focus": [
            "模型查询规划不可用，改用本地信号指南提供候选查询。",
            "最终结论仍由当前配置模型或兜底摘要根据证据生成。",
        ],
        "problem_type": problem_type,
        "problem_description": problem_description,
        "queries": [
            {"group": "phenomenon", "signals": preset_context.get("vehicle_signals", []), "why": "确认现象是否成立"},
            {
                "group": "algorithm_request",
                "signals": preset_context.get("algorithm_request_signals", []),
                "why": "确认算法请求是否主动变化",
            },
            {
                "group": "algorithm_cause",
                "signals": preset_context.get("algorithm_cause_signals", []),
                "why": "确认场景输入是否能解释算法请求",
            },
            {
                "group": "actuator_response",
                "signals": preset_context.get("actuator_response_signals", []),
                "why": "确认执行器响应是否跟随请求",
            },
        ],
    }


def _fallback_final(evidence: list[SignalSummary], raw_reply: str) -> dict[str, Any]:
    changed = [item for item in evidence if item.status == "ok" and item.changed]
    missing = [item.requested_name for item in evidence if item.status != "ok"]
    return {
        "headline": "模型最终结论未生成，本地仅保留 H5 证据摘要",
        "root_cause_factor": "模型结论未生成",
        "cause_side": "证据不足",
        "confidence": "低",
        "summary": f"根因：模型结论未生成；H5已读{len(evidence)}个候选信号，{len(changed)}个明显变化，{len(missing)}个缺失。",
        "event_window": _fallback_event_window(changed),
        "key_signals": [
            {
                "signal": item.requested_name,
                "role": item.group or "证据",
                "why": _change_reason_text(item),
            }
            for item in changed[:8]
        ],
        "evidence_chain": [
            f"{item.group}: {item.resolved_name or item.requested_name} {_change_reason_text(item)}"
            for item in changed[:8]
        ],
        "missing_evidence": missing[:12],
        "recommended_next_queries": [
            "重试模型最终结论生成。",
            "如仍超时，增大 BATCH_ROOT_CAUSE_DEEPSEEK_TIMEOUT_SECONDS 或缩短信号证据输入。",
        ],
        "llm_fallback_reason": raw_reply[:300],
    }


def _fallback_lateral_sudden_steering_final(evidence: list[SignalSummary], raw_reply: str) -> dict[str, Any] | None:
    actual = _best_lateral_evidence(evidence, ("strwhang", "strwhlangle", "steeringwheel"))
    request = _best_lateral_evidence(evidence, ("strgreq", "steeringrequest"))
    lane_items = [
        item
        for item in evidence
        if item.status == "ok"
        and any(token in _normalize_name(item.requested_name + item.resolved_name) for token in ("lanemodelc0", "lanemodelc1", "lanea0", "lanea1"))
    ]
    if actual is None:
        return None
    actual_delta = _summary_window_delta(actual)
    if actual_delta < 30.0:
        return None

    request_delta = _summary_window_delta(request) if request else 0.0
    request_ratio = request_delta / actual_delta if actual_delta else 0.0
    lane_triggered = [
        item
        for item in lane_items
        if _lateral_lane_triggered(item)
    ]
    missing = [item.requested_name for item in evidence if item.status != "ok"]
    key_items = [item for item in (actual, request, *lane_triggered[:4]) if item is not None]
    if request and request_ratio >= 0.3 and lane_triggered:
        factor = "车道几何突变触发转向请求猛打"
        headline = "LCC 横向猛打更像车道几何突变触发请求侧大幅转向"
        cause_side = "场景/感知目标"
        confidence = "高"
    elif request and request_ratio >= 0.3:
        factor = "转向请求侧突变"
        headline = "LCC 横向猛打更像请求侧突变，车道几何诱因未闭合"
        cause_side = "算法请求策略"
        confidence = "中"
    elif request:
        factor = "EPS/执行器响应异常"
        headline = "LCC 横向猛打更像请求平稳但实际方向盘角突变"
        cause_side = "执行器响应"
        confidence = "中"
    else:
        factor = "缺少转向请求证据"
        headline = "已检测到方向盘猛打，但缺少转向请求信号，不能区分请求侧和执行器侧"
        cause_side = "证据不足"
        confidence = "低"

    return {
        "headline": headline,
        "root_cause_factor": factor,
        "cause_side": cause_side,
        "confidence": confidence,
        "summary": f"根因：{factor}。",
        "event_window": _fallback_event_window([item for item in key_items if item.changed]),
        "key_signals": [
            {
                "signal": item.requested_name,
                "role": item.group or "证据",
                "why": _change_reason_text(item),
            }
            for item in key_items[:8]
        ],
        "evidence_chain": [
            f"{item.group}: {item.resolved_name or item.requested_name} {_change_reason_text(item)}"
            for item in key_items[:8]
        ],
        "missing_evidence": missing[:12],
        "recommended_next_queries": ["复核事件窗口内车道 C0/C1、转向请求和方向盘实际角的变化顺序是否一致。"],
        "llm_fallback_reason": raw_reply[:300],
    }


def _best_lateral_evidence(evidence: list[SignalSummary], tokens: tuple[str, ...]) -> SignalSummary | None:
    matches = [
        item
        for item in evidence
        if item.status == "ok"
        and any(token in _normalize_name(item.requested_name + item.resolved_name) for token in tokens)
    ]
    if not matches:
        return None
    return max(matches, key=_summary_window_delta)


def _summary_window_delta(item: SignalSummary | None) -> float:
    if item is None:
        return 0.0
    try:
        return abs(float(item.max_window_delta if item.max_window_delta is not None else item.max_delta or 0.0))
    except Exception:
        return 0.0


def _lateral_lane_triggered(item: SignalSummary) -> bool:
    value = _normalize_name(item.requested_name + item.resolved_name)
    delta = _summary_window_delta(item)
    if "c0" in value or "a0" in value:
        return delta > 0.3
    if "c1" in value or "a1" in value:
        return delta > 0.02
    return False


def _fallback_event_window(changed: list[SignalSummary]) -> dict[str, float | str | None]:
    times = [
        float(item.max_window_delta_time if item.max_window_delta_time is not None else item.max_delta_time)
        for item in changed
        if (
            (item.max_window_delta_time is not None and math.isfinite(float(item.max_window_delta_time)))
            or (item.max_delta_time is not None and math.isfinite(float(item.max_delta_time)))
        )
    ]
    if not times:
        return {"start": None, "end": None, "reason": "未能从变化信号中定位稳定时间窗"}
    return {
        "start": _round_float(max(0.0, min(times) - 1.0)),
        "end": _round_float(max(times) + 1.0),
        "reason": "根据候选信号最大跳变时间兜底定位",
    }


def _read_signal_values(h5_file: Path, ref: SignalRef) -> tuple[np.ndarray, np.ndarray]:
    with h5py.File(h5_file, "r") as h5:
        if ref.timestamp_path:
            timestamps = np.asarray(h5[ref.timestamp_path][:], dtype=float)
            values = np.asarray(h5[ref.dataset_path][:], dtype=float)
            return timestamps, values
        dataset = h5[ref.dataset_path]
        data = dataset[:]
        names = dataset.dtype.names or ()
        if ref.field_name not in names:
            raise KeyError(ref.field_name)
        if "timestamp" in names:
            timestamps = np.asarray(data["timestamp"], dtype=float)
        else:
            timestamps = np.arange(len(data), dtype=float)
        values = np.asarray(data[ref.field_name], dtype=float)
    return timestamps, values


def _elapsed_timestamps(timestamps: np.ndarray) -> np.ndarray:
    values = np.asarray(timestamps, dtype=float)
    finite = values[np.isfinite(values)]
    if len(finite) == 0:
        return values
    return values - float(finite[0])


def _max_window_delta(
    timestamps: np.ndarray,
    values: np.ndarray,
    *,
    window_s: float,
) -> tuple[float | None, float | None, float | None, float | None]:
    if len(values) < 2 or len(timestamps) != len(values):
        return None, None, None, None
    best_delta = -1.0
    best_time = None
    best_from = None
    best_to = None
    for start_index, start_time in enumerate(timestamps):
        end_index = int(np.searchsorted(timestamps, float(start_time) + window_s, side="right"))
        if end_index - start_index < 2:
            continue
        window = values[start_index:end_index]
        valid_mask = np.isfinite(window)
        if int(np.sum(valid_mask)) < 2:
            continue
        valid_indices = np.where(valid_mask)[0]
        valid_values = window[valid_mask]
        local_min_pos = int(valid_indices[int(np.nanargmin(valid_values))])
        local_max_pos = int(valid_indices[int(np.nanargmax(valid_values))])
        delta = float(abs(window[local_max_pos] - window[local_min_pos]))
        if delta <= best_delta:
            continue
        first_pos, second_pos = sorted((local_min_pos, local_max_pos))
        best_delta = delta
        best_time = float(timestamps[start_index + second_pos])
        best_from = float(window[first_pos])
        best_to = float(window[second_pos])
    if best_delta < 0:
        return None, None, None, None
    return best_delta, best_time, best_from, best_to


def _expanded_signal_names(name: str) -> list[str]:
    original = str(name or "").strip()
    names = [original]
    paren_match = re.match(r"^(?P<signal>.+?)\s*[（(](?P<message>.+?)[）)]\s*$", original)
    if paren_match:
        signal = paren_match.group("signal").strip()
        message = paren_match.group("message").strip()
        names.extend([f"{message}/{signal}", f"{message}.{signal}", signal])
    for alias in ROOT_CAUSE_SIGNAL_ALIASES.get(original, ()):
        names.append(alias)
    return [item for item in dict.fromkeys(names) if item]


def _name_variants(*names: str) -> list[str]:
    variants: list[str] = []
    for name in names:
        value = str(name or "").strip()
        if not value:
            continue
        parts = [value, value.rsplit(".", 1)[-1], value.rsplit("/", 1)[-1]]
        if "." in value:
            parts.append(value.replace(".", "/"))
        if "/" in value:
            parts.append(value.replace("/", "."))
        for part in parts:
            normalized = _normalize_name(part)
            if normalized:
                variants.append(normalized)
                variants.append(_strip_leading_i(normalized))
    return list(dict.fromkeys(item for item in variants if item))


def _message_aliases(message_name: str) -> list[str]:
    value = str(message_name or "").strip()
    aliases = [value]
    no_prefix = re.sub(r"^0x[0-9A-Fa-f]+_", "", value)
    aliases.append(no_prefix)
    aliases.append(re.sub(r"_0x[0-9A-Fa-f]+$", "", no_prefix))
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _normalize_name(value: str) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _strip_leading_i(value: str) -> str:
    return value[1:] if value.startswith("i") and len(value) > 1 else value


def _best_match(requested_name: str, matches: list[SignalRef]) -> SignalRef:
    requested_key = _normalize_name(requested_name)
    requested_variants = set(_name_variants(requested_name))
    for ref in matches:
        if (
            _normalize_name(ref.display_name) in requested_variants
            or _normalize_name(ref.dataset_path) in requested_variants
        ):
            return ref
    for ref in matches:
        if _normalize_name(ref.field_name) == requested_key:
            return ref
    stripped = _strip_leading_i(requested_key)
    for ref in matches:
        if _strip_leading_i(_normalize_name(ref.field_name)) == stripped:
            return ref
    return sorted(matches, key=lambda item: (len(item.dataset_path), item.display_name))[0]


def _active_segments(timestamps: np.ndarray, values: np.ndarray) -> list[dict[str, float]]:
    if len(values) < 2:
        return []
    baseline = float(values[0])
    span = float(np.nanmax(values) - np.nanmin(values))
    threshold = max(1e-6, span * 0.05)
    active = np.abs(values - baseline) >= threshold
    segments: list[dict[str, float]] = []
    start_index: int | None = None
    for index, is_active in enumerate(active):
        if is_active and start_index is None:
            start_index = index
        if (not is_active or index == len(active) - 1) and start_index is not None:
            end_index = index if is_active and index == len(active) - 1 else max(start_index, index - 1)
            segments.append(
                {
                    "start": _round_float(float(timestamps[start_index])),
                    "end": _round_float(float(timestamps[end_index])),
                    "min": _round_float(float(np.nanmin(values[start_index : end_index + 1]))),
                    "max": _round_float(float(np.nanmax(values[start_index : end_index + 1]))),
                }
            )
            start_index = None
    return segments[:4]


def _sample_points(timestamps: np.ndarray, values: np.ndarray, limit: int = 7) -> list[dict[str, float]]:
    if len(values) == 0:
        return []
    count = min(limit, len(values))
    indices = np.linspace(0, len(values) - 1, count, dtype=int)
    return [
        {"t": _round_float(float(timestamps[index])), "v": _round_float(float(values[index]))}
        for index in dict.fromkeys(int(item) for item in indices)
    ]


def _format_investigation_markdown(
    *,
    h5_file: Path,
    problem_type: str,
    problem_description: str,
    final_json: dict[str, Any],
    evidence: list[SignalSummary],
    key_signals_text: str = "",
    image_paths: list[Path] | None = None,
) -> str:
    lines = [
        f"模型调查式原因分析",
        f"",
        f"- 文件：{h5_file.name}",
            f"- 问题类型：{problem_type}",
    ]
    if problem_description:
        lines.append(f"- 问题描述：{problem_description}")
    lines.extend(
        [
            "",
            f"结论：{final_json.get('headline', '未生成结论')}",
            f"根因因素：{final_json.get('root_cause_factor', '') or '证据不足'}",
            f"方向：{final_json.get('cause_side', '证据不足')}；置信度：{final_json.get('confidence', '低')}",
            "",
            _compact_root_cause_summary(final_json, 120),
        ]
    )

    problem_time_hint = final_json.get("problem_time_hint")
    if isinstance(problem_time_hint, dict) and problem_time_hint.get("enabled"):
        lines.extend(
            [
                "",
                "表格问题时间定位：",
                (
                    f"- H5 起始时间：{problem_time_hint.get('h5_start_time', '')}；"
                    f"表格问题时间：{problem_time_hint.get('problem_datetime', '')}；"
                    f"H5 内目标时间：{problem_time_hint.get('target_elapsed_seconds')}s"
                ),
            ]
        )

    candidate_windows = final_json.get("candidate_windows")
    if isinstance(candidate_windows, list) and candidate_windows:
        lines.extend(["", "候选窗口："])
        for window in candidate_windows[:3]:
            if not isinstance(window, dict):
                continue
            start = _float_or_none(window.get("start"))
            end = _float_or_none(window.get("end"))
            time_text = f"{start:.2f}s - {end:.2f}s" if start is not None and end is not None else "未定位"
            reason = str(window.get("reason") or "").strip()
            priority = str(window.get("priority") or "").strip()
            prefix = f"- {window.get('id') or ''} {time_text}".strip()
            if priority:
                prefix += f"；优先级 {priority}"
            if reason:
                prefix += f"；{reason}"
            alignment = str(window.get("description_alignment") or "").strip()
            if alignment:
                prefix += f"；描述对齐：{alignment}"
            lines.append(prefix)

    window_analyses = final_json.get("window_analyses")
    if isinstance(window_analyses, list) and window_analyses:
        lines.extend(["", "候选窗口分析："])
        for item in window_analyses[:3]:
            if not isinstance(item, dict):
                continue
            start = _float_or_none(item.get("start"))
            end = _float_or_none(item.get("end"))
            time_text = f"{start:.2f}s - {end:.2f}s" if start is not None and end is not None else "未定位"
            factor = str(item.get("root_cause_factor") or "证据不足").strip()
            confidence = str(item.get("confidence") or "").strip()
            headline = str(item.get("headline") or "").strip()
            line = f"- {item.get('window_id') or ''} {time_text}：{factor}".strip()
            if confidence:
                line += f"（置信度：{confidence}）"
            if headline:
                line += f"；{headline}"
            lines.append(line)
            evidence_items = _as_string_list(item.get("evidence"))[:3]
            lines.extend(f"  - {text}" for text in evidence_items)

    alignment_level, alignment_note, matched_clues, uncertain_clues = _description_alignment_details(final_json)
    if alignment_level:
        lines.extend(["", "描述对齐：", f"- 匹配度：{alignment_level}"])
        if alignment_note:
            lines.append(f"- 选择依据：{alignment_note}")
        if matched_clues:
            lines.append("- 已对上线索：" + "；".join(matched_clues[:4]))
        if uncertain_clues:
            lines.append("- 未确认或冲突线索：" + "；".join(uncertain_clues[:4]))

    if key_signals_text:
        lines.extend(["", "关键分析信号：", key_signals_text])

    if image_paths:
        lines.extend(["", "分析信号图示："])
        for path in image_paths:
            lines.append(f"- {Path(path).name}")

    chain = _as_string_list(final_json.get("evidence_chain"))
    if chain:
        lines.extend(["", "证据链："])
        lines.extend(f"- {item}" for item in chain)

    missing = _as_string_list(final_json.get("missing_evidence"))
    if missing:
        lines.extend(["", "缺失或仍需补充："])
        lines.extend(f"- {item}" for item in missing)

    next_queries = _as_string_list(final_json.get("recommended_next_queries"))
    if next_queries:
        lines.extend(["", "下一步建议查询："])
        lines.extend(f"- {item}" for item in next_queries)

    changed = [item for item in evidence if item.status == "ok" and item.changed]
    if changed:
        lines.extend(["", "本地工具返回的主要变化信号："])
        for item in changed[:10]:
            lines.append(
                f"- {item.group} | {item.resolved_name}: "
                f"{_change_reason_text(item)}"
            )

    missing_signals = [item.requested_name for item in evidence if item.status != "ok"]
    if missing_signals:
        lines.extend(["", "未解析到的候选信号：", "- " + "、".join(missing_signals[:18])])

    return "\n".join(lines).strip()


def _change_reason_text(item: SignalSummary) -> str:
    window_delta = item.max_window_delta
    if window_delta is not None and math.isfinite(float(window_delta)):
        return (
            f"1s窗口变化 {_round_float(window_delta)} @ {_round_float(item.max_window_delta_time)}s "
            f"({_round_float(item.max_window_delta_from)} -> {_round_float(item.max_window_delta_to)})"
        )
    return (
        f"逐点最大跳变 {_round_float(item.max_delta)} @ {_round_float(item.max_delta_time)}s "
        f"({_round_float(item.max_delta_from)} -> {_round_float(item.max_delta_to)})"
    )


def _description_alignment_details(final_json: dict[str, Any]) -> tuple[str, str, list[str], list[str]]:
    alignment = final_json.get("description_alignment")
    if not isinstance(alignment, dict):
        return "", "", [], []
    level = str(alignment.get("match_level") or "").strip()
    note = str(alignment.get("selection_note") or "").strip()
    matched = _as_string_list(alignment.get("matched_clues"))
    uncertain = _as_string_list(alignment.get("mismatch_or_uncertain_clues"))
    return level, note, matched, uncertain


def _compact_root_cause_summary(final_json: dict[str, Any], limit: int = 80) -> str:
    alignment_level, alignment_note, _, uncertain_clues = _description_alignment_details(final_json)
    if alignment_level == "低":
        note = alignment_note or "；".join(uncertain_clues[:2]) or "需复核问题时间、描述和 H5 数据是否对应"
        return _compact_text(f"根因：证据不足。候选窗口与描述匹配度低：{note}", limit)
    factor = str(final_json.get("root_cause_factor") or "").strip()
    summary = str(final_json.get("summary") or "").strip()
    headline = str(final_json.get("headline") or "").strip()
    if factor:
        if factor != "证据不足":
            return _compact_text(f"根因：{factor}。", limit)
        detail = _remove_root_cause_prefix(summary or headline)
        if detail and factor not in detail:
            return _compact_text(f"根因：{factor}。{detail}", limit)
        return _compact_text(f"根因：{factor}。{detail or headline or '见关键证据信号'}", limit)
    if summary.startswith("根因："):
        return _compact_text(summary, limit)
    if headline:
        return _compact_text(f"根因：{headline}", limit)
    return "根因：证据不足。DeepSeek 未生成明确因素。"


def _remove_root_cause_prefix(value: str) -> str:
    text = str(value or "").strip()
    for prefix in ("根因：", "根因:", "结论：", "结论:"):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def _format_key_signal_text(final_json: dict[str, Any], evidence: list[SignalSummary], limit: int = 1200) -> str:
    evidence_by_name = _evidence_lookup(evidence)
    entries = _key_signal_entries(final_json)
    if not entries:
        entries = [
            {
                "signal": item.requested_name,
                "role": item.group or "证据",
                "why": _change_reason_text(item),
            }
            for item in _changed_evidence_by_priority(evidence)[:8]
        ]

    lines: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        signal = str(entry.get("signal") or "").strip()
        if not signal:
            continue
        item = evidence_by_name.get(_normalize_name(signal))
        display = item.resolved_name if item and item.resolved_name else signal
        key = _normalize_name(display)
        if key in seen:
            continue
        seen.add(key)
        role = str(entry.get("role") or (item.group if item else "") or "证据").strip()
        why = str(entry.get("why") or "").strip()
        if item and not why:
            why = _change_reason_text(item)
        line = f"- {role}：{display}"
        if why:
            line += f"；{why}"
        lines.append(line)
        if len(lines) >= 8:
            break
    return _clip_multiline("\n".join(lines), limit)


def build_deepseek_signal_plots(
    *,
    h5_file: str | Path,
    evidence: list[SignalSummary],
    final_json: dict[str, Any],
    image_root: str | Path | None,
    max_signals: int = 8,
    signals_per_image: int = 8,
    max_windows: int = 3,
) -> list[Path]:
    if image_root is None:
        return []
    source = Path(h5_file)
    if not source.exists():
        return []
    selected = _select_plot_evidence(final_json, evidence, max_signals=max_signals)
    if not selected:
        return []

    output_root = Path(image_root)
    output_root.mkdir(parents=True, exist_ok=True)
    image_paths: list[Path] = []
    plot_windows = _plot_windows_from_final(final_json, max_windows=max_windows)
    multiple_windows = len(plot_windows) > 1
    for window_index, window in enumerate(plot_windows, start=1):
        window_json = dict(final_json)
        window_json["event_window"] = window
        event_start, event_end, plot_start, plot_end = _deepseek_plot_window(window_json, selected)
        for chunk_index, chunk in enumerate(_chunks(selected, max(1, signals_per_image)), start=1):
            series = []
            for item in chunk:
                if not item.dataset_path or not item.field_name:
                    continue
                try:
                    timestamps, values = _read_signal_values(
                        source,
                        SignalRef(item.dataset_path, item.field_name, item.timestamp_path),
                    )
                    timestamps = _elapsed_timestamps(timestamps)
                    point_times, point_values = _windowed_series(timestamps, values, plot_start, plot_end)
                except Exception:
                    continue
                if len(point_times) < 2:
                    continue
                series.append(
                    {
                        "label": item.resolved_name or item.requested_name,
                        "role": item.group or "证据",
                        "times": point_times,
                        "values": point_values,
                        "change_time": item.max_window_delta_time or item.max_delta_time,
                    }
                )
            if not series:
                continue
            if multiple_windows:
                output_path = output_root / f"deepseek_key_signals_w{window_index:02d}_{chunk_index:02d}.png"
            else:
                output_path = output_root / f"deepseek_key_signals_{chunk_index:02d}.png"
            image = _render_deepseek_plot(
                file_name=source.name,
                problem_factor=str(final_json.get("root_cause_factor") or "证据不足"),
                summary=_compact_root_cause_summary(final_json, 120),
                event_start=event_start,
                event_end=event_end,
                plot_start=plot_start,
                plot_end=plot_end,
                series=series,
            )
            image.save(output_path)
            image_paths.append(output_path)
    return image_paths


def _plot_windows_from_final(final_json: dict[str, Any], *, max_windows: int) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for item in final_json.get("candidate_windows") or []:
        if not isinstance(item, dict):
            continue
        start = _float_or_none(item.get("start"))
        end = _float_or_none(item.get("end"))
        if start is None or end is None:
            continue
        windows.append({"start": start, "end": end, "reason": item.get("reason", "")})
        if len(windows) >= max_windows:
            break
    if windows:
        return windows
    event = final_json.get("event_window") if isinstance(final_json.get("event_window"), dict) else {}
    if event:
        return [event]
    return [{}]


def _key_signal_entries(final_json: dict[str, Any]) -> list[dict[str, Any]]:
    value = final_json.get("key_signals")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _evidence_lookup(evidence: list[SignalSummary]) -> dict[str, SignalSummary]:
    lookup: dict[str, SignalSummary] = {}
    for item in evidence:
        for name in (item.requested_name, item.resolved_name, item.field_name):
            key = _normalize_name(name)
            if key:
                lookup[key] = item
    return lookup


def _select_plot_evidence(
    final_json: dict[str, Any],
    evidence: list[SignalSummary],
    *,
    max_signals: int,
) -> list[SignalSummary]:
    lookup = _evidence_lookup(evidence)
    selected: list[SignalSummary] = []
    seen: set[str] = set()
    for entry in _key_signal_entries(final_json):
        signal = str(entry.get("signal") or "")
        item = lookup.get(_normalize_name(signal))
        if item and item.status == "ok" and item.dataset_path:
            key = f"{item.dataset_path}.{item.field_name}"
            if key not in seen:
                selected.append(item)
                seen.add(key)
        if len(selected) >= max_signals:
            return selected

    for item in _changed_evidence_by_priority(evidence):
        if item.status != "ok" or not item.dataset_path:
            continue
        key = f"{item.dataset_path}.{item.field_name}"
        if key in seen:
            continue
        selected.append(item)
        seen.add(key)
        if len(selected) >= max_signals:
            break
    return selected


def _changed_evidence_by_priority(evidence: list[SignalSummary]) -> list[SignalSummary]:
    group_order = {
        "phenomenon": 0,
        "algorithm_request": 1,
        "algorithm_cause": 2,
        "actuator_response": 3,
        "context": 4,
    }
    changed = [item for item in evidence if item.status == "ok" and item.changed]
    return sorted(
        changed,
        key=lambda item: (
            group_order.get(item.group, 9),
            -(abs(float(item.max_window_delta or item.max_delta or 0.0))),
            item.requested_name,
        ),
    )


def _deepseek_plot_window(
    final_json: dict[str, Any],
    selected: list[SignalSummary],
) -> tuple[float, float, float, float]:
    event = final_json.get("event_window") if isinstance(final_json.get("event_window"), dict) else {}
    start = _float_or_none(event.get("start")) if isinstance(event, dict) else None
    end = _float_or_none(event.get("end")) if isinstance(event, dict) else None
    if start is None or end is None or end <= start:
        times = [
            float(item.max_window_delta_time if item.max_window_delta_time is not None else item.max_delta_time)
            for item in selected
            if (
                (item.max_window_delta_time is not None and math.isfinite(float(item.max_window_delta_time)))
                or (item.max_delta_time is not None and math.isfinite(float(item.max_delta_time)))
            )
        ]
        if times:
            start = max(0.0, min(times) - 0.8)
            end = max(times) + 0.8
    if start is None or end is None or end <= start:
        starts = [float(item.time_start) for item in selected if item.time_start is not None]
        ends = [float(item.time_end) for item in selected if item.time_end is not None]
        start = min(starts) if starts else 0.0
        end = max(ends) if ends else start + 1.0
    if end <= start:
        end = start + 1.0
    plot_start = max(0.0, start - 5.0)
    plot_end = max(end + 5.0, plot_start + 1.0)
    return float(start), float(end), float(plot_start), float(plot_end)


def _windowed_series(
    timestamps: np.ndarray,
    values: np.ndarray,
    plot_start: float,
    plot_end: float,
    limit: int = 900,
) -> tuple[list[float], list[float]]:
    valid = np.isfinite(timestamps) & np.isfinite(values)
    timestamps = timestamps[valid]
    values = values[valid]
    mask = (timestamps >= plot_start) & (timestamps <= plot_end)
    timestamps = timestamps[mask]
    values = values[mask]
    if len(timestamps) > limit:
        indices = np.linspace(0, len(timestamps) - 1, limit, dtype=int)
        timestamps = timestamps[indices]
        values = values[indices]
    return [float(item) for item in timestamps], [float(item) for item in values]


def _render_deepseek_plot(
    *,
    file_name: str,
    problem_factor: str,
    summary: str,
    event_start: float,
    event_end: float,
    plot_start: float,
    plot_end: float,
    series: list[dict[str, Any]],
) -> Image.Image:
    width = 1600
    left = 120
    right = 40
    top = 92
    bottom = 44
    panel_height = 150
    plot_height = 92
    height = top + bottom + panel_height * len(series)
    background = (255, 255, 255)
    grid = (232, 236, 240)
    axis = (120, 130, 145)
    text = (30, 41, 59)
    subtext = (90, 103, 120)
    event_fill = (255, 220, 220)
    event_line = (220, 38, 38)
    palette = [(37, 99, 235), (5, 150, 105), (202, 138, 4), (219, 39, 119), (124, 58, 237)]
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    title_font = _load_plot_font(24)
    font = _load_plot_font(16)
    small_font = _load_plot_font(13)
    plot_left = left
    plot_right = width - right
    plot_width = plot_right - plot_left
    time_span = max(1e-6, plot_end - plot_start)

    _draw_text(draw, (left, 18), f"DeepSeek关键证据信号｜根因：{problem_factor or '证据不足'}", text, title_font)
    _draw_text(draw, (left, 48), _compact_text(summary, 120), subtext, font)
    _draw_text(draw, (left, 68), f"file={file_name} | window={event_start:.2f}s-{event_end:.2f}s", subtext, small_font)

    def map_x(value: float) -> float:
        return plot_left + (value - plot_start) / time_span * plot_width

    for panel_index, item in enumerate(series):
        panel_top = top + panel_index * panel_height
        plot_top = panel_top + 28
        plot_bottom = plot_top + plot_height
        values = [float(value) for value in item["values"]]
        times = [float(value) for value in item["times"]]
        y_min = min(values)
        y_max = max(values)
        if abs(y_max - y_min) < 1e-9:
            padding = max(1.0, abs(y_max) * 0.05)
            y_min -= padding
            y_max += padding
        else:
            padding = (y_max - y_min) * 0.08
            y_min -= padding
            y_max += padding
        y_span = max(1e-6, y_max - y_min)

        def map_y(value: float) -> float:
            return plot_top + (y_max - value) / y_span * plot_height

        label = f"{item.get('role') or '证据'} | {_short_plot_label(str(item.get('label') or 'signal'))}"
        _draw_text(draw, (plot_left, panel_top + 4), label, text, font)
        for tick in range(4):
            y_value = y_min + (y_max - y_min) * tick / 3.0
            y = map_y(y_value)
            draw.line((plot_left, y, plot_right, y), fill=grid, width=1)
            _draw_text(draw, (14, y - 8), f"{y_value:.3g}", subtext, small_font)
        for tick in range(6):
            t = plot_start + (plot_end - plot_start) * tick / 5.0
            x = map_x(t)
            draw.line((x, plot_top, x, plot_bottom), fill=(244, 246, 248), width=1)
            _draw_text(draw, (x - 18, plot_bottom + 5), f"{t:.1f}s", subtext, small_font)
        x1 = map_x(max(event_start, plot_start))
        x2 = map_x(min(event_end, plot_end))
        if x2 >= x1:
            draw.rectangle((x1, plot_top, max(x2, x1 + 2), plot_bottom), fill=event_fill)
            draw.line((x1, plot_top, x1, plot_bottom), fill=event_line, width=2)
            draw.line((x2, plot_top, x2, plot_bottom), fill=event_line, width=2)
        draw.line((plot_left, plot_bottom, plot_right, plot_bottom), fill=axis, width=1)
        draw.line((plot_left, plot_top, plot_left, plot_bottom), fill=axis, width=1)

        points = [(map_x(t), map_y(v)) for t, v in zip(times, values)]
        if len(points) >= 2:
            draw.line(points, fill=palette[panel_index % len(palette)], width=2)
        if item.get("change_time") is not None:
            change_time = float(item["change_time"])
            if plot_start <= change_time <= plot_end:
                x = map_x(change_time)
                draw.line((x, plot_top, x, plot_bottom), fill=(245, 158, 11), width=1)

    return image


def _load_plot_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        try:
            if path.exists():
                return ImageFont.truetype(str(path), size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], value: str, fill: tuple[int, int, int], font: ImageFont.ImageFont) -> None:
    try:
        draw.text(xy, value, fill=fill, font=font)
    except UnicodeEncodeError:
        draw.text(xy, value.encode("ascii", errors="ignore").decode("ascii"), fill=fill, font=font)


def _short_plot_label(value: str, limit: int = 128) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return "..." + text[-(limit - 3) :]


def _chunks(values: list[SignalSummary], size: int) -> list[list[SignalSummary]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _compact_text(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _clip_multiline(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _build_context(
    plan: dict[str, Any],
    evidence: list[SignalSummary],
    final_json: dict[str, Any],
    *,
    problem_time_hint: dict[str, Any] | None = None,
    description_clues: dict[str, Any] | None = None,
    candidate_windows: list[dict[str, Any]] | None = None,
    window_evidence: list[dict[str, Any]] | None = None,
    compact_evidence: dict[str, Any] | None = None,
) -> str:
    return json.dumps(
        {
            "plan": plan,
            "compact_evidence": compact_evidence or {},
            "evidence": [item.to_prompt_dict() for item in evidence],
            "problem_time_hint": problem_time_hint or {},
            "problem_description_clues": description_clues or {},
            "candidate_windows": candidate_windows or [],
            "window_evidence": window_evidence or [],
            "final": final_json,
        },
        ensure_ascii=False,
        indent=2,
    )


def _status_from_final(final_json: dict[str, Any], evidence: list[SignalSummary]) -> str:
    alignment_level, _, _, _ = _description_alignment_details(final_json)
    if alignment_level == "低":
        return "error"
    if str(final_json.get("root_cause_factor") or "") in {"DeepSeek结论未生成", "模型结论未生成"}:
        return "error"
    if str(final_json.get("cause_side") or "") == "证据不足":
        return "ok" if any(item.status == "ok" for item in evidence) else "error"
    return "issue"


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _round_float(value: Any, digits: int = 4) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)
