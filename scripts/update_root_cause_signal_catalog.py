from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "config" / "vehicle_response_signal_catalog.xlsx"


HEADERS = ["信号语义", "具体信号名", "所属范围", "常见用途", "当前状态", "备注"]

ROWS = [
    [
        "纵向加速度",
        "IIMULonAccSec\nIIMULonAcc_adas",
        "现象层 / 实车表现",
        "判断顿挫、重制动、误制动、减速度波谷等实际车辆表现。",
        "已匹配",
        "VC9 H5 已匹配；两个来源都可作为纵向加速度候选，分析时优先结合有效性和采样质量判断。",
    ],
    [
        "驱动轮平均车速",
        "VehSpdAvgDrvn_1",
        "现象层 / 实车表现",
        "判断车速变化、未达到 Setspeed、不合理低速、溜车等。",
        "已匹配",
        "VC9 H5 已匹配。",
    ],
    [
        "非驱动轮平均车速",
        "VehSpdAvgNDrvn",
        "现象层 / 实车表现",
        "与驱动轮车速互相校验，辅助判断低速、溜车和车速异常。",
        "已匹配",
        "VC9 H5 已匹配。",
    ],
    [
        "车辆静止标志",
        "IVehMvngStat",
        "现象层 / 场景诱因",
        "判断低速保持、起步、溜车等场景是否处于静止或近静止状态。",
        "已匹配",
        "真实路径 Automatic_Braking_Status_044_M.IVehMvngStat；0=moving，1=stationary。",
    ],
    [
        "跟车目标 ID",
        "FLT1_Global_ID",
        "算法请求诱因",
        "判断目标切换、目标丢失或重新捕获是否导致算法请求变化。",
        "已匹配",
        "VC9 H5 已匹配。FLT1_CUTIN_Flag 当前数据暂未接入，代码按默认值 0 处理，即 FLT1 默认视为跟车目标；待真实 flag 入数后再切换为真实信号。",
    ],
    [
        "跟车目标信息（位置，速度，加速度，TTC）",
        "FLT1_Long_Range\nFLT1_Long_Velocity\nFLT1_Long_Accel\nLeadTargetTTC（由距离/相对速度计算）",
        "算法请求诱因",
        "判断前车距离、相对速度、加速度和 TTC 是否触发减速、低速或跟车距离异常。",
        "已匹配",
        "FLT1_CUTIN_Flag 当前数据暂未接入，代码按默认值 0 处理，即 FLT1 默认视为跟车目标；TTC 由距离和相对速度计算。",
    ],
    [
        "Cut-in 目标 ID",
        "FLT1_Global_ID",
        "算法请求诱因",
        "判断 cut-in 目标切换是否导致算法请求变化。",
        "已匹配",
        "FLT1_CUTIN_Flag=1 时 FLT1 表示 Cut-in 目标；=0 时表示跟车目标。当前数据暂未接入 FLT1_CUTIN_Flag，代码按默认值 0 处理，即 FLT1 默认视为跟车目标；待真实 flag 入数后再切换为真实信号。",
    ],
    [
        "Cut-in 目标信息（位置，速度，加速度，TTC）",
        "FLT1_Long_Range\nFLT1_Long_Velocity\nFLT1_Long_Accel\nCutInTargetTTC（由距离/相对速度计算）",
        "算法请求诱因",
        "判断插入目标距离、速度、加速度或 TTC 是否触发减速、低速或误制动。",
        "已匹配",
        "FLT1_CUTIN_Flag=1 时 FLT1 表示 Cut-in 目标；TTC 由距离和相对速度计算。跟车目标和 Cut-in 目标互斥，分析时不要因为另一个目标缺失而提示缺少判断依据。当前数据暂未接入 FLT1_CUTIN_Flag，代码按默认值 0 处理。",
    ],
    [
        "坡度",
        "Debug_single_Reserved_51",
        "算法请求诱因 / 场景",
        "辅助判断溜车、低速保持和制动保持是否受坡度影响。",
        "已匹配",
        "真实路径 Eth_Debug_CAN5/MPC_DebugMsg_Reserved_18.Debug_single_Reserved_51；坡度角，单位 deg。",
    ],
    [
        "设定车速",
        "IACCDrvrSeltdSpd",
        "算法请求诱因",
        "判断未达到 Setspeed、不合理低速、重制动或顿挫是否与设速变化相关。",
        "已匹配",
        "用户确认：IACCDrvrSeltdSpd 对应设定车速。",
    ],
    [
        "ACC功能开启标志位",
        "IACCAct376",
        "现象层 / 场景",
        "确认 ACC 是否处于激活或相关工作状态。",
        "已匹配",
        "VC9 H5 已匹配。",
    ],
    [
        "ACC功能 Override 标志位",
        "IAccPdlOvrrdAtv",
        "现象层 / 场景",
        "判断驾驶员接管或油门覆盖是否影响 ACC 请求和退出。",
        "已匹配",
        "VC9 H5 已匹配。",
    ],
    [
        "曲率限速值",
        "Debug_uint_Reserved_48",
        "算法请求诱因",
        "判断曲率/道路限速是否低于当前车速或设速，从而触发减速。",
        "已匹配",
        "VC9 H5 已匹配：MPC_DebugMsg_Reserved_14.Debug_uint_Reserved_48。本次样例中取值恒为 0，后续分析按曲率限速值读取。",
    ],
    [
        "曲率",
        "Left_Lane_Track_Coeff.LaneModelC2\nRight_Lane_Track_Coeff.LaneModelC2\nCurvature（由 1/(Left_Lane_Track_Coeff.LaneModelC2 + Right_Lane_Track_Coeff.LaneModelC2) 计算）",
        "算法请求诱因",
        "判断道路曲率或曲率突变是否可能导致限速或请求波动。",
        "已匹配",
        "VC9 H5 未找到 F_V_Right_Lane_ModelC2 / F_V_Left_Lane_ModelC2，已匹配真实字段 Left_Lane_Track_Coeff.LaneModelC2 与 Right_Lane_Track_Coeff.LaneModelC2；分母为 0 或接近 0 时跳过。",
    ],
    [
        "曲率限速使能标志",
        "Debug_uint_Reserved_49",
        "算法请求诱因",
        "判断曲率限速策略是否真的启用。",
        "已匹配",
        "VC9 H5 已匹配：MPC_DebugMsg_Reserved_15.Debug_uint_Reserved_49。本次样例中取值恒为 2；当前代码按非 0 视为使能，具体枚举语义后续可再确认。",
    ],
    [
        "ACC驱动请求",
        "IACCATC_ACCAct\nIACCATC_ACCTyp\nIACCATC_AxlTrqRq\nIACCATC_DrvAstdGoSt",
        "算法请求侧",
        "判断 ACC 是否主动发出驱动请求、请求扭矩和停走状态。",
        "已匹配",
        "VC9 H5 已匹配。",
    ],
    [
        "ACC驱动响应",
        "IACCATCS_RqStat\nIACCATCS_LmtgStat\nIActAxleTrq\nIActAxleTrqV",
        "执行器响应侧",
        "判断驱动系统是否接受、拒绝、仲裁丢失、限扭或实际扭矩未跟随。",
        "已匹配",
        "VC9 H5 已匹配。",
    ],
    [
        "ACC制动请求",
        "IACCBSCE_ACCAct1\nIACCBSCE_AutBrkTp1\nIACCBSCE_ACCAccl1\nIACCBSCE_ACCTrq1",
        "算法请求侧",
        "判断 ACC 是否主动请求制动、目标减速度和制动扭矩。",
        "已匹配",
        "VC9 H5 已匹配。",
    ],
    [
        "AEB制动请求",
        "IFCACBSC_AutBrkRqTp1\nIFCACBSC_CtrlTrq1",
        "算法请求侧",
        "判断 AEB/FCA 是否主动请求按扭矩制动。",
        "已匹配",
        "VC9 H5 已匹配。",
    ],
    [
        "制动响应",
        "IACCBrkngAct\nIAutoBrkngAct\nIBrkSysAutBrkStat\nICSTBATS_TrqVl\nIRegenBrkAxTrqAch",
        "执行器响应侧",
        "判断 EBCM 是否进入自动制动、ACC制动状态以及实际制动扭矩响应。",
        "已匹配",
        "液压制动扭矩响应 ICSTBATS_TrqVl 和电制动扭矩响应 IRegenBrkAxTrqAch 均为响应侧证据；IRegenBrkAxTorqRqst 是请求值，不能替代响应值。",
    ],
    [
        "制动踏板状态",
        "BrkPedApp\nBrkPedTrvlAchvd",
        "现象层 / 接管",
        "判断驾驶员踩制动是否影响 ACC/AEB 请求或退出。",
        "已匹配",
        "用于接管/override 场景解释。",
    ],
    [
        "挡位",
        "Gear\nITransEstGear_ECP_H1",
        "现象层 / 场景",
        "辅助判断溜车、低速保持、起步和倒挡/非前进挡场景。",
        "已匹配",
        "ITransEstGear_ECP_H1 已在 H5 中匹配；Gear 作为通用候选保留。",
    ],
    [
        "轮速方向",
        "IWhlRotDirStatLFHigFreq\nIWhlRotDirStatRFHigFreq\nIWhlRotDirStatLRHigFreq\nIWhlRotDirStatRRHigFreq",
        "现象层 / 实车表现",
        "判断溜车、后溜和车轮方向异常。",
        "待核对",
        "保留为溜车分析关注项，具体 H5 覆盖需要按数据确认。",
    ],
    [
        "车轮角速度有效位",
        "IWhlAngVelLFrtAuth\nIWhlAngVelRFrtAuth\nIWhlAngVelLRrAuth\nIWhlAngVelRRrAuth",
        "现象层 / 实车表现",
        "校验轮速方向或角速度信号是否有效。",
        "待核对",
        "保留为溜车分析关注项，具体 H5 覆盖需要按数据确认。",
    ],
]


def main() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "signal_catalog"
    ws.append(HEADERS)
    for row in ROWS:
        ws.append(row)

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    widths = [24, 42, 20, 42, 14, 54]
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        status = row[4].value
        if status == "已匹配":
            row[4].fill = PatternFill("solid", fgColor="D9EAD3")
        elif status == "部分匹配":
            row[4].fill = PatternFill("solid", fgColor="FFF2CC")
        elif status == "候选匹配":
            row[4].fill = PatternFill("solid", fgColor="D9EAF7")
        else:
            row[4].fill = PatternFill("solid", fgColor="F4CCCC")

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT)
    print(f"updated {OUT}")


if __name__ == "__main__":
    main()
