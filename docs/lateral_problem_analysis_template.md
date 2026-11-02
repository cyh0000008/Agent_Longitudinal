# 横向问题分析代码模板说明

本文档用于定义“横向问题分析”后续接入当前性能问题分析 skill 时的代码模板和输入输出接口。目标是让横向指标和现有纵向指标保持同一套运行协议，后续只需要把指标模块放入 `analysis_engine/metrics/code` 并在 `performance_analysis.py` 注册到问题组即可运行。

## 当前纵向指标的接入方式

当前性能问题分析的执行链路是：

1. 用户上传 `.blf`、`.h5`、`.hdf5`。
2. 如果是 `.blf`，`performance_analysis.py::convert_blf_to_h5` 会根据指标模块的 `REQUIRED_SIGNALS` 找到需要解码的报文，并转换成 H5。
3. `performance_analysis.py::run_h5_metrics` 加载 `analysis_engine/metrics/code` 下的指标模块。
4. 每个指标模块必须提供：
   - `REQUIRED_SIGNALS`
   - `__run_haoran_data_run__(folder_context)`
5. 指标返回异常明细文本，`performance_analysis.py::parse_events` 从文本里的 `[Event ...]` 或 `[Scene ...]` 解析时间窗。
6. `performance_analysis.py::build_issue_plots` 根据指标的 `REQUIRED_SIGNALS` 和事件时间窗生成信号图。

横向指标应完全复用这个协议。

## 指标模块文件结构

建议每个横向指标一个 `.py` 文件，放在：

```text
analysis_engine/metrics/code/
```

推荐命名：

```text
lateral_steering_oscillation.py
lateral_lane_offset_error.py
lateral_yaw_rate_overshoot.py
lateral_eps_response_delay.py
lateral_lane_centering_unstable.py
```

命名要求：

- 文件名就是 metric name。
- 使用小写下划线。
- 不要在文件名中写项目名、数据库版本或临时实验名。

## 必须暴露的模块级变量

### `METRIC_CATEGORY`

用于标识指标类别，目前纵向指标统一使用 `"MPI"`，横向也先保持一致：

```python
METRIC_CATEGORY = "MPI"
```

### `REQUIRED_SIGNALS`

声明指标运行所需的 H5 信号完整 key。

格式：

```python
REQUIRED_SIGNALS = [
    "报文名.信号名",
    "报文名.信号名",
]
```

要求：

- 必须是 H5 中的完整 key。
- BLF 转 H5 会根据 `REQUIRED_SIGNALS` 的报文名决定解码哪些 CAN 报文。
- 信号缺失时，当前指标会被跳过，不会进入 `__run_haoran_data_run__`。

推荐同时定义语义化映射：

```python
SIGNAL_KEYS = {
    "steering_angle": REQUIRED_SIGNALS[0],
    "yaw_rate": REQUIRED_SIGNALS[1],
    "vehicle_speed": REQUIRED_SIGNALS[2],
}
```

## `folder_context` 输入接口

当前框架传给指标模块的输入是一个 `dict[str, Any]`：

```python
folder_context = {
    "folder_path": str(h5_file.parent),
    "h5_files": [str(h5_file)],
    "primary_h5_file": str(h5_file),
    "session": session,
    "apa_total_count": 0,
}
```

横向指标只需要重点使用：

| 字段 | 类型 | 说明 |
|---|---|---|
| `session` | `signal_lib.FolderSession` | 当前 H5 的信号读取入口，推荐优先使用 |
| `primary_h5_file` | `str` | 当前主 H5 文件路径，用于日志展示 |
| `folder_path` | `str` | H5 所在目录 |
| `h5_files` | `list[str]` | 当前批次 H5 文件列表，目前通常只有 1 个 |

`FolderSession` 常用接口：

```python
session.common_time_range(REQUIRED_SIGNALS)
session.get_signal_slice(signal_name, start_timestamp, end_timestamp)
session.sample_signal_to_axis(signal_name, axis, method="nearest", start_timestamp=start, end_timestamp=end)
session.missing_signals(REQUIRED_SIGNALS)
```

## 指标输出接口

每个指标模块必须实现：

```python
def __run_haoran_data_run__(folder_context: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    ...
```

返回值固定为三元组：

```python
(
    bad_case: bool,
    result_lines: list[str],
    stats: dict[str, Any],
)
```

### `bad_case`

是否命中问题：

- `True`：本指标发现异常。
- `False`：本指标未发现异常。

### `result_lines`

用于给上层解析和展示的文本列表。

必须包含：

- 运行耗时。
- 指标总览。
- 每个异常事件的 `[Event ...]` 或 `[Scene ...]` 行。

事件行必须包含 `start` 和 `end` 字段，否则上层无法自动生成图示和时间窗。

推荐格式：

```text
[Event type=lateral_oscillation, start=12.300, end=13.900, duration=1.600, sample_count=38, amplitude=2.100, severity=4.200]
```

或：

```text
[Scene start=12.300, end=13.900, duration=1.600, sample_count=38, severity=4.200]
```

### `stats`

用于上层判断异常数量和严重度。

必须至少包含：

```python
stats = {
    "All Case Count": 0,
    "Bad Case": 0,
    "Severity Score": 0.0,
}
```

建议横向指标额外加入：

```python
stats.update({
    "Max Lateral Error": 0.0,
    "Max Yaw Rate": 0.0,
    "Max Steering Angle Jump": 0.0,
})
```

但注意：上层当前只强依赖 `Bad Case`，其他字段主要用于报告说明。

## 推荐的横向指标结果结构

指标内部可以返回更丰富的 dict，但最终需要转成三元组。

推荐内部结果：

```python
{
    "metric_name": "lateral_steering_oscillation",
    "all_case_count": len(events),
    "bad_case_count": len(events),
    "severity_score": max_severity,
    "start_timestamp": start_timestamp,
    "end_timestamp": end_timestamp,
    "parameters": {
        "min_event_duration_seconds": 0.5,
        "min_angle_amplitude_deg": 2.0,
    },
    "events": [
        {
            "start_timestamp": 12.3,
            "end_timestamp": 13.9,
            "duration": 1.6,
            "sample_count": 38,
            "amplitude": 2.1,
            "severity": 4.2,
            "detection_type": "oscillation",
        }
    ],
}
```

## 横向指标代码模板

```python
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from signal_lib import FolderSession, mask_to_segments


METRIC_CATEGORY = "MPI"

# TODO: 按实际数据库/H5 key 修改
STEERING_ANGLE_SIGNAL = "Example_Message.ISteeringWheelAngle"
YAW_RATE_SIGNAL = "Example_Message.IYawRate"
VEHICLE_SPEED_SIGNAL = "PPEI_Vehicle_Speed_and_Distance_209_M1.IVehSpdAvgDrvn_1"
LATERAL_ACCEL_SIGNAL = "Example_Message.ILateralAcceleration"
LATERAL_CONTROL_ACTIVE_SIGNAL = "Example_Message.ILateralControlActive"

DEFAULT_MIN_EVENT_DURATION_SECONDS = 0.5
DEFAULT_MIN_STEERING_ANGLE_AMPLITUDE_DEG = 2.0
DEFAULT_MIN_YAW_RATE_AMPLITUDE = 1.0
DEFAULT_MERGE_GAP_SECONDS = 0.2

REQUIRED_SIGNALS = [
    STEERING_ANGLE_SIGNAL,
    YAW_RATE_SIGNAL,
    VEHICLE_SPEED_SIGNAL,
    LATERAL_CONTROL_ACTIVE_SIGNAL,
]

SIGNAL_KEYS = {
    "steering_angle": REQUIRED_SIGNALS[0],
    "yaw_rate": REQUIRED_SIGNALS[1],
    "vehicle_speed": REQUIRED_SIGNALS[2],
    "lateral_control_active": REQUIRED_SIGNALS[3],
}


def __run_haoran_data_run__(folder_context: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    start_time = time.time()
    result_lines: list[str] = []
    stats: dict[str, Any] = {
        "All Case Count": 0,
        "Bad Case": 0,
        "Severity Score": 0.0,
    }

    session = folder_context.get("session")
    if session is None:
        result_lines.append("当前文件夹下未找到可处理的H5文件")
        result_lines.insert(0, f"【运算总耗时】: {time.time() - start_time:.4f} 秒")
        return False, result_lines, stats

    result = analyze_lateral_template(session)
    bad_case_count = int(result["bad_case_count"])
    all_case_count = int(result["all_case_count"])
    severity_score = float(result["severity_score"])

    stats["All Case Count"] = all_case_count
    stats["Bad Case"] = bad_case_count
    stats["Severity Score"] = severity_score

    result_lines.append(
        f"[Lateral Template] file={Path(session.primary_h5_file).name}, "
        f"all_case_count={all_case_count}, bad_case_count={bad_case_count}, "
        f"severity_score={severity_score:.3f}"
    )
    for event in result["events"]:
        result_lines.append(
            f"[Event type={event.get('detection_type', 'lateral_issue')}, "
            f"start={event['start_timestamp']:.3f}, end={event['end_timestamp']:.3f}, "
            f"duration={event['duration']:.3f}, sample_count={event['sample_count']}, "
            f"amplitude={event['amplitude']:.3f}, severity={event['severity']:.3f}]"
        )

    result_lines.insert(0, f"【运算总耗时】: {time.time() - start_time:.4f} 秒")
    return bad_case_count > 0, result_lines, stats


def analyze_lateral_template(
    session_or_h5_path: FolderSession | str | Path,
    min_event_duration_seconds: float = DEFAULT_MIN_EVENT_DURATION_SECONDS,
    min_steering_angle_amplitude_deg: float = DEFAULT_MIN_STEERING_ANGLE_AMPLITUDE_DEG,
    merge_gap_seconds: float = DEFAULT_MERGE_GAP_SECONDS,
) -> dict[str, object]:
    session = session_or_h5_path if isinstance(session_or_h5_path, FolderSession) else FolderSession(session_or_h5_path)
    start_timestamp, end_timestamp = session.common_time_range(REQUIRED_SIGNALS)

    axis, steering_angle_raw = session.get_signal_slice(
        SIGNAL_KEYS["steering_angle"],
        start_timestamp,
        end_timestamp,
    )
    if len(axis) < 3:
        return _empty_result(start_timestamp, end_timestamp)

    vehicle_speed = session.sample_signal_to_axis(
        SIGNAL_KEYS["vehicle_speed"],
        axis,
        method="nearest",
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )
    lateral_active = session.sample_signal_to_axis(
        SIGNAL_KEYS["lateral_control_active"],
        axis,
        method="nearest",
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )

    steering_angle = [_to_float(value) for value in steering_angle_raw]
    valid_mask = [
        _as_bool(active) and _to_float(speed) > 0.5
        for active, speed in zip(lateral_active, vehicle_speed)
    ]

    candidate_mask = _detect_lateral_issue_candidates(
        axis,
        steering_angle,
        valid_mask,
        min_steering_angle_amplitude_deg,
    )
    merged_mask = _merge_short_gaps(axis, candidate_mask, merge_gap_seconds)

    events = []
    severity_score = 0.0
    for segment in mask_to_segments(axis, merged_mask):
        if segment.duration < min_event_duration_seconds:
            continue
        window_values = [
            steering_angle[index]
            for index, timestamp in enumerate(axis)
            if segment.start_timestamp <= timestamp <= segment.end_timestamp
        ]
        amplitude = max(window_values) - min(window_values) if window_values else 0.0
        severity = amplitude * segment.duration
        severity_score = max(severity_score, severity)
        events.append(
            {
                "start_timestamp": segment.start_timestamp,
                "end_timestamp": segment.end_timestamp,
                "duration": segment.duration,
                "sample_count": segment.sample_count,
                "amplitude": amplitude,
                "severity": severity,
                "detection_type": "lateral_template",
            }
        )

    return {
        "metric_name": "lateral_template",
        "all_case_count": len(events),
        "bad_case_count": len(events),
        "severity_score": round(severity_score, 3),
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "parameters": {
            "min_event_duration_seconds": min_event_duration_seconds,
            "min_steering_angle_amplitude_deg": min_steering_angle_amplitude_deg,
            "merge_gap_seconds": merge_gap_seconds,
        },
        "events": events,
    }


def _detect_lateral_issue_candidates(
    timestamps: list[float],
    steering_angle: list[float],
    valid_mask: list[bool],
    min_amplitude: float,
) -> list[bool]:
    candidate_mask = [False] * len(timestamps)
    for index in range(1, len(timestamps)):
        if not valid_mask[index] or not valid_mask[index - 1]:
            continue
        if abs(steering_angle[index] - steering_angle[index - 1]) >= min_amplitude:
            candidate_mask[index] = True
    return candidate_mask


def _merge_short_gaps(timestamps: list[float], mask: list[bool], max_gap_seconds: float) -> list[bool]:
    merged = mask[:]
    last_true_index = None
    for index, active in enumerate(mask):
        if not active:
            continue
        if last_true_index is not None and timestamps[index] - timestamps[last_true_index] <= max_gap_seconds:
            for fill_index in range(last_true_index, index + 1):
                merged[fill_index] = True
        last_true_index = index
    return merged


def _empty_result(start_timestamp: float, end_timestamp: float) -> dict[str, object]:
    return {
        "metric_name": "lateral_template",
        "all_case_count": 0,
        "bad_case_count": 0,
        "severity_score": 0.0,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "events": [],
        "parameters": {},
    }


def _as_bool(value: object) -> bool:
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "active", "on", "enable", "enabled"}
    if hasattr(value, "item"):
        value = value.item()
    return bool(value)


def _to_float(value: object) -> float:
    try:
        if hasattr(value, "item"):
            value = value.item()
        return float(value)
    except Exception:
        return 0.0
```

## 横向问题建议拆分

建议不要把所有横向逻辑写成一个大指标，而是拆成多个可独立解释的指标：

| 指标文件 | 用户可理解的问题 | 典型输入信号 |
|---|---|---|
| `lateral_steering_oscillation.py` | 方向盘/转向角抖动 | 转向角、横摆角速度、横向控制激活、车速 |
| `lateral_yaw_rate_overshoot.py` | 横摆响应过冲 | 横摆角速度、目标曲率/目标横摆、车速 |
| `lateral_lane_offset_error.py` | 车道居中偏差大 | 横向偏差、车道线质量、横向控制激活 |
| `lateral_eps_response_delay.py` | EPS 响应慢或未响应 | 横向控制请求、EPS 实际角度/扭矩响应 |
| `lateral_lka_unexpected_exit.py` | 横向控制异常退出/降级 | 横向控制状态、降级原因、驾驶员接管信号 |

后续如果要做“横向行车问题原因分析”，再把这些指标和 EPS 执行器响应规则组合起来，类似当前纵向行车问题原因分析的三层证据链：

```text
现象层：横向偏差、横摆、转向角抖动
算法请求侧：目标曲率、目标角度、目标扭矩、LKA/NOA/APA 横向请求
执行器响应侧：EPS 实际角度/扭矩响应、状态、故障/降级位
```

## 接入当前性能问题分析 skill 的步骤

新增横向指标后，需要改动以下位置：

### 1. 新增指标模块

把指标文件放入：

```text
analysis_engine/metrics/code/lateral_xxx.py
```

保证文件内有：

```python
REQUIRED_SIGNALS = [...]
def __run_haoran_data_run__(folder_context): ...
```

### 2. 在 `performance_analysis.py` 注册指标描述

在 `METRIC_DESCRIPTIONS` 增加：

```python
"lateral_steering_oscillation": (
    "横向转向抖动",
    "横向控制激活时，转向角或横摆响应出现明显振荡或跳变。",
),
```

### 3. 增加问题组

在 `PROBLEM_GROUP_DEFINITIONS` 增加：

```python
"lateral_control": {
    "label": "横向控制",
    "description": "横向居中、转向响应、横摆稳定和横向控制退出相关问题。",
    "metrics": {
        "lateral_steering_oscillation",
        "lateral_yaw_rate_overshoot",
        "lateral_lane_offset_error",
        "lateral_eps_response_delay",
    },
},
```

### 4. 增加自然语言识别

在 `infer_problem_groups_from_text` 中增加横向关键词：

```python
if any(token in value for token in ["横向", "居中", "压线", "画龙", "转向", "方向盘", "lka", "noa", "横摆"]):
    groups.append("lateral_control")
```

### 5. 可选：补充行车问题原因分析

如果后续横向问题也要进入行车问题原因分析，需要补充：

- 横向现象层信号候选。
- 横向算法请求侧信号候选。
- EPS 执行器响应侧信号候选。
- 横向问题范围的文案和报告展示。

## 输出给用户时的注意点

横向指标报告不要直接甩内部指标名。推荐表达：

```text
发现 2 类横向问题：
- 横向转向抖动：12.300s - 13.900s，转向角快速来回变化，最大幅值 2.1deg。
- 横摆响应过冲：15.200s - 16.100s，横摆角速度超过目标响应范围。
```

不要输出：

```text
lateral_steering_oscillation bad case = 1
```

如果信号缺失，建议表达：

```text
当前数据缺少 EPS 实际转角响应信号，因此只能确认横向现象，暂不能判断是算法请求侧还是 EPS 响应侧。
```

## 最小验收标准

一个横向指标接入前，至少满足：

1. 模块能被 `performance_analysis.py::load_metric_modules` 正常导入。
2. `REQUIRED_SIGNALS` 中的信号在测试 H5 中能匹配。
3. `__run_haoran_data_run__` 返回三元组。
4. 异常事件行包含 `start` 和 `end`。
5. `stats["Bad Case"]` 与 `bad_case` 一致。
6. 命中问题时能生成图示。
7. 用户看到的是中文问题描述，不是源码变量名。
