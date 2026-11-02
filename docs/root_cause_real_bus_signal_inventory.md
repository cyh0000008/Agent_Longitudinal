# 行车问题原因分析真实总线信号清单

本文档整理当前“行车问题原因分析”会用到的真实总线信号名。口径如下：

- 主来源：`config/vehicle_response_signal_catalog.xlsx` 中 `当前状态=已匹配` 且 `具体信号名` 非空的条目。
- 运行补充：`root_cause_analysis.py` 中固定会尝试读取的请求/响应信号也列入，方便核对实际代码行为。
- 不列入真实清单：`CutInTarget*`、`Curvature*` 等当前标记为 `待补充` 的占位项。
- `LeadTargetTTC` 是由距离和相对速度计算的派生量，不是总线原始信号。

## 现象层 / 实车表现

| 语义 | 真实信号名 | 已知路径 / 报文 | 主要用途 |
|---|---|---|---|
| 纵向加速度 | `IIMULonAccSec`<br>`IIMULonAcc_adas` | `IMU_Yaw_Long_Acc_037_M2.IIMULonAccSec`<br>`IMU_Yaw_Long_Acc_037_M2.IIMULonAcc_adas` | 顿挫、重制动、误制动、跟车距离异常等现象确认 |
| 驱动轮平均车速 | `VehSpdAvgDrvn_1` | `PPEI_Vehicle_Speed_and_Distance_209_M1.IVehSpdAvgDrvn_1` | 车速变化、稳速未达、不合理低速、溜车 |
| 非驱动轮平均车速 | `VehSpdAvgNDrvn` | `PPEI_Vehicle_Speed_and_Distance_208_M2.IVehSpdAvgNDrvn` | 车速互校、低速、溜车、速度异常 |
| 车辆静止 / 运动状态 | `IVehMvngStat` | `Automatic_Braking_Status_044_M.IVehMvngStat` | 静止、起步、低速保持、溜车场景判断 |
| ACC 功能状态 | `IACCAct376` | `Adaptive_Cruise_Disp_Stat.IACCAct376` | 判断 ACC 是否处于激活或相关工作状态 |
| ACC Override 状态 | `IAccPdlOvrrdAtv` | `PPEI_Propulsion_General_Data_1_0C2_M.IAccPdlOvrrdAtv` | 驾驶员接管、油门覆盖、ACC 退出或请求变化解释 |
| 制动踏板状态 | `BrkPedApp`<br>`BrkPedTrvlAchvd` | `PPEI_Chassis_General_Data_1_031_M.IBrkPedApp`<br>`PPEI_Brake_Apply_Status_030_M.IBrkPedTrvlAchvd` | 驾驶员踩制动是否影响 ACC 请求或退出 |
| 挡位 | `Gear`<br>`ITransEstGear_ECP_H1` | `ITransEstGear_ECP_H1` 已在 H5 中匹配；`Gear` 为通用信号名 | 溜车、起步、倒挡/非前进挡场景判断 |
| 轮速方向 | `IWhlRotDirStatLFHigFreq`<br>`IWhlRotDirStatRFHigFreq`<br>`IWhlRotDirStatLRHigFreq`<br>`IWhlRotDirStatRRHigFreq` | 待从 H5 路径自动匹配 | 溜车、后溜、车辆运动方向判断 |
| 车轮角速度有效位 | `IWhlAngVelLFrtAuth`<br>`IWhlAngVelRFrtAuth`<br>`IWhlAngVelLRrAuth`<br>`IWhlAngVelRRrAuth` | 待从 H5 路径自动匹配 | 判断轮速/角速度信号是否可信 |

## 算法请求侧

| 语义 | 真实信号名 | 已知路径 / 报文 | 主要用途 |
|---|---|---|---|
| ACC 驱动请求 | `IACCATC_ACCAct`<br>`IACCATC_ACCTyp`<br>`IACCATC_AxlTrqRq`<br>`IACCATC_DrvAstdGoSt` | `PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M.*` | 判断 ACC 是否主动发出驱动请求、请求扭矩和停走状态 |
| ACC 制动请求 | `IACCBSCE_ACCAct1`<br>`IACCBSCE_AutBrkTp1`<br>`IACCBSCE_ACCAccl1`<br>`IACCBSCE_ACCTrq1` | `ADAS_Command_Ext1.*` | 判断 ACC 是否主动请求制动、目标减速度和制动扭矩 |

## 执行器响应侧

| 语义 | 真实信号名 | 已知路径 / 报文 | 主要用途 |
|---|---|---|---|
| ACC 驱动响应 | `IACCATCS_RqStat`<br>`IACCATCS_LmtgStat`<br>`IActAxleTrq`<br>`IActAxleTrqV` | `PPEI_Propulsion_General_Data_1_0C2_M.IACCATCS_RqStat`<br>`PPEI_Propulsion_General_Data_1_0C2_M.IACCATCS_LmtgStat`<br>`PPEI_Engine_Torque_Status_4_08D_M.IActAxleTrq`<br>`PPEI_Engine_Torque_Status_4_08D_M.IActAxleTrqV` | 判断驱动系统是否接受、拒绝、仲裁丢失、限扭或实际扭矩未跟随 |
| 制动响应主信号 | `IACCBrkngAct`<br>`IBrkSysAutBrkStat`<br>`ICSTBATS_TrqVl`<br>`IRegenBrkAxTrqAch` | `PPEI_Chassis_General_Data_3_0AC_M.IACCBrkngAct`<br>`Automatic_Braking_Status_044_M.IBrkSysAutBrkStat`<br>`ICSTBATS_TrqVl`<br>`IRegenBrkAxTrqAch` | 判断 EBCM 是否进入 ACC 制动状态，以及液压/电制动实际扭矩响应 |

## 算法请求诱因 / 场景输入

| 语义 | 真实信号名 | 已知路径 / 报文 | 主要用途 |
|---|---|---|---|
| 跟车目标 ID | `FLT1_Global_ID` | 待从 H5 路径自动匹配 | 判断前车目标切换、丢失或重新捕获 |
| 跟车目标信息 | `FLT1_Long_Range`<br>`FLT1_Long_Velocity`<br>`FLT1_Long_Accel` | 待从 H5 路径自动匹配 | 判断前车距离、相对速度、加速度是否触发减速、低速或跟车距离异常 |
| 跟车目标 TTC | `LeadTargetTTC` | 派生量，不是总线信号 | 由 `FLT1_Long_Range` 和 `FLT1_Long_Velocity` 计算；仅在闭合速度大于 0 时作为风险证据 |
| 设定车速 | `IACCDrvrSeltdSpd` | `Adaptive_Cruise_Disp_Stat.IACCDrvrSeltdSpd` | 判断未达 SetSpeed、不合理低速、重制动、顿挫是否与设速变化相关 |
| 坡度 | `Debug_single_Reserved_51` | 待从 H5 路径自动匹配 | 辅助判断溜车、低速保持和制动保持 |
| 跟车时距设置 | `Adaptive_Cruise_Disp_Stat.IACCHdwyStg`<br>`IACCHdwyStg`<br>`ACCHdwyStg` | `Adaptive_Cruise_Disp_Stat.IACCHdwyStg` | 1/2/3 分别对应 1.2s/1.8s/2.4s，用于跟车距离异常分析 |

## ACC/LCC 功能退出原因分析

来源：`config/acc_lcc_exit_reason_rules.xlsx` 的 `signals` 工作表，运行入口为 `function_exit_analysis.py`。这些信号用于判断 ACC/LCC 从配置表定义的强激活态退到非强激活态、非 OFF 状态退到 OFF 或临时退出时，状态变化和退出原因码是否同步出现。

| 功能 | 语义 | 真实信号名 | H5 数据集路径 | 主要用途 |
|---|---|---|---|---|
| ACC | ACC 功能状态（debug） | `MPC_DebugMsg_Reserved_14.Debug_uint_Reserved_47` | `/VC9_ver0520_2026-06-29_21_19_48_CAN.asc/Eth_Debug_CAN5/MPC_DebugMsg_Reserved_14` | 判断 ACC debug 状态变化，例如 Off、Passive、Standby、Active、Override |
| ACC | ACC 功能抑制/退出原因 | `MPC_DebugMsg_Reserved_14.ACC_Inhibit_Reasion` | `/VC9_ver0520_2026-06-29_21_19_48_CAN.asc/Eth_Debug_CAN5/MPC_DebugMsg_Reserved_14` | 读取 ACC 功能抑制和退出原因枚举，解释为什么不能激活或为什么退出 |
| LCC | LCC 功能状态 | `MPC_DebugMsg_Reserved_10.Debug_uint_Reserved_11` | `/VC9_ver0520_2026-06-29_21_19_48_CAN.asc/Eth_Debug_CAN5/MPC_DebugMsg_Reserved_10` | 判断 LCC 状态变化 |
| LCC | LCC 抑制/退出原因 | `MPC_DebugMsg_Reserved_10.LCC_Inhibit_Reasion` | `/VC9_ver0520_2026-06-29_21_19_48_CAN.asc/Eth_Debug_CAN5/MPC_DebugMsg_Reserved_10` | 读取 LCC 抑制或退出原因 |

说明：

- ACC 状态使用 `Debug_uint_Reserved_47`，ACC 功能抑制/退出原因使用 `ACC_Inhibit_Reasion`；不再查询 MRR 给 ESC 状态或 ACC 退出原因位图组。
- LCC 状态使用 `Debug_uint_Reserved_11`，LCC 抑制/退出原因使用 `LCC_Inhibit_Reasion`。
- 状态类信号按枚举值解码；ACC/LCC 会把 `decode_values` 中状态枚举的 `0/OFF` 和 `reserved` 之外的状态作为非 OFF 状态。强激活态退到非强激活态会被识别为退出；非 OFF 可用态退到 OFF 时，只有原因码同步置位才作为功能退出/不可用事件。退出原因类信号按枚举解码，具体含义维护在同一配置表的 `decode_values` 工作表。
- 如果上传 H5 中缺少 `Eth_Debug_CAN5/MPC_DebugMsg_Reserved_*` 这类 debug 报文，只能看到功能状态变化，无法完整解释退出原因。

## 当前不纳入真实信号清单的占位项

| 语义 | 当前状态 | 说明 |
|---|---|---|
| Cut-in 目标 ID | 待补充 | 用户已确认 `FLT3/FLT5` 不是 Cut-in 目标 ID，不可作为替代 |
| Cut-in 目标信息 | 待补充 | 用户已确认 `FLT3/FLT5` 不是 Cut-in 目标信息，不可作为替代 |
| 曲率限速值 | 待补充 | 用户已确认 `IFCMSpdLmtSnSpdLmtVau / IFCMSpdLmtSnSpdLmtVauAs` 不是曲率限速值 |
| 曲率 | 待补充 | 用户已确认 `LaneModelC2 / Left_Lane_A2 / Right_Lane_A2` 不等价于需要的车辆路径曲率 |
| 曲率限速使能标志 | 待补充 | 当前未匹配到明确使能标志 |

## 需要同步修的实现风险

- `performance_analysis.py` 的 `ROOT_CAUSE_EXTRA_SIGNALS` 里仍保留部分旧占位/候选信号。后续运行时应继续以 `vehicle_response_signal_catalog.xlsx` 的 `已匹配` 状态为准。
- `root_cause_analysis.py` 中部分中文语义映射和提示文本存在乱码，可能导致 `vehicle_response_signal_catalog.xlsx` 里的 `待补充/已匹配` 状态没有完全生效。建议下一步优先修复这段映射。
- `IRegenBrkAxTrqAch` 是电制动响应；`IRegenBrkAxTorqRqst` 是请求值，不能替代响应值。
