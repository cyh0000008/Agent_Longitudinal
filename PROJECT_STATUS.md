# 布鲁项目状态

本文档用于让后续 AI 或开发者快速理解当前工程，不需要重新从头猜架构。

## 项目定位

这是一个飞书群机器人，名称统一为“布鲁”。目标是在群里被用户 @ 后，理解用户自然语言需求，并调用本地能力完成行车数据分析、故障表查询、故障诊断等任务。

核心原则：

- 所有文本消息优先交给 DeepSeek 理解意图。
- 本地规则和函数只是工具，不是主决策者。
- 非文本附件不能触发 DeepSeek 闲聊，但必须进入本地文件处理流程。
- 多用户并发按 `open_id + chat_id` 隔离。
- 重启后只处理重启后的新消息，避免历史消息触发。
- 回复要尽量一次说清楚，避免连续多条互相打架。

## 当前能力状态

### 性能问题分析

状态：可用。

输入：

- `.h5`
- `.hdf5`
- `.blf`，但 BLF 需要结合 `.arxml` 数据库转换

数据库：

- 位于 `database/`
- 用户不指定时默认使用最新数据库
- 数据库版本排序按数字大小判断，同版本优先级：`HC > MC > LC`
- 用户可以上传新的 `.arxml` 文件加入数据库

问题范围：

- `stability_control`：车身稳定与附着控制
- `acc_control`：ACC 跟车与纵向控制
- `apa_control`：APA 泊车控制
- 不指定时默认全跑

具体指标来自 `performance_analysis.py` 的 `METRIC_DESCRIPTIONS` 和 `PROBLEM_GROUP_DEFINITIONS`。当前包括：

- ABS 激活
- TCS 牵引力控制介入
- VSE 车身稳定控制介入
- ACC 加速度抖动
- ACC 接管释放后加速度抖动
- ACC 接管期间制动请求抖动
- ACC 起步阶段抖动
- ACC 急减速/重刹
- 接管释放后 ACC 急减速
- ACC 制动快速切换导致减速度跳变
- ACC 低速溜车
- ACC 控制权丢失
- ACC 未达到设定车速
- APA 泊车加速度抖动
- APA 换挡顿挫
- APA 泊车溜车
- APA 泊车完成状态异常
- APA 控制权丢失

结果：

- 飞书卡片展示总结、问题类型、问题时间点、图示数量
- 最多发送 3 张相关总线信号图示
- 当前已修复“只发图示标题但图片失败无提示”的问题：现在图片发送失败会明确回复失败原因
- 分析完成后会清理本次上传文件和生成产物

### 故障降级诊断

状态：可用，但当前仅支持 J6B 项目。

项目限制：

- J6B：支持
- 8650：开发中
- BMC：开发中

故障表：

- 文档来源：本机获准配置的 FDA3 / FDA7 故障表 Excel
- FDA7 对应应用层故障
- FDA3 对应底软故障

支持输入：

- 上传 `slog` 日志文件
- 上传 DID 日志文件
- 粘贴 `fault_event` 日志行
- 粘贴 `62 FD A3 ...` 或 `62 FD A7 ...` 整段字节
- 直接查询故障码，例如 `FDA3[113]`、`FDA7[8]`、`byte 0 bit 4`

重要解析规则：

- DID 中例如 `<Rx ... 62 FD A3 ...>`，`62 FD A3` 是响应头和 DID，后面的数据区才是故障表内容。
- `fault_event` 主要按 FDA3 故障表解释。
- 用户问“APA/环视/ACC 相关需要关注哪些故障码”时，应走故障表查询，不应要求上传日志。

### 执行器状态判断 / 执行器握手诊断

状态：开发中。

用户选择或询问该能力时，应明确说明开发中，并引导到当前可用能力：

- 性能问题分析
- J6B 故障降级诊断

## 关键文件

### `main.py`

飞书事件入口和会话状态机。

主要职责：

- 接收飞书消息事件
- 区分文本、文件、media
- 维护 `sessions`
- 调用 DeepSeek 意图编排
- 调用性能分析、故障诊断、本地回复卡片
- 发送文本、卡片、图片
- 清理上传文件和分析产物

重要函数：

- `handle_message`
- `handle_message_event`
- `handle_session_text`
- `handle_file_message`
- `handle_agent_decision`
- `process_uploaded_file`
- `send_message`
- `send_image`
- `send_analysis_image`

注意：`main.py` 中历史旧流程和重复函数已清理。后续修改前仍建议用 `rg` 确认活跃调用链，避免只改到未触发的辅助函数。

### `llm_service.py`

DeepSeek 调用和智能体意图编排。

主要职责：

- 调用 DeepSeek Chat Completion
- 维护群聊普通对话历史
- 判断用户消息是闲聊、分析请求、故障表查询、性能能力咨询、取消、状态查询等
- 输出结构化 `AgentDecision`

当前总控工具：

- `fault_table_search`
- `fault_diagnosis`
- `performance_capability_search`
- `performance_analysis_setup`
- `actuator_handshake`

关键函数：

- `decide_agent_action`
- `merge_agent_decision`
- `heuristic_agent_decision`
- `analyze_session_message`
- `is_performance_capability_question`
- `infer_problem_groups`

设计重点：

- DeepSeek 是主脑。
- 本地 heuristic 只兜底模型漏判的高置信场景。
- 用户问能力细节时，不要直接推进到等待上传。
- 用户已经上传文件或明确说“开始分析”时，才进入上传/分析流程。

### `performance_analysis.py`

性能问题分析执行和结果格式化。

主要职责：

- 加载 H5 数据
- 调用 `analysis_engine/metrics/code` 中的指标脚本
- 生成问题事件和图示
- 格式化性能分析结果卡片
- 回答性能指标能力咨询
- 清理本次上传和分析产物

关键函数：

- `run_performance_analysis`
- `answer_performance_capability_question`
- `format_analysis_markdown_clean`
- `build_issue_plots`
- `cleanup_analysis_artifacts`
- `metric_title`
- `metric_explanation`
- `format_problem_group_scope`

新增或修改性能指标时，优先改：

- `METRIC_DESCRIPTIONS`
- `PROBLEM_GROUP_DEFINITIONS`
- `analysis_engine/metrics/code/*.py`

### `fault_diagnosis.py`

故障表解析、故障码查询、故障诊断。

主要职责：

- 读取 FDA3 / FDA7 Excel
- 搜索功能相关故障条目
- 解析 fault_event / DID / byte bit
- 输出故障诊断结果

关键函数：

- `answer_fault_reference_question`
- `process_fault_diagnosis_input`
- `diagnose_fault_text`
- `diagnose_fault_file`
- `format_fault_markdown`

固定知识：

- FDA7 = 应用层故障
- FDA3 = 底软故障
- 故障诊断项目限制只针对故障诊断，不影响性能问题分析

## 会话状态

session key：

```text
open_id:chat_id
```

常见 stage：

- `collecting_requirements`：收集需求，DeepSeek 判断下一步
- `waiting_file`：性能分析等待上传数据文件
- `waiting_fault_input`：故障诊断等待日志、字节串或故障码
- `processing`：正在分析
- `completed`：本次请求完成

重要字段：

- `task_type`
- `database`
- `problem_groups`
- `project_type`
- `request_id`
- `waiting_file_since`
- `file_key`
- `file_name`
- `message_id`
- `message_type`
- `fault_reference_context`

## 文件上传处理

文本消息：

- 先走 DeepSeek 总控
- 根据决策调用工具或回复

非文本消息：

- `file` / `media` 才处理
- 不触发 DeepSeek 闲聊
- 进入 `handle_file_message`

文件类型：

- `.arxml`：保存到 `database/`
- `.blf` / `.h5` / `.hdf5`：性能分析数据
- 其他文件：根据当前任务提示格式不支持

关键注意：

- 用户上传文件不一定紧跟文本事件；如果用户说“我刚上传了”，会尝试查最近文件。
- 多个用户并发时必须按 `open_id + chat_id` 区分。
- 每次分析结束只删除本次 session 的上传文件和分析产物，不要删其他用户的数据。

## 图片发送逻辑

当前流程：

1. 性能分析生成 `issue.image_paths`
2. 结果卡片说明生成了多少图
3. `send_analysis_image` 尝试上传并发送图片
4. 图片发送成功后补发“图示 N：问题名”
5. 图片发送失败时给用户明确提示失败原因
6. 最后清理本次文件和图示产物

如果用户反馈“没有图”，优先检查：

- `logs/feishuagent.out.log` 中是否有 `发送分析图示失败`
- 飞书应用是否有图片上传和发送权限
- 图片路径是否存在且文件大小不为 0
- `build_issue_plots` 是否实际生成 PNG
- 是否在图片发送前误删了 `analysis_results`

## 启动与脚本

推荐启动：

```bat
scripts\restart_agent.bat
```

当前新增包装器：

```text
scripts/run_bot.py
```

作用：

- 把项目根目录加入 `sys.path`
- 把 stdout 写入 `logs/feishuagent.out.log`
- 把 stderr 写入 `logs/feishuagent.err.log`
- 再执行 `main.py`

状态检查：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/status.ps1
```

注意：

- 之前直接后台启动 `main.py` 偶尔会没有日志或进程退出。
- 如果脚本不稳定，先前台运行 `python -u main.py` 确认代码能否连接飞书。

## 环境变量

常见配置来自 `.env`：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `LLM_API_KEY`
- `LLM_API_BASE_URL`
- `LLM_MODEL`
- `LLM_TIMEOUT_SECONDS`

不要把 key 写进文档或提交到外部。

## 近期已知问题和处理记录

### 图片标题出现但图片没有

现象：

- 卡片显示“信号图示 1 张”
- 机器人发了“图示 1：ACC 急减速/重刹”
- 但飞书里没有图片

原因：

- 旧逻辑先发标题，再上传图片。
- 图片上传失败时只写日志，用户看不到失败原因。

当前修复：

- 新增 `send_analysis_image`
- 先尝试发送图片
- 成功后补发图示说明
- 失败时在群里明确提示失败原因

### 用户问能力细节却进入上传流程

现象：

- 用户问“更具体点”
- 机器人继续问项目、数据库、问题组

当前修复：

- `llm_service.py` 新增 `performance_capability_search`
- `performance_analysis.py` 新增 `answer_performance_capability_question`
- 这类问题直接列出具体可识别指标，不进入 `waiting_file`

### 故障表咨询被误判为上传日志

现象：

- 用户问“针对环视功能，需要关注哪些 FDA3”
- 机器人要求上传日志

当前修复：

- DeepSeek prompt 明确：这类问题走 `fault_table_search`
- 本地 fallback 强制兜底
- 输出区分 FDA7 应用层故障 / FDA3 底软故障

## 修改建议

后续改动时按这个顺序看：

1. 先看 `llm_service.py`，确认用户意图是否应该由 DeepSeek 分发到某个工具
2. 再看 `main.py` 的 `handle_agent_decision`，确认工具是否接入正确
3. 再看对应能力文件：
   - 性能分析：`performance_analysis.py`
   - 故障诊断：`fault_diagnosis.py`
4. 最后跑：

```powershell
python -m py_compile main.py llm_service.py performance_analysis.py fault_diagnosis.py scripts/run_bot.py
```

不要轻易做的事：

- 不要把 DeepSeek 绕开，直接用一堆固定关键词决定所有对话
- 不要把故障诊断的 J6B 限制套到性能分析
- 不要在分析结束时清理整个 `uploads/` 或 `analysis_results/`
- 不要让附件触发 DeepSeek 闲聊
- 不要在图片发送失败时只写日志不告诉用户
- 不要让任何能力只回复“我来查”“稍等”“我看看”。当前 `main.py` 里的 `ensure_substantive_reply` 是统一保险丝：本地工具无结果或模型只给过渡语时，必须调用 DeepSeek 基于用户问题和本地线索给出实质回复。

## 统一回复兜底规则

所有功能线都必须遵守：

1. DeepSeek 先理解用户意图。
2. 如果能调用本地能力，就调用本地能力。
3. 如果本地能力没有结果，不要沉默，也不要只说“稍等”。
4. 必须调用 DeepSeek 基于用户原话、本地检索线索和能力边界生成回复。
5. 回复至少包含以下之一：
   - 查到了什么
   - 没查到什么
   - 可能原因
   - 下一步需要用户补充什么
   - 当前能力限制和可替代路径

相关代码：

- `llm_service.py`：`generate_tool_fallback_reply`
- `main.py`：`ensure_substantive_reply`
- `main.py`：`is_empty_progress_reply`
## 执行器状态判断 / 执行器握手诊断（2026-06-17 更新）

状态：可用。

规则来源：
- `ADAS_interface_machine_readable_rules/`
- 主规则表：`diagnostic_rules.csv`
- 信号字典：`signal_dictionary.csv`
- 参数默认值：`parameters.csv`

核心模块：
- `actuator_handshake.py`

当前支持两类入口：
- 文本查询：例如“导致制动断握手的有哪些原因”“驱动系统 ACC 握手失败看哪些信号”
- 上传数据诊断：上传 `.h5` / `.hdf5` 总线数据后，按规则包检查断握手、功能降级、状态未按设计闭环等问题

同义词归一：
- 制动系统：`EBCM`、`ebooster`、`制动`、`刹车`
- 驱动系统：`PDF`、`ECP`、`ECM`、`驱动`、`动力`

主流程接入：
- `llm_service.py` 的 `actuator_handshake` 工具说明已改为可用
- `main.py` 的 `handle_agent_decision` 会根据 DeepSeek 决策进入执行器工具
- 如果用户只是问原因，调用 `answer_actuator_query`
- 如果用户明确说上传数据 / h5 / hdf5 / 总线数据，进入 `waiting_file` 并发送 `actuator_upload_card`
- 文件上传后由 `handle_file_message` 路由到 `process_uploaded_file`
- `process_uploaded_file` 调用 `run_actuator_handshake_analysis` 并用 `format_actuator_markdown` 输出卡片

当前限制：
- 数据诊断当前先支持 `.h5` / `.hdf5`
- `.blf` 已接入数据库转换链路，会先结合 `.arxml` 解码后再进入规则引擎
- 规则引擎当前优先支持状态响应、简单条件、`rising_edge`、`==`、`!=`、`in`、`>`、`<`
- `tracks`、复杂持续条件、复杂仲裁链路会在结果中列为“未自动判定/需补充”
