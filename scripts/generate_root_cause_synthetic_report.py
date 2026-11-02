from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "sample_data" / "batch_root_cause" / "root_cause_synthetic_cases"
REPORT_PATH = OUT_DIR / "root_cause_synthetic_test_report.md"
SUMMARY_JSON_PATH = OUT_DIR / "root_cause_synthetic_summary.json"

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from performance_analysis import run_performance_analysis  # noqa: E402
from root_cause_analysis import RootCauseReport, build_root_cause_report  # noqa: E402
from batch_root_cause_analysis import _format_root_cause_summary  # noqa: E402


CASE_WINDOW = (10.0, 12.0)
SYNTHETIC_DATABASE_NAME = "Clea_Mode_Year23_ADM_LC_V27.78.4.arxml"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cases = _build_cases()
    summaries: list[dict[str, Any]] = []

    for case in cases:
        h5_path = OUT_DIR / f"{case['key']}.h5"
        signals = _base_signals()
        case["mutate"](signals)
        _sync_derived_aliases(signals)
        _write_h5(h5_path, signals)

        perf_result = run_performance_analysis(
            h5_path,
            database_name=SYNTHETIC_DATABASE_NAME,
            selected_problem_groups=["acc_control"],
            request_text=case["request_text"],
        )
        report = build_root_cause_report(
            request_text=case["request_text"],
            performance_result=perf_result,
            actuator_result=None,
        )
        summaries.append(_case_summary(case, h5_path, perf_result, report))

    SUMMARY_JSON_PATH.write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    REPORT_PATH.write_text(_render_report(summaries, generated_at), encoding="utf-8-sig")
    print(f"wrote {REPORT_PATH}")
    print(f"wrote {SUMMARY_JSON_PATH}")


def _time_axis() -> np.ndarray:
    return np.round(np.arange(0.0, 20.01, 0.1), 3)


def _base_array(value: float) -> np.ndarray:
    return np.full_like(_time_axis(), float(value), dtype=float)


def _mask(start: float = CASE_WINDOW[0], end: float = CASE_WINDOW[1]) -> np.ndarray:
    t = _time_axis()
    return (t >= start) & (t <= end)


def _ramp(start_value: float, end_value: float, start: float = CASE_WINDOW[0], end: float = CASE_WINDOW[1]) -> np.ndarray:
    t = _time_axis()
    arr = _base_array(start_value)
    window = _mask(start, end)
    if window.any():
        arr[window] = np.linspace(start_value, end_value, int(window.sum()))
        arr[t > end] = end_value
    return arr


def _pulse(value: float, base: float = 0.0, start: float = CASE_WINDOW[0], end: float = CASE_WINDOW[1]) -> np.ndarray:
    arr = _base_array(base)
    arr[_mask(start, end)] = value
    return arr


def _base_signals() -> dict[str, dict[str, np.ndarray]]:
    t = _time_axis()
    speed = _base_array(22.0)
    return {
        "IMU_Yaw_Long_Acc_037_M2": {
            "IIMULonAccSec": _base_array(0.0),
            "IIMULonAcc_adas": _base_array(0.0),
        },
        "PPEI_Vehicle_Speed_and_Distance_209_M1": {
            "IVehSpdAvgDrvn_1": speed.copy(),
        },
        "PPEI_Vehicle_Speed_and_Distance_208_M2": {
            "IVehSpdAvgNDrvn": speed.copy(),
        },
        "Adaptive_Cruise_Disp_Stat": {
            "IACCAct376": _base_array(1.0),
            "IACCHdwyStg": _base_array(2.0),
            "IACCDrvrSeltdSpd": _base_array(28.0),
        },
        "PPEI_Propulsion_General_Data_1_0C2_M": {
            "IAccPdlOvrrdAtv": _base_array(0.0),
            "IACCATCS_RqStat": _base_array(1.0),
            "IACCATCS_LmtgStat": _base_array(0.0),
        },
        "PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M": {
            "IACCATC_ACCAct": _base_array(1.0),
            "IACCATC_ACCTyp": _base_array(3.0),
            "IACCATC_AxlTrqRq": _base_array(80.0),
            "IACCATC_DrvAstdGoSt": _base_array(1.0),
        },
        "PPEI_Engine_Torque_Status_4_08D_M": {
            "IActAxleTrq": _base_array(75.0),
            "IActAxleTrqV": _base_array(1.0),
        },
        "ADAS_Command_Ext1": {
            "IACCBSCE_ACCAct1": _base_array(0.0),
            "IACCBSCE_AutBrkTp1": _base_array(0.0),
            "IACCBSCE_ACCAccl1": _base_array(0.0),
            "IACCBSCE_ACCTrq1": _base_array(0.0),
            "IFCACBSC_AutBrkRqTp1": _base_array(0.0),
            "IFCACBSC_CtrlTrq1": _base_array(0.0),
        },
        "PPEI_Chassis_General_Data_2_191_M": {
            "IAutoBrkngAvlbl": _base_array(1.0),
            "IAutoBrkngAct": _base_array(0.0),
            "IBrkSysAutBrkFld": _base_array(0.0),
        },
        "PPEI_Chassis_General_Data_3_0AC_M": {
            "IACCBrkngAct": _base_array(0.0),
        },
        "Automatic_Braking_Status_044_M": {
            "IBrkSysAutBrkStat": _base_array(0.0),
            "IFSRACCBrkngReqDenied": _base_array(0.0),
            "IVehMvngStat": _base_array(0.0),
        },
        "PPEI_Chassis_General_Status_2": {
            "ICSTBATS_TrqVl": _base_array(0.0),
        },
        "BrakeTorqueDerived": {
            "IRegenBrkAxTrqAch": _base_array(0.0),
        },
        "PerceptionTargets": {
            "FLT1_CUTIN_Flag": _base_array(0.0),
            "FLT1_Global_ID": _base_array(0.0),
            "FLT1_Long_Range": _base_array(120.0),
            "FLT1_Long_Velocity": _base_array(0.0),
            "FLT1_Long_Accel": _base_array(0.0),
            "FLT3_Global_ID": _base_array(0.0),
            "FLT3_Long_Range": _base_array(80.0),
            "FLT3_Long_Velocity": _base_array(0.0),
            "FLT3_Long_Accel": _base_array(0.0),
            "FLT5_Global_ID": _base_array(0.0),
            "FLT5_Long_Range": _base_array(80.0),
            "FLT5_Long_Velocity": _base_array(0.0),
            "FLT5_Long_Accel": _base_array(0.0),
        },
        "RoadLimitContext": {
            "Debug_uint_Reserved_48": _base_array(35.0),
            "Debug_uint_Reserved_49": _base_array(0.0),
            "Debug_single_Reserved_51": _base_array(0.0),
        },
        "Left_Lane_Track_Coeff": {
            "LaneModelC2": _base_array(0.0),
        },
        "Right_Lane_Track_Coeff": {
            "LaneModelC2": _base_array(0.0),
        },
        "VehicleStateContext": {
            "ITransEstGear_ECP_H1": _base_array(4.0),
            "VehicleStandstillFlag": _base_array(0.0),
        },
        "Wheel_Distance_Direction_Info_High_Frequency": {
            "IWhlRotDirStatLFHigFreq": _base_array(1.0),
            "IWhlRotDirStatRFHigFreq": _base_array(1.0),
            "IWhlRotDirStatLRHigFreq": _base_array(1.0),
            "IWhlRotDirStatRRHigFreq": _base_array(1.0),
        },
        "Adaptive_Cruise_Command_Ext_1A3_M": {
            "IACCBSCE_AutBrkTp": _base_array(0.0),
        },
        "PPEI_Chassis_General_Data_1_031_M": {
            "IWhlAngVelLFrtAuth": _base_array(0.0),
            "IWhlAngVelRFrtAuth": _base_array(0.0),
            "IWhlAngVelLRrAuth": _base_array(0.0),
            "IWhlAngVelRRrAuth": _base_array(0.0),
        },
        "PPEI_Trans_General_Status_2_ECP_H1_0D7_M": {
            "ITransEstGear_ECP_H1": _base_array(12.0),
        },
        "SyntheticClock": {
            "Elapsed": t.copy(),
        },
    }


def _sync_derived_aliases(signals: dict[str, dict[str, np.ndarray]]) -> None:
    """Add short-name aliases used by metrics and keep computed context signals fresh."""
    cruise = signals["Adaptive_Cruise_Disp_Stat"]
    cruise["ACCAct376"] = cruise["IACCAct376"].copy()
    cruise["ACCHdwyStg"] = cruise["IACCHdwyStg"].copy()
    cruise["ACCDrvrSeltdSpd"] = cruise["IACCDrvrSeltdSpd"].copy()

    targets = signals["PerceptionTargets"]
    targets.setdefault("FLT1_CUTIN_Flag", _base_array(0.0))
    targets["LeadTargetId"] = targets["FLT1_Global_ID"].copy()
    targets["LeadTargetDistance"] = targets["FLT1_Long_Range"].copy()
    targets["LeadTargetVelocity"] = targets["FLT1_Long_Velocity"].copy()
    targets["LeadTargetAccel"] = targets["FLT1_Long_Accel"].copy()
    targets["LeadTargetTTC"] = _compute_ttc(targets["FLT1_Long_Range"], targets["FLT1_Long_Velocity"])
    targets["CutInTargetId"] = targets["FLT1_Global_ID"].copy()
    targets["CutInTargetDistance"] = targets["FLT1_Long_Range"].copy()
    targets["CutInTargetVelocity"] = targets["FLT1_Long_Velocity"].copy()
    targets["CutInTargetAccel"] = targets["FLT1_Long_Accel"].copy()
    targets["CutInTargetTTC"] = targets["LeadTargetTTC"].copy()

    road = signals["RoadLimitContext"]
    left_lane = signals["Left_Lane_Track_Coeff"]
    right_lane = signals["Right_Lane_Track_Coeff"]
    left_lane["F_V_Left_Lane_ModelC2"] = left_lane["LaneModelC2"].copy()
    right_lane["F_V_Right_Lane_ModelC2"] = right_lane["LaneModelC2"].copy()
    road["CurvatureLimitSpeed"] = road["Debug_uint_Reserved_48"].copy()
    road["CurvatureLimitEnable"] = road["Debug_uint_Reserved_49"].copy()
    denominator = left_lane["LaneModelC2"] + right_lane["LaneModelC2"]
    curvature = np.zeros_like(denominator, dtype=float)
    valid = np.abs(denominator) > 1e-12
    curvature[valid] = 1.0 / denominator[valid]
    road["Curvature"] = curvature


def _compute_ttc(distance: np.ndarray, relative_velocity: np.ndarray) -> np.ndarray:
    closing_speed = np.where(relative_velocity < -1e-6, -relative_velocity, np.nan)
    return np.where(np.isfinite(closing_speed), distance / closing_speed, 100000.0)


def _compose_mutators(
    *mutators: Callable[[dict[str, dict[str, np.ndarray]]], None],
) -> Callable[[dict[str, dict[str, np.ndarray]]], None]:
    def mutate(signals: dict[str, dict[str, np.ndarray]]) -> None:
        for mutator in mutators:
            mutator(signals)

    return mutate


def _case_definition(
    key: str,
    label: str,
    scenario: str,
    metric_name: str,
    request_text: str,
    metric_detail: str,
    expected: str,
    *mutators: Callable[[dict[str, dict[str, np.ndarray]]], None],
    cause_hint: str = "",
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "scenario": scenario,
        "metric_name": metric_name,
        "request_text": request_text,
        "metric_detail": metric_detail,
        "cause_hint": cause_hint,
        "expected": expected,
        "mutate": _compose_mutators(*mutators),
    }


def _build_cases() -> list[dict[str, Any]]:
    return [
        _case_definition(
            "jerkiness",
            "顿挫",
            "基础触发：驱动请求快速波动",
            "acc_jerkiness",
            "分析这段数据顿挫的原因",
            "合成窗口内纵向加速度与驱动扭矩请求发生快速波动。",
            "应至少读到实车加速度、ACC 驱动请求、驱动响应和目标/Cut-in/限速等诱因信号。",
            _case_jerkiness,
            cause_hint="驱动请求和实车加速度同窗波动，用于验证顿挫基础检测链路。",
        ),
        _case_definition(
            "jerkiness_lead_target_jump",
            "顿挫",
            "诱因场景：前车距离/相对速度跳变",
            "acc_jerkiness",
            "分析这段数据顿挫的原因，重点看前车目标是否跳变",
            "合成窗口内纵向加速度波动，同时 FLT1 前车距离和相对速度突然变化。",
            "应在顿挫时间窗附近读到前车目标状态跳变，并优先判断算法请求是否跟随该目标变化。",
            _case_jerkiness,
            _cause_lead_target_jump,
            cause_hint="前车距离快速缩短、相对速度变负，模拟目标状态变化诱发请求波动。",
        ),
        _case_definition(
            "jerkiness_cutin_jump",
            "顿挫",
            "诱因场景：Cut-in 目标突然进入",
            "acc_jerkiness",
            "分析这段数据顿挫的原因，重点看 Cut-in 目标",
            "合成窗口内纵向加速度波动，同时 FLT1_CUTIN_Flag 置位且目标距离快速缩短。",
            "应能说明 Cut-in 目标进入是否早于或同步于请求波动。",
            _case_jerkiness,
            _cause_cutin_target_jump,
            cause_hint="Cut-in flag 置 1 后目标距离快速缩短，模拟 Cut-in 引起算法请求波动。",
        ),
        _case_definition(
            "jerkiness_setspeed_step",
            "顿挫",
            "诱因场景：SetSpeed 阶跃变化",
            "acc_jerkiness",
            "分析这段数据顿挫的原因，重点看设定车速变化",
            "合成窗口内纵向加速度波动，同时 SetSpeed 出现阶跃下降。",
            "应能把 SetSpeed 变化纳入诱因证据，不直接定执行器原因。",
            _case_jerkiness,
            _cause_setspeed_drop,
            cause_hint="设定车速在问题窗口前后下降，模拟算法因目标速度变化调整请求。",
        ),
        _case_definition(
            "heavy_braking",
            "重制动",
            "基础触发：前车快速逼近后制动请求增大",
            "acc_heavy_braking_count",
            "分析这段数据重制动的原因",
            "合成窗口内纵向加速度出现明显负向波谷，ACC 制动请求同步增大。",
            "应确认重制动现象，并具备制动请求、制动响应、前车/曲率/SetSpeed 诱因证据。",
            _case_heavy_braking,
            cause_hint="前车距离和相对速度持续恶化，制动请求与实车减速度同步出现。",
        ),
        _case_definition(
            "heavy_braking_curve_limit",
            "重制动",
            "诱因场景：曲率限速触发减速",
            "acc_heavy_braking_count",
            "分析这段数据重制动的原因，重点看曲率限速",
            "合成窗口内重制动成立，同时曲率限速使能且曲率限速值显著低于当前车速。",
            "应能把曲率限速作为算法给出减速请求的诱因候选。",
            _case_heavy_braking,
            _cause_curve_limit,
            cause_hint="曲率限速使能且限速值降低，模拟弯道限速导致算法主动降速。",
        ),
        _case_definition(
            "heavy_braking_setspeed_drop",
            "重制动",
            "诱因场景：SetSpeed 降低触发减速",
            "acc_heavy_braking_count",
            "分析这段数据重制动的原因，重点看 SetSpeed 是否降低",
            "合成窗口内重制动成立，同时设定车速出现明显下降。",
            "应能把设定车速下降作为算法制动请求的诱因候选。",
            _case_heavy_braking,
            _cause_setspeed_drop,
            cause_hint="SetSpeed 在问题窗口附近下降，模拟目标速度降低触发减速请求。",
        ),
        _case_definition(
            "false_braking",
            "误制动",
            "基础触发：制动成立但常见诱因稳定",
            "acc_false_braking_context_count",
            "分析这段数据误制动的原因",
            "合成窗口内有制动请求和减速度，但前车、Cut-in、曲率限速均保持稳定。",
            "应看到制动现象和制动请求成立，同时暴露诱因信号稳定，便于验证“无合理诱因”的判断。",
            _case_false_braking,
            cause_hint="制动请求和实车减速度成立，但目标、曲率限速均无明显触发条件。",
        ),
        _case_definition(
            "missed_braking",
            "漏制动",
            "基础触发：前车风险出现但制动请求缺失",
            "acc_missed_braking_count",
            "分析这段数据漏制动的原因",
            "合成窗口内前车距离快速缩短，但 ACC/AEB 制动请求和制动响应都没有出现。",
            "应倾向请求侧未发出制动；响应侧无动作是跟随请求缺失，而不是直接定执行器故障。",
            _case_missed_braking,
            cause_hint="前车 TTC 降低但请求侧没有制动命令，用于验证漏制动基础链路。",
        ),
        _case_definition(
            "missed_braking_cutin",
            "漏制动",
            "诱因场景：Cut-in 风险出现但制动请求缺失",
            "acc_missed_braking_count",
            "分析这段数据漏制动的原因，重点看 Cut-in 目标",
            "合成窗口内 Cut-in 目标距离快速缩短，但制动请求和响应都没有出现。",
            "应把 Cut-in 风险识别为现象诱因，同时把责任收敛到请求侧缺失。",
            _case_missed_braking,
            _cause_cutin_risk,
            cause_hint="Cut-in flag 置位且目标快速逼近，但请求侧没有制动命令。",
        ),
        _case_definition(
            "set_speed_not_reached",
            "稳速未达 Setspeed",
            "基础触发：实车长期略低于设定车速",
            "acc_set_speed_reach",
            "分析为什么没有达到 SetSpeed",
            "合成窗口内实车速度长时间稳定在设定车速附近但始终未达到目标显示车速。",
            "应关注设定车速、实车车速、驱动请求/响应，以及前车、Cut-in、曲率限速诱因。",
            _case_setspeed_not_reached,
            cause_hint="无明显外部限速诱因时，先验证车速、请求和执行器响应是否闭合。",
        ),
        _case_definition(
            "set_speed_not_reached_lead_target",
            "稳速未达 Setspeed",
            "诱因场景：前车限制导致未达设定车速",
            "acc_set_speed_reach",
            "分析为什么没有达到 SetSpeed，重点看前车限制",
            "合成窗口内实车车速低于 SetSpeed，同时前车低速且距离较近。",
            "应优先说明前车限制是否能解释算法不继续加速。",
            _case_setspeed_not_reached,
            _cause_lead_target_limit,
            cause_hint="前车距离较近且相对速度偏低，模拟跟车目标限制车速。",
        ),
        _case_definition(
            "set_speed_not_reached_curve_limit",
            "稳速未达 Setspeed",
            "诱因场景：曲率限速导致未达设定车速",
            "acc_set_speed_reach",
            "分析为什么没有达到 SetSpeed，重点看曲率限速",
            "合成窗口内实车车速低于 SetSpeed，同时曲率限速使能且限速值低。",
            "应优先说明曲率限速是否能解释算法不继续加速。",
            _case_setspeed_not_reached,
            _cause_curve_limit,
            cause_hint="曲率限速值低于 SetSpeed，模拟道路曲率限制车速。",
        ),
        _case_definition(
            "unreasonable_low_speed",
            "不合理低速",
            "基础触发：低速且常见诱因不足",
            "acc_unreasonable_low_speed_context_count",
            "分析这段数据不合理低速的原因",
            "合成窗口内车速明显低于设定车速，且目标、曲率限速都没有合理解释。",
            "应能看到实际车速低、驱动请求偏弱，同时确认常见诱因不足。",
            _case_unreasonable_low_speed,
            cause_hint="车速明显偏低但外部诱因稳定，用于验证不合理低速基础场景。",
        ),
        _case_definition(
            "unreasonable_low_speed_lead_target",
            "不合理低速",
            "诱因场景：前车低速导致降速",
            "acc_unreasonable_low_speed_context_count",
            "分析这段数据不合理低速的原因，重点看前车",
            "合成窗口内车速低，前车距离较近且相对速度偏低。",
            "应优先说明是否是前车限制导致算法低速。",
            _case_unreasonable_low_speed,
            _cause_lead_target_limit,
            cause_hint="前车距离和相对速度构成跟车约束，模拟目标导致低速。",
        ),
        _case_definition(
            "unreasonable_low_speed_curve_limit",
            "不合理低速",
            "诱因场景：曲率限速导致低速",
            "acc_unreasonable_low_speed_context_count",
            "分析这段数据不合理低速的原因，重点看曲率限速",
            "合成窗口内车速低，曲率限速使能且限速值低。",
            "应优先说明是否是曲率限速导致算法主动低速。",
            _case_unreasonable_low_speed,
            _cause_curve_limit,
            cause_hint="曲率限速使能且限速值较低，模拟弯道限速引起低速。",
        ),
        _case_definition(
            "rollback",
            "溜车",
            "基础触发：坡道静止附近反向轮速",
            "acc_rollback_count",
            "分析这段数据溜车的原因",
            "合成窗口内车辆接近静止后出现反向轮速，坡度为正，制动响应不足。",
            "应提示溜车需要车速、静止标志、坡度、制动或驱动响应共同闭环。",
            _case_rollback,
            cause_hint="坡度为正且轮速反向，制动响应不足，模拟坡道保持失败。",
        ),
        _case_definition(
            "following_distance_far",
            "跟车距离",
            "基础触发：跟车距离过远",
            "acc_following_distance_anomaly",
            "分析这段数据跟车距离异常的原因",
            "合成窗口内跟车目标距离明显大于当前时距档位对应的合理距离。",
            "应读取跟车目标距离、目标相对速度、时距档位、设定车速与驱动响应。",
            _case_following_distance,
            cause_hint="实际跟车距离远高于时距档位参考距离，模拟跟车过远。",
        ),
        _case_definition(
            "following_distance_close",
            "跟车距离",
            "基础触发：跟车距离过近",
            "acc_following_distance_anomaly",
            "分析这段数据跟车距离过近的原因",
            "合成窗口内跟车目标距离明显小于当前时距档位对应的合理距离。",
            "应提示跟车距离过近，并检查目标状态、请求和响应是否及时变化。",
            _case_following_distance_too_close,
            cause_hint="目标距离低于时距档位参考距离，模拟跟车过近。",
        ),
        _case_definition(
            "following_distance_oscillation",
            "跟车距离",
            "基础触发：跟车距离波动",
            "acc_following_distance_anomaly",
            "分析这段数据跟车距离波动的原因",
            "合成窗口内跟车目标距离围绕参考距离大幅来回波动。",
            "应提示距离波动，并把目标距离、相对速度、请求响应变化一起作为证据。",
            _case_following_distance_oscillation,
            cause_hint="目标距离周期性大幅波动，模拟跟车距离不稳定。",
        ),
    ]

def _case_jerkiness(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m_request = _mask(9.0, 12.8)
    m_response = _mask(9.25, 13.05)
    m_phenomenon = _mask(9.35, 13.15)
    request_phase = np.arange(int(m_request.sum())) // 4
    request_wave = np.where(request_phase % 2 == 0, -2.2, 1.3).astype(float)
    response_phase = np.arange(int(m_response.sum())) // 4
    response_wave = np.where(response_phase % 2 == 0, -1.9, 1.1).astype(float)
    phenomenon_phase = np.arange(int(m_phenomenon.sum())) // 4
    phenomenon_wave = np.where(phenomenon_phase % 2 == 0, -2.1, 1.2).astype(float)
    signals["PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M"]["IACCATC_AxlTrqRq"][m_request] = 80 + request_wave * 140
    signals["PPEI_Engine_Torque_Status_4_08D_M"]["IActAxleTrq"][m_response] = 75 + response_wave * 120
    signals["IMU_Yaw_Long_Acc_037_M2"]["IIMULonAccSec"][m_phenomenon] = phenomenon_wave


def _case_heavy_braking(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask(9.6, 11.8)
    target_m = _mask(8.9, 11.8)
    target_n = int(target_m.sum())
    acc_window = np.full(int(m.sum()), -3.2, dtype=float)
    ramp_count = min(5, len(acc_window))
    acc_window[:ramp_count] = np.linspace(0.0, -3.2, ramp_count)
    signals["IMU_Yaw_Long_Acc_037_M2"]["IIMULonAccSec"][m] = acc_window
    signals["PPEI_Vehicle_Speed_and_Distance_209_M1"]["IVehSpdAvgDrvn_1"][m] = np.linspace(24.0, 18.0, int(m.sum()))
    signals["ADAS_Command_Ext1"]["IACCBSCE_ACCAct1"][m] = 1
    signals["ADAS_Command_Ext1"]["IACCBSCE_AutBrkTp1"][m] = 6
    signals["ADAS_Command_Ext1"]["IACCBSCE_ACCAccl1"][m] = np.linspace(-0.5, -3.0, int(m.sum()))
    signals["ADAS_Command_Ext1"]["IACCBSCE_ACCTrq1"][m] = np.linspace(200, 1100, int(m.sum()))
    signals["PPEI_Chassis_General_Data_2_191_M"]["IAutoBrkngAct"][m] = 1
    signals["PPEI_Chassis_General_Data_3_0AC_M"]["IACCBrkngAct"][m] = 1
    signals["Automatic_Braking_Status_044_M"]["IBrkSysAutBrkStat"][m] = 1
    signals["PPEI_Chassis_General_Status_2"]["ICSTBATS_TrqVl"][m] = np.linspace(150, 720, int(m.sum()))
    signals["BrakeTorqueDerived"]["IRegenBrkAxTrqAch"][m] = np.linspace(40, 180, int(m.sum()))
    signals["PerceptionTargets"]["FLT1_CUTIN_Flag"][target_m] = 0
    signals["PerceptionTargets"]["FLT1_Global_ID"][target_m] = 1001
    signals["PerceptionTargets"]["FLT1_Long_Range"][target_m] = np.linspace(34, 14, target_n)
    signals["PerceptionTargets"]["FLT1_Long_Velocity"][target_m] = np.linspace(-2.0, -8.0, target_n)
    signals["PerceptionTargets"]["FLT1_Long_Accel"][target_m] = np.linspace(0.0, -2.5, target_n)


def _case_false_braking(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask(9.6, 11.8)
    phase = np.linspace(0.0, 1.0, int(m.sum()))
    acc_window = np.interp(phase, [0.0, 0.25, 0.68, 1.0], [0.0, -2.7, -2.7, 0.2])
    signals["IMU_Yaw_Long_Acc_037_M2"]["IIMULonAccSec"][m] = acc_window
    signals["PPEI_Vehicle_Speed_and_Distance_209_M1"]["IVehSpdAvgDrvn_1"][m] = np.linspace(25.0, 19.0, int(m.sum()))
    signals["ADAS_Command_Ext1"]["IACCBSCE_ACCAct1"][m] = 1
    signals["ADAS_Command_Ext1"]["IACCBSCE_AutBrkTp1"][m] = 6
    signals["ADAS_Command_Ext1"]["IACCBSCE_ACCAccl1"][m] = np.interp(phase, [0.0, 0.3, 1.0], [0.0, -2.4, 0.0])
    signals["ADAS_Command_Ext1"]["IACCBSCE_ACCTrq1"][m] = np.interp(phase, [0.0, 0.3, 0.7, 1.0], [0, 900, 900, 0])
    signals["PPEI_Chassis_General_Data_2_191_M"]["IAutoBrkngAct"][m] = 1
    signals["PPEI_Chassis_General_Data_3_0AC_M"]["IACCBrkngAct"][m] = 1
    signals["Automatic_Braking_Status_044_M"]["IBrkSysAutBrkStat"][m] = 1
    signals["PPEI_Chassis_General_Status_2"]["ICSTBATS_TrqVl"][m] = np.interp(phase, [0.0, 0.3, 0.7, 1.0], [0, 520, 520, 0])
    signals["BrakeTorqueDerived"]["IRegenBrkAxTrqAch"][m] = np.interp(phase, [0.0, 0.3, 0.7, 1.0], [0, 150, 150, 0])
    signals["PerceptionTargets"]["FLT1_Global_ID"][:] = 0
    signals["PerceptionTargets"]["FLT1_Long_Range"][:] = 85
    signals["PerceptionTargets"]["FLT1_Long_Velocity"][:] = 1.0
    signals["PerceptionTargets"]["FLT1_Long_Accel"][:] = 0.0
    signals["PerceptionTargets"]["FLT3_Global_ID"][:] = 0
    signals["RoadLimitContext"]["Debug_uint_Reserved_48"][:] = 35
    signals["RoadLimitContext"]["Debug_uint_Reserved_49"][:] = 0
    signals["Left_Lane_Track_Coeff"]["LaneModelC2"][:] = 0
    signals["Right_Lane_Track_Coeff"]["LaneModelC2"][:] = 0


def _case_missed_braking(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask(9.4, 12.4)
    signals["IMU_Yaw_Long_Acc_037_M2"]["IIMULonAccSec"][m] = np.linspace(0.0, -0.2, int(m.sum()))
    signals["PPEI_Vehicle_Speed_and_Distance_209_M1"]["IVehSpdAvgDrvn_1"][m] = 24.0
    signals["PerceptionTargets"]["FLT1_Global_ID"][m] = 1001
    signals["PerceptionTargets"]["FLT1_Long_Range"][m] = np.linspace(34, 5, int(m.sum()))
    signals["PerceptionTargets"]["FLT1_Long_Velocity"][m] = -10.0
    signals["PerceptionTargets"]["FLT1_Long_Accel"][m] = -2.0
    signals["ADAS_Command_Ext1"]["IACCBSCE_ACCAct1"][m] = 0
    signals["ADAS_Command_Ext1"]["IACCBSCE_ACCTrq1"][m] = 0
    signals["ADAS_Command_Ext1"]["IACCBSCE_ACCAccl1"][m] = 0
    signals["ADAS_Command_Ext1"]["IFCACBSC_CtrlTrq1"][m] = 0
    signals["PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M"]["IACCATC_AxlTrqRq"][m] = 0
    signals["PPEI_Chassis_General_Data_3_0AC_M"]["IACCBrkngAct"][m] = 0
    signals["Automatic_Braking_Status_044_M"]["IBrkSysAutBrkStat"][m] = 0


def _case_setspeed_not_reached(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask(4.0, 15.5)
    signals["Adaptive_Cruise_Disp_Stat"]["IACCDrvrSeltdSpd"][:] = 30
    signals["Adaptive_Cruise_Disp_Stat"]["IACCDrvrSeltdSpd"][m] = 30
    signals["PPEI_Vehicle_Speed_and_Distance_209_M1"]["IVehSpdAvgDrvn_1"][m] = 29
    signals["PPEI_Vehicle_Speed_and_Distance_208_M2"]["IVehSpdAvgNDrvn"][m] = 29
    signals["PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M"]["IACCATC_AxlTrqRq"][m] = 25
    signals["PPEI_Engine_Torque_Status_4_08D_M"]["IActAxleTrq"][m] = 15
    signals["PerceptionTargets"]["FLT1_Global_ID"][m] = 0
    signals["PerceptionTargets"]["FLT1_Long_Range"][m] = 120
    signals["PerceptionTargets"]["FLT1_Long_Velocity"][m] = 0


def _case_unreasonable_low_speed(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask(8.8, 12.6)
    signals["Adaptive_Cruise_Disp_Stat"]["IACCDrvrSeltdSpd"][:] = 32
    signals["Adaptive_Cruise_Disp_Stat"]["IACCDrvrSeltdSpd"][m] = 32
    signals["PPEI_Vehicle_Speed_and_Distance_209_M1"]["IVehSpdAvgDrvn_1"][m] = np.linspace(17, 10, int(m.sum()))
    signals["PPEI_Vehicle_Speed_and_Distance_208_M2"]["IVehSpdAvgNDrvn"][m] = np.linspace(17, 10, int(m.sum()))
    signals["PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M"]["IACCATC_AxlTrqRq"][m] = -80
    signals["PPEI_Engine_Torque_Status_4_08D_M"]["IActAxleTrq"][m] = -70
    signals["PerceptionTargets"]["FLT1_Global_ID"][m] = 0
    signals["PerceptionTargets"]["FLT1_Long_Range"][m] = 120
    signals["PerceptionTargets"]["FLT1_Long_Velocity"][m] = 0
    signals["RoadLimitContext"]["Debug_uint_Reserved_48"][m] = 35
    signals["RoadLimitContext"]["Debug_uint_Reserved_49"][m] = 0
    signals["Left_Lane_Track_Coeff"]["LaneModelC2"][m] = 0
    signals["Right_Lane_Track_Coeff"]["LaneModelC2"][m] = 0


def _case_rollback(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask()
    signals["PPEI_Vehicle_Speed_and_Distance_209_M1"]["IVehSpdAvgDrvn_1"][m] = np.linspace(0.2, 0.1, int(m.sum()))
    signals["PPEI_Vehicle_Speed_and_Distance_208_M2"]["IVehSpdAvgNDrvn"][m] = np.linspace(0.2, 0.1, int(m.sum()))
    signals["VehicleStateContext"]["VehicleStandstillFlag"][m] = 0.0
    signals["Automatic_Braking_Status_044_M"]["IVehMvngStat"][m] = 0.0
    signals["RoadLimitContext"]["Debug_single_Reserved_51"][m] = 8.0
    signals["Wheel_Distance_Direction_Info_High_Frequency"]["IWhlRotDirStatLFHigFreq"][m] = 4.0
    signals["Wheel_Distance_Direction_Info_High_Frequency"]["IWhlRotDirStatRFHigFreq"][m] = 4.0
    signals["Wheel_Distance_Direction_Info_High_Frequency"]["IWhlRotDirStatLRHigFreq"][m] = 4.0
    signals["Wheel_Distance_Direction_Info_High_Frequency"]["IWhlRotDirStatRRHigFreq"][m] = 4.0
    signals["Adaptive_Cruise_Command_Ext_1A3_M"]["IACCBSCE_AutBrkTp"][m] = 5.0
    signals["PPEI_Chassis_General_Data_1_031_M"]["IWhlAngVelLFrtAuth"][m] = 0.18
    signals["PPEI_Chassis_General_Data_1_031_M"]["IWhlAngVelRFrtAuth"][m] = 0.18
    signals["PPEI_Chassis_General_Data_1_031_M"]["IWhlAngVelLRrAuth"][m] = 0.18
    signals["PPEI_Chassis_General_Data_1_031_M"]["IWhlAngVelRRrAuth"][m] = 0.18
    signals["PPEI_Trans_General_Status_2_ECP_H1_0D7_M"]["ITransEstGear_ECP_H1"][m] = 12.0
    signals["ADAS_Command_Ext1"]["IACCBSCE_ACCAct1"][m] = 1
    signals["ADAS_Command_Ext1"]["IACCBSCE_ACCTrq1"][m] = 300
    signals["PPEI_Chassis_General_Data_3_0AC_M"]["IACCBrkngAct"][m] = 0
    signals["Automatic_Braking_Status_044_M"]["IBrkSysAutBrkStat"][m] = 0


def _case_following_distance(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask()
    signals["Adaptive_Cruise_Disp_Stat"]["IACCHdwyStg"][m] = 1
    signals["Adaptive_Cruise_Disp_Stat"]["IACCDrvrSeltdSpd"][m] = 20
    signals["PPEI_Vehicle_Speed_and_Distance_209_M1"]["IVehSpdAvgDrvn_1"][m] = 20
    signals["PerceptionTargets"]["FLT1_Global_ID"][m] = 1001
    signals["PerceptionTargets"]["FLT1_CUTIN_Flag"][m] = 0
    signals["PerceptionTargets"]["FLT1_Long_Range"][m] = np.linspace(75, 35, int(m.sum()))
    signals["PerceptionTargets"]["FLT1_Long_Velocity"][m] = np.linspace(-0.5, -2.5, int(m.sum()))
    signals["PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M"]["IACCATC_AxlTrqRq"][m] = -20
    signals["PPEI_Engine_Torque_Status_4_08D_M"]["IActAxleTrq"][m] = -15


def _case_following_distance_too_close(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask()
    signals["Adaptive_Cruise_Disp_Stat"]["IACCHdwyStg"][m] = 1
    signals["Adaptive_Cruise_Disp_Stat"]["IACCDrvrSeltdSpd"][m] = 20
    signals["PPEI_Vehicle_Speed_and_Distance_209_M1"]["IVehSpdAvgDrvn_1"][m] = 20
    signals["PPEI_Vehicle_Speed_and_Distance_208_M2"]["IVehSpdAvgNDrvn"][m] = 20
    signals["PerceptionTargets"]["FLT1_Global_ID"][m] = 1001
    signals["PerceptionTargets"]["FLT1_CUTIN_Flag"][m] = 0
    signals["PerceptionTargets"]["FLT1_Long_Range"][m] = np.linspace(16, 10, int(m.sum()))
    signals["PerceptionTargets"]["FLT1_Long_Velocity"][m] = np.linspace(-2.0, -4.0, int(m.sum()))
    signals["PerceptionTargets"]["FLT1_Long_Accel"][m] = -1.0
    signals["PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M"]["IACCATC_AxlTrqRq"][m] = -60
    signals["PPEI_Engine_Torque_Status_4_08D_M"]["IActAxleTrq"][m] = -50
    signals["ADAS_Command_Ext1"]["IACCBSCE_ACCAct1"][m] = 1
    signals["ADAS_Command_Ext1"]["IACCBSCE_ACCAccl1"][m] = -1.0
    signals["ADAS_Command_Ext1"]["IACCBSCE_ACCTrq1"][m] = 250


def _case_following_distance_oscillation(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask(8.0, 14.0)
    n = int(m.sum())
    phase = np.linspace(0.0, 4.0 * math.pi, n)
    signals["Adaptive_Cruise_Disp_Stat"]["IACCHdwyStg"][m] = 2
    signals["Adaptive_Cruise_Disp_Stat"]["IACCDrvrSeltdSpd"][m] = 22
    signals["PPEI_Vehicle_Speed_and_Distance_209_M1"]["IVehSpdAvgDrvn_1"][m] = 22 + 0.8 * np.sin(phase)
    signals["PPEI_Vehicle_Speed_and_Distance_208_M2"]["IVehSpdAvgNDrvn"][m] = 22 + 0.8 * np.sin(phase)
    signals["PerceptionTargets"]["FLT1_Global_ID"][m] = 1001
    signals["PerceptionTargets"]["FLT1_CUTIN_Flag"][m] = 0
    signals["PerceptionTargets"]["FLT1_Long_Range"][m] = 42 + 24 * np.sin(phase)
    signals["PerceptionTargets"]["FLT1_Long_Velocity"][m] = -2.0 + 2.5 * np.cos(phase)
    signals["PerceptionTargets"]["FLT1_Long_Accel"][m] = -1.5 * np.sin(phase)
    signals["PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M"]["IACCATC_AxlTrqRq"][m] = 40 + 120 * np.sin(phase)
    signals["PPEI_Engine_Torque_Status_4_08D_M"]["IActAxleTrq"][m] = 35 + 100 * np.sin(phase + 0.25)


def _cause_lead_target_jump(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask(9.4, 11.4)
    n = int(m.sum())
    targets = signals["PerceptionTargets"]
    targets["FLT1_CUTIN_Flag"][m] = 0
    targets["FLT1_Global_ID"][m] = 1001
    targets["FLT1_Long_Range"][m] = np.linspace(58, 15, n)
    targets["FLT1_Long_Velocity"][m] = np.linspace(-0.5, -7.5, n)
    targets["FLT1_Long_Accel"][m] = np.linspace(0.0, -3.0, n)


def _cause_cutin_target_jump(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask(9.4, 11.4)
    n = int(m.sum())
    targets = signals["PerceptionTargets"]
    targets["FLT1_CUTIN_Flag"][m] = 1
    targets["FLT1_Global_ID"][m] = 2101
    targets["FLT1_Long_Range"][m] = np.linspace(46, 13, n)
    targets["FLT1_Long_Velocity"][m] = np.linspace(-0.5, -8.0, n)
    targets["FLT1_Long_Accel"][m] = np.linspace(0.0, -3.2, n)


def _cause_cutin_risk(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask(9.4, 12.4)
    n = int(m.sum())
    targets = signals["PerceptionTargets"]
    targets["FLT1_CUTIN_Flag"][m] = 1
    targets["FLT1_Global_ID"][m] = 2102
    targets["FLT1_Long_Range"][m] = np.linspace(30, 4, n)
    targets["FLT1_Long_Velocity"][m] = -11.0
    targets["FLT1_Long_Accel"][m] = -2.5


def _cause_lead_target_limit(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask(8.0, 13.5)
    n = int(m.sum())
    signals["Adaptive_Cruise_Disp_Stat"]["IACCDrvrSeltdSpd"][:] = 30
    targets = signals["PerceptionTargets"]
    targets["FLT1_CUTIN_Flag"][m] = 0
    targets["FLT1_Global_ID"][m] = 1001
    targets["FLT1_Long_Range"][m] = np.linspace(26, 19, n)
    targets["FLT1_Long_Velocity"][m] = -4.0
    targets["FLT1_Long_Accel"][m] = -0.8


def _cause_curve_limit(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask(8.8, 12.6)
    signals["Adaptive_Cruise_Disp_Stat"]["IACCDrvrSeltdSpd"][:] = 30
    road = signals["RoadLimitContext"]
    road["Debug_uint_Reserved_49"][m] = 1
    road["Debug_uint_Reserved_48"][m] = 12
    signals["Left_Lane_Track_Coeff"]["LaneModelC2"][m] = 0.0018
    signals["Right_Lane_Track_Coeff"]["LaneModelC2"][m] = 0.0018


def _cause_setspeed_drop(signals: dict[str, dict[str, np.ndarray]]) -> None:
    m = _mask(8.8, 12.6)
    n = int(m.sum())
    cruise = signals["Adaptive_Cruise_Disp_Stat"]
    cruise["IACCDrvrSeltdSpd"][m] = np.linspace(30, 16, n)
    cruise["IACCDrvrSeltdSpd"][_time_axis() > 12.6] = 16


def _write_h5(path: Path, signals: dict[str, dict[str, np.ndarray]]) -> None:
    timestamps = _time_axis()
    if path.exists():
        path.unlink()
    with h5py.File(path, "w") as h5_file:
        channel = h5_file.create_group("synthetic").create_group("Channel1")
        for message_name, fields in signals.items():
            dtype = [("timestamp", "f8")]
            for field_name in fields:
                dtype.append((field_name, "f8"))
            data = np.zeros(len(timestamps), dtype=np.dtype(dtype))
            data["timestamp"] = timestamps
            for field_name, values in fields.items():
                data[field_name] = values
            channel.create_dataset(message_name, data=data, compression="gzip")


def _case_summary(case: dict[str, Any], h5_path: Path, performance_result: Any, report: RootCauseReport) -> dict[str, Any]:
    problems = list(getattr(performance_result, "problems", []) or [])
    performance_events = sum(len(getattr(issue, "events", []) or []) for issue in problems)
    setattr(report, "problem_type", case["label"])
    setattr(
        report,
        "problem_description",
        "；".join(
            part
            for part in (
                case.get("request_text", ""),
                case.get("scenario", ""),
                case.get("cause_hint", ""),
            )
            if part
        ),
    )
    return {
        "key": case["key"],
        "label": case["label"],
        "problem_type": case["label"],
        "scenario": case.get("scenario", "基础触发"),
        "request_text": case["request_text"],
        "h5_file": str(h5_path.relative_to(ROOT)),
        "metric_detail": case.get("metric_detail", ""),
        "cause_hint": case.get("cause_hint", ""),
        "expected": case["expected"],
        "batch_summary": _format_root_cause_summary(report),
        "performance": {
            "status": getattr(performance_result, "status", ""),
            "summary": getattr(performance_result, "summary", ""),
            "selected_metric_names": list(getattr(performance_result, "selected_metric_names", []) or []),
            "problem_count": len(problems),
            "event_count": performance_events,
            "issues": [
                {
                    "metric_name": getattr(issue, "metric_name", ""),
                    "bad_case": getattr(issue, "statistics", {}).get("Bad Case"),
                    "all_case_count": getattr(issue, "statistics", {}).get("All Case Count"),
                    "events": len(getattr(issue, "events", []) or []),
                }
                for issue in problems
            ],
        },
        "report": {
            "status": report.status,
            "cause_side": report.cause_side,
            "problem_scope": report.problem_scope,
            "headline": report.headline,
            "summary": report.summary,
            "counts": {
                "performance_issue_count": report.performance_issue_count,
                "performance_event_count": report.performance_event_count,
                "actuator_issue_count": report.actuator_issue_count,
                "phenomenon_signal_count": report.phenomenon_signal_count,
                "request_signal_count": report.request_signal_count,
                "algorithm_cause_signal_count": report.algorithm_cause_signal_count,
                "response_signal_count": report.response_signal_count,
            },
            "missing": {
                "phenomenon": report.missing_phenomenon_signals,
                "request": report.missing_request_signals,
                "algorithm_cause": report.missing_algorithm_cause_signals,
                "response": report.missing_response_signals,
            },
            "evidence_rows": report.evidence_rows,
            "diagnosis_steps": report.diagnosis_steps,
            "temporal_judgement": report.temporal_judgement,
            "next_checks": report.next_checks,
        },
        "observation": _observation(report),
    }


def _observation(report: RootCauseReport) -> str:
    if report.performance_event_count <= 0:
        return "正式指标未命中或没有可用时间窗；样例需要调整或补充对应筛选代码，不能再手工注入命中。"
    missing_total = sum(
        len(items)
        for items in (
            report.missing_phenomenon_signals,
            report.missing_request_signals,
            report.missing_algorithm_cause_signals,
            report.missing_response_signals,
        )
    )
    if report.temporal_judgement and report.temporal_judgement.get("confidence") in {"high", "medium"}:
        return f"已形成时序判断：{report.temporal_judgement.get('headline', '')}"
    if report.request_signal_count and report.response_signal_count and report.algorithm_cause_signal_count:
        return "证据面基本能闭合；但当前时序链仍不够完整，需要继续确认谁先变化。"
    if report.request_signal_count and report.response_signal_count:
        return "请求/响应两侧能读到，诱因侧证据不足或未被当前规则充分利用。"
    if missing_total:
        return "能跑出结论，但存在关键信号缺口，当前输出应保持保守。"
    return "未发现明显缺口。"


def _render_report(summaries: list[dict[str, Any]], generated_at: str) -> str:
    lines: list[str] = []
    lines.append("# 行车问题原因分析合成数据回归测试报告")
    lines.append("")
    lines.append(f"- 生成时间：{generated_at}")
    lines.append(f"- 测试脚本：`scripts/generate_root_cause_synthetic_report.py`")
    lines.append(f"- 合成 H5 目录：`{OUT_DIR.relative_to(ROOT)}/`")
    lines.append(f"- 汇总 JSON：`{SUMMARY_JSON_PATH.relative_to(ROOT)}`")
    lines.append("")
    lines.append("## 测试方法")
    lines.append("")
    lines.append(
        "本次测试为真实指标闭环回归：脚本为每类问题生成基础触发样例，并为典型诱因拆出独立 H5，先调用 "
        "`performance_analysis.run_performance_analysis(...)` 获取正式指标结果，再调用 "
        "`root_cause_analysis.build_root_cause_report(...)` 做归因。"
    )
    lines.append("")
    lines.append(
        "它验证的是：正式筛选代码是否能给出可用问题时间窗，以及行车问题原因分析是否能围绕同一时间窗读取现象、"
        "算法请求、执行器响应、算法请求诱因四类证据。它不是完整 CAN/BLF 解码链路测试。"
    )
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append("| 问题类型 | 场景 | 正式指标 | 归因状态 | 归因方向 | 证据计数 现象/请求/诱因/响应 | 主要观察 |")
    lines.append("|---|---|---|---|---|---|---|")
    for item in summaries:
        report = item["report"]
        counts = report["counts"]
        performance = item["performance"]
        lines.append(
            "| {label} | {scenario} | {metric_status} | {status} | {cause} | {p}/{r}/{a}/{resp} | {obs} |".format(
                label=item["label"],
                scenario=_escape_table(item.get("scenario", "")),
                metric_status=f"{performance['problem_count']}项/{performance['event_count']}窗",
                status=report["status"],
                cause=_escape_table(report["cause_side"]),
                p=counts["phenomenon_signal_count"],
                r=counts["request_signal_count"],
                a=counts["algorithm_cause_signal_count"],
                resp=counts["response_signal_count"],
                obs=_escape_table(item["observation"]),
            )
        )
    lines.append("")
    lines.append("## 分项结果")
    lines.append("")
    for item in summaries:
        report = item["report"]
        counts = report["counts"]
        lines.append(f"### {item['label']}｜{item.get('scenario', '基础触发')}")
        lines.append("")
        lines.append(f"- 合成文件：`{item['h5_file']}`")
        lines.append(f"- 用户需求样例：{item['request_text']}")
        if item.get("metric_detail"):
            lines.append(f"- 指标触发设计：{item['metric_detail']}")
        if item.get("cause_hint"):
            lines.append(f"- 诱因设计：{item['cause_hint']}")
        lines.append(f"- 预期覆盖：{item['expected']}")
        performance = item["performance"]
        lines.append(f"- 正式指标：{performance['problem_count']} 项问题，{performance['event_count']} 个时间窗")
        if performance["issues"]:
            lines.append(
                "- 指标明细："
                + "；".join(
                    f"{issue['metric_name']} Bad={issue['bad_case']} All={issue['all_case_count']} Events={issue['events']}"
                    for issue in performance["issues"]
                )
            )
        lines.append(f"- 实际归因方向：{report['cause_side']}")
        lines.append(f"- 核心结论：{report['headline']}")
        lines.append(f"- 说明：{report['summary']}")
        temporal = report.get("temporal_judgement") or {}
        if temporal:
            lines.append(f"- 谁先变化：{temporal.get('order_text', '未形成完整时序')}")
            lines.append(f"- 最终原因：{temporal.get('final_cause', '')}")
        lines.append(
            "- 证据计数：现象 {phenomenon_signal_count}，请求 {request_signal_count}，诱因 {algorithm_cause_signal_count}，响应 {response_signal_count}，执行器风险 {actuator_issue_count}".format(
                **counts
            )
        )
        missing = report["missing"]
        missing_parts = []
        for label, key in (("现象", "phenomenon"), ("请求", "request"), ("诱因", "algorithm_cause"), ("响应", "response")):
            values = missing.get(key) or []
            if values:
                missing_parts.append(f"{label}缺口：{', '.join(values[:8])}")
        lines.append(f"- 信号缺口：{'; '.join(missing_parts) if missing_parts else '无明显缺口'}")
        lines.append(f"- 测试观察：{item['observation']}")
        lines.append("")
        lines.append("代表性证据：")
        lines.append("")
        lines.append("| 侧向 | 问题/信号组 | 时间窗 | 证据摘要 |")
        lines.append("|---|---|---|---|")
        for row in report["evidence_rows"][:8]:
            lines.append(
                "| {side} | {issue} | {time} | {evidence} |".format(
                    side=_escape_table(str(row.get("side", ""))),
                    issue=_escape_table(str(row.get("issue", ""))),
                    time=_escape_table(str(row.get("time", ""))),
                    evidence=_escape_table(str(row.get("evidence", ""))[:220]),
                )
            )
        lines.append("")
        if report["diagnosis_steps"]:
            lines.append("归因链路摘录：")
            lines.append("")
            for row in report["diagnosis_steps"][:5]:
                lines.append(f"- {row.get('step', '')}：{row.get('result', '')}")
            lines.append("")
    lines.append("## 本次测试暴露的共性问题")
    lines.append("")
    lines.append("1. 当前归因层已经能抽取现象、请求、响应、诱因四类证据中的首个显著变化点，并在结论里输出“谁先变化”。")
    lines.append("2. 若请求与响应在采样精度内几乎同步，报告会保持为“未见执行器明显先触发”，避免把 0.1s 级差异说成绝对先后。")
    lines.append("3. 误制动、漏制动、跟车距离异常这类问题，仍依赖真实诱因信号覆盖质量；缺失时应说明诱因未闭合，而不是硬说某个目标导致。")
    lines.append("4. 这份报告会直接暴露“样例没被正式指标命中”的问题，后续新增问题类型时必须先补筛选代码或调整样例数据。")
    lines.append("")
    return "\n".join(lines)


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    main()
