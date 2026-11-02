# 行车问题原因分析信号匹配核对表

本文档用于核对“行车问题原因分析”准备使用的信号匹配关系。目标是把同一段数据拆成四层证据：

- 现象层：车辆实际表现，例如纵向加速度、车速、轮速方向。
- 算法请求侧：本轮 ACC/LCC 行车问题只看已定义的 ACC/LCC 请求，例如驱动扭矩请求、ACC 制动扭矩请求、目标加速度、转向请求。
- 算法请求诱因：如果算法请求侧确实主动给出了减速、制动或扭矩波动请求，继续看目标、曲率限速、SetSpeed 等场景输入是否能解释算法为什么这么请求。
- 执行器响应侧：制动、驱动、转向、换挡等执行器是否按接口文档响应。

后续代码实现时，性能指标命中的问题只代表“现象层发现异常”，不能直接推断是算法问题。必须再结合算法请求侧、算法请求诱因和执行器响应侧信号做归因。

## H5 匹配规则

H5 里的信号可能来自不同解析链路，名字不一定完全等于执行器文档里的短名。归因层应按下面顺序匹配：

1. 完整 H5 key：`报文名.信号名`，例如 `ADAS_Command_Ext1.ACCBSCE_ACCTrq1`。
2. 带 `I` 前缀的完整 key：例如 `ADAS_Command_Ext1.IACCBSCE_ACCTrq1`。
3. 文档短名：例如 `ACCBSCE_ACCTrq1`。
4. 带 `I` 前缀的短名：例如 `IACCBSCE_ACCTrq1`。

如果信号在接口文档中定义，但当前 H5 没有，应在报告中表达为“请求侧证据缺失 / 待补充信号”，不能当成“请求侧正常”。

行车问题原因分析输出前必须先检查当前现象对应的关键请求侧、请求诱因和响应侧信号是否齐全。匹配到若干非关键请求/响应信号，只能说明数据里有部分覆盖，不能替代关键证据。只要关键量缺失，结论应明确写成“现象成立，但原因分析证据不足，需要补充信号”，不要继续判断算法侧或执行器侧为根因。

## 现象层信号

这些信号主要来自性能指标代码，用于判断车辆实际表现是否异常。

| 短名 | H5 候选 key | 归属 | 用途 | 来源 |
|---|---|---|---|---|
| `IIMULonAccSec` | `IMU_Yaw_Long_Acc_037_M2.IIMULonAccSec` | 车辆响应 | 纵向加速度，用于顿挫、重制动、加速度跳变判断 | 性能指标代码 |
| `IVehSpdAvgDrvn_1` / `VehSpdAvgDrvn_1` | `PPEI_Vehicle_Speed_and_Distance_209_M1.IVehSpdAvgDrvn_1` / `PPEI_Vehicle_Speed_and_Distance_209_M1.VehSpdAvgDrvn_1` | 车辆响应 | 驱动轮平均车速，用于低速、设速达成、场景速度判断 | 性能指标代码 / 信号字典 |
| `IVehSpdAvgNDrvn` / `VehSpdAvgNDrvn` | `PPEI_Vehicle_Speed_and_Distance_208_M2.IVehSpdAvgNDrvn` / `PPEI_Vehicle_Speed_and_Distance_208_M2.VehSpdAvgNDrvn` | 车辆响应 | 非驱动轮平均车速，用于溜车、低速和轮速一致性判断 | 性能指标代码 / 信号字典 |

## ACC 驱动请求与响应

用于判断 ACC 纵向驱动侧：算法是否提出驱动扭矩/激活请求，驱动系统是否接受、拒绝、仲裁丢失或限扭。

| 短名 | 报文 | CAN | 用途 | 正常关注点 | 来源 |
|---|---|---:|---|---|---|
| `ACCATC_ACCAct` | `PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M` | `0x194` | ACC 驱动请求激活 | `1=True` 表示 ADAS 正在请求 ACC 驱动控制 | PS ICD / 信号字典 |
| `ACCATC_ACCTyp` | `PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M` | `0x194` | ACC 类型 | `0=ACC`，`1=FSRA`，`2=FSRA with Booster`，`3=FSRA with LCC` | PS ICD / 信号字典 |
| `ACCATC_AxlTrqRq` | `PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M` | `0x194` | ADAS 请求驱动轴扭矩 | 请求突变、阶跃或斜率异常时，偏向算法请求侧 | PS ICD / 信号字典 |
| `ACCATC_DrvAstdGoSt` | `PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M` | `0x194` | FSRA 停走状态 | 静止起步、等待驾驶员输入场景判断 | PS ICD / 信号字典 |
| `ACCATCS_RqStat` | `PPEI_Propulsion_General_Data_1_0C2_M` | `0x0C2` | 驱动系统对 ACC 请求的响应状态 | `1=Request Honored`，`2=Lost Arbitration`，`3=Request Denied`，`4=Suspended Till Driver Input` | PS ICD / 信号字典 |
| `ACCATCS_LmtgStat` | `PPEI_Propulsion_General_Data_1_0C2_M` | `0x0C2` | 驱动限扭状态 | `0=Not Limited`，`1/2/3` 表示最小/最大/斜率限制 | PS ICD / 信号字典 |
| `ActAxleTrq` | `PPEI_Engine_Torque_Status_4_08D_M` | `0x08D` | 实际驱动轴扭矩 | 用于对比 `ACCATC_AxlTrqRq` 是否被执行器跟随 | PS ICD / 信号字典 |
| `ActAxleTrqV` | `PPEI_Engine_Torque_Status_4_08D_M` | `0x08D` | 实际驱动轴扭矩有效位 | `0=Valid`，`1=Invalid` | PS ICD / 信号字典 |
| `AccPdlOvrrdAtv` | `PPEI_Propulsion_General_Data_1_0C2_M` | `0x0C2` | 加速踏板 override | 驾驶员接管时需要解释 ACC 退出或请求拒绝 | PS ICD / 信号字典 |

## ACC 制动请求与响应

用于判断 ACC 制动侧：ACC 是否发出制动请求，EBCM 是否进入 ACC 制动 active / limited / failed。ACC/LCC 问题不使用 AEB/FCA/DAeb/*_AEBUsd 接口归因。

| 短名 | 报文 | CAN | 用途 | 正常关注点 | 来源 |
|---|---|---:|---|---|---|
| `ACCBSCE_ACCAct1` | `ADAS_Command_Ext1` | `0x0B0` | ACC 制动请求激活 | `1=True` 表示 ACC 制动命令有效 | EBCM ICD / 信号字典 |
| `ACCBSCE_AutBrkTp1` | `ADAS_Command_Ext1` | `0x0B0` | ACC 自动制动类型 | `1=No Braking`，`5=Hold`，`6=Brake Per Torque Request` | EBCM ICD / 信号字典 |
| `ACCBSCE_ACCAccl1` | `ADAS_Command_Ext1` | `0x0B0` | ACC 目标加减速度请求 | 用于判断算法请求是否本身产生重制动/顿挫 | EBCM ICD / 信号字典 |
| `ACCBSCE_ACCTrq1` | `ADAS_Command_Ext1` | `0x0B0` | ACC 总制动扭矩请求 | 用户确认它就是 ACC 制动扭矩请求，行车问题原因分析需要重点看 | EBCM ICD / 信号字典 |
| `AutoBrkngAvlbl` | `PPEI_Chassis_General_Data_2_191_M` | `0x191` | EBCM 自动制动可用 | `1=True` 通常表示自动制动能力可用 | EBCM ICD / 信号字典 |
| `ACCBrkngAct` | `PPEI_Chassis_General_Data_3_0AC_M` | `0x0AC` | ACC 制动激活 | ACC 制动请求后 EBCM 应反馈制动 active | EBCM ICD / 信号字典 |
| `BrkSysAutBrkStat` | `Automatic_Braking_Status_044_M` | `0x044` | 制动系统自动制动状态 | `0=Not active`，`1=Active`，`2=Limited`，`3=Failed` | EBCM ICD / 信号字典 |
| `BrkSysAutBrkFld` | `PPEI_Chassis_General_Data_2_191_M` | `0x191` | 自动制动失败 | `1=True` 表示 EBCM 报告自动制动失败 | EBCM ICD / 信号字典 |
| `FSRACCBrkngReqDenied` | `Automatic_Braking_Status_044_M` | `0x044` | FSRA/ACC 制动请求拒绝 | `1=True` 表示制动请求被拒绝 | EBCM ICD / 信号字典 |
| `BrkPedApp` | `PPEI_Chassis_General_Data_1_031_M` | `0x031` | 制动踏板踩下 | 驾驶员制动接管会影响 ACC 请求归因 | PS ICD / 信号字典 |
| `BrkPedTrvlAchvd` | `PPEI_Brake_Apply_Status_030_M` | `0x030` | 制动踏板行程达到 | 驾驶员制动接管判断 | PS ICD / 信号字典 |
| `ICSTBATS_TrqVl` | `PPEI_Chassis_General_Status_2.ICSTBATS_TrqVl` | 待确认 | 液压制动扭矩响应 | 当前 VC9 H5 已匹配，可作为制动实际响应值的一部分 | 用户补充 / VC9 H5 核对 |
| `IRegenBrkAxTrqAch` | 待数据库/后续 H5 匹配 | 待确认 | 电制动扭矩响应 | 当前 VC9 H5 未匹配到响应值；不能用 `IRegenBrkAxTorqRqst` 请求值替代响应值 | 用户补充 / VC9 H5 核对 |

说明：ACC 制动侧行车问题原因分析只保留 C-CAN 的 `ADAS_Command_Ext1` 报文信号。P-CAN 的 `Adaptive_Cruise_Command_Ext_1A3_M.ACCBSCE_*` 可能仍被历史性能指标使用，但不作为本轮 ACC 制动请求归因证据。

## 2026-07-07 VC9 H5 核对结果

核对文件：

- 数据：`VC9_ver0520_2026-06-29_21_19_48_CAN_parsed.h5`
- 有效性检测：`VC9_ETH_signal_check_result.xlsx`

当前工程读取逻辑可在该 H5 中识别 9552 个信号。关键匹配结果如下：

| 层级 | 已匹配信号 | 仍缺失 / 待确认 |
|---|---|---|
| 现象层 | `IIMULonAccSec`、`IIMULonAcc_adas`、`VehSpdAvgDrvn_1`、`VehSpdAvgNDrvn`、`IACCAct376`、`IAccPdlOvrrdAtv` | `VehicleStandstillFlag` |
| ACC 驱动请求 | `IACCATC_ACCAct`、`IACCATC_ACCTyp`、`IACCATC_AxlTrqRq`、`IACCATC_DrvAstdGoSt` | - |
| ACC 驱动响应 | `IACCATCS_RqStat`、`IACCATCS_LmtgStat`、`IActAxleTrq`、`IActAxleTrqV` | - |
| ACC 制动请求 | `IACCBSCE_ACCAct1`、`IACCBSCE_AutBrkTp1`、`IACCBSCE_ACCAccl1`、`IACCBSCE_ACCTrq1` | - |
| 制动响应 | `IACCBrkngAct`、`IBrkSysAutBrkStat`、`ICSTBATS_TrqVl` | `IRegenBrkAxTrqAch` |
| 跟车目标 | `FLT1_Global_ID`、`FLT1_Long_Range`、`FLT1_Long_Velocity`、`FLT1_Long_Accel` | `LeadTargetTTC` |
| Cut-in 目标候选 | `FLT3_*`、`FLT5_*` 作为候选 | `CutInTargetTTC`，且 Cut-in 目标源需要项目确认 |
| 曲率 / 限速 | `IFCMSpdLmtSnSpdLmtVau`、`IFCMSpdLmtSnSpdLmtVauAs`、`LaneModelC2`、`Left_Lane_A2`、`Right_Lane_A2` | `CurvatureLimitEnable`，曲率候选是否等价路径曲率需要确认 |
| 坡度 | `Debug_single_Reserved_51` 作为候选 | 候选语义需要项目确认 |

输出口径：

- 如果现象层、请求侧和响应侧都能读到，但目标、曲率限速、SetSpeed 等诱因侧信号缺失，结论必须写成“现象和请求/响应证据已看到，但解释算法为什么给出该请求的场景输入不足”。
- 不允许因为请求侧和响应侧匹配数量较多，就直接判断“算法原因”或“执行器原因”。
- `IRegenBrkAxTorqRqst` 是电制动请求，不是电制动实际响应；当前不能用它补 `IRegenBrkAxTrqAch`。

## 算法请求诱因候选信号

这些信号用于回答“算法为什么给出了减速、制动或扭矩波动请求”。如果这些信号在当前数据里缺失，报告应写清楚“无法解释算法请求的触发原因，需要补充场景输入信号”。

| 短名 | 含义 | 主要用于 | 归因关注点 |
|---|---|---|---|
| `LeadTargetId` | 跟车目标 ID | 顿挫、重制动、误制动、未达到 SetSpeed、不合理低速、跟车距离异常 | 目标是否切换、丢失或重新捕获 |
| `LeadTargetDistance` | 跟车目标距离 | 顿挫、重制动、误制动、未达到 SetSpeed、不合理低速、跟车距离异常 | 目标距离是否突然变小或跳变 |
| `LeadTargetVelocity` | 跟车目标速度 | 顿挫、重制动、误制动、未达到 SetSpeed、不合理低速 | 目标速度是否突降或不连续 |
| `LeadTargetAccel` | 跟车目标加速度 | 顿挫、重制动、误制动、未达到 SetSpeed、不合理低速 | 目标减速度是否突变 |
| `LeadTargetTTC` | 跟车目标 TTC | 重制动、误制动、跟车距离异常 | TTC 是否快速降低到危险区间 |
| `CutInTargetId` | Cut-in 目标 ID | 顿挫、重制动、误制动、未达到 SetSpeed、不合理低速 | 是否发生 cut-in 目标切换 |
| `CutInTargetDistance` | Cut-in 目标距离 | 顿挫、重制动、误制动、未达到 SetSpeed、不合理低速 | 插入目标距离是否过近 |
| `CutInTargetVelocity` | Cut-in 目标速度 | 顿挫、重制动、误制动、未达到 SetSpeed、不合理低速 | 插入目标速度是否明显低于自车 |
| `CutInTargetAccel` | Cut-in 目标加速度 | 顿挫、重制动、误制动、未达到 SetSpeed、不合理低速 | 插入目标是否急减速 |
| `CutInTargetTTC` | Cut-in 目标 TTC | 重制动、误制动、跟车距离异常 | TTC 是否快速降低 |
| `CurvatureLimitSpeed` | 曲率限速值 | 顿挫、重制动、未达到 SetSpeed、不合理低速 | 曲率限速是否低于当前车速或设速 |
| `Curvature` | 曲率 | 顿挫、重制动、未达到 SetSpeed、不合理低速 | 曲率是否突变或进入急弯 |
| `CurvatureLimitEnable` | 曲率限速使能 | 顿挫、重制动、未达到 SetSpeed、不合理低速 | 曲率限速是否在问题窗口内使能 |
| `IACCDrvrSeltdSpd` | ACC 设定车速 | 未达到 SetSpeed、不合理低速、顿挫、重制动 | 设速是否下降或低于用户期望 |

## APA 制动 / 驱动请求与响应

用于判断 APA/HPP 泊车过程中的制动和驱动握手是否闭环。

| 短名 | 报文 | CAN | 用途 | 正常关注点 | 来源 |
|---|---|---:|---|---|---|
| `APABT_APAType` | `APA_Brake_Propulsion_Torqu_1F0_M` | `0x1F0` | APA/HPP 类型 | `1=APA`，`2=HPP` | EBCM / PS ICD / 信号字典 |
| `APABT_SRPBrkTrqReq` | `APA_Brake_Propulsion_Torqu_1F0_M` | `0x1F0` | SRP 制动扭矩请求 | 请求突变或不合理大值时，偏向算法请求侧 | EBCM ICD / 信号字典 |
| `APABT_SRPBrkTrqReqType` | `APA_Brake_Propulsion_Torqu_1F0_M` | `0x1F0` | SRP 制动请求类型 | `2=Brake Per Torque`，`3=Hold` | EBCM ICD / 信号字典 |
| `APABT_SRPEPBReq` | `APA_Brake_Propulsion_Torqu_1F0_M` | `0x1F0` | SRP EPB 请求 | `1=Apply`，`2=Release` | EBCM ICD / 信号字典 |
| `APAPpTrq_SRPPpSysActReq` | `APA_Brake_Propulsion_Torqu_1F0_M` | `0x1F0` | SRP 驱动系统激活请求 | `1=True` 后驱动系统应响应 | PS ICD / 信号字典 |
| `APAPpTrq_SRPPpSysTrqReq` | `APA_Brake_Propulsion_Torqu_1F0_M` | `0x1F0` | SRP 驱动扭矩请求 | 用于判断 APA 请求侧是否产生扭矩突变 | PS ICD / 信号字典 |
| `SuprRmtPrkPrplSysResp` | `PPEI_Trans_General_Status_2_ECP_H1_0D7_M` | `0x0D7` | SRP 驱动系统响应 | `0=Standby`，`1=Torque Request Honored`，`2=Arbitration Failed`，`3=Failed` | PS ICD / 信号字典 |
| `BrkSysAPABrk_SRPBrkResp` | `EBCMGnrlInfo2_Prtctd_MSG_130_M` | `0x130` | EBCM SRP 制动响应 | `2=Active Hold`，`3=Active Control`，`4=Failed` | EBCM ICD / 信号字典 |
| `APABrkStat` | `Automatic_Braking_Status_044_M` | `0x044` | APA 制动状态 | `2=Active Hold`，`3=Active Control`，`4=Failed`，`5=Finished Hold` | EBCM ICD / 信号字典 |

## 归因时的初步使用方式

| 用户现象 | 先看现象层 | 再看算法请求侧 | 再看算法请求诱因 | 再看执行器响应侧 | 初步归因口径 |
|---|---|---|---|---|---|
| 顿挫 / 抖动 | `IIMULonAccSec` 是否跳变 | `ACCBSCE_ACCAccl1`、`ACCBSCE_ACCTrq1`、`ACCATC_AxlTrqRq`、`APABT_SRPBrkTrqReq` 是否同步跳变 | 跟车目标 / Cut-in 目标是否切换或距离、速度、加速度、TTC 跳变；曲率限速、SetSpeed 是否突变 | `ACCATCS_RqStat`、`BrkSysAutBrkStat`、`ACCBrkngAct`、`ActAxleTrq`、实际制动扭矩是否滞后或异常 | 请求先跳变且诱因存在，更偏算法按场景主动请求；请求跳变但诱因缺失，需补场景输入；请求平滑但响应突变/滞后，更偏执行器侧 |
| 重制动 / 误制动 | 加速度是否出现明显负向峰值 | `ACCBSCE_AutBrkTp1`、`ACCBSCE_ACCTrq1` 是否发出 ACC 制动扭矩请求 | 前车或 Cut-in 目标是否过近 / TTC 下降；曲率限速是否低于当前车速；SetSpeed 是否下降 | `ACCBrkngAct`、`BrkSysAutBrkStat`、实际制动扭矩是否按请求进入制动 | 若 ACC 主动要求大制动且场景诱因成立，优先解释为策略响应；若 ACC 主动要求大制动但诱因信号缺失，结论应写“原因未闭环”；若无 ACC 请求但制动激活，优先查 ACC 制动响应/接口异常 |
| 漏制动 / 制动没响应 | 实车是否未减速或减速不足 | `ACCBSCE_ACCTrq1` 是否已有明确 ACC 扭矩请求 | 如果 ACC 未请求，继续看目标、曲率或 SetSpeed 是否本应触发制动 | `ACCBrkngAct`、`BrkSysAutBrkStat`、`BrkSysAutBrkFld`、实际制动扭矩是否未响应/失败 | 有 ACC 请求无响应偏执行器侧；无 ACC 请求且诱因存在偏算法侧漏请求；无请求且诱因缺失则需要补场景输入 |
| 未达到 set speed / 不合理低速 | `VehSpdAvgDrvn_1` 与设速差距，车速是否长期低于设速或异常降低 | `IACCDrvrSeltdSpd`、`ACCATC_AxlTrqRq`、制动请求是否给出合理目标 | 前车 / Cut-in 是否限制车速；曲率限速是否压低目标速度；SetSpeed 是否本身下降 | `ACCATCS_RqStat`、`ACCATCS_LmtgStat`、`ActAxleTrq` 是否被限扭或未跟随 | 算法请求低速且诱因成立，偏策略限速；算法要求加速但执行器限扭/不跟随，偏驱动响应；诱因信号缺失时不能解释为什么低速 |
| 溜车 | `VehSpdAvgDrvn_1`、`VehSpdAvgNDrvn` 方向和速度 | APA/ACC 请求是否要求保持、制动或驱动 | 坡度、目标状态、SetSpeed 或泊车状态是否要求保持 | APA/ACC 制动/驱动响应是否进入 hold/control | 请求侧没要求保持偏算法侧；有请求但响应未闭环偏执行器侧；坡度等诱因缺失时只说明证据不足 |

说明：上表中的“算法请求侧 / 算法请求诱因 / 执行器响应侧”共同构成关键证据链。若对应关键信号缺失，报告只能说明目前能确认到哪一步，不能用其他匹配到的信号数量替代归因。

## 已确认口径

1. `ACCBSCE_ACCTrq1` 就是 ACC 制动扭矩请求，位于 `ADAS_Command_Ext1 / 0x0B0 / C-CAN`。
2. ACC/LCC 行车问题不使用 AEB/FCA 接口；如果相关 H5 信号存在，也不能作为 ACC/LCC 根因证据。
3. ACC 制动侧行车问题原因分析只保留 C-CAN 的 `ADAS_Command_Ext1` 信号。
4. ACC 驱动侧实际响应只看 `ACCATCS_RqStat / ACCATCS_LmtgStat / ActAxleTrq`。
5. APA 泊车侧行车问题原因分析暂不加入 EPS / ETRS 转向和换挡响应信号。
6. 制动扭矩实际响应值 = 液压制动扭矩响应 `ICSTBATS_TrqVl` + 电制动扭矩响应 `IRegenBrkAxTrqAch`；如果 `IRegenBrkAxTrqAch` 没有，就按 0 参与估算，并在报告中注明缺失。
