# 控制频繁抖动检测说明

## 目标

针对 BLF 数据筛选“控制频繁抖动导致加速度抖动”的场景。当前实现只使用 BLF + ARXML 直解，不读取 MDF。

制动侧证据只使用两个信号：

- `IACCBSCE_ACCTrq1`：制动请求扭矩
- `ICSTBATS_TrqVl`：制动响应扭矩

不再使用，也不在图示中展示：

- `IDrvIntndTtlBrkTrq`
- `IAvgWhlBrkPrsrEst`
- `IAvgWhlBrkPrsrEst_1`

## 输入

- BLF 文件或 BLF 文件夹
- ARXML 数据库，例如：
  `D:\PATAC\数据库\27.80.1\27.80.1-CAN\Database\ARXML\Clea_Mode_Year23_ADM_HC_V27.80.0.arxml`

## 核心信号

| 类型 | 信号 |
|---|---|
| 主加速度 | `IIMULonAccPri` |
| 参考加速度 | `IActVehAccel`, `IACCBSCE_ACCAccl1` |
| 车速 | `IVehSpdAvgDrvn_1` |
| 激活状态 | `IACCBSCE_ACCAct1`, `IACCATC_ACCAct` |
| 驱动请求/响应 | `IACCATC_AxlTrqRq`, `IActAxleTrq` |
| 制动请求/响应 | `IACCBSCE_ACCTrq1`, `ICSTBATS_TrqVl` |
| 状态上下文 | `IACCBrkngAct`, `IBrkSysAutBrkStat`, `IACCBSCE_AutBrkTp1` |

状态信号只用于激活/上下文展示，不作为制动扭矩证据替代。

## 检测流程

1. 扫描 BLF，结合 ARXML 建立物理通道到逻辑报文的路由。
2. 只解目标信号，不生成 MDF。
3. 将信号重采样到 `0.05s`。
4. 使用 `8s` 滑窗、`0.5s` 步长扫描。
5. 只分析 ACC 激活且车速有效的窗口。
6. 对 `IIMULonAccPri` 做线性去趋势，计算抖动幅值和过零次数。
7. 根据驱动扭矩或制动请求/响应扭矩判定问题类型。
8. 输出 JSON 汇总和候选窗口 PNG 图。

## 基础过滤

窗口需要满足：

- `IACCBSCE_ACCAct1 == 1` 或 `IACCATC_ACCAct == 1`
- 窗口内激活有效比例 `>= 85%`
- `IVehSpdAvgDrvn_1 > 5 km/h`
- 主判断加速度为 `IIMULonAccPri`

## 驱动扭矩波动型规则

命中条件：

- `IIMULonAccPri` 去趋势后 `P95-P5 >= 0.58 m/s²`
- 加速度过零次数 `zc >= 6`
- `IACCATC_AxlTrqRq P95-P5 >= 280 Nm`
- `IActAxleTrq P95-P5 >= 250 Nm`
- 制动请求/响应基本不参与：
  - `IACCBSCE_ACCTrq1 P95-P5 < 200 Nm`
  - `ICSTBATS_TrqVl P95-P5 < 200 Nm`
  - 制动扭矩活跃比例 `<= 10%`

输出类型：

- `drive torque oscillation`

## 制动请求/响应波动型规则

命中条件：

- `IIMULonAccPri` 去趋势后 `P95-P5 >= 1.45 m/s²`
- 加速度过零次数 `zc >= 5`
- 制动请求/响应有明显证据，满足至少一种：
  - `IACCBSCE_ACCTrq1 P95-P5 >= 500 Nm`
  - `ICSTBATS_TrqVl P95-P5 >= 500 Nm`
  - `IACCBSCE_ACCTrq1` 出现明显脉冲
  - `ICSTBATS_TrqVl` 出现明显脉冲
- 重复性要求：
  - `IACCBSCE_ACCTrq1` 或 `ICSTBATS_TrqVl` 至少出现 1 次明显脉冲，或
  - 加速度峰谷反转次数 `accTurns >= 4`

输出类型：

- `brake request/response oscillation`

## 输出图示

每个候选窗口生成一张 PNG，四个面板分别为：

1. 加速度/控制减速度：`IIMULonAccPri`, `IActVehAccel`, `IACCBSCE_ACCAccl1`
2. 驱动/轴端扭矩：`IACCATC_AxlTrqRq`, `IActAxleTrq`
3. 制动请求/响应扭矩：`IACCBSCE_ACCTrq1`, `ICSTBATS_TrqVl`
4. 状态上下文：`IACCBSCE_ACCAct1`, `IACCATC_ACCAct`, `IACCBrkngAct`, `IBrkSysAutBrkStat`, `IACCBSCE_AutBrkTp1`

## 运行方式

在 Agent 工程根目录运行：

```powershell
python .\deliverables\control_shake_detection\control_shake_detector.py `
  --arxml "D:\PATAC\数据库\27.80.1\27.80.1-CAN\Database\ARXML\Clea_Mode_Year23_ADM_HC_V27.80.0.arxml" `
  --blf-dir "D:\下载\控制频繁抖动" `
  --out-dir "D:\Data_Mining\Agent\runtime_outputs\control_shake" `
  --progress
```

也可以只跑单个 BLF：

```powershell
python .\deliverables\control_shake_detection\control_shake_detector.py `
  --arxml "D:\PATAC\数据库\27.80.1\27.80.1-CAN\Database\ARXML\Clea_Mode_Year23_ADM_HC_V27.80.0.arxml" `
  --blf "D:\下载\控制频繁抖动\2026-08-11-16-55-00_can.blf" `
  --out-dir "D:\Data_Mining\Agent\runtime_outputs\control_shake_single" `
  --progress
```

## 输出文件

- `control_shake_summary.json`：文件级和窗口级检测结果
- `index_control_shake.png`：候选窗口总览
- `*.png`：每个候选窗口的信号图

## 依赖

依赖当前 Agent 工程已有组件：

- `FileConverter/blf_to_mdf.py`
- `asammdf`
- `python-can`
- `canmatrix`
- `numpy`
- `Pillow`

如果只需要 JSON 结果，可以加 `--no-plots`，此时不依赖 Pillow 画图能力。
