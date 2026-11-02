from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from actuator_handshake import actuator_issue_rows
from performance_analysis import (
    DEFAULT_PROBLEM_GROUPS,
    _read_xlsx_rows,
    add_can_code_to_path,
    clean_preset_signal_name,
    infer_performance_problem_preset_keys,
    load_performance_problem_presets,
    metric_title,
    normalize_problem_groups,
    split_preset_cell,
)
from runtime_paths import ensure_writable_dir


ROOT = Path(__file__).resolve().parent
VEHICLE_RESPONSE_SIGNAL_CATALOG_PATH = ROOT / "config" / "vehicle_response_signal_catalog.xlsx"
CATALOG_MATCHED_STATUS = "\u5df2\u5339\u914d"
ROOT_CAUSE_DEFAULT_SIGNAL_VALUES: dict[str, float] = {
    "FLT1_CUTIN_Flag": 0.0,
}
CUTIN_TARGET_SIGNAL_KEYS: frozenset[str] = frozenset(
    {
        "CutInTargetId",
        "CutInTargetDistance",
        "CutInTargetVelocity",
        "CutInTargetAccel",
        "CutInTargetTTC",
    }
)

ROOT_CAUSE_PROBLEM_SCOPES: tuple[dict[str, Any], ...] = (
    {
        "label": "顿挫/抖动",
        "terms": ("顿挫", "抖动", "颠簸", "jerk", "jerkiness"),
        "groups": ("acc_control",),
    },
    {
        "label": "重制动/急减速",
        "terms": ("重制动", "重刹", "急减速", "急刹", "heavy braking"),
        "groups": ("acc_control",),
    },
    {
        "label": "不合理低速",
        "terms": ("不合理低速", "低速异常", "速度太低", "车速低"),
        "groups": ("acc_control",),
    },
    {
        "label": "误制动",
        "terms": ("误制动", "误刹", "不该刹", "错误制动", "false braking"),
        "groups": ("acc_control",),
    },
    {
        "label": "漏制动",
        "terms": ("漏制动", "没制动", "未制动", "没有制动", "制动没有响应", "制动没响应"),
        "groups": ("acc_control",),
    },
    {
        "label": "未达到 set speed",
        "terms": ("setspeed", "set speed", "设定车速", "设速", "未达到设速", "达不到目标车速"),
        "groups": ("acc_control",),
    },
    {
        "label": "溜车",
        "terms": ("溜车", "rollback", "后溜"),
        "groups": ("acc_control",),
    },
    {
        "label": "跟车距离异常",
        "terms": ("跟车距离", "车距异常", "距离异常", "时距异常", "headway", "following distance"),
        "groups": ("acc_control",),
    },
)

VEHICLE_RESPONSE_SIGNALS = (
    "IMU_Yaw_Long_Acc_037_M2.IIMULonAccSec",
    "IMU_Yaw_Long_Acc_037_M2.IIMULonAcc_adas",
    "PPEI_Vehicle_Speed_and_Distance_209_M1.VehSpdAvgDrvn_1",
    "PPEI_Vehicle_Speed_and_Distance_208_M2.VehSpdAvgNDrvn",
    "Adaptive_Cruise_Disp_Stat.IACCAct376",
    "PPEI_Propulsion_General_Data_1_0C2_M.IAccPdlOvrrdAtv",
    "VehicleStandstillFlag",
)
ACC_PROPULSION_REQUEST_SIGNALS = (
    "PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M.ACCATC_ACCAct",
    "PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M.ACCATC_ACCTyp",
    "PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M.ACCATC_AxlTrqRq",
    "PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M.ACCATC_DrvAstdGoSt",
)
ACC_PROPULSION_RESPONSE_SIGNALS = (
    "PPEI_Propulsion_General_Data_1_0C2_M.ACCATCS_RqStat",
    "PPEI_Propulsion_General_Data_1_0C2_M.ACCATCS_LmtgStat",
    "PPEI_Engine_Torque_Status_4_08D_M.ActAxleTrq",
    "PPEI_Engine_Torque_Status_4_08D_M.ActAxleTrqV",
)
ACC_BRAKE_REQUEST_SIGNALS = (
    "ADAS_Command_Ext1.ACCBSCE_ACCAct1",
    "ADAS_Command_Ext1.ACCBSCE_AutBrkTp1",
    "ADAS_Command_Ext1.ACCBSCE_ACCAccl1",
    "ADAS_Command_Ext1.ACCBSCE_ACCTrq1",
)
AEB_BRAKE_REQUEST_SIGNALS = (
    "ADAS_Command_Ext1.FCACBSC_AutBrkRqTp1",
    "ADAS_Command_Ext1.FCACBSC_CtrlTrq1",
)
BRAKE_RESPONSE_SIGNALS = (
    "PPEI_Chassis_General_Data_2_191_M.AutoBrkngAvlbl",
    "PPEI_Chassis_General_Data_2_191_M.AutoBrkngAct",
    "PPEI_Chassis_General_Data_3_0AC_M.ACCBrkngAct",
    "Automatic_Braking_Status_044_M.BrkSysAutBrkStat",
    "PPEI_Chassis_General_Data_2_191_M.BrkSysAutBrkFld",
    "Automatic_Braking_Status_044_M.FSRACCBrkngReqDenied",
    "PPEI_Chassis_General_Status_2.ICSTBATS_TrqVl",
    "IRegenBrkAxTrqAch",
)
ALGORITHM_CAUSE_SIGNAL_FALLBACKS = (
    "LeadTargetId",
    "LeadTargetDistance",
    "LeadTargetVelocity",
    "LeadTargetAccel",
    "LeadTargetTTC",
    "CutInTargetId",
    "CutInTargetDistance",
    "CutInTargetVelocity",
    "CutInTargetAccel",
    "CutInTargetTTC",
    "CurvatureLimitSpeed",
    "Curvature",
    "CurvatureLimitEnable",
    "IACCDrvrSeltdSpd",
    "HeadwaySetting",
)

ROOT_CAUSE_KEY_SIGNAL_PRIORITIES: dict[str, tuple[str, ...]] = {
    "phenomenon": (
        "iimulongaccsec",
        "iimulongacc_adas",
        "ivehspdavgdrvn",
        "vehspdavgdrvn",
        "ivehspdavgndr",
        "iaccact376",
    ),
    "request": (
        "iaccbsce_accaccl1",
        "accbsce_accaccl1",
        "iaccbsce_acctrq1",
        "accbsce_acctrq1",
        "iaccbsce_accact1",
        "accbsce_accact1",
        "ifcacbsc_ctrltrq1",
        "fcacbsc_ctrltrq1",
        "iaccatc_axltrqrq",
        "accatc_axltrqrq",
        "iaccatc_accact",
        "accatc_accact",
        "iaccatc_acctyp",
        "accatc_acctyp",
    ),
    "response": (
        "iaccbrkngact",
        "accbrkngact",
        "ibrksysautbrkstat",
        "brksysautbrkstat",
        "iautobrkngact",
        "autobrkngact",
        "iregenbrkaxtrqach",
        "iactaxletrq",
        "actaxletrq",
        "iaccatcs_rqstat",
        "accatcs_rqstat",
        "iaccatcs_lmtgstat",
        "accatcs_lmtgstat",
    ),
    "algorithm_cause": (
        "flt1_long_range",
        "flt1_long_velocity",
        "flt1_long_accel",
        "itrflgtmidblbclr",
        "debug_uint_reserved_48",
        "debug_uint_reserved_49",
        "iaccdrvrseltdspd",
        "iacchdwystg",
        "flt1_cutin_flag",
    ),
}
ROOT_CAUSE_KEY_SIGNAL_LIMITS: dict[str, int] = {
    "phenomenon": 2,
    "request": 2,
    "response": 2,
    "algorithm_cause": 4,
}
ROOT_CAUSE_SIGNAL_ALIASES: dict[str, tuple[str, ...]] = {
    "VehicleStandstillFlag": (
        "Automatic_Braking_Status_044_M.IVehMvngStat",
        "IVehMvngStat",
        "VehMvngStat",
    ),
    "FLT1_CUTIN_Flag": ("FLT1_CUTIN_Flag",),
    "LeadTargetId": ("FLT1_Global_ID",),
    "LeadTargetDistance": ("FLT1_Long_Range",),
    "LeadTargetVelocity": ("FLT1_Long_Velocity",),
    "LeadTargetAccel": ("FLT1_Long_Accel",),
    "CutInTargetId": ("FLT1_Global_ID",),
    "CutInTargetDistance": ("FLT1_Long_Range",),
    "CutInTargetVelocity": ("FLT1_Long_Velocity",),
    "CutInTargetAccel": ("FLT1_Long_Accel",),
    "CutInTargetTTC": (),
    "CurvatureLimitSpeed": ("Debug_uint_Reserved_48",),
    "CurvatureLimitEnable": ("Debug_uint_Reserved_49",),
    "Curvature": (),
    "CurvatureLeftC2": (
        "ch4/Eth_Debug_CAN5/PATAC_Acore_Debug_Msg_v0p1p6_Release_0709/0xB00_Left_Lane_Track_Coeff/signals/LaneModelC2",
    ),
    "CurvatureRightC2": (
        "ch4/Eth_Debug_CAN5/PATAC_Acore_Debug_Msg_v0p1p6_Release_0709/0xB01_Right_Lane_Track_Coeff/signals/LaneModelC2",
    ),
    "RoadSlope": (
        "Eth_Debug_CAN5/MPC_DebugMsg_Reserved_18.Debug_single_Reserved_51",
        "MPC_DebugMsg_Reserved_18.Debug_single_Reserved_51",
        "Debug_single_Reserved_51",
    ),
    "HeadwaySetting": ("Adaptive_Cruise_Disp_Stat.IACCHdwyStg", "IACCHdwyStg", "ACCHdwyStg"),
    "ACCHdwyStg": ("Adaptive_Cruise_Disp_Stat.IACCHdwyStg", "IACCHdwyStg", "ACCHdwyStg"),
    "ACC功能开启标志位": ("IACCAct376",),
    "ACC功能 Override 标志位": ("IAccPdlOvrrdAtv",),
    "前车距离": ("FLT1_Long_Range",),
    "相对速度": ("FLT1_Long_Velocity",),
    "TTC": ("AEB_Vehicle_TTC", "Debug_TTC", "Obstacle_TTC"),
}
ROOT_CAUSE_SIGNAL_NOTES: dict[str, str] = {
    "LeadTargetTTC": "FLT1 TTC 由 FLT1_Long_Range / FLT1_Long_Velocity 计算；FLT1_CUTIN_Flag=0 时表示跟车目标",
    "CutInTargetId": "FLT1_CUTIN_Flag=1 时 FLT1_Global_ID 表示 Cut-in 目标；flag=0 时表示跟车目标",
    "CutInTargetDistance": "FLT1_CUTIN_Flag=1 时 FLT1_Long_Range 表示 Cut-in 目标距离；flag=0 时表示跟车目标",
    "CutInTargetVelocity": "FLT1_CUTIN_Flag=1 时 FLT1_Long_Velocity 表示 Cut-in 目标相对速度；flag=0 时表示跟车目标",
    "CutInTargetAccel": "FLT1_CUTIN_Flag=1 时 FLT1_Long_Accel 表示 Cut-in 目标加速度；flag=0 时表示跟车目标",
    "CutInTargetTTC": "Cut-in TTC 与跟车 TTC 同源，基于 FLT1 距离/相对速度计算，并由 FLT1_CUTIN_Flag 判定目标语义",
    "CurvatureLimitSpeed": "已按用户确认使用 Debug_uint_Reserved_48 作为曲率限速值",
    "Curvature": "由 Left_Lane_Track_Coeff.LaneModelC2 和 Right_Lane_Track_Coeff.LaneModelC2 计算：1/(left+right)，分母为 0 或接近 0 时跳过",
    "CurvatureLimitEnable": "已按用户确认使用 Debug_uint_Reserved_49 作为曲率限速使能标志",
    "VehicleStandstillFlag": "IVehMvngStat：0 表示 moving，1 表示 stationary。",
    "RoadSlope": "Debug_single_Reserved_51：坡度角，单位 deg。",
}

# Bridge stable internal placeholders to the editable signal catalog. The catalog
# owns concrete signal names and readiness state; this map only keeps old preset
# keys compatible with human-maintained rows.
CATALOG_KEY_TO_SEMANTIC: dict[str, tuple[str, ...]] = {
    "VehicleStandstillFlag": ("车辆静止标志",),
    "LeadTargetId": ("跟车目标 ID",),
    "LeadTargetDistance": ("跟车目标信息（位置，速度，加速度，TTC）",),
    "LeadTargetVelocity": ("跟车目标信息（位置，速度，加速度，TTC）",),
    "LeadTargetAccel": ("跟车目标信息（位置，速度，加速度，TTC）",),
    "LeadTargetTTC": ("跟车目标信息（位置，速度，加速度，TTC）",),
    "CutInTargetId": ("Cut-in 目标 ID",),
    "CutInTargetDistance": ("Cut-in 目标信息（位置，速度，加速度，TTC）",),
    "CutInTargetVelocity": ("Cut-in 目标信息（位置，速度，加速度，TTC）",),
    "CutInTargetAccel": ("Cut-in 目标信息（位置，速度，加速度，TTC）",),
    "CutInTargetTTC": ("Cut-in 目标信息（位置，速度，加速度，TTC）",),
    "RoadSlope": ("坡度",),
    "IACCDrvrSeltdSpd": ("设定车速",),
    "HeadwaySetting": ("跟车时距设置",),
    "ACCHdwyStg": ("跟车时距设置",),
    "CurvatureLimitSpeed": ("曲率限速值",),
    "Curvature": ("曲率",),
    "CurvatureLimitEnable": ("曲率限速使能标志",),
}

CATALOG_USE_ROW_SIGNALS: frozenset[str] = frozenset(
    {
        "VehicleStandstillFlag",
        "LeadTargetId",
        "LeadTargetDistance",
        "LeadTargetVelocity",
        "LeadTargetAccel",
        "CutInTargetId",
        "CutInTargetDistance",
        "CutInTargetVelocity",
        "CutInTargetAccel",
        "RoadSlope",
        "IACCDrvrSeltdSpd",
        "HeadwaySetting",
        "ACCHdwyStg",
        "CurvatureLimitSpeed",
        "CurvatureLimitEnable",
    }
)


@dataclass(frozen=True)
class VehicleResponseSignalCatalog:
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    known_keys: frozenset[str] = frozenset()
    confirmed_keys: frozenset[str] = frozenset()
    blocked_keys: frozenset[str] = frozenset()
    notes: dict[str, str] = field(default_factory=dict)


_VEHICLE_RESPONSE_SIGNAL_CATALOG_CACHE: VehicleResponseSignalCatalog | None = None


def _load_vehicle_response_signal_catalog() -> VehicleResponseSignalCatalog:
    global _VEHICLE_RESPONSE_SIGNAL_CATALOG_CACHE
    if _VEHICLE_RESPONSE_SIGNAL_CATALOG_CACHE is not None:
        return _VEHICLE_RESPONSE_SIGNAL_CATALOG_CACHE

    semantic_to_keys: dict[str, list[str]] = {}
    for key, semantics in CATALOG_KEY_TO_SEMANTIC.items():
        for semantic in semantics:
            semantic_to_keys.setdefault(semantic, []).append(key)

    aliases: dict[str, tuple[str, ...]] = {}
    notes: dict[str, str] = {}
    known_keys: set[str] = set()
    confirmed_keys: set[str] = set()
    blocked_keys: set[str] = set()

    if not VEHICLE_RESPONSE_SIGNAL_CATALOG_PATH.exists():
        _VEHICLE_RESPONSE_SIGNAL_CATALOG_CACHE = VehicleResponseSignalCatalog()
        return _VEHICLE_RESPONSE_SIGNAL_CATALOG_CACHE

    try:
        rows = _read_xlsx_rows(VEHICLE_RESPONSE_SIGNAL_CATALOG_PATH)
    except Exception:
        _VEHICLE_RESPONSE_SIGNAL_CATALOG_CACHE = VehicleResponseSignalCatalog()
        return _VEHICLE_RESPONSE_SIGNAL_CATALOG_CACHE

    header = _catalog_header_map(rows[0]) if rows else {}
    for values in rows[1:]:
        semantic = _catalog_row_value(values, header, ("信号语义", "signal_semantic"), 0)
        if not semantic:
            continue
        signal_values = _catalog_signal_values(
            _catalog_row_value(values, header, ("具体信号名", "signal_name"), 1)
        )
        path_values = _catalog_signal_values(
            _catalog_row_value(values, header, ("H5数据集路径", "h5_path", "dataset_path"), None)
        )
        signals = tuple(dict.fromkeys((*signal_values, *path_values)))
        status = _catalog_row_value(values, header, ("当前状态", "status"), 4)
        note = _catalog_row_value(values, header, ("备注", "note"), 5)
        keys = semantic_to_keys.get(semantic, [])
        for key in keys:
            known_keys.add(key)
            if note:
                notes[key] = note
            if status == CATALOG_MATCHED_STATUS and signals:
                confirmed_keys.add(key)
                if key in CATALOG_USE_ROW_SIGNALS:
                    aliases[key] = _catalog_signals_for_key(key, signals)
            else:
                blocked_keys.add(key)

    _VEHICLE_RESPONSE_SIGNAL_CATALOG_CACHE = VehicleResponseSignalCatalog(
        aliases={key: tuple(value) for key, value in aliases.items()},
        known_keys=frozenset(known_keys),
        confirmed_keys=frozenset(confirmed_keys),
        blocked_keys=frozenset(blocked_keys),
        notes=notes,
    )
    return _VEHICLE_RESPONSE_SIGNAL_CATALOG_CACHE


def _row_value(values: list[str], index: int) -> str:
    if index >= len(values):
        return ""
    return str(values[index] or "").strip()


def _catalog_header_map(values: list[str]) -> dict[str, int]:
    return {str(value or "").strip(): index for index, value in enumerate(values) if str(value or "").strip()}


def _catalog_row_value(
    values: list[str],
    header: dict[str, int],
    names: tuple[str, ...],
    fallback_index: int | None,
) -> str:
    index = _catalog_header_index(header, names)
    if index is not None:
        return _row_value(values, index)
    if fallback_index is None:
        return ""
    return _row_value(values, fallback_index)


def _catalog_header_index(header: dict[str, int], names: tuple[str, ...]) -> int | None:
    for name in names:
        index = header.get(name)
        if index is not None:
            return index

    lowered_names = {name.lower() for name in names}
    if {"h5_path", "dataset_path"} & lowered_names:
        for label, index in header.items():
            normalized = label.lower()
            if "h5" in normalized:
                return index
    return None


def _catalog_signal_values(value: str) -> tuple[str, ...]:
    signals: list[str] = []
    for item in split_preset_cell(value):
        token = item.strip()
        if not token:
            continue
        if "computed" in token.lower() or "计算" in token or "由" in token:
            continue
        signals.append(_clean_signal_candidate(token))
    return tuple(dict.fromkeys(signal for signal in signals if signal))


def _catalog_signals_for_key(key: str, signals: tuple[str, ...]) -> tuple[str, ...]:
    key_aliases = ROOT_CAUSE_SIGNAL_ALIASES.get(key, ())
    if not key_aliases:
        return signals

    alias_values: set[str] = set()
    alias_shorts: set[str] = set()
    for alias in key_aliases:
        for alias_value in _path_signal_aliases(_clean_signal_candidate(alias)):
            lowered = alias_value.lower()
            alias_values.add(lowered)
            alias_shorts.add(lowered.rsplit(".", 1)[-1].rsplit("/", 1)[-1])
    matched: list[str] = []
    for signal in signals:
        cleaned = _clean_signal_candidate(signal)
        signal_aliases = {alias.lower() for alias in _path_signal_aliases(cleaned)}
        signal_shorts = {alias.rsplit(".", 1)[-1].rsplit("/", 1)[-1] for alias in signal_aliases}
        if signal_aliases & alias_values or signal_shorts & alias_shorts:
            matched.append(cleaned)
    return tuple(dict.fromkeys(matched)) or signals


def _catalog_aliases_for(name: str) -> tuple[str, ...]:
    value = _clean_signal_candidate(name)
    if not value:
        return ()
    catalog = _load_vehicle_response_signal_catalog()
    if value in catalog.blocked_keys:
        return ()

    aliases: list[str] = []
    aliases.extend(catalog.aliases.get(value, ()))
    if value not in catalog.known_keys or value in catalog.confirmed_keys:
        aliases.extend(ROOT_CAUSE_SIGNAL_ALIASES.get(value, ()))
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


@dataclass
class RootCauseReport:
    status: str
    cause_side: str
    problem_scope: str
    headline: str
    summary: str
    evidence_rows: list[dict[str, str]] = field(default_factory=list)
    next_checks: list[str] = field(default_factory=list)
    performance_issue_count: int = 0
    performance_event_count: int = 0
    actuator_issue_count: int = 0
    phenomenon_signal_count: int = 0
    request_signal_count: int = 0
    algorithm_cause_signal_count: int = 0
    response_signal_count: int = 0
    missing_phenomenon_signals: list[str] = field(default_factory=list)
    missing_request_signals: list[str] = field(default_factory=list)
    missing_algorithm_cause_signals: list[str] = field(default_factory=list)
    missing_response_signals: list[str] = field(default_factory=list)
    performance_error: str = ""
    actuator_error: str = ""
    diagnosis_steps: list[dict[str, str]] = field(default_factory=list)
    temporal_judgement: dict[str, Any] = field(default_factory=dict)
    evidence_plots: list["RootCauseEvidencePlot"] = field(default_factory=list)


@dataclass(frozen=True)
class RootCauseEvidencePlot:
    """A readable evidence-chart page generated for a root-cause conclusion."""

    path: Path
    title: str


def infer_root_cause_problem_groups(user_text: str, fallback: list[str] | None = None) -> list[str]:
    value = (user_text or "").lower()
    groups: list[str] = []
    for scope in ROOT_CAUSE_PROBLEM_SCOPES:
        if any(term.lower() in value for term in scope["terms"]):
            groups.extend(scope["groups"])

    if "acc" in value or "跟车" in value or "纵向" in value:
        groups.append("acc_control")
    if any(term in value for term in ("abs", "tcs", "vse", "稳定", "附着")):
        groups.append("stability_control")

    if groups:
        return normalize_problem_groups(list(dict.fromkeys(groups)))
    if fallback:
        return normalize_problem_groups(fallback)
    return list(DEFAULT_PROBLEM_GROUPS)


def describe_root_cause_scope(user_text: str, performance_result: Any = None) -> str:
    labels: list[str] = []
    _append_scope_labels_from_text(labels, user_text or "")

    for label in getattr(performance_result, "selected_problem_types", []) or []:
        text = str(label or "").strip()
        matched = _append_scope_labels_from_text(labels, text)
        if text and not matched and "?" not in text:
            labels.append(text)

    for issue in getattr(performance_result, "problems", []) or []:
        metric_name = str(getattr(issue, "metric_name", "") or "")
        metric_text = f"{metric_name} {metric_title(metric_name)}"
        _append_scope_labels_from_text(labels, metric_text)

    if labels:
        return "、".join(dict.fromkeys(labels))
    return "用户描述的行车异常"


def _append_scope_labels_from_text(labels: list[str], text: str) -> bool:
    value = (text or "").lower()
    matched = False
    for scope in ROOT_CAUSE_PROBLEM_SCOPES:
        label = str(scope["label"])
        if label.lower() in value or any(term.lower() in value for term in scope["terms"]):
            labels.append(label)
            matched = True
    return matched


def build_root_cause_report(
    *,
    request_text: str,
    performance_result: Any = None,
    performance_error: str | None = None,
    actuator_result: Any = None,
    actuator_error: str | None = None,
) -> RootCauseReport:
    performance_error = performance_error or ""
    actuator_error = actuator_error or ""
    if performance_result is not None and not performance_error:
        perf_status = str(getattr(performance_result, "status", "") or "")
        if perf_status and perf_status != "ok":
            performance_error = str(getattr(performance_result, "summary", "") or perf_status)
    if actuator_result is not None and not actuator_error:
        actuator_status = str(getattr(actuator_result, "status", "") or "")
        if actuator_status and actuator_status not in {"ok", "issue"}:
            actuator_error = str(getattr(actuator_result, "summary", "") or actuator_status)

    performance_problems = list(getattr(performance_result, "problems", []) or [])
    actuator_violations = list(getattr(actuator_result, "violations", []) or [])
    perf_count = len(performance_problems)
    perf_events = sum(len(getattr(issue, "events", []) or []) for issue in performance_problems)
    actuator_count = len(actuator_violations)

    signal_probe = _probe_request_response_signals(request_text, performance_result, actuator_result)
    rows = _performance_evidence_rows(performance_problems)
    rows.extend(signal_probe["rows"])
    rows.extend(_actuator_evidence_rows(actuator_result))

    phenomenon_count = int(signal_probe["phenomenon_count"])
    request_count = int(signal_probe["request_count"])
    algorithm_cause_count = int(signal_probe["algorithm_cause_count"])
    response_count = int(signal_probe["response_count"])
    missing_phenomenon = list(signal_probe["missing_phenomenon"])
    missing_request = list(signal_probe["missing_request"])
    missing_algorithm_cause = list(signal_probe["missing_algorithm_cause"])
    missing_response = list(signal_probe["missing_response"])
    has_error = bool(performance_error or actuator_error or signal_probe["error"])

    problem_scope = describe_root_cause_scope(request_text, performance_result)
    temporal_judgement = _build_temporal_judgement(
        problem_scope=problem_scope,
        perf_count=perf_count,
        actuator_count=actuator_count,
        timeline=list(signal_probe.get("timeline", [])),
        missing_phenomenon=missing_phenomenon,
        missing_request=missing_request,
        missing_algorithm_cause=missing_algorithm_cause,
        missing_response=missing_response,
        has_error=has_error,
    )
    diagnosis_steps = _build_diagnosis_steps(
        problem_scope=problem_scope,
        perf_count=perf_count,
        perf_events=perf_events,
        actuator_count=actuator_count,
        phenomenon_count=phenomenon_count,
        request_count=request_count,
        algorithm_cause_count=algorithm_cause_count,
        response_count=response_count,
        missing_phenomenon=missing_phenomenon,
        missing_request=missing_request,
        missing_algorithm_cause=missing_algorithm_cause,
        missing_response=missing_response,
        evidence_rows=rows,
        has_error=has_error,
        temporal_judgement=temporal_judgement,
    )
    cause_side, headline, summary = _build_conclusion(
        problem_scope=problem_scope,
        perf_count=perf_count,
        perf_events=perf_events,
        actuator_count=actuator_count,
        phenomenon_count=phenomenon_count,
        request_count=request_count,
        algorithm_cause_count=algorithm_cause_count,
        response_count=response_count,
        missing_phenomenon=missing_phenomenon,
        missing_request=missing_request,
        missing_algorithm_cause=missing_algorithm_cause,
        missing_response=missing_response,
        has_error=has_error,
        diagnosis_steps=diagnosis_steps,
        temporal_judgement=temporal_judgement,
    )

    status = "error" if has_error and not rows else "issue" if (perf_count or actuator_count) else "ok"
    return RootCauseReport(
        status=status,
        cause_side=cause_side,
        problem_scope=problem_scope,
        headline=headline,
        summary=summary,
        evidence_rows=rows,
        next_checks=_next_checks(
            perf_count,
            actuator_count,
            has_error,
            missing_phenomenon,
            missing_request,
            missing_algorithm_cause,
            missing_response,
        ),
        performance_issue_count=perf_count,
        performance_event_count=perf_events,
        actuator_issue_count=actuator_count,
        phenomenon_signal_count=phenomenon_count,
        request_signal_count=request_count,
        algorithm_cause_signal_count=algorithm_cause_count,
        response_signal_count=response_count,
        missing_phenomenon_signals=missing_phenomenon,
        missing_request_signals=missing_request,
        missing_algorithm_cause_signals=missing_algorithm_cause,
        missing_response_signals=missing_response,
        performance_error=performance_error,
        actuator_error=actuator_error,
        diagnosis_steps=diagnosis_steps,
        temporal_judgement=temporal_judgement,
    )


def root_cause_report_context(report: RootCauseReport) -> str:
    lines = [
        f"归因方向：{report.cause_side}",
        f"问题范围：{report.problem_scope}",
        f"核心结论：{report.headline}",
        f"性能问题：{report.performance_issue_count} 类 / {report.performance_event_count} 个时间点",
        f"执行器风险点：{report.actuator_issue_count} 个",
        f"现象层信号：匹配 {report.phenomenon_signal_count} 个，缺失 {len(report.missing_phenomenon_signals)} 个",
        f"请求侧信号：匹配 {report.request_signal_count} 个，缺失 {len(report.missing_request_signals)} 个",
        f"诱因侧信号：匹配 {report.algorithm_cause_signal_count} 个，缺失 {len(report.missing_algorithm_cause_signals)} 个",
        f"响应侧信号：匹配 {report.response_signal_count} 个，缺失 {len(report.missing_response_signals)} 个",
    ]
    if report.temporal_judgement:
        final_cause = str(report.temporal_judgement.get("final_cause", "") or "")
        order_text = str(report.temporal_judgement.get("order_text", "") or "")
        if final_cause:
            lines.append(f"时序结论：{final_cause}")
        if order_text:
            lines.append(f"谁先变化：{order_text}")
    if report.evidence_rows:
        lines.append("证据：")
        for row in report.evidence_rows[:16]:
            lines.append(
                f"- {row.get('side', '')} | {row.get('issue', '')} | "
                f"{row.get('time', '')} | {row.get('evidence', '')}"
            )
    if report.diagnosis_steps:
        lines.append("归因链路：")
        for row in report.diagnosis_steps:
            lines.append(f"- {row.get('step', '')}：{row.get('result', '')}")
    return "\n".join(lines)


def build_root_cause_evidence_plots(
    *,
    h5_file: str | Path,
    request_text: str,
    performance_result: Any = None,
    actuator_result: Any = None,
    image_root: str | Path,
    signals_per_image: int = 8,
) -> list[RootCauseEvidencePlot]:
    """Render every matched signal that participates in the root-cause evidence chain.

    Signal panels are split into multiple images only for readability.  Unlike the
    old metric chart path, no matched key signal is discarded because of a global
    image-count or signal-count limit.
    """
    source = Path(h5_file)
    if source.suffix.lower() not in {".h5", ".hdf5"} or not source.exists():
        return []

    windows = _collect_windows(
        list(getattr(performance_result, "problems", []) or []),
        list(getattr(actuator_result, "violations", []) or []),
        max_performance_events=None,
        max_actuator_violations=None,
    )
    if not windows:
        return []

    try:
        add_can_code_to_path()
        from manage_bad_cases import build_plot_image, extract_signal_series
        from signal_lib import FolderSession

        session = FolderSession(source)
        alias_index = _build_alias_index(session.get_store().list_signals())
    except Exception as exc:
        print(f"root cause evidence plot initialization failed: {exc}", flush=True)
        return []

    output_root = ensure_writable_dir(Path(image_root))
    chunk_size = max(1, int(signals_per_image or 8))
    selected_groups = _select_signal_groups(request_text, performance_result)
    result: list[RootCauseEvidencePlot] = []

    for window_index, (event_start, event_end, window_title) in enumerate(windows, start=1):
        plot_start = max(0.0, float(event_start) - 5.0)
        plot_end = max(float(event_end) + 5.0, plot_start + 1.0)
        event = {
            "start": float(event_start),
            "end": float(event_end),
            "duration": max(0.0, float(event_end) - float(event_start)),
        }
        key_plot = _build_key_evidence_plot(
            session=session,
            alias_index=alias_index,
            selected_groups=selected_groups,
            window_index=window_index,
            window_title=window_title,
            source=source,
            plot_start=plot_start,
            plot_end=plot_end,
            event=event,
            output_root=output_root,
            build_plot_image=build_plot_image,
            extract_signal_series=extract_signal_series,
        )
        if key_plot:
            result.append(key_plot)
        for group_index, group in enumerate(selected_groups, start=1):
            resolved_signals = _resolved_group_plot_signals(alias_index, group)
            if not resolved_signals:
                continue
            for chunk_index, signal_chunk in enumerate(_chunked(resolved_signals, chunk_size), start=1):
                try:
                    axis_signal, axis, axis_values = _select_plot_axis(
                        session, signal_chunk, plot_start, plot_end
                    )
                    if not axis_signal or len(axis) < 2:
                        continue
                    session._axis_signal_name = axis_signal
                    session._axis_values = axis_values
                    signal_series: dict[str, list[float]] = {}
                    for signal_name in signal_chunk:
                        values = _extract_plot_signal_series(
                            session,
                            signal_name,
                            axis,
                            plot_start,
                            plot_end,
                            extract_signal_series,
                        )
                        if len(values) == len(axis):
                            signal_series[signal_name] = values
                    if not signal_series:
                        continue

                    group_title = str(group.get("title") or "关键证据")
                    title = f"{window_title}｜{group_title}"
                    if len(resolved_signals) > chunk_size:
                        title += f"（第 {chunk_index} 组）"
                    detail = (
                        f"归因证据：{group_title}。红色阴影为本次问题时间窗；"
                        "图中包含本次结论实际读取到的全部已匹配信号。"
                    )
                    image = build_plot_image(
                        f"行车问题原因分析：{title}",
                        source.parent.name,
                        source.name,
                        axis,
                        signal_series,
                        [event],
                        0.0,
                        detail,
                    )
                    output_path = output_root / (
                        f"root_cause_{window_index:02d}_{group_index:02d}_{chunk_index:02d}.png"
                    )
                    image.save(output_path)
                    result.append(RootCauseEvidencePlot(path=output_path, title=title))
                except Exception as exc:
                    print(
                        "root cause evidence plot failed: "
                        f"window={window_index}, group={group.get('title')} | {exc}",
                        flush=True,
                    )
    return result


def _build_key_evidence_plot(
    *,
    session: Any,
    alias_index: dict[str, str],
    selected_groups: list[dict[str, Any]],
    window_index: int,
    window_title: str,
    source: Path,
    plot_start: float,
    plot_end: float,
    event: dict[str, float],
    output_root: Path,
    build_plot_image: Any,
    extract_signal_series: Any,
) -> RootCauseEvidencePlot | None:
    signal_names = _select_key_evidence_signals(alias_index, selected_groups)
    if not signal_names:
        return None

    axis_signal, axis, axis_values = _select_plot_axis(session, signal_names, plot_start, plot_end)
    if not axis_signal or len(axis) < 2:
        return None

    session._axis_signal_name = axis_signal
    session._axis_values = axis_values
    signal_series: dict[str, list[float]] = {}
    for signal_name in signal_names:
        values = _extract_plot_signal_series(
            session,
            signal_name,
            axis,
            plot_start,
            plot_end,
            extract_signal_series,
        )
        if len(values) == len(axis):
            signal_series[signal_name] = values
    if not signal_series:
        return None

    title = f"{window_title}｜关键证据图"
    detail = (
        "精选能支撑归因判断的关键信号：先看实车表现是否成立，再看算法请求、执行器响应、"
        "目标/Cut-in、曲率、设速等诱因是否在同一时间窗内发生变化。红色阴影为本次问题时间窗。"
    )
    image = build_plot_image(
        f"行车问题原因分析：{title}",
        source.parent.name,
        source.name,
        axis,
        signal_series,
        [event],
        0.0,
        detail,
    )
    output_path = output_root / f"root_cause_{window_index:02d}_00_key.png"
    image.save(output_path)
    return RootCauseEvidencePlot(path=output_path, title=title)


def _select_key_evidence_signals(
    alias_index: dict[str, str],
    selected_groups: list[dict[str, Any]],
) -> list[str]:
    by_kind: dict[str, list[str]] = {}
    for group in selected_groups:
        kind = str(group.get("kind") or "")
        if kind not in ROOT_CAUSE_KEY_SIGNAL_LIMITS:
            continue
        resolved = _resolved_group_plot_signals(alias_index, group)
        if not resolved:
            continue
        bucket = by_kind.setdefault(kind, [])
        for signal_name in resolved:
            if signal_name not in bucket:
                bucket.append(signal_name)

    selected: list[str] = []
    for kind in ("phenomenon", "request", "response", "algorithm_cause"):
        limit = ROOT_CAUSE_KEY_SIGNAL_LIMITS.get(kind, 0)
        for signal_name in _prioritize_key_signals(kind, by_kind.get(kind, []))[:limit]:
            if signal_name not in selected:
                selected.append(signal_name)
    return selected


def _prioritize_key_signals(kind: str, signal_names: list[str]) -> list[str]:
    priorities = ROOT_CAUSE_KEY_SIGNAL_PRIORITIES.get(kind, ())

    def score(signal_name: str) -> tuple[int, str]:
        lowered = signal_name.lower()
        for index, token in enumerate(priorities):
            if token in lowered:
                return index, lowered
        return len(priorities) + 100, lowered

    return sorted(signal_names, key=score)


def _select_plot_axis(
    session: Any,
    signal_names: list[str],
    plot_start: float,
    plot_end: float,
) -> tuple[str | None, list[float], list[Any]]:
    for signal_name in signal_names:
        try:
            axis, axis_values = session.get_signal_slice(signal_name, plot_start, plot_end)
        except Exception:
            continue
        if len(axis) >= 2:
            return signal_name, axis, axis_values
    return None, [], []


def _extract_plot_signal_series(
    session: Any,
    signal_name: str,
    axis: list[float],
    plot_start: float,
    plot_end: float,
    extractor: Any,
) -> list[float]:
    try:
        return extractor(session, signal_name, axis, plot_start, plot_end)
    except ValueError as exc:
        if "source signal is empty" not in str(exc):
            raise
        try:
            values = session.sample_signal_to_axis(signal_name, axis, method="nearest")
            return [float(value) for value in values]
        except Exception:
            return []


def _resolved_group_plot_signals(alias_index: dict[str, str], group: dict[str, Any]) -> list[str]:
    """Resolve actual H5 signal names, including source signals for derived evidence."""
    resolved_signals: list[str] = []
    for candidate in tuple(group.get("signals", ()) or ()):
        key = _clean_signal_candidate(candidate)
        if _skip_cutin_candidate_by_default_target_role(alias_index, key):
            continue
        for source_candidate in _plot_source_candidates(candidate):
            resolved = _resolve_signal(alias_index, source_candidate)
            if resolved and resolved not in resolved_signals:
                resolved_signals.append(resolved)
    return resolved_signals


def _plot_source_candidates(candidate: str) -> tuple[str, ...]:
    """Expose real source signals for TTC and curvature instead of plotting a fake value."""
    key = _clean_signal_candidate(candidate)
    derived_sources = {
        "Curvature": ("CurvatureLeftC2", "CurvatureRightC2"),
        "LeadTargetTTC": ("LeadTargetDistance", "LeadTargetVelocity"),
        "CutInTargetTTC": ("CutInTargetDistance", "CutInTargetVelocity"),
    }
    return derived_sources.get(key, (candidate,))


def _chunked(values: list[str], chunk_size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), chunk_size):
        yield values[index : index + chunk_size]


def _probe_request_response_signals(
    request_text: str,
    performance_result: Any,
    actuator_result: Any,
) -> dict[str, Any]:
    windows = _collect_windows(
        list(getattr(performance_result, "problems", []) or []),
        list(getattr(actuator_result, "violations", []) or []),
    )
    h5_file = _analysis_h5_file(performance_result)
    base = {
        "rows": [],
        "phenomenon_count": 0,
        "request_count": 0,
        "algorithm_cause_count": 0,
        "response_count": 0,
        "missing_phenomenon": [],
        "missing_request": [],
        "missing_algorithm_cause": [],
        "missing_response": [],
        "timeline": [],
        "error": "",
    }
    if not h5_file or not windows:
        return base

    try:
        add_can_code_to_path()
        from signal_lib import FolderSession

        session = FolderSession(h5_file)
        alias_index = _build_alias_index(session.get_store().list_signals())
    except Exception as exc:
        base["rows"].append(
            {
                "side": "信号证据",
                "issue": "请求/响应信号读取失败",
                "time": "-",
                "evidence": str(exc)[:180],
                "expected": "H5 能被 signal_lib 正常读取",
            }
        )
        base["error"] = str(exc)
        return base

    selected_groups = _select_signal_groups(request_text, performance_result)
    request_missing: set[str] = set()
    phenomenon_missing: set[str] = set()
    algorithm_cause_missing: set[str] = set()
    response_missing: set[str] = set()
    phenomenon_count = 0
    request_count = 0
    algorithm_cause_count = 0
    response_count = 0
    rows: list[dict[str, str]] = []
    timeline: list[dict[str, Any]] = []

    for start, end, title in windows[:4]:
        for group in selected_groups:
            evidence, found, missing = _summarize_signal_group(
                session,
                alias_index,
                group["signals"],
                start,
                end,
                include_brake_torque=bool(group.get("include_brake_torque")),
            )
            timeline.extend(
                _collect_signal_timeline_events(
                    session,
                    alias_index,
                    group,
                    start,
                    end,
                    title or f"{start:.3f}s-{end:.3f}s",
                )
            )
            if group["side"] == "现象层":
                phenomenon_count += found
                phenomenon_missing.update(missing)
            elif group["side"] == "算法请求侧":
                request_count += found
                request_missing.update(missing)
            elif group["side"] == "算法请求诱因":
                algorithm_cause_count += found
                algorithm_cause_missing.update(missing)
            elif group["side"] == "执行器响应侧":
                response_count += found
                response_missing.update(missing)
            if not evidence and not missing:
                continue
            rows.append(
                {
                    "side": group["side"],
                    "issue": group["title"],
                    "time": title or f"{start:.3f}s-{end:.3f}s",
                    "evidence": evidence or f"未找到本窗口内关键信号：{'、'.join(missing[:6])}",
                    "expected": group["expected"],
                }
            )
            if len(rows) >= 14:
                break
        if len(rows) >= 14:
            break

    missing_algorithm_cause_list = _normalize_flt1_target_missing_names(algorithm_cause_missing)

    base.update(
        {
            "rows": rows,
            "phenomenon_count": phenomenon_count,
            "request_count": request_count,
            "algorithm_cause_count": algorithm_cause_count,
            "response_count": response_count,
            "missing_phenomenon": sorted(phenomenon_missing),
            "missing_request": sorted(request_missing),
            "missing_algorithm_cause": missing_algorithm_cause_list,
            "missing_response": sorted(response_missing),
            "timeline": sorted(timeline, key=lambda item: float(item.get("time", 0.0)))[:32],
        }
    )
    return base


def _dedupe_preset_signal_names(signals: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for signal in signals or []:
        cleaned = clean_preset_signal_name(signal)
        cleaned = _clean_signal_candidate(cleaned)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return tuple(result)


def _merge_signal_names(*signal_groups: Iterable[str]) -> tuple[str, ...]:
    merged: list[str] = []
    for group in signal_groups:
        merged.extend(list(group or []))
    return _dedupe_preset_signal_names(merged)


def _selected_performance_presets(request_text: str, performance_result: Any = None) -> list[Any]:
    presets = load_performance_problem_presets()
    if not presets:
        return []

    selected_keys = list(infer_performance_problem_preset_keys(request_text or ""))
    label_to_key: dict[str, str] = {}
    metric_to_key: dict[str, str] = {}
    for key, preset in presets.items():
        label_to_key[str(key)] = key
        label_to_key[str(preset.label)] = key
        for alias in getattr(preset, "aliases", ()) or ():
            label_to_key[str(alias)] = key
        for metric in getattr(preset, "metrics", ()) or ():
            metric_to_key[str(metric)] = key

    for label in list(getattr(performance_result, "selected_problem_types", []) or []):
        key = label_to_key.get(str(label))
        if key and key not in selected_keys:
            selected_keys.append(key)

    for issue in list(getattr(performance_result, "problems", []) or []):
        metric_name = str(getattr(issue, "metric_name", "") or "")
        key = metric_to_key.get(metric_name)
        if key and key not in selected_keys:
            selected_keys.append(key)

    return [presets[key] for key in selected_keys if key in presets]


def _preset_signals(presets: Iterable[Any], attr_name: str, *, include_flt1_flag: bool = False) -> tuple[str, ...]:
    signals: list[str] = []
    for preset in presets or []:
        signals.extend(list(getattr(preset, attr_name, ()) or ()))
    result = list(_dedupe_preset_signal_names(signals))
    if include_flt1_flag and _uses_flt1_target_context(result) and "FLT1_CUTIN_Flag" not in result:
        result.append("FLT1_CUTIN_Flag")
    return tuple(result)


def _signal_matches_short_name(signal: str, expected_names: set[str]) -> bool:
    short_name = _short_name(signal)
    variants = {short_name}
    if short_name.startswith("I") and len(short_name) > 1 and short_name[1].isupper():
        variants.add(short_name[1:])
    elif short_name:
        variants.add(f"I{short_name}")
    return bool(variants & expected_names)


def _select_signal_groups(request_text: str, performance_result: Any) -> list[dict[str, Any]]:
    value = (request_text or "").lower()
    metric_names = " ".join(
        str(getattr(issue, "metric_name", "") or "") for issue in list(getattr(performance_result, "problems", []) or [])
    ).lower()
    scope_text = f"{value} {metric_names}"
    selected_presets = _selected_performance_presets(request_text, performance_result)
    preset_vehicle_signals = _preset_signals(selected_presets, "vehicle_signals")
    preset_request_signals = _preset_signals(selected_presets, "algorithm_request_signals")
    preset_response_signals = _preset_signals(selected_presets, "actuator_request_signals")
    use_aeb = any(term in scope_text for term in ("aeb", "cib", "误制动", "漏制动", "没制动", "制动没有响应"))
    use_brake = use_aeb or any(term in scope_text for term in ("制动", "刹", "brake", "重制动", "急减速"))
    use_propulsion = any(
        term in scope_text
        for term in ("acc", "跟车", "驱动", "动力", "set speed", "setspeed", "设速", "低速", "溜车", "车距")
    )
    if not (use_aeb or use_brake or use_propulsion):
        use_brake = True
        use_propulsion = True

    groups: list[dict[str, Any]] = [
        {
            "kind": "phenomenon",
            "side": "现象层",
            "title": "车辆实际表现",
            "signals": preset_vehicle_signals or VEHICLE_RESPONSE_SIGNALS,
            "expected": "先确认实车是否确实出现加速度、车速或轮速异常",
        }
    ]
    if selected_presets:
        if preset_request_signals:
            groups.append(
                {
                    "kind": "request",
                    "side": "算法请求侧",
                    "title": "本问题算法请求关注信号",
                    "signals": preset_request_signals,
                    "expected": "按预设表核对算法是否主动给出了与实车现象一致的请求",
                }
            )
        if preset_response_signals:
            groups.append(
                {
                    "kind": "response",
                    "side": "执行器响应侧",
                    "title": "本问题执行器响应关注信号",
                    "signals": preset_response_signals,
                    "expected": "按预设表核对执行器是否按请求形成了实际响应或状态闭环",
                    "include_brake_torque": any(
                        _signal_matches_short_name(
                            signal,
                            {"ACCBrkngAct", "AutoBrkngAct", "CSTBATS_TrqVl", "RegenBrkAxTrqAch"},
                        )
                        for signal in preset_response_signals
                    ),
                }
            )
    elif use_propulsion:
        groups.extend(
            [
                {
                    "kind": "request",
                    "side": "算法请求侧",
                    "title": "ACC 驱动请求",
                    "signals": ACC_PROPULSION_REQUEST_SIGNALS,
                    "expected": "请求应与目标车速/跟车场景一致，变化不应无故突变",
                },
                {
                    "kind": "response",
                    "side": "执行器响应侧",
                    "title": "驱动系统实际响应",
                    "signals": ACC_PROPULSION_RESPONSE_SIGNALS,
                    "expected": "驱动系统应反馈请求已接受，实际扭矩应跟随请求侧变化",
                },
            ]
        )
    if not selected_presets and use_brake:
        groups.extend(
            [
                {
                    "kind": "request",
                    "side": "算法请求侧",
                    "title": "ACC 制动请求（C-CAN）",
                    "signals": ACC_BRAKE_REQUEST_SIGNALS,
                    "expected": "ACC 制动请求应只按 C-CAN 的 ADAS_Command_Ext1 参与归因",
                },
                {
                    "kind": "response",
                    "side": "执行器响应侧",
                    "title": "制动系统实际响应",
                    "signals": BRAKE_RESPONSE_SIGNALS,
                    "expected": "EBCM 应进入对应自动制动状态；实际制动扭矩按液压扭矩 + 电制动扭矩估算",
                    "include_brake_torque": True,
                },
            ]
        )
    if not selected_presets and use_aeb:
        groups.append(
            {
                "kind": "request",
                "side": "算法请求侧",
                "title": "AEB 制动扭矩请求",
                "signals": AEB_BRAKE_REQUEST_SIGNALS,
                "expected": "AEB 侧看扭矩请求 FCACBSC_CtrlTrq1，不用 FCACBSC_CtrlAcc1 归因",
            }
        )
    algorithm_cause_group = _algorithm_cause_signal_group(request_text, performance_result)
    if algorithm_cause_group:
        groups.append(algorithm_cause_group)
    return groups


def _algorithm_cause_signal_group(request_text: str, performance_result: Any) -> dict[str, Any] | None:
    selected_presets = _selected_performance_presets(request_text, performance_result)
    signals = list(_preset_signals(selected_presets, "algorithm_cause_signals", include_flt1_flag=True))
    logic_parts: list[str] = []
    hints: list[str] = []
    for preset in selected_presets:
        if preset.algorithm_cause_logic:
            logic_parts.append(f"{preset.label}：{preset.algorithm_cause_logic}")
        if preset.root_cause_output_hint:
            hints.append(f"{preset.label}：{preset.root_cause_output_hint}")

    if not signals and any(
        term in (request_text or "")
        for term in (
            "重制动",
            "急减速",
            "误制动",
            "漏制动",
            "没有制动",
            "顿挫",
            "抖动",
            "不合理低速",
            "跟车距离",
            "未达到",
            "setspeed",
            "SetSpeed",
        )
    ):
        signals.extend(ALGORITHM_CAUSE_SIGNAL_FALLBACKS)

    if _uses_flt1_target_context(signals) and "FLT1_CUTIN_Flag" not in signals:
        signals.append("FLT1_CUTIN_Flag")

    signals = list(dict.fromkeys(signal for signal in signals if signal))
    if not signals:
        return None

    expected = "如果请求侧已经主动减速或扭矩波动，再看这些场景信号是否能解释请求来源。"
    if logic_parts:
        expected += " 判断逻辑：" + "；".join(logic_parts[:3])
    if hints:
        expected += " 输出口径：" + "；".join(hints[:3])
    return {
        "kind": "algorithm_cause",
        "side": "算法请求诱因",
        "title": "目标/Cut-in/曲率/设速上下文",
        "signals": tuple(signals),
        "expected": expected,
    }


def _uses_flt1_target_context(signals: list[str]) -> bool:
    target_keys = {
        "LeadTargetId",
        "LeadTargetDistance",
        "LeadTargetVelocity",
        "LeadTargetAccel",
        "LeadTargetTTC",
        "CutInTargetId",
        "CutInTargetDistance",
        "CutInTargetVelocity",
        "CutInTargetAccel",
        "CutInTargetTTC",
    }
    return any(_clean_signal_candidate(signal) in target_keys for signal in signals)


def _summarize_signal_group(
    session: Any,
    alias_index: dict[str, str],
    candidates: tuple[str, ...],
    start: float,
    end: float,
    *,
    include_brake_torque: bool = False,
) -> tuple[str, int, list[str]]:
    parts: list[str] = []
    missing: list[str] = []
    found = 0
    for candidate in candidates:
        key = _clean_signal_candidate(candidate)
        if _skip_cutin_candidate_by_default_target_role(alias_index, key):
            continue
        derived_summary, derived_found, derived_missing, derived_handled = _derived_signal_summary(
            session,
            alias_index,
            candidate,
            start,
            end,
        )
        if derived_handled:
            if derived_summary:
                parts.append(derived_summary)
            found += derived_found
            missing.extend(derived_missing)
            continue

        resolved = _resolve_signal(alias_index, candidate)
        if not resolved:
            missing.append(_short_name(candidate))
            continue
        summary = _range_summary(session, resolved, start, end)
        if summary:
            parts.append(summary)
            found += 1

    if include_brake_torque:
        torque_summary, torque_found, torque_missing = _brake_torque_summary(session, alias_index, start, end)
        if torque_summary:
            parts.append(torque_summary)
        found += torque_found
        missing.extend(torque_missing)

    missing = sorted(dict.fromkeys(missing))
    if missing and parts:
        parts.append(f"未匹配：{'、'.join(missing[:5])}" + (" 等" if len(missing) > 5 else ""))
    return "；".join(parts[:8]), found, missing


def _skip_cutin_candidate_by_default_target_role(alias_index: dict[str, str], key: str) -> bool:
    if key not in CUTIN_TARGET_SIGNAL_KEYS:
        return False
    if ROOT_CAUSE_DEFAULT_SIGNAL_VALUES.get("FLT1_CUTIN_Flag") != 0.0:
        return False
    return _resolve_signal(alias_index, "FLT1_CUTIN_Flag") is None


def _collect_signal_timeline_events(
    session: Any,
    alias_index: dict[str, str],
    group: dict[str, Any],
    start: float,
    end: float,
    window_title: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for candidate in tuple(group.get("signals", ()) or ()):
        event = _detect_signal_change_event(session, alias_index, candidate, start, end)
        if not event:
            continue
        event.update(
            {
                "kind": str(group.get("kind", "")),
                "side": str(group.get("side", "")),
                "issue": str(group.get("title", "")),
                "window": window_title,
            }
        )
        events.append(event)
    events.sort(key=lambda item: (float(item.get("time", start)), -float(item.get("strength", 0.0))))

    # 保留每个证据层最早变化和最大跳变，避免同类信号把时序链挤得太长。
    selected: list[dict[str, Any]] = []
    if events:
        selected.append(events[0])
        strongest = max(events, key=lambda item: float(item.get("strength", 0.0)))
        if strongest.get("signal") != events[0].get("signal"):
            selected.append(strongest)
    return selected


def _detect_signal_change_event(
    session: Any,
    alias_index: dict[str, str],
    candidate: str,
    start: float,
    end: float,
) -> dict[str, Any] | None:
    resolved = _resolve_signal(alias_index, candidate)
    if not resolved:
        return None
    timestamps, values = _range_series(session, resolved, start, end)
    if len(values) < 2:
        return None

    first = values[0]
    min_value = min(values)
    max_value = max(values)
    span = max_value - min_value
    max_jump = _max_delta(values)
    if _nearly_equal(min_value, max_value):
        return None

    threshold = _change_threshold(values)
    change_index: int | None = None
    for index in range(1, len(values)):
        if abs(values[index] - first) >= threshold or abs(values[index] - values[index - 1]) >= threshold:
            change_index = index
            break
    if change_index is None:
        return None

    previous = values[change_index - 1]
    current = values[change_index]
    time_value = timestamps[change_index] if change_index < len(timestamps) else start
    return {
        "signal": _short_name(resolved),
        "resolved_signal": resolved,
        "time": float(time_value),
        "previous": previous,
        "current": current,
        "first": first,
        "last": values[-1],
        "min": min_value,
        "max": max_value,
        "span": span,
        "max_jump": max_jump,
        "strength": max(abs(current - previous), span, max_jump),
        "summary": (
            f"{_short_name(resolved)} 在 {float(time_value):.3f}s "
            f"由 {_fmt_number(previous)} 变为 {_fmt_number(current)}"
        ),
    }


def _range_series(session: Any, signal_name: str, start: float, end: float) -> tuple[list[float], list[float]]:
    try:
        data = session.get_store().get_range(signal_name, start, end)
    except Exception:
        return [], []

    raw_values = list(data.get("values", []) or [])
    raw_timestamps = list(
        data.get("timestamps", [])
        or data.get("timestamp", [])
        or data.get("time", [])
        or data.get("times", [])
        or []
    )
    timestamps: list[float] = []
    values: list[float] = []
    denominator = max(len(raw_values) - 1, 1)
    for index, raw_value in enumerate(raw_values):
        value = _as_float(raw_value)
        if value is None or not math.isfinite(value):
            continue
        timestamp = _as_float(raw_timestamps[index]) if index < len(raw_timestamps) else None
        if timestamp is None or not math.isfinite(timestamp):
            timestamp = start + (end - start) * index / denominator
        timestamps.append(float(timestamp))
        values.append(float(value))
    return timestamps, values


def _change_threshold(values: list[float]) -> float:
    if not values:
        return 0.0
    min_value = min(values)
    max_value = max(values)
    span = max_value - min_value
    max_abs = max(abs(value) for value in values)
    if span <= 1e-9:
        return 0.0
    if span <= 1.5 and max_abs <= 5.0:
        return max(0.25, span * 0.35)
    return max(0.05, span * 0.18)


def _derived_signal_summary(
    session: Any,
    alias_index: dict[str, str],
    candidate: str,
    start: float,
    end: float,
) -> tuple[str, int, list[str], bool]:
    key = _clean_signal_candidate(candidate)
    default_summary = _default_signal_summary(alias_index, key)
    if default_summary is not None:
        return default_summary
    if key == "Curvature":
        return _derived_curvature_summary(session, alias_index, start, end)
    if key not in {"LeadTargetTTC", "CutInTargetTTC"}:
        return "", 0, [], False
    if key in _load_vehicle_response_signal_catalog().blocked_keys:
        return "", 0, [key], True

    distance_key = "CutInTargetDistance" if key == "CutInTargetTTC" else "LeadTargetDistance"
    velocity_key = "CutInTargetVelocity" if key == "CutInTargetTTC" else "LeadTargetVelocity"
    distance_signal = _resolve_signal(alias_index, distance_key)
    velocity_signal = _resolve_signal(alias_index, velocity_key)
    if not distance_signal or not velocity_signal:
        return "", 0, [key], True

    try:
        distances = _numeric_values(session.get_store().get_range(distance_signal, start, end).get("values", []))
        velocities = _numeric_values(session.get_store().get_range(velocity_signal, start, end).get("values", []))
    except Exception:
        return "", 0, [key], True

    ttc_values: list[float] = []
    for distance, relative_velocity in zip(distances, velocities):
        if not math.isfinite(distance) or not math.isfinite(relative_velocity):
            continue
        if distance <= 0:
            continue
        closing_speed = -relative_velocity
        if closing_speed <= 1e-3:
            continue
        ttc_values.append(distance / closing_speed)

    if not ttc_values:
        return f"{key} 未形成有效闭合速度，暂不作为风险证据", 1, [], True

    return (
        f"{key} 估算范围 {_fmt_number(min(ttc_values))}~{_fmt_number(max(ttc_values))}s",
        1,
        [],
        True,
    )


def _default_signal_summary(
    alias_index: dict[str, str],
    key: str,
) -> tuple[str, int, list[str], bool] | None:
    if key not in ROOT_CAUSE_DEFAULT_SIGNAL_VALUES:
        return None
    if _resolve_signal(alias_index, key):
        return None
    value = ROOT_CAUSE_DEFAULT_SIGNAL_VALUES[key]
    if key == "FLT1_CUTIN_Flag":
        return (
            f"FLT1_CUTIN_Flag 暂按默认值 {_fmt_number(value)} 处理：FLT1 当前视为跟车目标，Cut-in 目标不单独成立",
            1,
            [],
            True,
        )
    return None


def _derived_curvature_summary(
    session: Any,
    alias_index: dict[str, str],
    start: float,
    end: float,
) -> tuple[str, int, list[str], bool]:
    if "Curvature" in _load_vehicle_response_signal_catalog().blocked_keys:
        return "", 0, ["Curvature"], True

    left_signal = _resolve_signal(alias_index, "CurvatureLeftC2")
    right_signal = _resolve_signal(alias_index, "CurvatureRightC2")
    if not left_signal or not right_signal:
        return "", 0, ["Curvature"], True

    left_times, left_values = _range_series(session, left_signal, start, end)
    _, right_values = _range_series(session, right_signal, start, end)
    if not left_values or not right_values:
        return "", 0, ["Curvature"], True

    curvature_values: list[float] = []
    skipped_near_zero = 0
    for left, right in zip(left_values, right_values):
        denominator = left + right
        if not math.isfinite(denominator) or abs(denominator) <= 1e-12:
            skipped_near_zero += 1
            continue
        curvature_values.append(1.0 / denominator)

    if not curvature_values:
        return "Curvature source C2 signals exist, but left+right is zero/near-zero in this window", 1, [], True

    first = curvature_values[0]
    last = curvature_values[-1]
    min_value = min(curvature_values)
    max_value = max(curvature_values)
    max_jump = _max_delta(curvature_values)
    skip_text = f", skipped_near_zero={skipped_near_zero}" if skipped_near_zero else ""
    summary = (
        f"Curvature derived from Left/Right LaneModelC2: "
        f"{_fmt_number(first)}->{_fmt_number(last)} "
        f"(range {_fmt_number(min_value)}~{_fmt_number(max_value)}, "
        f"max_jump {_fmt_number(max_jump)}{skip_text})"
    )
    return summary, 1, [], True


def _brake_torque_summary(session: Any, alias_index: dict[str, str], start: float, end: float) -> tuple[str, int, list[str]]:
    hydraulic_signal = _resolve_signal(alias_index, "ICSTBATS_TrqVl")
    regen_signal = _resolve_signal(alias_index, "IRegenBrkAxTrqAch")
    missing: list[str] = []
    if not hydraulic_signal:
        missing.append("ICSTBATS_TrqVl")
        if not regen_signal:
            missing.append("IRegenBrkAxTrqAch")
        return "", 0, missing

    hydraulic_values = _numeric_values(session.get_store().get_range(hydraulic_signal, start, end).get("values", []))
    regen_values = _numeric_values(session.get_store().get_range(regen_signal, start, end).get("values", [])) if regen_signal else []
    if not hydraulic_values:
        return "", 0, []

    hydraulic_min, hydraulic_max = min(hydraulic_values), max(hydraulic_values)
    if regen_values:
        regen_min, regen_max = min(regen_values), max(regen_values)
        found = 2
        regen_text = f"IRegenBrkAxTrqAch 范围 {_fmt_number(regen_min)}~{_fmt_number(regen_max)}"
    else:
        regen_min = regen_max = 0.0
        found = 1
        regen_text = "IRegenBrkAxTrqAch 未找到，按 0 参与估算"
        missing.append("IRegenBrkAxTrqAch")

    estimated_min = hydraulic_min + regen_min
    estimated_max = hydraulic_max + regen_max
    text = (
        "实际制动扭矩≈ICSTBATS_TrqVl + IRegenBrkAxTrqAch："
        f"液压范围 {_fmt_number(hydraulic_min)}~{_fmt_number(hydraulic_max)}，"
        f"{regen_text}，合计范围 {_fmt_number(estimated_min)}~{_fmt_number(estimated_max)}"
    )
    return text, found, missing


def _build_temporal_judgement(
    *,
    problem_scope: str,
    perf_count: int,
    actuator_count: int,
    timeline: list[dict[str, Any]],
    missing_phenomenon: list[str],
    missing_request: list[str],
    missing_algorithm_cause: list[str],
    missing_response: list[str],
    has_error: bool,
) -> dict[str, Any]:
    if has_error or not timeline:
        return {}

    critical_gaps = _critical_signal_gaps(
        problem_scope,
        missing_phenomenon,
        missing_request,
        missing_response,
        missing_algorithm_cause,
    )
    request_response_has_gaps = bool(
        critical_gaps.get("现象层") or critical_gaps.get("请求侧") or critical_gaps.get("响应侧")
    )
    if perf_count and request_response_has_gaps:
        return {
            "confidence": "low",
            "cause_side": "证据不足",
            "headline": "关键证据还没齐，暂时不能做最终归因",
            "final_cause": "现象、请求、响应三段证据没有同时闭合；先补齐缺失信号，再比较谁先变化。",
            "order": [_public_temporal_event(event) for event in timeline[:6]],
            "order_text": _temporal_order_text(timeline[:6]),
        }

    phenomenon = _earliest_temporal_event(timeline, "phenomenon")
    request = _earliest_temporal_event(timeline, "request")
    algorithm_cause = _earliest_temporal_event(timeline, "algorithm_cause")
    response = _earliest_temporal_event(timeline, "response")
    order_events = [event for event in (algorithm_cause, request, response, phenomenon) if event]
    tolerance = 0.25

    if algorithm_cause and request and response and _time_before_or_together(algorithm_cause, request, tolerance):
        cause_request_gap = float(request.get("time", 0.0)) - float(algorithm_cause.get("time", 0.0))
        if abs(cause_request_gap) <= tolerance:
            headline = "场景诱因与请求几乎同步出现，执行器主要是在跟随"
        else:
            headline = "场景诱因先出现，请求随后变化，执行器主要是在跟随"
        return _temporal_result(
            confidence="high",
            cause_side="算法/控制请求侧",
            headline=headline,
            final_cause=(
                f"最终原因更偏向请求侧：{_event_short(algorithm_cause)} 先出现或同步出现，"
                f"随后 {_event_short(request)}，再到 {_event_short(response)}。"
                "这条链路说明执行器不是最先触发异常的一侧，重点应查算法为什么根据该场景给出这次请求。"
            ),
            order_events=order_events,
        )

    if request and response and _time_before_or_together(request, response, tolerance):
        response_gap = float(response.get("time", 0.0)) - float(request.get("time", 0.0))
        near_sync = abs(response_gap) <= tolerance
        if abs(response_gap) <= tolerance:
            final_cause = (
                f"最终原因更偏向请求侧：{_event_short(request)} 与 {_event_short(response)} 在采样精度内几乎同步，"
                "没有看到执行器响应明显早于请求。"
            )
        else:
            final_cause = (
                f"最终原因更偏向请求侧：{_event_short(request)} 是当前证据链里更靠前的变化，"
                f"{_event_short(response)} 随后跟随。"
            )
        if phenomenon and _time_before_or_together(response, phenomenon, 0.5):
            if abs(response_gap) <= tolerance:
                final_cause = (
                    f"最终原因更偏向请求侧：{_event_short(request)} 与 {_event_short(response)} 几乎同步，"
                    "执行器没有明显先触发，实车现象随后出现。"
                )
            else:
                final_cause = (
                f"最终原因更偏向请求侧：{_event_short(request)} 先变化，"
                f"{_event_short(response)} 随后跟随，实车现象再出现。"
            )
        if not algorithm_cause and _scope_needs_algorithm_context(problem_scope):
            final_cause += " 但诱因侧还没解释清楚，需要继续看前车/Cut-in、曲率限速或 SetSpeed 等信号。"
            confidence = "medium"
            headline = "请求和响应几乎同步；诱因还需要补证据" if near_sync else "请求先变，执行器跟随；诱因还需要补证据"
        else:
            confidence = "high"
            headline = "请求和响应几乎同步，执行器没有明显先触发" if near_sync else "请求先变，执行器跟随"
        return _temporal_result(
            confidence=confidence,
            cause_side="算法/控制请求侧",
            headline=headline,
            final_cause=final_cause,
            order_events=order_events,
        )

    if response and request and float(response.get("time", 0.0)) < float(request.get("time", 0.0)) - tolerance:
        return _temporal_result(
            confidence="high",
            cause_side="执行器响应侧",
            headline="响应侧先变，早于请求侧证据",
            final_cause=(
                f"最终原因更偏向执行器响应侧：{_event_short(response)} 早于 {_event_short(request)}。"
                "如果请求侧没有先给出对应变化，就要优先查执行器接口、反馈状态或总线响应是否异常。"
            ),
            order_events=order_events,
        )

    if request and not response:
        return _temporal_result(
            confidence="medium",
            cause_side="执行器响应侧",
            headline="请求已经出现，但响应没有形成对应变化",
            final_cause=(
                f"最终原因更偏向执行器未跟随：已经看到 {_event_short(request)}，"
                "但窗口里没有匹配到响应侧的同步变化。优先查执行器是否接收、拒绝、降级或反馈延迟。"
            ),
            order_events=order_events,
        )

    if response and not request and (perf_count or actuator_count):
        return _temporal_result(
            confidence="medium",
            cause_side="执行器响应侧",
            headline="响应侧有变化，但请求侧没有对应先导",
            final_cause=(
                f"最终原因更偏向执行器/接口侧：{_event_short(response)} 出现了变化，"
                "但没有看到请求侧先导变化。需要确认请求信号是否缺失，或执行器是否存在自触发/反馈异常。"
            ),
            order_events=order_events,
        )

    if algorithm_cause and not request and _scope_needs_algorithm_context(problem_scope):
        return _temporal_result(
            confidence="medium",
            cause_side="算法/控制请求侧",
            headline="场景风险先出现，但请求没有跟上",
            final_cause=(
                f"最终原因更偏向请求漏触发或策略未响应：{_event_short(algorithm_cause)} 已先出现，"
                "但请求侧没有形成对应变化。需要继续查目标选择、触发门限和功能接管状态。"
            ),
            order_events=order_events,
        )

    return _temporal_result(
        confidence="low",
        cause_side="证据不足",
        headline="能看到部分信号变化，但先后关系还不够闭合",
        final_cause=(
            "当前只能列出变化顺序，暂时不能稳定判断最终原因。"
            "请优先补齐同一时间窗内的现象、请求、响应和诱因信号。"
        ),
        order_events=order_events or timeline[:6],
    )


def _temporal_result(
    *,
    confidence: str,
    cause_side: str,
    headline: str,
    final_cause: str,
    order_events: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(order_events, key=lambda item: float(item.get("time", 0.0)))
    return {
        "confidence": confidence,
        "cause_side": cause_side,
        "headline": headline,
        "final_cause": final_cause,
        "order": [_public_temporal_event(event) for event in ordered],
        "order_text": _temporal_order_text(ordered),
    }


def _earliest_temporal_event(timeline: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    events = [event for event in timeline if event.get("kind") == kind]
    if not events:
        return None
    return min(events, key=lambda item: (float(item.get("time", 0.0)), -float(item.get("strength", 0.0))))


def _time_before_or_together(first: dict[str, Any], second: dict[str, Any], tolerance: float) -> bool:
    return float(first.get("time", 0.0)) <= float(second.get("time", 0.0)) + tolerance


def _event_short(event: dict[str, Any]) -> str:
    side = str(event.get("side") or event.get("kind") or "信号")
    signal = str(event.get("signal") or "")
    time_value = _as_float(event.get("time"))
    if time_value is None:
        return f"{side} {signal}"
    return f"{side} {signal} 在 {time_value:.3f}s 变化"


def _temporal_order_text(events: list[dict[str, Any]]) -> str:
    ordered = sorted(events, key=lambda item: float(item.get("time", 0.0)))
    return " → ".join(_event_short(event) for event in ordered[:8])


def _public_temporal_event(event: dict[str, Any]) -> dict[str, Any]:
    previous = _as_float(event.get("previous"))
    current = _as_float(event.get("current"))
    return {
        "time": f"{float(event.get('time', 0.0)):.3f}s",
        "side": str(event.get("side", "")),
        "issue": str(event.get("issue", "")),
        "signal": str(event.get("signal", "")),
        "change": f"{_fmt_number(previous or 0.0)} -> {_fmt_number(current or 0.0)}",
    }


def _build_conclusion(
    *,
    problem_scope: str,
    perf_count: int,
    perf_events: int,
    actuator_count: int,
    phenomenon_count: int,
    request_count: int,
    algorithm_cause_count: int,
    response_count: int,
    missing_phenomenon: list[str],
    missing_request: list[str],
    missing_algorithm_cause: list[str],
    missing_response: list[str],
    has_error: bool,
    diagnosis_steps: list[dict[str, str]] | None = None,
    temporal_judgement: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    diagnosis_steps = diagnosis_steps or []
    temporal_judgement = temporal_judgement or {}
    is_heavy_brake = any(term in problem_scope for term in ("重制动", "急减速", "误制动"))
    critical_gaps = _critical_signal_gaps(
        problem_scope,
        missing_phenomenon,
        missing_request,
        missing_response,
        missing_algorithm_cause,
    )
    phenomenon_gaps = critical_gaps.get("现象层", [])
    request_response_has_gaps = bool(
        phenomenon_gaps or critical_gaps.get("请求侧") or critical_gaps.get("响应侧")
    )
    algorithm_cause_gaps = critical_gaps.get("诱因侧", [])
    if temporal_judgement and temporal_judgement.get("confidence") in {"high", "medium"}:
        cause_side = str(temporal_judgement.get("cause_side", "") or "按时序链判断")
        headline = str(temporal_judgement.get("headline", "") or temporal_judgement.get("final_cause", ""))
        summary = str(temporal_judgement.get("final_cause", "") or headline)
        if algorithm_cause_gaps and temporal_judgement.get("confidence") == "medium":
            summary += " 诱因侧还缺关键上下文，不能把具体原因直接说成前车、Cut-in、曲率限速或 SetSpeed。"
        chain_hint = _chain_hint_from_steps(diagnosis_steps)
        if chain_hint and chain_hint not in summary:
            summary += f" {chain_hint}"
        return cause_side, headline, summary
    if temporal_judgement and temporal_judgement.get("confidence") == "low":
        cause_side = str(temporal_judgement.get("cause_side", "") or "证据不足")
        headline = str(temporal_judgement.get("headline", "") or "先后关系还不够闭合")
        summary = str(temporal_judgement.get("final_cause", "") or headline)
        order_text = str(temporal_judgement.get("order_text", "") or "")
        if order_text and order_text not in summary:
            summary += f" 当前能看到的先后顺序是：{order_text}。"
        return cause_side, headline, summary
    if perf_count and _has_critical_gaps(critical_gaps) and request_response_has_gaps:
        cause_side = "证据还不够，先别急着定根因"
        headline = (
            f"{problem_scope}现象已经露头了，但现象、请求、响应这三段证据还没有闭环，"
            "现在不适合直接说是算法请求问题或执行器响应问题。"
        )
        summary = _human_gap_summary(
            "要把原因说清楚，至少得同时看到实车表现、算法/控制请求和执行器实际响应。",
            critical_gaps,
        )
    elif perf_count and algorithm_cause_gaps and not request_response_has_gaps and request_count and response_count:
        cause_side = "先看请求侧，场景原因还缺一块"
        headline = (
            f"{problem_scope}现象已确认，请求侧和响应侧也能读到；"
            "执行器暂时没有形成明确断点，更应该先追算法为什么给出了这个请求。"
        )
        summary = _human_gap_summary(
            "现在能比较请求有没有被执行器跟上，但还缺少解释请求来源的场景输入。",
            {"诱因侧": algorithm_cause_gaps},
        )
    elif perf_count and _has_critical_gaps(critical_gaps):
        cause_side = "现象成立，但证据链还没闭合"
        headline = (
            f"{problem_scope}现象能确认，但关键证据缺口还在，"
            "现在只能说问题存在，不能稳稳归到请求侧或执行器侧。"
        )
        if actuator_count:
            summary = (
                "虽然同时看到执行器接口风险，但关键证据不全时，不能直接判定执行器就是根因。"
                "需要先补齐同一时间窗内的关键请求和响应信号，再比较请求和响应谁先变化。"
            )
        else:
            summary = (
                f"当前只能说明实车确实出现了{problem_scope}，暂未看到明确执行器断握手/降级证据；"
                "但关键证据不全，所以无法解释为什么会触发这次现象。"
            )
    elif perf_count and actuator_count:
        cause_side = "执行器响应侧有风险，需要和请求侧对齐确认"
        headline = (
            f"这段数据里既有{problem_scope}现象，也看到 {actuator_count} 个执行器接口风险点；"
            "更像是请求和响应链路里有一段没有闭环，需要按时间窗对齐。"
        )
        summary = "如果请求是合理发出的、但响应没有跟上，优先查执行器；如果请求本身先突变或不合理，再转到控制策略侧查。"
    elif perf_count and request_count and response_count:
        lacks_cause_context = _scope_needs_algorithm_context(problem_scope) and not algorithm_cause_count
        if actuator_count:
            cause_side = "请求和响应都要看，执行器侧有风险点"
        elif lacks_cause_context:
            cause_side = "先看请求侧，但还缺场景诱因证据"
        else:
            cause_side = "先看请求侧，执行器暂未见明显断点"
        if is_heavy_brake:
            headline = (
                "重制动/急减速现象能确认，请求侧和响应侧也都有数据。"
                "下一步的关键不是看数量，而是看制动请求是不是先主动变大。"
            )
            summary = (
                "判断口径：如果 ACC/AEB 制动扭矩或目标减速度请求先突变，且 EBCM/实际制动响应随后跟随，"
                "更偏算法/控制策略为什么发出强制动；如果请求侧没有强制动变化，但制动响应突然激活或扭矩异常，"
                "才更偏执行器接口或制动响应异常。"
            )
            if lacks_cause_context:
                summary += " 但这次还没有形成可用的目标、曲率限速或 SetSpeed 证据，所以只能说请求侧值得优先查，暂时不能解释为什么会这么请求。"
        else:
            headline = f"{problem_scope}现象能确认，请求侧和响应侧也都有证据；当前优先看请求是不是先发生了目标/扭矩变化。"
            summary = "如果请求先变、响应只是跟随，更像请求侧/控制策略触发；如果请求正常但响应滞后、拒绝或不闭环，更像执行器响应侧。"
            if lacks_cause_context:
                summary += " 当前还缺少能解释请求来源的场景证据，所以不要直接把原因说成前车、Cut-in、曲率限速或 SetSpeed。"
    elif perf_count and request_count:
        cause_side = "请求侧能看到，响应侧还差证据"
        headline = f"{problem_scope}现象能确认，也读到了请求侧信号；但响应侧还不完整，暂时不能判断执行器有没有跟上。"
        summary = "这时不应该直接说执行器问题。先看请求侧是否给出了异常目标、扭矩或制动类型，再补响应侧闭环证据。"
    elif perf_count:
        cause_side = "现象成立，但还不能判断请求还是响应"
        headline = f"{problem_scope}现象能确认，识别到 {perf_count} 类异常、{perf_events} 个时间点；但请求侧和响应侧证据还没有同时闭合。"
        summary = "这一步只能说明车确实表现异常。要判断原因，至少还需要同一时间窗内的请求侧信号和执行器响应信号一起看。"
    elif actuator_count:
        cause_side = "执行器侧有风险，但还要补现象和请求"
        headline = f"识别到 {actuator_count} 个执行器接口风险点，执行器响应链路值得优先看。"
        summary = "不过如果用户关心体感现象，还需要把实车表现和请求侧一起拉进来，确认是不是请求合理但响应没闭环。"
    elif has_error:
        cause_side = "分析未完全完成"
        headline = "部分分析链路没有跑通，当前不能稳定判断执行器侧还是算法侧。"
        summary = "请先按失败原因处理数据库、文件格式或信号匹配问题，再重新上传同一段数据做归因。"
    else:
        cause_side = "暂未发现明确异常"
        headline = "性能指标和执行器接口规则均未发现明确问题点。"
        summary = "当前数据在已覆盖规则内没有形成明确异常。若体感问题仍存在，建议补充具体发生时间段和更完整的目标信号。"

    if missing_phenomenon:
        summary += f" 当前现象层缺失：{'、'.join(missing_phenomenon[:5])}" + (" 等。" if len(missing_phenomenon) > 5 else "。")
    if missing_request:
        summary += f" 当前请求侧缺失：{'、'.join(missing_request[:5])}" + (" 等。" if len(missing_request) > 5 else "。")
    if missing_algorithm_cause:
        summary += f" 当前诱因侧缺失：{'、'.join(missing_algorithm_cause[:5])}" + (" 等。" if len(missing_algorithm_cause) > 5 else "。")
    if missing_response:
        summary += f" 当前响应侧缺失：{'、'.join(missing_response[:5])}" + (" 等。" if len(missing_response) > 5 else "。")
    chain_hint = _chain_hint_from_steps(diagnosis_steps)
    if chain_hint:
        summary += f" {chain_hint}"
    return cause_side, headline, summary


def _human_gap_summary(prefix: str, gaps: dict[str, list[str]]) -> str:
    parts: list[str] = []
    labels = {
        "现象层": "实车表现",
        "请求侧": "请求侧",
        "诱因侧": "场景诱因",
        "响应侧": "执行器响应",
    }
    for side in ("现象层", "请求侧", "响应侧", "诱因侧"):
        names = list(gaps.get(side, []) or [])
        if not names:
            continue
        label = labels.get(side, side)
        parts.append(f"{label}还缺 {'、'.join(names[:4])}" + (" 等" if len(names) > 4 else ""))
    if not parts:
        return prefix
    return f"{prefix} 现在卡在：{'; '.join(parts)}。"


def _build_diagnosis_steps(
    *,
    problem_scope: str,
    perf_count: int,
    perf_events: int,
    actuator_count: int,
    phenomenon_count: int,
    request_count: int,
    algorithm_cause_count: int,
    response_count: int,
    missing_phenomenon: list[str],
    missing_request: list[str],
    missing_algorithm_cause: list[str],
    missing_response: list[str],
    evidence_rows: list[dict[str, str]],
    has_error: bool,
    temporal_judgement: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    temporal_judgement = temporal_judgement or {}
    request_evidence = _first_evidence_by_side(evidence_rows, "算法请求侧")
    algorithm_cause_evidence = _first_evidence_by_side(evidence_rows, "算法请求诱因")
    response_evidence = _first_evidence_by_side(evidence_rows, "执行器响应侧")
    phenomenon_evidence = _first_evidence_by_side(evidence_rows, "现象层")
    scope = problem_scope or "用户描述的行车异常"
    critical_gaps = _critical_signal_gaps(
        problem_scope,
        missing_phenomenon,
        missing_request,
        missing_response,
        missing_algorithm_cause,
    )
    critical_missing_phenomenon = critical_gaps.get("现象层", [])
    critical_missing_request = critical_gaps.get("请求侧", [])
    critical_missing_algorithm = critical_gaps.get("诱因侧", [])
    critical_missing_response = critical_gaps.get("响应侧", [])
    rows: list[dict[str, str]] = []

    if perf_count:
        rows.append(
            {
                "step": "现象先确认",
                "result": (
                    f"能确认。性能侧识别到 {perf_count} 类问题、{perf_events} 个时间点，关注现象是 {scope}。"
                    f"同时读到 {phenomenon_count} 个实车表现信号。"
                    f"{' 代表性证据：' + phenomenon_evidence if phenomenon_evidence else ''}"
                ),
            }
        )
    elif critical_missing_phenomenon:
        rows.append(
            {
                "step": "现象先确认",
                "result": (
                    f"还不够稳。当前缺少判断 {scope} 的实车表现关键量：{'、'.join(critical_missing_phenomenon)}。"
                    "先把现象本身钉住，再看请求和响应。"
                ),
            }
        )
    elif has_error:
        rows.append(
            {
                "step": "现象先确认",
                "result": "还不能确认。性能分析或文件/数据库链路没有完全跑通，需要先处理失败原因。",
            }
        )
    else:
        rows.append(
            {
                "step": "现象先确认",
                "result": f"未在当前覆盖规则内确认 {scope}。",
            }
        )

    if critical_missing_request:
        rows.append(
            {
                "step": "请求有没有先变",
                "result": (
                    f"当前缺少判断 {scope} 原因的请求侧关键量：{'、'.join(critical_missing_request)}。"
                    "即使读到了其他请求信号，也不能替代这些关键请求证据，因此还不能判断算法/控制策略是否主动触发该现象。"
                ),
            }
        )
    elif request_count:
        rows.append(
            {
                "step": "请求有没有先变",
                "result": (
                    f"已读到 {request_count} 个请求侧信号。重点看它们是否在问题时间窗先于实车表现发生目标、扭矩或制动类型变化。"
                    f"{' 当前窗口摘要：' + request_evidence if request_evidence else ''}"
                ),
            }
        )
    else:
        rows.append(
            {
                "step": "请求有没有先变",
                "result": "请求侧关键量没有读到，当前无法判断算法/控制策略是否主动发出了异常请求。",
            }
        )

    if critical_missing_algorithm:
        rows.append(
            {
                "step": "如果是请求侧，为什么会请求",
                "result": (
                    f"当前缺少解释 {scope} 请求来源的场景输入：{'、'.join(critical_missing_algorithm)}。"
                    "因此即使现象、请求和响应都能读到，也不能判断是前车/Cut-in/曲率限速/SetSpeed 等因素导致算法主动给出请求。"
                ),
            }
        )
    elif algorithm_cause_count:
        rows.append(
            {
                "step": "如果是请求侧，为什么会请求",
                "result": (
                    f"已读到 {algorithm_cause_count} 个诱因侧信号。"
                    "需要看这些场景输入是否在问题窗口前后发生目标、曲率限速或 SetSpeed 变化。"
                    f"{' 当前窗口摘要：' + algorithm_cause_evidence if algorithm_cause_evidence else ''}"
                ),
            }
        )
    elif _scope_needs_algorithm_context(problem_scope) and request_count:
        rows.append(
            {
                "step": "如果是请求侧，为什么会请求",
                "result": (
                    "当前还没有形成可用的场景诱因证据。也就是说，就算请求侧先发生了变化，"
                    "也还需要继续看前车/Cut-in、曲率限速、SetSpeed 或接管状态，"
                    "才能解释算法为什么给出这个请求。"
                ),
            }
        )
    elif algorithm_cause_evidence:
        rows.append(
            {
                "step": "如果是算法请求导致，继续看什么",
                "result": (
                    "继续看前车/Cut-in、曲率限速和 SetSpeed 等场景信号，"
                    "判断算法减速或扭矩波动是否有合理触发源。"
                    f" 当前窗口摘要：{algorithm_cause_evidence}"
                ),
            }
        )

    if critical_missing_response:
        rows.append(
            {
                "step": "执行器有没有跟上",
                "result": (
                    f"当前缺少判断 {scope} 原因的响应侧关键量：{'、'.join(critical_missing_response)}。"
                    "即使部分响应信号存在，也不能完整判断执行器是否按请求闭环。"
                ),
            }
        )
    elif actuator_count:
        rows.append(
            {
                "step": "执行器有没有跟上",
                "result": (
                    f"执行器规则命中 {actuator_count} 个风险点，需要优先看未响应信号和期望响应是否与现象窗口重叠。"
                    f"{' 当前窗口摘要：' + response_evidence if response_evidence else ''}"
                ),
            }
        )
    elif response_count:
        rows.append(
            {
                "step": "执行器有没有跟上",
                "result": (
                    f"已读到 {response_count} 个响应侧信号，但执行器规则暂未发现明确断握手/降级。"
                    "这不等于执行器绝对正常，只说明当前规则没有抓到未响应闭环。"
                    f"{' 当前窗口摘要：' + response_evidence if response_evidence else ''}"
                ),
            }
        )
    else:
        rows.append(
            {
                "step": "执行器有没有跟上",
                "result": "响应侧关键量没有读到，当前无法判断执行器是否跟随请求。",
            }
        )

    if temporal_judgement.get("order_text") or temporal_judgement.get("final_cause"):
        rows.append(
            {
                "step": "谁先变化",
                "result": (
                    f"{temporal_judgement.get('order_text', '')}"
                    f"{'。' if temporal_judgement.get('order_text') else ''}"
                    f"{temporal_judgement.get('final_cause', '')}"
                ).strip(),
            }
        )

    rows.append(
        {
            "step": "我现在会怎么判断",
            "result": _diagnosis_takeaway(
                problem_scope=problem_scope,
                perf_count=perf_count,
                actuator_count=actuator_count,
                phenomenon_count=phenomenon_count,
                request_count=request_count,
                algorithm_cause_count=algorithm_cause_count,
                response_count=response_count,
                missing_phenomenon=missing_phenomenon,
                missing_request=missing_request,
                missing_algorithm_cause=missing_algorithm_cause,
                missing_response=missing_response,
            ),
        }
    )
    return rows


def _diagnosis_takeaway(
    *,
    problem_scope: str,
    perf_count: int,
    actuator_count: int,
    phenomenon_count: int,
    request_count: int,
    algorithm_cause_count: int,
    response_count: int,
    missing_phenomenon: list[str],
    missing_request: list[str],
    missing_algorithm_cause: list[str],
    missing_response: list[str],
) -> str:
    is_heavy_brake = any(term in problem_scope for term in ("重制动", "急减速", "误制动"))
    critical_gaps = _critical_signal_gaps(
        problem_scope,
        missing_phenomenon,
        missing_request,
        missing_response,
        missing_algorithm_cause,
    )
    if perf_count and _has_critical_gaps(critical_gaps):
        phenomenon_gap = critical_gaps.get("现象层", [])
        request_gap = critical_gaps.get("请求侧", [])
        algorithm_gap = critical_gaps.get("诱因侧", [])
        response_gap = critical_gaps.get("响应侧", [])
        gap_parts = []
        if phenomenon_gap:
            gap_parts.append(f"现象层缺 {'、'.join(phenomenon_gap[:4])}")
        if request_gap:
            gap_parts.append(f"请求侧缺 {'、'.join(request_gap[:4])}")
        if algorithm_gap:
            gap_parts.append(f"诱因侧缺 {'、'.join(algorithm_gap[:4])}")
        if response_gap:
            gap_parts.append(f"响应侧缺 {'、'.join(response_gap[:4])}")
        if algorithm_gap and not request_gap and not response_gap and request_count and response_count:
            return (
                f"我会先把它看成“请求侧需要继续解释”的问题：{problem_scope}已经出现，请求和响应也都能看到，"
                "但还缺场景输入，所以现在只能说请求可能是主动给出的，不能说清楚是前车、Cut-in、曲率限速还是 SetSpeed 触发。"
            )
        return (
            f"我现在只能确认“确实出现了{problem_scope}”。由于关键证据不完整"
            f"（{'; '.join(gap_parts)}），当前不能判断原因，也不能直接把某一侧当成根因。"
            "需要补齐关键信号后，再比较同一时间窗内请求和响应谁先变化。"
        )
    if is_heavy_brake and perf_count and not actuator_count:
        if request_count and response_count:
            return (
                "我会先从请求侧查起：重制动/急减速已经确认，也没有明显执行器断握手证据。"
                "如果同窗里制动请求先变大，就是控制策略为什么这么请求；如果请求没变但响应突变，再转查制动执行器/接口。"
            )
        if request_count:
            return "这份结果能确认重制动现象，但响应侧证据不完整，不能判断执行器是否跟随。"
        return "这份结果只能确认重制动现象，缺少请求侧关键量，所以还解释不了为什么会重制动。"
    if perf_count and actuator_count:
        return "现象和执行器风险都存在，我会优先把请求和响应放到同一个时间窗里对齐：请求合理但响应没跟上，偏执行器；请求本身先异常，偏控制侧。"
    if perf_count and request_count and response_count:
        if algorithm_cause_count:
            return "现象、请求、响应和场景输入都有数据，我会按“请求是否先变、响应是否跟随、场景是否能解释请求”的顺序给结论。"
        return "现象、请求侧、响应侧都有数据，我会先判断请求和响应谁先变化；如果偏请求侧，再补看场景输入解释为什么这么请求。"
    if perf_count:
        return "当前主要停留在现象确认阶段，还不能稳定归到算法侧或执行器侧。"
    if actuator_count:
        return "当前主要是执行器接口风险，若用户关注体感问题，还需要同时补性能侧现象窗口。"
    if missing_request or missing_response:
        return "当前证据链不完整，需要先补齐缺失信号或确认数据库/通道。"
    return "当前没有形成明确归因。"


def _critical_signal_gaps(
    problem_scope: str,
    missing_phenomenon: list[str],
    missing_request: list[str],
    missing_response: list[str],
    missing_algorithm_cause: list[str] | None = None,
) -> dict[str, list[str]]:
    scope = problem_scope or ""
    phenomenon_missing = set(missing_phenomenon or [])
    missing = set(missing_request or [])
    algorithm_missing = set(missing_algorithm_cause or [])
    response_missing = set(missing_response or [])
    phenomenon_critical: list[str] = []
    request_critical: list[str] = []
    algorithm_critical: list[str] = []
    response_critical: list[str] = []

    def add_phenomenon(*names: str) -> None:
        phenomenon_critical.extend(names)

    def add_request(*names: str) -> None:
        request_critical.extend(names)

    def add_response(*names: str) -> None:
        response_critical.extend(names)

    def add_algorithm(*names: str) -> None:
        algorithm_critical.extend(names)

    selected_presets = _selected_performance_presets(scope)
    if selected_presets:
        add_phenomenon(*_preset_signals(selected_presets, "vehicle_signals"))
        add_request(*_preset_signals(selected_presets, "algorithm_request_signals"))
        add_response(*_preset_signals(selected_presets, "actuator_request_signals"))
        add_algorithm(*_preset_signals(selected_presets, "algorithm_cause_signals", include_flt1_flag=True))

    if not selected_presets and any(term in scope for term in ("重制动", "急减速", "误制动", "漏制动", "制动")):
        add_phenomenon("IIMULonAccSec", "VehSpdAvgDrvn_1")
        add_request(
            "ACCBSCE_ACCTrq1",
            "ACCBSCE_ACCAccl1",
            "ACCBSCE_AutBrkTp1",
            "ACCBSCE_ACCAct1",
            "FCACBSC_CtrlTrq1",
            "FCACBSC_AutBrkRqTp1",
        )
        add_algorithm(
            "LeadTargetDistance",
            "LeadTargetVelocity",
            "CutInTargetDistance",
            "CutInTargetVelocity",
            "CurvatureLimitSpeed",
            "Curvature",
            "CurvatureLimitEnable",
            "IACCDrvrSeltdSpd",
        )
        add_response("ACCBrkngAct", "BrkSysAutBrkStat", "AutoBrkngAct", "ICSTBATS_TrqVl")

    if not selected_presets and any(term in scope for term in ("顿挫", "抖动", "颠簸")):
        add_phenomenon("IIMULonAccSec", "VehSpdAvgDrvn_1", "IACCAct376")
        add_request("ACCATC_AxlTrqRq", "ACCBSCE_ACCTrq1", "ACCBSCE_ACCAccl1")
        add_algorithm(
            "LeadTargetDistance",
            "LeadTargetVelocity",
            "LeadTargetAccel",
            "CutInTargetDistance",
            "CutInTargetVelocity",
            "CutInTargetAccel",
            "CurvatureLimitSpeed",
            "Curvature",
            "CurvatureLimitEnable",
            "IACCDrvrSeltdSpd",
        )
        add_response("ActAxleTrq", "ACCBrkngAct", "BrkSysAutBrkStat", "ICSTBATS_TrqVl")

    if not selected_presets and any(term in scope for term in ("不合理低速", "未达到 set speed", "跟车距离异常")):
        add_phenomenon("VehSpdAvgDrvn_1", "VehSpdAvgNDrvn", "IACCAct376")
        add_request("ACCATC_ACCAct", "ACCATC_AxlTrqRq", "ACCATC_ACCTyp")
        add_algorithm(
            "LeadTargetDistance",
            "LeadTargetVelocity",
            "LeadTargetTTC",
            "CutInTargetDistance",
            "CutInTargetVelocity",
            "CutInTargetTTC",
            "CurvatureLimitSpeed",
            "Curvature",
            "CurvatureLimitEnable",
            "IACCDrvrSeltdSpd",
        )
        add_response("ACCATCS_RqStat", "ACCATCS_LmtgStat", "ActAxleTrq")

    if not selected_presets and any(
        term in scope for term in ("跟车距离", "车距异常", "距离异常", "时距异常", "headway", "following distance")
    ):
        add_algorithm("HeadwaySetting")

    if not selected_presets and "溜车" in scope:
        add_phenomenon("VehSpdAvgDrvn_1", "VehSpdAvgNDrvn", "VehicleStandstillFlag")
        add_request("ACCATC_AxlTrqRq", "ACCBSCE_ACCAct1", "ACCBSCE_AutBrkTp1", "ACCBSCE_ACCAccl1", "ACCBSCE_ACCTrq1")
        add_algorithm("RoadSlope", "VehicleStandstillFlag", "ITransEstGear_ECP_H1", "IACCAct376")
        add_response("ActAxleTrq", "ACCBrkngAct", "BrkSysAutBrkStat")

    algorithm_gaps = _collapse_flt1_target_critical_gaps(
        _missing_critical_names(algorithm_critical, algorithm_missing),
        algorithm_missing,
    )

    return {
        "现象层": _missing_critical_names(phenomenon_critical, phenomenon_missing),
        "请求侧": _missing_critical_names(request_critical, missing),
        "诱因侧": algorithm_gaps,
        "响应侧": _missing_critical_names(response_critical, response_missing),
    }


def _scope_needs_algorithm_context(problem_scope: str) -> bool:
    scope = problem_scope or ""
    return any(
        term in scope
        for term in (
            "重制动",
            "急减速",
            "误制动",
            "漏制动",
            "制动",
            "顿挫",
            "抖动",
            "颠簸",
            "不合理低速",
            "未达到 set speed",
            "跟车距离异常",
        )
    )


FLT1_TARGET_MISSING_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("LeadTargetId", "CutInTargetId", "FLT1目标ID（跟车/Cut-in 复用）"),
    ("LeadTargetDistance", "CutInTargetDistance", "FLT1目标距离（跟车/Cut-in 复用）"),
    ("LeadTargetVelocity", "CutInTargetVelocity", "FLT1目标相对速度（跟车/Cut-in 复用）"),
    ("LeadTargetAccel", "CutInTargetAccel", "FLT1目标加速度（跟车/Cut-in 复用）"),
    ("LeadTargetTTC", "CutInTargetTTC", "FLT1目标TTC（由距离/相对速度计算）"),
)

FLT1_TARGET_LOGICAL_NAMES: frozenset[str] = frozenset(
    name for pair in FLT1_TARGET_MISSING_PAIRS for name in pair[:2]
)


def _normalize_flt1_target_missing_names(names: Iterable[str]) -> list[str]:
    raw_names = list(names or [])
    raw_set = set(raw_names)
    result = [name for name in raw_names if name not in FLT1_TARGET_LOGICAL_NAMES]
    for left, right, label in FLT1_TARGET_MISSING_PAIRS:
        if left in raw_set or right in raw_set:
            result.append(label)
    return sorted(dict.fromkeys(result))


def _collapse_flt1_target_critical_gaps(gaps: list[str], missing_names: set[str]) -> list[str]:
    result = [name for name in gaps if name not in FLT1_TARGET_LOGICAL_NAMES]
    for left, right, label in FLT1_TARGET_MISSING_PAIRS:
        if _critical_name_missing(left, missing_names) and _critical_name_missing(right, missing_names):
            result.append(label)
    return list(dict.fromkeys(result))


def _critical_name_missing(name: str, missing_names: set[str]) -> bool:
    for variant in _candidate_aliases(name, allow_bare_short=True):
        if variant in missing_names or _short_name(variant) in missing_names:
            return True
    return False


def _missing_critical_names(candidates: list[str], missing_names: set[str]) -> list[str]:
    result: list[str] = []
    for name in dict.fromkeys(candidates):
        if _critical_name_missing(name, missing_names):
            result.append(name)
    return result


def _has_critical_gaps(gaps: dict[str, list[str]]) -> bool:
    return any(bool(values) for values in gaps.values())


def _first_evidence_by_side(rows: list[dict[str, str]], side: str) -> str:
    for row in rows:
        if row.get("side") == side and row.get("evidence"):
            return str(row.get("evidence", ""))[:220]
    return ""


def _chain_hint_from_steps(steps: list[dict[str, str]]) -> str:
    for row in steps:
        if row.get("step") in {"当前能解释到哪一步", "我现在会怎么判断"} and row.get("result"):
            return row["result"]
    return ""


def _performance_evidence_rows(problems: list[Any], limit: int = 8) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for issue in problems[:limit]:
        events = list(getattr(issue, "events", []) or [])
        first_event = events[0] if events else {}
        rows.append(
            {
                "side": "现象层",
                "issue": metric_title(getattr(issue, "metric_name", "")),
                "time": _event_time_text(first_event, len(events)),
                "evidence": _issue_detail_text(issue),
                "expected": "这只能说明实车表现异常，不能单独代表算法或执行器有问题",
            }
        )
    return rows


def _actuator_evidence_rows(result: Any, limit: int = 8) -> list[dict[str, str]]:
    if result is None:
        return []
    rows: list[dict[str, str]] = []
    for item in actuator_issue_rows(result, limit=limit):
        rows.append(
            {
                "side": "执行器响应侧",
                "issue": item.get("issue", ""),
                "time": item.get("time_window", ""),
                "evidence": f"{item.get('failed_signals', '')}：{item.get('actual', '')}".strip("："),
                "expected": item.get("expected", ""),
            }
        )
    return rows


def _collect_windows(
    performance_problems: list[Any],
    actuator_violations: list[Any],
    *,
    max_performance_events: int | None = 2,
    max_actuator_violations: int | None = 4,
) -> list[tuple[float, float, str]]:
    windows: list[tuple[float, float, str]] = []
    for issue in performance_problems:
        issue_title = metric_title(getattr(issue, "metric_name", ""))
        events = list(getattr(issue, "events", []) or [])
        if max_performance_events is not None:
            events = events[:max_performance_events]
        for event in events:
            start = _as_float(event.get("start"))
            end = _as_float(event.get("end"))
            if start is None or end is None:
                continue
            windows.append((max(0.0, start - 0.2), end + 0.2, f"{issue_title}：{start:.3f}s-{end:.3f}s"))
    violations = actuator_violations
    if max_actuator_violations is not None:
        violations = violations[:max_actuator_violations]
    for violation in violations:
        start = _as_float(getattr(violation, "start_time", None))
        end = _as_float(getattr(violation, "end_time", None))
        if start is None or end is None:
            continue
        title = str(getattr(getattr(violation, "rule", None), "violation", "") or "执行器风险")
        windows.append((max(0.0, start - 0.2), end + 0.2, f"{title}：{start:.3f}s-{end:.3f}s"))

    deduped: list[tuple[float, float, str]] = []
    seen: set[tuple[float, float]] = set()
    for start, end, title in sorted(windows, key=lambda item: item[0]):
        key = (round(start, 2), round(end, 2))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((start, end, title))
    return deduped


def _analysis_h5_file(performance_result: Any) -> Path | None:
    if performance_result is None:
        return None
    for attr in ("local_file", "h5_file", "source_file", "file_path", "input_file"):
        candidate = getattr(performance_result, attr, None)
        if not candidate:
            continue
        path = Path(candidate)
        if path.suffix.lower() in {".h5", ".hdf5"} and path.exists():
            return path
    return None


def _build_alias_index(signal_names: list[str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for signal_name in sorted(signal_names):
        for alias in _candidate_aliases(signal_name, allow_bare_short=True):
            aliases.setdefault(alias.lower(), signal_name)
    return aliases


def _resolve_signal(alias_index: dict[str, str], candidate: str) -> str | None:
    for alias in _candidate_aliases(candidate, allow_bare_short="." not in candidate):
        resolved = alias_index.get(alias.lower())
        if resolved:
            return resolved
    return None


def _candidate_aliases(name: str, *, allow_bare_short: bool) -> list[str]:
    value = _clean_signal_candidate(name)
    if not value:
        return []
    if value in _load_vehicle_response_signal_catalog().blocked_keys:
        return []
    aliases = _raw_candidate_aliases(value, allow_bare_short=allow_bare_short)
    for mapped in _catalog_aliases_for(value):
        aliases.extend(_raw_candidate_aliases(mapped, allow_bare_short=True))
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _raw_candidate_aliases(name: str, *, allow_bare_short: bool) -> list[str]:
    value = str(name or "").strip()
    aliases = [value]
    slash_value = value.replace("\\", "/").strip("/")
    if slash_value and slash_value != value:
        aliases.append(slash_value)
    for path_alias in _path_signal_aliases(slash_value):
        aliases.append(path_alias)
    if "." in value:
        message, short = value.rsplit(".", 1)
        for variant in _short_variants(short):
            aliases.append(f"{message}.{variant}")
        if allow_bare_short:
            aliases.extend(_short_variants(short))
    else:
        aliases.extend(_short_variants(value))
    return aliases


def _path_signal_aliases(name: str) -> list[str]:
    value = str(name or "").strip().replace("\\", "/").strip("/")
    if not value:
        return []
    aliases = [value]
    if "/signals/" in value:
        message_path, signal_part = value.rsplit("/signals/", 1)
        signal = signal_part.split("/", 1)[0]
        message_name = message_path.rsplit("/", 1)[-1]
        aliases.extend(_short_variants(signal))
        for message_alias in _message_name_aliases(message_name):
            aliases.append(f"{message_alias}.{signal}")
    elif "/" in value:
        aliases.append(value.rsplit("/", 1)[-1])
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _message_name_aliases(message_name: str) -> list[str]:
    value = str(message_name or "").strip()
    aliases = [value]
    no_prefix = re.sub(r"^0x[0-9A-Fa-f]+_", "", value)
    aliases.append(no_prefix)
    aliases.append(re.sub(r"_0x[0-9A-Fa-f]+$", "", no_prefix))
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _clean_signal_candidate(name: str) -> str:
    value = str(name or "").strip()
    if not value:
        return ""
    for marker in ("（", "(", "，", ",", "；", ";"):
        if marker in value:
            value = value.split(marker, 1)[0].strip()
    return value


def _short_variants(short: str) -> list[str]:
    value = str(short or "").strip()
    variants = [value]
    if len(value) > 1 and value.startswith("I") and value[1].isupper():
        variants.append(value[1:])
    elif value:
        variants.append(f"I{value}")
    return variants


def _range_summary(session: Any, signal_name: str, start: float, end: float) -> str:
    _, values = _range_series(session, signal_name, start, end)
    if not values:
        return ""
    first = values[0]
    last = values[-1]
    min_value = min(values)
    max_value = max(values)
    max_jump = _max_delta(values)
    label = _short_name(signal_name)
    if _nearly_equal(min_value, max_value):
        return f"{label}={_fmt_number(first)}"
    return (
        f"{label} {_fmt_number(first)}→{_fmt_number(last)}"
        f"（范围 {_fmt_number(min_value)}~{_fmt_number(max_value)}，最大跳变 {_fmt_number(max_jump)}）"
    )


def _numeric_values(values: Any) -> list[float]:
    result: list[float] = []
    for value in values or []:
        number = _as_float(value)
        if number is not None and math.isfinite(number):
            result.append(number)
    return result


def _max_delta(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return max(abs(values[index] - values[index - 1]) for index in range(1, len(values)))


def _event_time_text(event: dict[str, Any], event_count: int) -> str:
    if not event:
        return f"{event_count} 个时间点" if event_count else "-"
    start = _as_float(event.get("start"))
    end = _as_float(event.get("end"))
    if start is None or end is None:
        return f"{event_count} 个时间点"
    suffix = f"，共 {event_count} 个" if event_count > 1 else ""
    return f"{start:.3f}s-{end:.3f}s{suffix}"


def _issue_detail_text(issue: Any) -> str:
    details = list(getattr(issue, "details", []) or [])
    if details:
        return str(details[0])[:180]
    stats = getattr(issue, "statistics", {}) or {}
    if isinstance(stats, dict) and stats:
        parts = [f"{key}={value}" for key, value in list(stats.items())[:3]]
        return "；".join(parts)
    return "指标命中异常事件"


def _next_checks(
    perf_count: int,
    actuator_count: int,
    has_error: bool,
    missing_phenomenon: list[str],
    missing_request: list[str],
    missing_algorithm_cause: list[str],
    missing_response: list[str],
) -> list[str]:
    if has_error:
        return ["先处理未跑通链路的失败原因，确认数据库版本、文件格式和目标信号是否匹配。"]
    if missing_phenomenon:
        return [f"先补齐实车表现侧信号，把现象钉准：{'、'.join(missing_phenomenon[:5])}。"]
    if missing_request:
        return [f"补齐请求侧信号后再判断算法侧：{'、'.join(missing_request[:5])}。"]
    if missing_algorithm_cause:
        return [
            f"补齐或确认诱因侧信号后再解释算法为什么给出请求：{'、'.join(missing_algorithm_cause[:5])}。",
            "重点确认前车/Cut-in、曲率限速、SetSpeed 等场景输入是否在问题窗口前后变化。",
        ]
    if perf_count and actuator_count:
        return [
            "对齐性能异常时间窗、请求信号变化和执行器风险时间窗，看谁先发生。",
            "若请求合理但响应不闭环，优先查执行器；若请求本身突变或不合理，优先查算法策略。",
        ]
    if actuator_count:
        return ["优先排查未响应信号对应的执行器接口、EBCM/驱动系统状态和报文有效性。"]
    if perf_count:
        return ["继续查看算法侧目标车速、跟车目标、制动/驱动扭矩请求是否与场景一致。"]
    if missing_response:
        return [f"如需判断执行器响应，请补齐响应侧信号：{'、'.join(missing_response[:5])}。"]
    return ["若用户体感异常仍存在，建议补充具体时间段或更完整的目标信号。"]


def _short_name(name: str) -> str:
    return _clean_signal_candidate(name).rsplit(".", 1)[-1]


def _fmt_number(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:.1f}"
    if abs(value) >= 10:
        return f"{value:.2f}"
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _nearly_equal(left: float, right: float) -> bool:
    return abs(left - right) <= 1e-9


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
