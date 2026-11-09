# AGENT.md

本文档给后续接手本工程的 AI / 开发者使用。目标是快速理解当前飞书布鲁的系统结构、代码入口、能力边界和开发注意事项，避免重新摸索。

## 项目定位

本项目是一个本地运行的飞书群机器人，面向 ADAS / 行车数据分析场景。

核心链路：

1. 群成员在飞书群里 @ 机器人并描述需求，或上传数据文件。
2. 文本消息优先交给 DeepSeek 理解意图。
3. DeepSeek 作为云端总控，判断是否调用本地能力。
4. 本地 Agent 根据任务类型调用性能分析、执行器诊断、故障表查询等工具。
5. 分析结果以飞书消息、Markdown 风格卡片、表格、图片图示返回群里。

核心原则：

- DeepSeek 是主决策者，本地规则和函数是工具。
- 本地 heuristic 只做兜底和高置信修正，不应替代 DeepSeek 的意图理解。
- 一类动作和能力应优先抽象成 `agent_skills.py` 里的 skill，再由 DeepSeek 选择 skill、本地 Python 执行；不要把新能力散落成 `main.py` 里的大段业务 if/else。
- 当前所有对外能力还需要同步维护在 `config/agent_capabilities.xlsx`。`enabled=FALSE` 表示关闭该能力，DeepSeek 总控不会看到，本地执行分支也会拒绝调用；`visible=FALSE` 表示能力仍可被内部上下文使用，但不展示给 DeepSeek 能力列表。
- 对外能力说明必须由 `agent_skills.build_public_capability_text()` 从 `config/agent_capabilities.xlsx` 动态生成，禁止在 `main.py` / `llm_service.py` 里手写另一份能力清单；尤其不要出现已关闭能力或历史遗留的性能平台指标描述。
- `requirement_intake` 是需求识别与澄清 skill：用户需求不明确、可能落到多个能力，或正在纠正上一轮理解时，先进入该 skill；一旦用户给出明确新需求，哪怕换了别的方向，也必须立刻切到对应业务 skill，不要继续卡在旧澄清里。
- `result_transformer` 是结果展示生成 skill：它不是入口业务 skill。正确链路是先由本地业务 skill 产出可信结果，再由 DeepSeek 按 skill 定义规划展示产物，本地 Python 负责生成图片、动图、表格、Markdown/HTML 等实际产物。不要在代码里另加一套“用户是否要画图/生成文件”的关键词 if/else 规则。
- 当用户说“把上面的/刚才的/这个结果画成流程图、表格、图示”，或在上一轮刚回复后短句说“图片的形式生成 / 生成图片 / 可视化一下”等承接上一轮结果的需求时，必须优先使用上一轮结果进入 `result_transformer`，不要重新需求识别，也不要切到数据诊断流程。
- 上下文理解是总控层通用能力，不属于某一个业务 skill。每条文本消息都应结合 `recent_turns`、`last_skill_result`、当前 `stage/task_type` 和用户最新反馈一起判断；业务 skill 只负责检索、诊断或生成可信本地结果。
- 用户问“你是谁 / 怎么用你 / 你有哪些能力 / 你现在有哪些能力 / 你能做什么”时，必须走 `smalltalk_chat` 的能力说明，不继承上一轮业务 skill，也不能被本地 fallback 覆盖成执行器查询、性能查询或诊断分析。
- `session["last_skill_result"]` 只是短期展示上下文，不是长期记忆。它必须绑定 `open_id + chat_id`，默认只保留 30 分钟；程序重启后内存缓存自然消失，不跨重启恢复。用户发出非承接上一轮的明确新需求时要清掉旧结果；同一用户后续消息超过 `LAST_SKILL_RESULT_MAX_FOLLOWUP_MESSAGES` 后也要自动清理。不要把上传文件、生成图片路径或大体积原始数据放进去。
- 用户只说“分析这段数据/这个问题/有没有颠簸或异常”但没有明确指定性能或执行器时，不要擅自归到某一个能力；通过 `requirement_intake` 确认“性能分析 / 执行器分析 / 两个都分析”。如果用户选择两个都分析，同一个上传文件要同时跑性能问题分析和执行器分析，并汇总结果。
- 文件 / 图片 / media 等非文本消息不能触发 DeepSeek 闲聊，但必须进入本地文件处理流程。
- 行车问题原因分析、批量行车问题原因分析、执行器分析、性能分析、问题反馈截图和结果展示图示只能生成临时中转产物；禁止在用户/服务器 h5 数据所在目录写入图片、转换 H5、报告或缓存，也不要长期收口到工程目录。飞书消息发送或多维表格回写完成后，必须清理本地中转文件和空目录。
- 当前默认使用查询链路。用户只是问“查一下 / 了解一下 / 流程 / 条件 / 需要看哪些信号 / 能做什么”等，应优先走查询或澄清；只有明确要求基于上传数据分析某个具体问题原因，例如“布鲁，分析数据中出现 xxx 问题的原因”，才进入上传数据诊断分析。
- 多用户并发必须按 `open_id + chat_id` 隔离。
- 重启后只处理重启后的新消息，避免历史消息触发旧任务。
- 回复尽量一次说清楚，避免连续多条互相干扰。

## 代码修改准入流程

后续任何功能修改都必须先按本节检查，再动代码。目标是保持“DeepSeek 负责理解和决策，skill 负责能力执行，普通代码只负责安全边界和流程胶水”。

### 1. 先归类，不先写逻辑

修改前先判断需求属于哪类能力：

- 通用沟通类：寒暄、能力介绍、咨询、澄清、用户纠正。
- 查询类：查故障表、查性能指标、查执行器原始文档、查规则/信号/条件/流程。
- 诊断分析类：上传文件后跑性能分析、执行器分析、故障诊断或行车问题原因分析。
- 结果展示生成类：把已有结果转成表格、流程图、时序图、图片、GIF、报告文件等。
- 反馈 / 运维类：问题反馈、日志、启动、状态检查、群公告等支撑能力。

如果需求能落到某一类能力，先检查 `agent_skills.py` 是否已有合适 skill。没有就新增或扩展 skill，不要先在 `main.py` / `llm_service.py` 里堆关键词判断。

### 2. 修改顺序

标准顺序：

1. 改 `agent_skills.py`：定义或调整 skill 的语义触发、输入、输出、边界和用户可见能力说明。
2. 改 `config/agent_capabilities.xlsx`：新增能力必须补一行；需要临时关闭能力时优先改这张表，而不是删代码。
3. 改 `llm_service.py`：让 DeepSeek 基于 skill 列表做总控选择、澄清、切换和组合调用。
4. 改具体能力文件：例如 `performance_analysis.py`、`actuator_handshake.py`、`fault_diagnosis.py`、`dtc_query.py`、`root_cause_analysis.py`、`result_transformer.py`。
5. 改 `main.py`：只补必要的执行分支、文件下载、session 状态、卡片发送和异常兜底。
6. 改展示：优先 CardKit 表格、状态栏、图示和同卡更新，避免裸 Markdown 表格和散乱多条消息。
7. 改 `AGENT.md`：如果能力边界、skill 名称、文件职责或运行方式发生变化，必须同步更新本文档。
8. 如果改动影响 Ubuntu 迁移、启动脚本、依赖、环境变量、打包内容或部署路径，必须重新生成并验证 `dist/Agent_ubuntu_one_click/` 和 `dist/Agent_ubuntu_one_click.tar.gz`；`deploy/ubuntu_run/` 只作为 Windows 侧打包源码，Ubuntu 用户只使用 `dist` 生成包，不能只改 Windows 当前运行工程。

### 3. 哪些逻辑可以写在普通代码里

普通代码允许做这些稳定边界：

- 消息去重、历史消息过滤、重启后清理。
- `open_id + chat_id` 多用户隔离。
- 文件类型、文件大小、下载失败、权限不足、依赖缺失等安全校验。
- 上传文件和生成产物的生命周期管理。
- CardKit 发送失败后的降级。
- 当前配置的模型 API 不可用时的最小兜底回复。

普通代码不应承担这些业务决策：

- 仅靠关键词决定用户到底要性能分析、执行器分析还是故障查询。
- 用硬编码规则阻止用户从上一轮 skill 切到新 skill。
- 在 `main.py` 里直接生成大量业务结论文案。
- 在 `result_transformer.py` 里重新判断技术结论。
- 对用户展示内部规则 ID、状态机 ID、CSV 名称或源码片段。

### 4. Skill 编写规则

- `when_to_use` 是语义说明，不是固定句式。用户意思到了就可以触发。
- 每个 skill 要写清楚：什么时候用、需要什么输入、能输出什么、不能做什么。
- 查询类 skill 负责“找资料和解释资料”，不要进入等待上传文件。
- 诊断分析类 skill 负责“基于输入数据跑诊断”，不要代替查询 skill 回答泛化知识。
- 结果展示生成类 skill 只能消费本地可信结果，不要凭空新增技术结论。
- 用户反馈“不是这个意思 / 不是这个能力 / 你用错了”时，必须重新进入需求理解并允许切换 skill。
- 如果一个需求需要多个能力，例如“综合判断是算法还是执行器”，应由 DeepSeek 编排多个 skill，而不是新增一堆互相嵌套的 if/else。

### 5. 回复与体验规则

- 新用户消息触发新处理时，应在该消息后面新建或更新本次卡片，不要回头更新旧卡。
- 同一次处理尽量用一张动态卡承载“分析需求中 / 查询中 / 问题分析中 / 回传结果中 / 完成”等状态。
- 最终结果要优先回答用户真正的问题，再给证据、表格和图示。
- 用户没有明确要求画图时，不要主动生成流程图；用户说“流程”通常先按文字流程解释。
- 用户明确说“把上面的画成图/表格/报告”时，优先使用 `last_skill_result` 进入 `result_transformer`，不要重新切到业务诊断。
- 输出要像工程同事在解释问题，避免“二次加工”“规则包命中”“状态机 XXX”这种内部话术。

### 6. 每次改完的最小验收

- 用 `rg` 确认实际调用链，避免只改到旧函数。
- 运行最小语法检查，至少覆盖本次改动文件。
- 检查是否破坏多用户隔离和历史消息过滤。
- 检查是否新增了绕过 `agent_skills.py` 的业务判断。
- 检查是否同步更新了 `config/agent_capabilities.xlsx` 的能力开关行。
- 检查用户可见回复是否暴露内部变量、规则 ID、源码片段或无意义工程术语。
- 检查是否影响 Ubuntu 迁移；若影响启动、依赖、环境变量、打包清单或部署路径，必须重新生成迁移脚本/包并验证。

## 当前核心能力

### 1. 性能问题分析

状态：性能能力查询和独立上传数据跑性能问题分析入口当前均已在 `config/agent_capabilities.xlsx` 关闭；行车问题原因分析内部调用不受影响。

说明：

- `performance_capability_search` 当前关闭，不对外提供性能指标规则、阈值、关注信号和预设表查询。
- `performance_analysis_setup` 当前关闭，用户单独要求“上传文件跑性能分析”会被拒绝或澄清。
- `root_cause_analysis` 不受影响，行车问题原因分析仍可在内部调用性能分析代码，用于和执行器证据一起判断原因。

输入：

- `.h5`
- `.hdf5`
- `.blf`，需要结合 `.arxml` 数据库解码

数据库：

- 默认读取 `database/`
- 用户不指定时使用最新数据库
- 版本排序按数字大小判断，同版本优先级：`HC > MC > LC`
- 用户可上传新的 `.arxml` 文件加入数据库

问题范围：

- `stability_control`：车身稳定与附着控制
- `acc_control`：ACC 跟车与纵向控制
- `lateral_control`：LCC 横向控制
- 当前问题分析只保留 ACC 相关或与 ACC 原因判断相关的部分；APA 泊车问题不再作为性能问题 / 行车问题原因分析预设输入
- 未指定时默认跑当前开启范围

问题类型预设：

- 唯一维护表：`config/performance_problem_presets.xlsx`
- 实车表现信号目录：`config/vehicle_response_signal_catalog.xlsx`
- 当前预设问题类型：顿挫、重制动、误制动、漏制动、溜车、稳速未达 Setspeed、不合理低速、跟车距离、横向-猛打
- 表内维护每类问题的实车表现关注信号、算法请求关注信号、执行器请求关注信号、算法请求诱因关注信号、关联问题筛选代码、行车问题原因分析输出要求和说明。
- 行车问题原因分析必须先按用户问题类型读取 `performance_problem_presets.xlsx`，再结合对应指标代码决定要检查哪些诱因信号；不要把跟车目标、Cut-in 目标、曲率限速或 SetSpeed 无脑套到所有问题上，只有预设表或指标代码要求时才作为必查项，其他场景最多作为二级排查。
- `vehicle_response_signal_catalog.xlsx` 维护“实车表现信号语义 -> 具体总线信号名”的映射。具体信号名为空表示尚未核对到可用信号，后续代码读取时必须把它当作缺失信号处理，不要在代码里硬猜。
- `root_cause_analysis.py` 读取 `vehicle_response_signal_catalog.xlsx` 时，只允许 `当前状态=已匹配` 且 `具体信号名` 非空的条目进入信号匹配；`待补充/待核对/候选匹配/部分匹配` 必须作为缺失证据，不可回退到旧硬编码候选。
- 跟车目标和 Cut-in 目标当前复用 FLT1 信号：`FLT1_CUTIN_Flag=0` 时 `FLT1_*` 表示跟车目标，`FLT1_CUTIN_Flag=1` 时 `FLT1_*` 表示 Cut-in 目标；两者同一时刻只会存在一个。当前数据暂未接入 `FLT1_CUTIN_Flag`，行车问题原因分析先按默认值 `0` 处理，即默认 FLT1 是跟车目标；不要因为 Cut-in 目标没有单独信号而提示缺少判断依据。
- `IVehMvngStat` 是车辆运动/静止状态：`0=moving`，`1=stationary`。不要按旧的“1 表示 moving”理解。
- `Eth_Debug_CAN5/MPC_DebugMsg_Reserved_18.Debug_single_Reserved_51` 是坡度角，单位 `deg`。
- 性能分析执行前必须读取并校验 `performance_problem_presets.xlsx`；如果用户明确了问题类型，应按表中关联指标做针对性分析
- 开发人员要调整问题类型或筛选代码时，改 `performance_problem_presets.xlsx`；要调整实车表现信号名时，改 `vehicle_response_signal_catalog.xlsx`。不要在 Python 代码或文档里维护第二份映射。

主要代码：

- `performance_analysis.py`
- `analysis_engine/metrics/code/`
- `analysis_engine/signal_lib.py`
- `config/performance_problem_presets.xlsx`
- `config/vehicle_response_signal_catalog.xlsx`

输出：

- 总结
- 发现的问题
- 问题时间点
- 相关总线信号图示

### 2. 执行器分析 / 执行器握手诊断

状态：可用并仍在迭代。

支持对象：

- 制动执行器 / 制动接口：`EBCM`、`ebooster`、`brake`
- 驱动执行器 / 驱动接口：`PDF`、`ECP`、`ECM`
- 功能场景 / 请求来源：`ACC`、`AEB`、`APA` 等。注意：`AEB`、`CIB` 不是制动系统；制动文档中的 `CIB` 可按 `AEB` 场景理解，表示触发制动请求的一类功能场景。

支持形式：

- 查询规则，例如“导致制动断握手有哪些原因”
- 上传总线数据，检查断握手、功能降级、状态未按设计闭环等问题

输入：

- `.h5`
- `.hdf5`
- `.blf`

重要 BLF 注意事项：

- 某些 CAN FD BLF 的 `valid_bytes=0`，标准 `python-can` 会把 payload 读成空。
- `actuator_handshake.py` 里已有自定义 BLF reader：当 `valid_bytes=0` 时按 DLC 长度回退读取 payload。
- CANoe 中的 CAN3 / CAN4 在 `python-can` 里可能显示为 channel `2` / `3`，不要误判为通道不匹配。

主要代码：

- `actuator_handshake.py`
- `ADAS_interface_machine_readable_rules/`

内部整理来源：

- `ADAS_interface_machine_readable_rules/` 中的机器可读规则、信号索引、参数索引、状态流转整理，仅作为本地检索和诊断执行材料。
- 原始制动 / 驱动接口文档也放在该目录下。
- 用户能反向查询和追溯的知识库只有原始接口文档，不要把机器可读规则、CSV、规则 ID、状态机 ID 当作来源展示给用户。

执行器查询内部索引：

- `diagnostic_rules.csv`：规则、触发条件、正常应答、异常含义、证据信号
- `signal_dictionary.csv`：信号概念、报文、CAN ID、枚举取值、转换关系
- `state_machines.csv`：执行器相关状态机、状态进入条件、期望状态
- `parameters.csv`：超时、阈值、标定参数
- `PS ICD for ADAS_V3.5.6.docx` 和 `EBCM ICD for ADAS and APA.docx`：原始文档片段检索

执行器查询流程：DeepSeek 先判断用户问题属于 `actuator_reference_search`，本地检索上述内部索引和原始文档，再由 DeepSeek 基于检索结果组织回答。不要让 DeepSeek 脱离检索结果编造文档结论。

执行器查询 / 分析对外表达要求：

- 不要对用户说“状态机 `DRV_ACC_PROPULSION` 定义了……”或“规则包命中……”这类内部实现话术。
- 不要展示内部规则 ID、CSV 文件名、机器可读规则包、状态机 ID。
- 要翻译成用户能理解的接口行为，例如“驱动系统在 ACC 请求激活后，应反馈请求已接受或等待驾驶员输入；当前数据里该响应没有出现”。
- 如果需要说明来源，只能引用原始接口文档名或“原始接口文档要求”，不要引用内部整理文件。
- 用户提出执行器接口变更、新需求合理性、行泊切换、超时降级、刹停/拉 EPB 等方案评审时，仍走 `actuator_reference_search`，不要进入等待上传文件。
- 需求评审类回复必须优先回答：需求方向是否合理、现有文档能支撑哪些部分、当前文档缺口、实施关注点、风险点、建议补齐项。不要只罗列信号。
- 如果原始文档没有直接定义某个 flag、500ms 阈值、状态或安全动作，要明确写成“当前文档未直接定义/需要补系统需求”，不要把相近信号包装成确定结论。

结果展示要求：

- 重点结论必须说人话，避免直接抛规则 ID、状态机 ID 或内部整理字段。
- 问题标题应该描述问题本身，例如“EBCM 未响应自动制动激活”，不要用泛泛的“ACC 制动扭矩握手”。
- 如果有时序图，图示要和诊断结果尽量一起发送。
- 不要展示无意义的“原始文档依据”大段文本，除非用户明确要求。

### 3. 行车问题原因分析

状态：可用。

定位：

- 这是一个编排型诊断 skill，对同一段总线数据同时运行性能问题分析和执行器分析，并补充读取算法请求侧与执行器响应侧信号。
- 目标不是单独输出两份报告，而是回答“这个现象的证据链是什么：实车现象、算法请求、执行器响应分别是什么情况”。

触发场景：

- 用户上传或准备上传 `.h5`，并希望解释某个现象的原因。
- 用户问“是执行器还是算法侧”“帮我归因”“为什么导致这个问题”“两个都分析 / 综合判断”。
- 当前重点覆盖现象：顿挫、重制动、不合理低速、误制动、漏制动、未达到 set speed、溜车、跟车距离异常、横向-猛打。

执行方式：

1. 按用户问题现象推断性能问题组，必要时默认全跑。
2. 调用 `performance_analysis.py` 输出性能侧问题、时间窗和图示。
3. 调用 `actuator_handshake.py` 输出执行器接口风险、未响应信号、期望响应和图示。
4. `root_cause_analysis.py` 继续读取 H5 里的请求/响应和场景诱因信号，形成四层证据：
   - 现象层：性能指标识别到的实车表现，如加速度跳变、车速异常、溜车等。
   - 算法请求侧：ACC/AEB 发出的驱动或制动请求。
   - 算法请求诱因：跟车目标、Cut-in 目标、曲率限速、SetSpeed 等场景输入是否能解释算法为什么给出该请求。
   - 执行器响应侧：制动、驱动等执行器是否按接口文档响应。
5. 行车问题原因分析必须说明现象层、请求侧、算法请求诱因侧和响应侧各自匹配到了哪些信号，哪些信号缺失。
6. 行车问题原因分析不能只输出“匹配 X 个 / 缺失 Y 个”。必须按同一时间窗回答：
   - 现象是否成立：例如是否真的出现重制动/急减速。
   - 请求侧是否主动触发：例如 `ACCBSCE_ACCTrq1`、`ACCBSCE_ACCAccl1`、`FCACBSC_CtrlTrq1` 是否在问题窗口同步变化。
   - 执行器是否闭环：例如 `ACCBrkngAct`、`BrkSysAutBrkStat`、实际制动扭矩是否按请求响应。
   - 当前更像哪一侧：请求先突变偏算法/控制策略侧；请求合理但响应异常、拒绝或不闭环偏执行器/接口侧。
   - 还缺什么证据：缺信号必须明确说“当前不能解释为什么”，不要假装已经归因完成。
   - 如果当前更像请求侧，还要继续用诱因信号解释“为什么会这么请求”，例如前车 / Cut-in、曲率限速、SetSpeed 或接管变化。
7. `root_cause_analysis.py` 必须维护 `temporal_judgement`：从现象、请求、诱因、响应四类信号中抽取首个显著变化点，结论优先说明“谁先变化”和“最终原因更偏哪侧”。如果请求与响应只差采样级小时间窗，要写成“几乎同步/未见执行器明显先触发”，不要强行说绝对先后。
8. 所有问题类型都必须检查对应的关键请求侧和响应侧信号是否齐全。不能因为读到了若干非关键请求/响应信号，就认为证据链完整；只要该现象的关键量缺失，结论必须降级为“现象成立但归因证据不足，需要补充信号”。
9. 用户可见输出要像工程同事在解释问题：先用一句自然结论说明“我会先从哪侧查 / 现在证据还差哪块 / 暂时不该下根因”，再展开证据链；不要用生硬的字段堆砌代替结论。

主要代码：

- `root_cause_analysis.py`
- `deepseek_root_cause_analysis.py`：模型调查式原因分析薄层，本地按问题类型预设和问题描述生成信号查询计划、读取 H5 信号摘要，再交给当前配置的 `LLM_MODEL` 筛选候选窗口并组织证据链和结论。
- `main.py::process_uploaded_file` 中 `root_cause_analysis` 分支：用户直接上传单个 `.h5` 时，必须调用 `batch_root_cause_analysis.run_single_batch_analysis(...)`，确保与飞书多维表格批量行车问题分析使用同一套单条分析核心。
- `agent_skills.py` 中 `root_cause_analysis` skill 定义

注意：

- 不要把行车问题原因分析写成新的硬编码指标体系；它只消费现有性能分析、执行器分析和 H5 请求/响应信号证据。
- 模型调查式原因分析是 opt-in 模式：设置 `BATCH_ROOT_CAUSE_ANALYSIS_MODE=deepseek_investigation` 后，飞书多维表格批量单条、用户直接上传单个 `.h5`、内网 Excel 批量单条都必须走 `deepseek_root_cause_analysis.py`。该模式下本地负责按 `performance_problem_presets.xlsx` 和问题描述生成查询计划、建立 H5 信号索引、输出 compact 证据摘要和缺失信号信息；当前配置的 `LLM_MODEL` 负责候选窗口筛选、根因因素、结论概要和关键证据信号选择。本地不要重新写根因判断规则。
- `agent_skills.py` 中的 `root_cause_analysis` 是该能力的内部 skill 契约，对外名称是“行车问题原因分析”；改原因分析行为时要同步检查 skill 描述、AGENT.md 和 `root_cause_analysis.py`，不要只在某个 if/else 里临时补文案。
- 详细信号核对文档在 `docs/root_cause_signal_matching.md`。
- 当前已确认的行车问题原因分析口径：
  - 行车问题原因分析单文件入口和批量入口当前仅支持标准 `.h5` 数据文件；`.blf`、`.hdf5` 或其他格式必须先转换成布鲁可读取的 `.h5` 后再分析。
  - AEB 请求侧看 `FCACBSC_CtrlTrq1`，不要用 `FCACBSC_CtrlAcc1` 做 AEB 请求归因。
  - ACC 制动请求侧只保留 C-CAN `ADAS_Command_Ext1` 中的 `ACCBSCE_ACCAct1 / ACCBSCE_AutBrkTp1 / ACCBSCE_ACCAccl1 / ACCBSCE_ACCTrq1`。
  - ACC 驱动实际响应只看 `ACCATCS_RqStat / ACCATCS_LmtgStat / ActAxleTrq`。
  - 制动实际扭矩响应按 `ICSTBATS_TrqVl + IRegenBrkAxTrqAch` 估算；若 `IRegenBrkAxTrqAch` 缺失，按 0 处理，但报告中要说明缺失。不能用 `IRegenBrkAxTorqRqst` 请求值替代 `IRegenBrkAxTrqAch` 响应值。
  - 如果现象层、请求侧和响应侧都能读到，但跟车目标、Cut-in、曲率限速、SetSpeed 等诱因侧信号缺失，结论要写成“现象和请求/响应证据已看到，但解释算法为什么给出该请求的场景输入不足”，不能直接判断算法根因。
- 问题分析 / 行车问题原因分析当前不纳入 APA 泊车问题；APA 相关流程仍属于执行器查询 / 执行器分析的文档能力范围。
- 用户明确只查性能指标规则时，当前会被 `performance_capability_search` 能力开关拒绝；不要改走上传分析或行车问题原因分析，除非用户明确要基于数据判断原因。
- 用户明确只查执行器文档时仍走 `actuator_reference_search`。
- 用户明确只要单独性能分析或单独执行器分析时，不要强行进入行车问题原因分析。

### 4. 批量行车问题原因分析

状态：可用。

定位：

- 这是 `root_cause_analysis` 的批量入口，不重新实现诊断逻辑。
- 用户把待分析任务预填在飞书多维表格里，布鲁只处理 `Agent_问题状态` 为 `待分析` 的记录；空白状态不会被处理。程序逐行读取布鲁运行机器/服务器上的 `.h5` 绝对路径，按 `Agent_问题类型` 调用对应分析能力，再把状态、结论概要、完整结论和信号图回写到表格。
- 有且只有固定指令 `分析飞书多维表格中的问题` 可以直接触发该入口；其他“批量跑表格 / Base 回写 / 处理表格问题”等相近说法必须只提示用户是否要处理多维表格，并告诉用户正确固定指令，不直接执行批量分析。
- 固定指令前面带机器人可见 @ 名称时也必须算固定指令，例如 `@布鲁 分析飞书多维表格中的问题`、`@布鲁Mate 分析飞书多维表格中的问题`。该入口必须在通用需求识别和通用进度卡之前拦截，不能先发送“分析需求中 / 确认分析范围”卡。
- 这条固定入口的优先级高于普通 session 状态。即使用户之前卡在“确认分析范围 / 等待上传 / 已完成”等状态，只要再次发送固定指令，就应直接进入批量分析；如果发送的是相近句式，只提示固定说法，不进入“确认分析范围”。
- 固定取消指令 `取消当前问题分析` 只取消同一 `open_id + chat_id` 下正在运行的批量行车问题原因分析。收到后必须设置后台任务 cancel token，当前 DeepSeek/H5 步骤结束后停止处理；若当前行已被写成 `分析中` 但尚未完成，应恢复为 `待分析` 并写入“已取消”短说明。批量分析进度卡必须显示取消方式；收到取消后优先把同一张进度卡更新为 `取消中` / `已取消`，只有原卡不存在或更新失败时才发送兜底取消提示。

飞书多维表格字段：

- `Agent_问题描述`：用户对现象或需求的自然语言描述。
- `Agent_h5数据路径`：可填写也可留空。已填写时必须是布鲁运行机器/服务器上真实存在的 `.h5` 绝对路径，不能填写 `.blf`、`.hdf5`、填表人电脑的本地路径、相对路径或只填文件名；留空时，飞书批量入口会根据 `Agent_车号`、`Agent_车型`、`Agent_问题日期`、`Agent_问题时间` 调用 `scripts/find_j6b_h5_path.py` 在 `BATCH_ROOT_CAUSE_H5_SEARCH_BASE_DIR` 下自动查找 H5，找到后先回写到 `Agent_h5数据路径` 再分析。自动查找不再按 `HH:mm` 硬匹配文件名，而是先按车号/车型/日期和文件起始时间粗筛相邻 H5，再读取 H5 内 `timestamp` 覆盖范围确认问题时间是否落在该文件中；若未找到对应 H5，会继续调用 `scripts/find_j6b_blf_path.py` 在 `BATCH_ROOT_CAUSE_BLF_SEARCH_BASE_DIR` 下按同样元数据查找 BLF，找到后用 `scripts/blf_to_h5_collect.py` 解析成 `<blf文件名>_parsed.h5`，输出到 `BATCH_ROOT_CAUSE_H5_SEARCH_BASE_DIR` 下对应的车型/车号目录和日期行程目录，再继续原来的 H5 读取和后续分析；若 H5 和 BLF 都找不到，则 `Agent_问题状态` 保持 `待分析`，`Agent_分析结论` 写入 `未找到对应h5数据，请确认数据解析是否完成`。
- `Agent_问题类型`：必须来自当前覆盖的问题类型；`config/performance_problem_presets.xlsx` 中包含纵向问题和 `横向-猛打`，额外支持 `功能退出`，用于 ACC/LCC 功能退出原因分析。
- `Agent_问题状态`：默认 `待分析`；批量任务只处理 `待分析` 记录，空白状态不会被处理。开始处理时写为 `分析中`，成功后写为 `已分析`，失败后写为 `分析失败`。如果自动定位 H5 时未找到对应数据，状态仍保持 `待分析`，等待数据解析完成后继续跑。
- 模型调查链路中，若候选窗口/最终结论调用超时或返回不可解析 JSON，但 H5 信号证据已经成功读取，应回写降级结论、关键变化信号和图示，并将 `Agent_问题状态` 写为 `已分析`；只有没有任何有效 H5 信号证据、文件读取失败或分析流程无法继续时，才写 `分析失败`。
- `Agent_问题日期` / `Agent_问题时间`：用户填写的问题发生日期和时间；程序会结合 H5 文件名起始时间换算 H5 内相对秒数，并优先让模型在该时间附近挑选候选窗口。历史表未改名前兼容读取旧列名 `问题日期` / `问题时间`，但新表和模板必须使用 `Agent_` 前缀。
- `Agent_车号` / `Agent_车型`：当 `Agent_h5数据路径` 为空时用于自动定位 parsed H5；`Agent_车号` 应使用数据源要求的完整车号格式。已有 H5 路径时不依赖这两列。
- `Agent_结论概要`：批量分析完成后只回写一句明确、简要的原因点或缺失证据点，方便用户在表格列表里快速扫读。必须说明更像哪一侧导致：感知/场景输入、算法请求、执行器响应，或缺少哪类关键证据；用路试人员能理解的文字描述原因，必要时可附 1-2 个真实信号名用于追溯。不要写完整证据链、下一步建议、内部字段名或规则名。完整信号证据只放在 `Agent_分析结论`。
- `Agent_分析结论`：批量分析完成后回写完整文本结论；是否处理记录以 `Agent_问题状态` 为准，不再靠该字段是否为空判断。
- `Agent_分析信号图示`：附件字段，用于上传本次生成的信号图。

表格可以按业务需要增加其他列，但不能改名或删除上述 `Agent_` 关键字段；批量入口只依赖这些关键字段。

配置项：

- `BATCH_ROOT_CAUSE_BASE_TOKEN`
- `BATCH_ROOT_CAUSE_TABLE_ID`
- `BATCH_ROOT_CAUSE_ATTACHMENT_FIELD`，默认 `Agent_分析信号图示`
- 可选字段名覆盖：`BATCH_ROOT_CAUSE_FIELD_PROBLEM`、`BATCH_ROOT_CAUSE_FIELD_H5_PATH`、`BATCH_ROOT_CAUSE_FIELD_PROBLEM_TYPE`、`BATCH_ROOT_CAUSE_FIELD_PROBLEM_DATE`、`BATCH_ROOT_CAUSE_FIELD_PROBLEM_TIME`、`BATCH_ROOT_CAUSE_FIELD_CAR_ID`、`BATCH_ROOT_CAUSE_FIELD_CAR_MODEL`、`BATCH_ROOT_CAUSE_FIELD_STATUS`、`BATCH_ROOT_CAUSE_FIELD_SUMMARY`、`BATCH_ROOT_CAUSE_FIELD_CONCLUSION`
- 推荐配置字段 ID：`BATCH_ROOT_CAUSE_FIELD_PROBLEM_ID`、`BATCH_ROOT_CAUSE_FIELD_H5_PATH_ID`、`BATCH_ROOT_CAUSE_FIELD_PROBLEM_TYPE_ID`、`BATCH_ROOT_CAUSE_FIELD_PROBLEM_DATE_ID`、`BATCH_ROOT_CAUSE_FIELD_PROBLEM_TIME_ID`、`BATCH_ROOT_CAUSE_FIELD_CAR_ID_ID`、`BATCH_ROOT_CAUSE_FIELD_CAR_MODEL_ID`、`BATCH_ROOT_CAUSE_FIELD_STATUS_ID`、`BATCH_ROOT_CAUSE_FIELD_SUMMARY_ID`、`BATCH_ROOT_CAUSE_FIELD_CONCLUSION_ID`、`BATCH_ROOT_CAUSE_ATTACHMENT_FIELD_ID`
- 可选执行参数：`BATCH_ROOT_CAUSE_SCAN_MAX_ROWS`、`BATCH_ROOT_CAUSE_MAX_ROWS`、`BATCH_ROOT_CAUSE_DATABASE`、`BATCH_ROOT_CAUSE_LARK_CLI_AS`、`BATCH_ROOT_CAUSE_H5_SEARCH_BASE_DIR`、`BATCH_ROOT_CAUSE_BLF_SEARCH_BASE_DIR`

建表脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\init_batch_root_cause_base.ps1 -As bot
```

主要代码：

- `batch_root_cause_analysis.py`
- `deepseek_root_cause_analysis.py`
- `scripts/find_j6b_blf_path.py`，用于在 parsed H5 缺失时按车型、车号、日期和问题时间从原始数据根目录定位 BLF。
- `scripts/blf_to_h5_collect.py`，用于把定位到的 BLF 解析成批量分析可读取的 `_parsed.h5`。
- `scripts/init_batch_root_cause_base.ps1`
- `scripts/run_deepseek_root_cause_sample.py`，用于用样例 H5 验证 DeepSeek 调查式原因分析链路。
- `scripts/generate_root_cause_synthetic_report.py`，用于生成行车问题原因分析的基础触发和典型诱因场景样例 H5，并输出回归报告。
- `scripts/seed_batch_root_cause_samples.py`，用于写入样例测试记录；它优先读取 `root_cause_synthetic_summary.json`，不要再手工维护另一份行车问题样例清单。
- `scripts/generate_function_exit_synthetic_cases.py`，用于生成功能退出样例 H5。
- `main.py::handle_agent_decision` 中 `batch_root_cause_analysis` 分支
- `agent_skills.py` 中 `batch_root_cause_analysis` skill 定义
- `local_batch_root_cause.py`，用于内网 Excel 表格批量分析，复用同一个 `run_single_batch_analysis(...)` 单条分析核心，不依赖飞书。
- `local_batch_ui.py`，用于内网浏览器 UI 上传 Excel、查看进度、下载结果包。
- `deploy/intranet_batch/*`，用于内网镜像部署；它与 `deploy/ubuntu_run/*` 并列维护，不能互相替代。

注意：

- 该固定指令入口只处理表格批量任务，不负责接收用户直接上传的单个文件；单个文件由 `main.py::process_uploaded_file` 接收，但分析时必须复用同一个 `run_single_batch_analysis(...)` 单条核心。
- 飞书多维表格版本的 Base 读写和附件上传必须通过 `lark-cli base +record-list/+record-upsert/+record-upload-attachment`，附件字段不能作为普通文本写入。
- Windows 下 `lark-cli --json @file` 读取中文字段名可能变成 `????`，并导致不同字段相互覆盖；批量回写必须优先用 `fld...` 字段 ID，`.env` 中应维护上述 `BATCH_ROOT_CAUSE_FIELD_*_ID`。
- `lark-cli --json @file` 和 `lark-cli base +record-upload-attachment --file` 都必须先把 JSON / 附件复制到 `runtime_paths.get_runtime_subdir(...)` 返回的当前用户可写暂存目录，执行命令时把 `cwd` 切到该目录，并只传 `@payload.json` 或文件名；执行后删除中转文件。
- 不要把 CLI 暂存文件放到工程 `.tmp/`、上传目录、数据目录或共享 `/tmp/blu_agent`，避免 Ubuntu 多用户权限和绝对路径解析问题。
- `功能退出` 问题类型走 `function_exit_analysis`，在批量入口中同样只支持 `.h5`；`.blf`、`.hdf5` 或其他格式需要先转换成标准 H5。
- `横向-猛打` 问题类型在 DeepSeek 调查式模式下仍交给 `deepseek_root_cause_analysis.py`，本地通过 `performance_problem_presets.xlsx` 和 `lateral_steering_sudden.py` 提供信号指南、事件筛选和图示；未开启 DeepSeek 模式时才由本地横向指标直接输出兜底结论。结论概要必须先写车道几何触发、请求侧其他诱因、EPS/执行器响应异常或证据不足。
- 不要自动覆盖 `Agent_问题状态` 不是 `待分析` 的记录，用户要重跑时应先把该行状态改回 `待分析`。
- 批量处理过程中群里只展示进度和摘要，完整长结论和图示以表格为准。
- 批量进度卡必须显示当前问题处理进度，包括“共多少条问题、已处理多少条、当前第几条 / 总数、当前问题类型和数据文件”。
- 批量进度卡运行期间必须显示“可通过 `@布鲁Mate 取消当前问题分析` 取消当前进程”，取消后同一张卡要明确显示 `取消中` 或 `已取消`，避免用户看到多张状态不一致的卡片。
- 批量飞书入口必须后台执行，不能长时间阻塞飞书消息事件处理；否则 `取消当前问题分析` 这类运行中控制指令无法及时生效。
- 批量测试样例 H5 是长期测试夹具，统一生成到 `sample_data/batch_root_cause/`，不要放在启动会清理的 `analysis_results/`；`analysis_results/` 只作为运行产物目录。

### 4.1 多维表格 KPI 报告 AI 分析

- 饼图下方的版本关注摘要按关注类型分行编号，不列指标数值或 MPI/CPI 类型。场景仅使用报告“场景”列原名（支持合并单元格），摘要去掉开头的“行车”及分隔符，不重新命名或从指标名猜测场景。同一状态内按指标名称归纳性能类型，例如最大 jerk、起步舒适性合并为“舒适性（静止起步到稳速、跟车起步）”，括号内去重列出原报告场景；不跨状态合并，不推测原因。无法归类保留指标名（同样去掉行车前缀）；每类最多展示三个性能主题，其余提示查看下方完整指标明细，文字区域自动增高。摘要基于本地解析及既有显著变化分组生成，不额外调用模型。优先提示新增未准出、明显下降与仍未准出，再说明恢复准出；未达到变化门槛不声称明显升降，无基线不推断版本趋势，结论缺失提示待确认。
- 图示重点对比按固定顺序分组：新增未准出、恢复准出、未准出但指标提升、未准出且指标下降、可准出但指标下降。准出转换优先，每项仅出现一次；空组隐藏且不占用画布空间。其余达到既有门槛的变化单列“其他显著变化”，不静默丢弃；无基线单列当前未准出，不推断新增/恢复。筛选门槛不因分组改变。
- 图头仅显示报告表头中的车型、动力形式、两版数据日期和版本变化；缺失标为未提供，不用文件名推断。图中不显示文件/工作表名称、来源单元格或“不可比”；数值无法判断升降时只展示准出变化，不虚构趋势。
- 仪表盘主标题为“行车纵向KPI当前版本问题与提升 | 数据仪表盘”；车型、动力形式使用 28px 字体，正文保持 18px。
- 摘要性能归并不仅限于舒适性：jerk/顿挫/抖动/点刹归舒适性，建立时间/响应时延/刹停反应归响应及时性，控速/稳速/SetSpeed 未达/不合理低速归速度跟踪与稳定性，重制动/最大减速度归制动强度，最大加速度归加速能力，刹停距离/风险归制动安全性，溜车归坡道保持能力，断握手/退出/降级归功能稳定性，另保留控制精度及目标识别连续性。具体表现优先于“稳速”等背景词；无法可靠归类（例如弯道安全速度）保留原名，不为归并强行推断。
- ABS/TCS/VSE 关联 ACC 退出指标在提取时排除，不进入 AI 或图表。AI 输入和图表对比明细共用筛选：相对上一版变化≥50%且绝对差值>6，或非空准出结论改变；上一版为零时相对变化无定义，仅按准出改变保留。无基线仅展示当前未准出项。准出总览仍统计其余全部有效指标，不能用筛选项推断整体准出。
- KPI 接口预检查默认使用 `KIMI_TIMEOUT_SECONDS`，而不是通用的 20 秒探测超时；失败信息保留脱敏后的实际错误，方便区分超时、认证和限流。
- KPI 部分失败时完成卡片显示前五条记录 ID 和安全分类后的错误，全部失败项输出到机器人终端；多余项提示查看终端。不得输出模型回复、报告正文、API 密钥或附件链接；未知错误保留异常类型而非原始内容。摘要数字和超长校验分别标明，避免只展示失败数量。
- 公共飞书 Agent 的通用模型和 KPI 共用 `KIMI_API_KEY`、`KIMI_MODEL`（默认 `kimi-for-coding`）、`KIMI_TIMEOUT_SECONDS`（默认 120 秒），固定通过 Kimi Code OpenAI 接口调用；不继承旧 `LLM_API_PROTOCOL`、模型地址或密钥，缺少密钥时明确报错。内网镜像专用适配器保持独立。真实内部报告不得外传至第三方模型；外部接口验证只使用非敏感合成数据。
- 有且只有固定指令 `分析飞书多维表格中的KPI报告` 可以启动；固定取消指令为 `取消当前KPI分析`。
- 生产目标是知识库 `OkEcwJBTYiDq6Ck0wcDcATXFnpe` 下的真实多维表格 `纵向KPI结果及分析汇总表`，默认 app token 为 `XzVtbQGDGaEgQ9s6rOXc0yyJnQg`，表 ID 为 `tblLhHnElIL7llm4`。不得凭空创建另一套 KPI 字段。
- 入口完整分页读取 Base 字段和记录，只处理 `是否进行KPI报告AI分析 = 是` 的行。成功写回后把该触发列复位为 `否`；附件下载、解析或 AI 分析失败时不修改该记录，保留 `是` 以便重试。
- 真实必需字段为 `版本KPI报告`（附件）、`是否进行KPI报告AI分析`（单选）和 `KPI报告AI分析结果`（文本），字段名可用 `KPI_REPORT_FIELD_REPORT`、`KPI_REPORT_FIELD_TRIGGER`、`KPI_REPORT_FIELD_RESULT` 覆盖。
- 只下载并解析 `版本KPI报告`，不得读取 `Bad Case列表` 作为模型输入，也不得读取或结合责任人、作者、部门等其他列。模型输入只能包含报告附件提取内容和记录 ID。
- `KPI报告AI分析结果` 只写不含具体数字的定性摘要，按总体判断、改善表现、退化与风险、证据边界四段组织；不再输出原因和建议章节。输入 compact 在本地缩短行标签、去除空白、提取重复长描述字典，保留指标数值与证据，不额外调用模型压缩；每次请求独立。图片按摘要本地绘制，不消耗额外模型 token。禁止通过截断报告冒充完整上下文压缩。
- `kpi_report_charts.py` 从 Excel 原始值及 RGB/主题绿色表头识别当前版本，对比左侧相邻版本；无基线仅展示当前状态。图示不是摘要截图：固定 1600 像素宽、自适应高度 PNG，含准出饼图、MPI/CPI 独立变化分布柱图、全部指标分组对比与明细表（真实数值、样本/事件、标准、准出变化、单元格来源）。旧版蓝、新版绿、未准出红。每行独立刻度，下界和零事件不判升降，不跨指标求和或暗示因果。结构不明报错，不猜测。写入 `KPI报告AI分析图示`，上传和摘要同次回写成功才计成功，临时图自动清理。
- 分析结果只写入多维表格对应列，不向群卡片展示报告正文或分析内容；群里只保留进度和最终计数状态。
- 必须配置 `KPI_REPORT_BASE_TOKEN`、`KPI_REPORT_TABLE_ID`；安装配置模板默认指向上述真实表。不得运行脚本创建虚构 KPI 表结构。
- 该能力必须后台执行，不能阻塞飞书事件处理；取消只作用于同一 `open_id + chat_id` 下正在运行的 KPI 分析。
- 在下载附件或写回前必须先探测 AI 接口；接口不可用时整批停止且不修改表格。单批模型调用遇到临时错误按 `KPI_REPORT_LLM_RETRY_ATTEMPTS` 重试，默认 3 次。

内网 Excel / 镜像部署补充：

- 内网形态只保留批量行车问题原因分析，不启动飞书机器人，不读取飞书多维表格，不安装或调用 `lark-cli`。入口是 `local_batch_ui.py` 本地 Web UI，用户上传同格式 Excel 后点击分析，最终下载 `分析结果.xlsx + images/ + run_report.json` 的结果包。
- 内网 Excel 表格仍沿用上述 `Agent_` 字段和状态语义，只处理 `Agent_问题状态 = 待分析` 的行。`Agent_问题日期` / `Agent_问题时间` 放在输入字段区，用于筛选 H5 内问题窗口；`Agent_分析信号图示` 在内网版中写入本地结果包里的相对图片路径，不作为飞书附件字段。
- `Agent_h5数据路径` 可以填写，也可以留空。已填写时必须是容器运行环境能访问的路径；Excel 中如果保留 Windows 路径或 NAS 路径，必须通过 UI 的路径映射、`LOCAL_BATCH_PATH_MAP` 或 `LOCAL_BATCH_PATH_MAP_FILE` 映射到容器内挂载目录，例如 `{"E:\\ADAS_data":"/data/adas_data"}`。留空时，内网版会根据 `Agent_车号`、`Agent_车型`、`Agent_问题日期`、`Agent_问题时间` 在 `LOCAL_BATCH_H5_SEARCH_BASE_DIR` 下自动定位 H5，查找逻辑与飞书版保持一致：先按文件起始时间粗筛相邻 H5，再读取 H5 内 `timestamp` 覆盖范围确认目标时间；若找不到，状态保持 `待分析`。
- 内网镜像默认通过 `LLM_API_BASE_URL/chat/completions` 调用本地或内网部署的 OpenAI 兼容模型 API；完全无公网环境也可以运行，但必须有容器可访问的本地模型服务。`deploy/intranet_batch/llm_service.py` 是镜像内专用适配器，允许 `LLM_API_KEY` 留空；当前飞书 Agent 和 Ubuntu 部署继续使用公共 `llm_service.py`，不受这个可选鉴权逻辑影响。网关地址、模型名、可选密钥只能通过 `.env`、Docker Secret 或运行时环境变量注入，不能写进镜像。
- 后续凡是改到批量行车问题原因分析的字段、状态、问题类型、DeepSeek 调查链路、结论概要、信号图示、路径规则、依赖库或运行时目录，必须同步检查并更新 `local_batch_root_cause.py`、`local_batch_ui.py` 和 `deploy/intranet_batch/*`。除非该改动明确影响飞书机器人 Ubuntu 运行，否则不要顺手修改 `deploy/ubuntu_run/*`。

### 5. 轴端扭矩准确性校验

状态：可用。

定位：

- 这是一个先确认车型/Final Ratio、再上传 `.blf` 的诊断分析 skill，用于判断 `IActAxleTrq` / 轴端扭矩是否与发动机扭矩、液力变矩器速比、实际档位速比、主减速比和传动效率复算结果一致。
- 它不是 BLF 转 MDF 交付能力；用户只是要 `.mdf` 文件时仍走 `blf_to_mdf_conversion`。

触发场景：

- 用户说“校验轴扭”“轴扭准不准”“轴端扭矩准确性”“复算 `IActAxleTrq`”“发动机扭矩到轴端扭矩”。
- 用户上传 `.blf`，希望用当前最新 ECM/CCB ARXML 解析并输出扭矩对比、误差、异常窗口和信号图示。

输入：

- `.blf`
- ECM/CCB `.arxml`。不指定时优先使用 `AXLE_TORQUE_ARXML`，其次找工程 `database/` 下的 ECM ARXML；飞书入口也允许用户先上传新的 `.arxml`，再上传 `.blf`。
- 车型/配置必须由用户先按编号选择，再提示上传 BLF，这个顺序不能变：`1=358-2`，Final Ratio 3.47；`2=C1UL`，Final Ratio 3.47；`3=E2LB-2`，Final Ratio 3.17；`4=其他车型`，必须继续让用户输入 Final Ratio 后才能分析。每次发起新的轴端扭矩校验都必须重新选择车型/Final Ratio，不能继承上一轮 session 的车型，也不能因为首轮文本里带了 `1/2/3/C1UL/3.47` 就跳过车型确认；飞书入口不得在用户未选择车型或未输入其他车型 Final Ratio 时默认使用 3.47，且不得在首轮把 BLF 上传放在车型选择之前。

计算口径：

- 公式来自《发动机扭矩至轴端扭矩计算方法说明书》：`EngActStdyStTorq * TC_Ratio(lambda) * GearRatio * FinalDriveRatio * TransmissionEfficiency`；`FinalDriveRatio` 按车型选择或用户手动输入，`358-2=3.47`，`C1UL=3.47`，`E2LB-2=3.17`。
- `lambda = TrnTrbAngVl / EngSpd`，按文档表格插值计算 TC Ratio。
- 档位源优先使用 `ITransEstGear_ECP_H1 / ITransEstGearV_ECP_H1`；ECP_H1 没有有效 D1-D9/R 样本时才回退到 `ITransEstGear_TCM`。
- 只在 D1-D9 前进档和 R 档、关键有效性信号为 valid、采样间隔满足要求时做准度判断；P/N/Park、D10 或无有效档位样本不下结论。R 档编码 14，`i_gear=2.96`，`η_trans=1`，按正值参与复算；图示中 R 显示为 `-1`，避免误读成 14 挡。
- 已确认 `IActAxleTrq` 就是真实轴端扭矩，因此默认 `AXLE_TORQUE_OUTPUT_SCALE=1`，文档公式复算结果不再做 0.5 折算。只有后续明确要按单侧/折半口径临时对比时，才手动覆盖该变量。
- 动力流状态是说明书定义的最小动态输入之一。优先读取 078 `PTEI_Transmission_Torque_Request_078_M` 中的 `TEPI_TrnsEngdPrFlwSt / TEPI_TrnsEngdPrFlwStV`，如果 BLF 为 ICE 变体，则读取 076 `PTEI_Transmission_Torque_Request_076_M` 中的 `TEPI_TrnsEngdPrFlwSt_ICE / TEPI_TrnsEngdPrFlwStV_ICE`；也兼容 ARXML 解码后的 `I` 前缀等价信号。两套都缺失时只能输出证据不足，不能降级用前进档位替代状态判定。
- 说明书规定复算公式、输入信号、标定表和边界条件；不规定“较准/不准”的工程阈值。若输出准/不准，必须明确这是 Agent 配置阈值，不是说明书标定值。

输出：

- 结论：`轴端扭矩较准`、`轴端扭矩不准` 或 `证据不足`。
- 指标：有效样本、覆盖率、MAE、P95、Max、通过率、最佳时间对齐偏移、异常窗口。
- 图示：`IActAxleTrq`、复算轴端扭矩、误差、档位。
- 飞书实时进度：下载完成后，同一张进度卡必须继续显示 BLF 扫描百分比、目标报文解析百分比、已读帧数、候选/匹配帧数，以及信号抽取、复算、图示生成等后处理阶段。

主要代码：

- `axle_torque_accuracy_analysis.py`
- `FileConverter/blf_to_mdf.py`：提供 BLF 解析、ARXML 报文选择、物理通道路由和信号解码基础能力；轴端扭矩校验直接复用这些函数把目标报文 071/073/0CB/0C8/0D7/076/078/08D 解到内存信号，不再先落临时 MDF。BLF FD 帧 `valid_bytes=0` 时必须按 DLC 回退读取 payload，不能把有效载荷当空帧。
- `main.py::process_uploaded_file` 中 `axle_torque_accuracy` 分支
- `agent_skills.py` 中 `axle_torque_accuracy_analysis` skill 定义

注意：

- 当前默认 ECM 数据库名是 `Clea_Mode_Year23_ECM_V27.80.1.arxml`；ADM ARXML 不含 0CB 涡轮转速信号时，不能用于该校验。
- 078/076 ICE 动力流状态都缺失时，必须返回证据不足；不要退化为按档位和有效性信号门控。
- 如果缺少发动机稳态扭矩、发动机转速、涡轮转速、实际档位或 `IActAxleTrq`，只能输出证据不足，不能编造“较准/不准”。
- Ubuntu 迁移包必须包含 `axle_torque_accuracy_analysis.py` 和 `FileConverter/blf_to_mdf.py`；内网 Excel 批量形态目前不开放该能力，除非后续明确新增本地 BLF 上传 UI。

### 6. ACC/LCC 功能退出原因分析

状态：可用。

定位：

- 这是一个上传 H5 后的诊断分析 skill，用于定位 ACC / LCC 从配置表定义的强激活态退到非强激活态、非 OFF 状态退到 OFF、临时退出、退出点、退出码和抑制原因。
- 它只回答“这段数据里 ACC/LCC 为什么退出或有没有退出”，不要和执行器断握手、行车问题原因分析混在一起。

触发场景：

- 用户明确说“基于这段数据 / H5 / 附件分析 ACC/LCC 功能退出原因”。
- 用户问“ACC 为什么退出”“LCC 临时退出原因”“这段数据里巡航退出点在哪里”，并准备上传或已经上传 `.h5/.hdf5`。

输入：

- `.h5`
- `.hdf5`
- `.blf` 当前不直接支持，需要先解析成 H5；如果用户上传 BLF，要清楚提示转换后再传。

规则配置：

- `config/acc_lcc_exit_reason_rules.xlsx`
- `signals` sheet：维护功能、用途、逻辑字段、真实信号名、解码方式、角色、备注和 H5 数据集路径。
- `decode_values` sheet：维护状态值、退出原因码、bitmask 含义。
- `source_notes` sheet：记录来源和维护说明。
- 后续如果 H5 信号名变化，优先改 `signals` 的真实信号名和 H5 数据集路径；原因码含义变化，改 `decode_values`。不要在代码里再维护第二份隐藏映射。
- ACC/LCC 退出判据不再固定写死为 ACC `4/5/6/7 -> 非激活` 或 LCC `4/7 -> 0`；运行时会按 `decode_values` 中状态枚举识别非 OFF 状态，并保留强激活态降级判断。强激活态退到非强激活态可判为退出；非 OFF 可用态退到 OFF 时，需要结合对应退出/抑制原因码在跳变时刻置位后再定因。对于包含 `Agent_问题日期/Agent_问题时间` 的批量行，会优先选择离问题时间最近的退出事件。

主要代码：

- `function_exit_analysis.py`
- `main.py::process_uploaded_file` 中 `function_exit_analysis` 分支
- `agent_skills.py` 中 `function_exit_analysis` skill 定义

重要边界：

- 用户只是问 ACC/LCC 状态流程、条件、信号置位、设计要求或让画流程图时，走 `actuator_reference_search` 或 `result_transformer`，不要进入等待上传。
- 用户问顿挫、重制动、误制动、跟车距离、未达 set speed 等行车现象原因时，走 `root_cause_analysis`。
- 信号缺失时只能说“当前证据不足，需要补充状态/原因信号”，不要编造退出原因。

### 7. 应用层 / 底软故障诊断查询

状态：故障表查询入口和故障降级诊断入口当前已在 `config/agent_capabilities.xlsx` 关闭。

说明：

- `fault_table_search` 当前关闭。
- `fault_diagnosis` 当前关闭。
- `dtc_code_search` 不受影响，制动 / 驱动 DTC 故障码查询仍可用。
- 代码和故障表仍保留，后续需要恢复时优先改能力开关表，不要删代码或绕过 skill。

项目限制：

- J6B：支持
- 8650：开发中
- BMC：开发中

注意：J6B / 8650 / BMC 限制只针对故障诊断，不限制性能分析和执行器分析。

知识来源：

- 项目内 `fault_tables/FDA3故障.xlsx`
- 项目内 `fault_tables/FDA7故障.xlsx`
- 可选环境变量 `BLU_LEGACY_FAULT_DOC_DIR` 可作为项目内故障表缺失时的回退目录；Ubuntu 部署包不得硬编码 Windows 盘符路径。
- FDA7 = 应用层故障
- FDA3 = 底软故障

支持输入：

- `slog` 日志文件
- DID 日志文件
- `fault_event` 日志行
- `62 FD A3 ...` / `62 FD A7 ...` 全量字节串
- 故障码查询，例如 `FDA3[113]`、`FDA7[8]`、`byte 0 bit 4`
- 自然语言查询，例如“环视相关需要关注哪些 FDA3 / FDA7”
- 故障表元信息查询，例如“FDA7 一共有多少项”“FDA7 根据哪个文档判断”“FDA3 和 FDA7 分别是什么”
- 上下文追问，例如用户先问“FDA7 有多少项”，再问“这 136 项帮我总结一下”，必须继承上一轮 FDA7 上下文。

重要解析规则：

- DID 中类似 `<Rx ... 62 FD A3 ...>`，`62 FD A3` 是响应头和 DID，后面的数据区才是故障表。
- `fault_event` 主要按 FDA3 故障表解释。
- 用户问功能相关故障条目时，应该查本地故障表并用 DeepSeek 组织回答，不要机械要求用户上传日志。
- 用户问故障表数量、来源文件、FDA3/FDA7 判断依据时，直接基于项目内 `fault_tables/` 回答；不要反问“完整表名”。
- 用户说“应用层故障”默认等价于 FDA7，说“底软故障”默认等价于 FDA3；如果上下文已明确，不要再次要求用户补充表名。

主要代码：

- `fault_diagnosis.py`

### 8. 制动 / 驱动 DTC 故障码查询

状态：可用。

知识来源：

- `dtc_tables/EBCM DTC.xlsx`：制动系统 DTC 表。
- `dtc_tables/PS DTC.xlsx`：驱动系统 DTC 表。

支持问题：

- 查询某个 DTC 码、Service DTC、CustomerDTCID、InternalDTCID 或 J1979 映射。
- 查询某个英文故障描述、监控项或关键词对应哪些 DTC。
- 查询制动系统 / 驱动系统 DTC 表当前有效条目数量。
- 判断某个 DTC 更像制动系统还是驱动系统相关。

重要边界：

- DTC 查询是 `dtc_code_search`，不等于 FDA3/FDA7 故障表查询。
- FDA7 = 应用层故障，FDA3 = 底软故障，仍由 `fault_table_search` / `fault_diagnosis` 处理。
- 没有命中时只说明未命中，并提示可继续检索的码值、系统范围或关键词，不要编造 DTC。

主要代码：

- `dtc_query.py`

## 关键文件

### `main.py`

飞书事件入口和会话状态机。

主要职责：

- 接收飞书长连接消息事件
- 区分文本、文件、media 等消息
- 维护多用户 session
- 调用 DeepSeek 意图编排
- 调用本地分析能力
- 发送飞书文本、卡片、图片
- 清理本次上传文件和分析产物

关键函数：

- `handle_message_event`
- `handle_agent_decision`
- `handle_file_message`
- `process_fault_diagnosis_input`
- `send_message`
- `send_card`
- `send_cardkit_card`
- `start_progress_card`
- `update_progress_card`
- `send_or_update_result_card`
- `send_image`
- `send_file`

注意：

- 历史旧流程和重复函数已清理。后续修改前仍建议用 `rg` 确认当前实际调用链，避免改到未触发的旧入口。
- 不要只改某个看起来相关的函数，改完要验证入口是否真的走到。
- BLF 转 MDF 的转换封装在 `blf_mdf_converter.py`，实际转换脚本随工程放在 `FileConverter/blf_to_mdf.py`。转换器会根据 BLF 各物理通道的 CAN ID、DLC、帧签名和 ARXML Cluster 覆盖度，自动建立“物理通道 -> 逻辑 CAN 网络”映射；同一 CAN ID 只允许使用当前物理通道所属逻辑网络的帧定义解码，不得跨网络猜测或重复解析。默认每个逻辑信号在 MDF 中只输出一份，不按物理通道复制，也不添加 `CAN0/CAN1/CAN6` 前缀；MDF 内容默认不压缩。飞书回传阶段可按大小或失败兜底把 MDF 外层打成 `.zip`，但 zip 只用于传输，解压后仍是未压缩 MDF；`main.py` 只负责收文件、选数据库、调用封装、回传 MDF/zip 和清理本次临时目录。
- 个别 BLF 头部绝对起始时间可能异常，导致 `datetime.fromtimestamp(...)` 或 asammdf 写 MDF v4 HeaderBlock 时抛出 `argument out of range`。转换器必须在保存前校验 `abs_time/tz_offset/time_flags` 等 header 字段；不可编码时把 MDF header `start_time` 回退到 Unix epoch，首次保存失败后允许清理半截输出并用安全 header 重试一次，同时在 summary 中写 `timestamp_warning`。样本相对时间戳仍按 BLF 对象时间保留，不能因此中断转换。
- BLF 转 MDF 文件交付时如果出现 `argument out of range`、MDF append/save 或 ARXML conversion 兼容性异常，`FileConverter/blf_to_mdf.py` 必须输出异常类型、traceback、报文组/信号上下文；能降级跳过单个 conversion 或报文组时不要让整个转换只返回一句无上下文错误。
- 轴端扭矩准确性校验复用 `FileConverter/blf_to_mdf.py` 的 BLF 解析、通道路由和信号解码函数，直接把目标报文解到内存，再由 `axle_torque_accuracy_analysis.py` 复算和作图；默认不生成临时 MDF，也不应因为 MDF 保存兼容性失败而中断轴扭判断。`main.py` 负责 DeepSeek 选中 skill 后进入等待 `.blf`、保存可选 `.arxml`、调用分析、上传图示和回写卡片。不要把它和 BLF 转 MDF 的文件交付分支合并。
- 轴端扭矩计算的有效样本门控必须同时包含：输入信号有限值、时间对齐、有效性位、支持档位、动力流匹配、发动机转速边界，以及“复算轴端扭矩为有限值”。最佳时间偏移后，指标统计必须使用该偏移对应的有效 mask，不能继续沿用零偏移 mask；否则边缘 NaN 会导致 MAE/P95 为 None 并触发格式化异常。

飞书 CardKit / 卡片更新机制：

- 卡片消息优先走 CardKit，而不是只发送旧版 `interactive` JSON。
- `send_card` 会先调用 `send_cardkit_card`：创建 CardKit 卡片，拿到 `card_id` 后再发送卡片引用。
- 如果 CardKit 创建或发送失败，会 fallback 到旧版飞书卡片发送，不能因为进度卡失败阻断本地分析。
- 上传分析类流程应尽量使用同一张动态卡：
  - `start_progress_card`：收到文件 / 输入后先发进度卡。
  - `update_progress_card`：下载文件、运行分析、生成图示等阶段更新同一张卡。
  - `send_or_update_result_card`：最终结果优先更新到同一张 CardKit 卡；只有拿不到 `card_id` 或更新失败时，才另发结果卡。
- 文本请求也应尽量使用同一张动态卡：
  - 用户发起需求后先 `start_progress_card(... stage="分析需求中")`。
  - DeepSeek 识别 skill 后按实际阶段更新为 `查询中`、`问题分析中`、`回传结果中`。
  - 最终回复用 `send_or_update_result_card` 覆盖同一张卡，不要再额外发第二条解释消息。
- 布鲁的主动回复应优先引用本次触发消息：`handle_message` 会用 `current_reply_message_id` 记录当前事件的 `message_id`，`send_message` 默认先走飞书 `message.reply`；引用失败时再降级普通群消息。不要在业务代码里绕过这个发送底座。
- 飞书 `im.message.message_read_v1`、表情新增/删除等非业务事件应注册为空处理器并明确忽略；它们不能触发 DeepSeek，也不能进入任何 skill。日志里如果出现这类事件，只用于说明飞书推送了状态事件，不代表用户发起了新请求。
- 卡片更新的边界是“本次用户输入”，不是整个 session：
  - 每当用户发出新的文本消息或上传新的文件消息，应先在这条新消息后面新建一张进度卡。
  - 这张新卡再随本次处理不断更新到最终结果。
  - 不要在用户新消息后继续更新上一轮旧卡，否则用户需要回到历史消息处找结果，会误以为机器人没反应。
- 进度说明只能展示可公开的处理状态，例如“正在检索故障表”“正在整理指标说明”；不要输出模型隐藏推理链路。
- 不要在分析流程里先 `send_text("已收到，请稍等")`，再另发结果卡。用户会误以为机器人连续多次回复或状态混乱。
- 性能问题分析、执行器握手诊断、故障降级诊断都应遵守同卡更新模式。
- 卡片内容优先使用飞书原生组件：`markdown`、`table`、`column_set`、`img`、`hr` 等。Markdown 表格不要直接作为纯文本甩给用户，尽量转换成 CardKit `table`。
- 时序图 / 信号图示应尽量放进诊断结果卡内一起发送，不要默认拆成多条图片消息。

### `agent_skills.py`

本地能力注册表。DeepSeek 只选择这里登记的 skill 和参数，本地 Python 执行真实逻辑。

主要职责：

- 集中维护可调用能力名称、用户可读标题、所属 `task_type`、触发场景、输入约束和能力边界。
- 为 `llm_service.py` 生成 DeepSeek 总控提示词里的“本地能力”说明。
- 为 `main.py` 提供任务类型展示列表，避免多处手写不一致。

注意：`when_to_use` 和 `examples` 是语义触发说明，不是精确字符串匹配模板。用户表达意思相同即可选择对应 skill。

当前 skill：

通用沟通类 skills：

- `requirement_intake`：需求识别与澄清。只在意图不明确或多能力冲突时使用，一旦用户明确目标就切到业务 skill。
- `smalltalk_chat`：处理打招呼、感谢、轻量咨询和日常沟通。用户没有明确提出技术查询或数据分析时，不要误判到执行器、性能或故障 skill；如果用户在寒暄里顺带提出明确需求，则按最终需求切换到对应 skill。
- `feedback_report`：问题反馈记录。用户用“反馈：”“报错：”“bug：”“问题反馈：”等语法时，收集同一 `open_id + chat_id` 最近对话，生成 PNG 快照，并写入配置好的飞书多维表格。它是支撑能力，不属于性能、执行器或故障业务流程。

查询类 skills：

- `fault_table_search`：故障表查询。当前 `enabled=FALSE`，不对外开放。
- `dtc_code_search`：DTC 故障码查询，基于项目内 `dtc_tables/PS DTC.xlsx` 和 `dtc_tables/EBCM DTC.xlsx`，查询驱动系统 / 制动系统 DTC 码、描述、内部编号、J1979 映射和统计数量；不要和 FDA3/FDA7 故障表混用。
- `performance_capability_search`：性能分析能力查询。当前 `enabled=FALSE`，不对外开放；代码保留，后续恢复时支持查总指标表、功能范围指标表、单个指标规则、阈值、关键信号、指标实现 method / parameters / 事件字段和伪代码逻辑；永远不要直接展示源码片段。
- `actuator_reference_search`：执行器查询，基于原始执行器接口文档和本地索引回答原因、条件、关键信号、信号置位、接口状态流转、流程图前置资料、设计要求和执行器需求评审；对外不要暴露内部规则 ID、CSV、状态机 ID。用户只是在问流程、状态跳转、条件、置位、设计要求、接口变更合理性、实施风险或让把查询结果画成图时，仍属于查询链路，不要进入等待上传文件。

诊断分析类 skills：

- `fault_diagnosis`：故障降级诊断。当前 `enabled=FALSE`，不对外开放。
- `performance_analysis_setup`：性能问题分析。当前 `enabled=FALSE`，不作为独立上传分析入口；行车问题原因分析仍可内部调用性能分析能力。
- `actuator_handshake_analysis`：执行器分析，基于上传的总线数据和原始接口文档整理出来的本地索引识别断握手、降级、状态未闭环等问题；结果必须翻译成用户可理解的接口响应问题。
- `root_cause_analysis`：行车问题原因分析，编排性能问题分析和执行器分析，并补充读取算法请求侧、算法请求诱因与执行器响应侧信号，面向顿挫、重制动、误制动、漏制动、未达到 set speed、溜车、跟车距离异常、横向-猛打等现象形成证据链。
- `batch_root_cause_analysis`：批量行车问题原因分析，是本地固定命令入口，不暴露给 DeepSeek 的可选 skill 列表。只有用户发送固定指令 `分析飞书多维表格中的问题` 时，本地代码才直接触发；读取配置好的飞书多维表格，逐行调用 `root_cause_analysis`，并把结论和信号图示回写到表格。
- `kpi_report_analysis`：KPI 报告 AI 分析，是本地固定命令入口，不暴露给 DeepSeek 的可选 skill 列表。只有用户发送固定指令 `分析飞书多维表格中的KPI报告` 时本地触发；只读取报告内容列，并把分析、原文可证实原因和改进建议写回 Base。
- `batch_root_cause_analysis` 回写 `Agent_结论概要` 时只写给路试人员看的原因点或缺失证据点，例如“误制动更像感知/场景输入触发：前车目标先出现，算法随后给出减速请求，执行器是在跟随”或“顿挫更像执行器响应导致：算法减速度请求较平顺，但制动/驱动实际响应在问题时刻抖动”。必要时可附 1-2 个真实信号名用于追溯；完整证据、下一步建议和表格留在 `Agent_分析结论`。
- `axle_torque_accuracy_analysis`：轴端扭矩准确性校验，上传 `.blf` 后用 ECM/CCB ARXML 解析目标报文，按发动机扭矩至轴端扭矩公式复算，并与 `IActAxleTrq` 对比输出准/不准、误差指标、异常窗口和图示。默认 `AXLE_TORQUE_OUTPUT_SCALE=1`，只做诊断结论，不回传 MDF 文件。
- `function_exit_analysis`：ACC/LCC 功能退出原因查询与分析。无文件时用于查询 `config/acc_lcc_exit_reason_rules.xlsx` 中定义的退出原因、退出原因码、退出条件、抑制原因，以及“某个条件是否已被定义为退出原因”；明确基于上传数据、H5、这段数据定位退出点时，才等待 `.h5/.hdf5` 并检查 ACC/LCC 激活态退出、临时退出、退出原因码和抑制条件。用户问 ACC/LCC 非退出相关的状态流程、条件、信号置位或设计要求时，才走 `actuator_reference_search`。

文件转换类 skills：

- `blf_to_mdf_conversion`：BLF 转 MDF。用户明确要把 `.blf` 转为 `.mdf`、导出 MDF 或做 BLF/MDF 格式转换时使用；默认数据库为 `database/Clea_Mode_Year23_ADM_HC_V27.80.0.arxml`，也允许用户指定已有 `.arxml` 或先上传新的 `.arxml`。本地实现通过 `blf_mdf_converter.py` 调用工程内的 `FileConverter/blf_to_mdf.py`，自动识别 BLF 物理 CAN 通道对应的 ARXML 逻辑网络，并按网络归属解析共享 CAN ID；默认使用 `--compression 0`，不按通道复制信号，确保每个逻辑信号只输出一份。转换成功后优先回传 `.mdf` 文件；如果飞书直传受限或超过阈值，则回传 `.zip`，用户解压后得到的仍是未压缩 MDF。直传阈值由环境变量 `BLF_MDF_DIRECT_UPLOAD_MAX_MB` 控制，默认 100 MB。它只做格式转换，不输出问题诊断结论；用户问“为什么顿挫/重制动/退出/响应异常”时应走对应分析 skill。

结果展示生成类 skills：

- `result_transformer`：对本地业务 skill 的可信结果做展示形态转换。支持规划 `rich_card`、`table`、`flowchart_image`、`timeline_image`、`diagram_image`、`markdown_file`、`html_file`、`gif` 等产物类型。DeepSeek 只生成结构化产物计划；本地 `result_transformer.py` 执行安全渲染。它不能跳过本地业务 skill 直接生成技术结论。触发条件和边界放在 `agent_skills.py` 的 skill 定义里，不要在 `main.py` 或 `result_transformer.py` 中堆业务关键词判断。若用户承接上一轮结果说“把上面的画成流程图/表格/图示”，应复用 `session["last_skill_result"]`，不要重新进入 `requirement_intake`。

新增能力时优先在这里登记，再到 `main.py::handle_agent_decision` 增加对应执行分支。不要让 DeepSeek 直接读写工程文件或直接调用 Codex skills。

能力开关表：

- 路径：`config/agent_capabilities.xlsx`
- `skill_name` 必须与 `agent_skills.py` 中注册名一致。
- `enabled=FALSE`：关闭该能力。`llm_service.py` 生成 DeepSeek 总控提示词时不会列出该 skill，`main.py` 执行前也会拒绝调用。
- `visible=FALSE`：能力仍开启，但不会出现在 DeepSeek 的可选能力列表里；只适合内部承接或临时隐藏，不建议随意使用。
- 需要完全固定句式触发的能力，放到 `agent_skills.py::LOCAL_ONLY_AGENT_SKILLS`，不要让 DeepSeek 在 prompt / enum 中看到；本地精确匹配命中后再直接调用对应 skill。
- 修改表格后建议重启机器人；代码会按文件更新时间重新读取配置，但重启更稳。

### `llm_service.py`

DeepSeek 调用和智能体意图编排。

主要职责：

- 调用 DeepSeek Chat Completion
- 维护群聊普通对话上下文
- 生成结构化 `AgentDecision`
- 判断是否调用本地工具

关键函数：

- `decide_agent_action`
- `merge_agent_decision`
- `heuristic_agent_decision`
- `analyze_session_message`
- `build_result_transform_plan`
- `infer_problem_groups`

工具列表来自 `agent_skills.py`，不要在提示词里重新手写一份。

`build_result_transform_plan` 用于结果展示生成：输入用户需求、来源 skill、本地可信上下文和基础回复，输出结构化产物计划。它只能规划展示形态，不能新增本地结果中没有的技术结论。

### `feedback_reporter.py`

问题反馈写入模块。

主要职责：

- 根据最近对话上下文生成系统临时目录下的 PNG 快照，写入飞书多维表格后清理本地文件。
- 创建飞书多维表格记录。
- 可选调用 `lark-cli base +record-upload-attachment` 将快照追加到附件字段。

目标表格配置：

- 指定已有表：在 `.env` 写入 `FEEDBACK_BASE_TOKEN`、`FEEDBACK_TABLE_ID`，可选 `FEEDBACK_ATTACHMENT_FIELD`，默认字段名是 `对话快照`。
- 自己新建表：运行 `scripts/init_feedback_base.ps1`，脚本会创建默认字段并打印需要写入 `.env` 的 token/table id。
- 附件字段不能作为普通文本字段写入；先创建记录，再把 PNG 快照追加到附件字段。
- 如果未配置目标表，`feedback_report` 只会提示需要配置或运行初始化脚本，不会静默丢反馈。

### `result_transformer.py`

结果展示生成的本地执行器。

主要职责：

- 根据 DeepSeek 的产物计划在本地生成安全产物。
- 当前已支持生成 PNG 流程图 / 状态图、PNG 时序图、GIF 流程动画、表格文本降级、Markdown/HTML 本地文件。

注意：

- DeepSeek 只负责结构化计划，本文件负责实际落地。
- 生成图示应优先嵌入同一张飞书结果卡，不要拆成多条消息。
- 未来新增 PDF、PPT、Excel、可上传文件等产物，应优先扩展此文件和 `main.py::prepare_result_transform_artifacts`，不要把生成逻辑散落到各个业务 skill 里。

### `performance_analysis.py`

性能问题分析实现与结果格式化。

关键函数：

- `run_performance_analysis`
- `load_performance_problem_presets`
- `resolve_performance_analysis_scope`
- `answer_performance_capability_question`
- `load_performance_metric_knowledge`
- `format_metric_table`
- `format_analysis_markdown_clean`
- `build_issue_plots`
- `cleanup_analysis_artifacts`

修改性能指标时优先看：

- `config/performance_problem_presets.xlsx`
- `config/vehicle_response_signal_catalog.xlsx`
- `METRIC_DESCRIPTIONS`
- `PROBLEM_GROUP_DEFINITIONS`
- `analysis_engine/metrics/code/*.py`

- 性能查询能力说明：

- `performance_capability_search` 当前关闭。用户问“性能分析能做哪些”“ACC/稳定性有哪些指标”“每个指标规则是什么”“阈值是多少”“需要哪些信号”“列个指标表”时，不要进入等待上传文件，也不要绕过能力开关；应说明该入口当前关闭。
- 用户问“问题类型有哪些”“顿挫/重制动/误制动/溜车/稳速未达 Setspeed/不合理低速/跟车距离分别关注哪些信号”“对应哪些筛选代码”时，也受 `performance_capability_search` 开关控制；恢复后答案必须来自 `config/performance_problem_presets.xlsx`、`config/vehicle_response_signal_catalog.xlsx` 和指标知识摘要。
- `answer_performance_capability_question` 会读取 `METRIC_DESCRIPTIONS`、`PROBLEM_GROUP_DEFINITIONS` 和每个指标实现，自动提取 `REQUIRED_SIGNALS`、`DEFAULT_*` 阈值、`*_VALUES` 枚举、`method`、事件字段和伪代码逻辑。即使用户问“源码怎么写”，也只总结成伪代码或工程步骤，不直接展示 Python 源码片段。
- `main.py` 会把本地指标知识交给 `llm_service.polish_performance_capability_answer`，由 DeepSeek 组织成更自然的工程回复；DeepSeek 不可用时回退到本地知识文本。

### `actuator_handshake.py`

执行器接口规则查询和总线数据诊断。

关键函数：

- `run_actuator_handshake_analysis`
- `run_actuator_handshake_analysis_with_database`
- `convert_blf_to_h5_for_actuator`
- `iter_blf_messages_preserve_fd_payload`

修改时注意：

- 不要把性能分析逻辑混进执行器逻辑。
- 不要把规则 ID 当作用户可读结论。
- 图示和文字结论要围绕用户问题组织。

### `fault_diagnosis.py`

故障表查询、DID / fault_event 解析和故障诊断。

关键函数：

- `answer_fault_reference_question`
- `process_fault_diagnosis_input`
- `diagnose_fault_text`
- `diagnose_fault_file`
- `format_fault_markdown`

### `dtc_query.py`

制动 / 驱动 DTC 表查询能力。

知识来源：

- `dtc_tables/EBCM DTC.xlsx`
- `dtc_tables/PS DTC.xlsx`

主要职责：

- 自动识别 DTC Excel 表头。
- 支持按 DTC 码、描述、J1979、内部 ID 和系统范围检索。
- 支持统计制动 / 驱动 DTC 表有效条目数量。
- 返回本地可信检索结果，再交给 DeepSeek 组织成用户可读回复。

## 会话状态

session key：

```text
open_id:chat_id
```

常见 stage：

- `collecting_requirements`：DeepSeek 收集并理解需求
- `waiting_file`：等待上传分析文件
- `waiting_fault_input`：等待故障诊断输入
- `processing`：正在分析
- `completed`：本次请求完成

常见 session 字段：

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
- `recent_turns`：同一用户同一群的短期对话上下文，供 DeepSeek 总控理解“上面/刚才/这些/继续/总结一下/为什么”等追问。
- `last_skill_result`：上一轮业务 skill 的可信结果缓存，用于追问、总结、画图、表格、报告等承接上一轮的需求；只保存摘要文本和本地上下文片段。

## 文件处理规则

文本消息：

- 优先进入 DeepSeek 总控。
- DeepSeek 判断是否闲聊、能力说明、查询、配置任务、取消任务或开始分析。

非文本消息：

- `file` / `media` 才进入文件处理。
- 不触发 DeepSeek 闲聊。
- 必须保留文件上传事件，用于后续分析。
- 文件上传事件也会写入短期反馈缓冲，但不会触发 DeepSeek 闲聊。

文件类型：

- `.arxml`：保存到 `database/`
- `.blf` / `.h5` / `.hdf5`：性能分析或执行器分析数据
- slog / DID 日志：故障诊断
- 其他文件：根据当前任务给出清晰格式提示

清理规则：

- 分析结束后只删除本次 session 的上传文件和分析产物。
- 不要删除其他用户的文件。
- 重启时可以清理历史上传和生成产物，但必须限定在项目自己的临时目录内。

## 启动与运维

推荐前台启动，方便观察状态：

```bat
scripts\start_bot.bat
```

后台启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run.ps1
```

查看状态：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\status.ps1
```

重启：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\restart.ps1
```

日志：

```text
logs/feishuagent.out.log
logs/feishuagent.err.log
```

最小语法检查：

```powershell
python -m py_compile agent_skills.py main.py llm_service.py feedback_reporter.py performance_analysis.py actuator_handshake.py fault_diagnosis.py
```

## 飞书侧依赖

飞书应用需要具备：

- 机器人能力
- 长连接事件订阅
- `im.message.receive_v1`
- 发送消息权限
- 接收消息权限
- 文件 / 图片上传下载相关权限
- `im:resource` 之类用于获取上传文件资源的权限
- 多维表格读写、字段读写、附件上传权限
- 读取用户基础信息权限，用于把 open_id 显示成用户姓名

如果机器人能启动但群里无响应，优先检查：

1. 是否有多个机器人进程同时运行。
2. 长连接是否成功连接。
3. 飞书事件权限是否生效。
4. 日志里是否有 `processor not found`、SSL、tenant token、文件下载失败等错误。
5. 当前消息是否真的 @ 了机器人。

## 开发注意事项

- 修改代码前先用 `rg` 查真实调用链。
- 不要无关重构，尤其不要大改 `main.py` 里的历史逻辑。
- 不要把故障诊断、性能分析、执行器分析的状态和规则混在一起。
- 用户说“执行器”“制动没响应”“AEB 没响应”时，应优先考虑执行器分析，不要误分到性能分析。
- 用户问“能分析哪些问题”“有哪些条目”“某功能关注哪些故障码”“每个性能指标规则是什么”“阈值/信号/指标表”时，优先走能力查询 / 本地文档查询，不要直接进入等待上传。
- 用户明确反馈当前 skill 不符合预期，例如“不是性能，是执行器”“不是查询，是诊断”“你用错了”，必须按最新反馈重新选择最终期望的 skill，不要沿用旧 session 或旧兜底判断形成循环。
- 飞书回复尽量合并成一条完整消息，不要先发一句又补一张卡片，让用户不知道哪个是最终结果。
- 报告展示优先使用清晰标题、短结论、表格和图示；避免直接暴露内部变量名、规则 ID 或冗长原始文档依据。
- 本地分析失败时要告诉用户具体原因，例如数据库不匹配、文件格式不支持、报文 payload 为空、缺少信号、权限不足。

## PPT / 展示材料

当前已有系统框架图：

```text
docs/feishu_agent_system_architecture.svg
docs/feishu_agent_signal_flow.gif
docs/feishu_agent_signal_flow_preview.png
```

生成 GIF 的脚本：

```text
scripts/generate_signal_flow_gif.py
```

## Ubuntu 22.04 迁移与一键脚本

后续把本工程迁移到 Ubuntu 22.04 时，对外只使用以下生成目录：

```text
dist/Agent_ubuntu_one_click.tar.gz
```

当前迁移目标是“干净工程目录 + 私有一键脚本”的前台启动版，不注册 systemd 服务。对外只保留 `dist` 迁移入口：先在 Windows 当前工程执行 `python deploy\ubuntu_run\prepare_ubuntu_project.py`，生成 `dist\Agent_ubuntu_one_click\Agent` 和 `dist\Agent_ubuntu_one_click.tar.gz`；再把这个压缩包复制到 Ubuntu，执行 `tar -xzf Agent_ubuntu_one_click.tar.gz && cd Agent_ubuntu_one_click/Agent && chmod +x dist/install_blu_one_click.sh && ./dist/install_blu_one_click.sh`。Ubuntu 机器上不需要使用 `deploy/ubuntu_run/`，打包脚本会把安装入口复制到 `Agent/dist/` 下；`deploy/ubuntu_run/` 只作为 Windows 工程内的打包源码维护。不要直接把 Windows 工作目录原样搬过去作为推荐流程，避免 `.venv`、日志、上传缓存、分析结果和临时文件混入部署包。脚本会自动安装 Ubuntu apt 依赖、通过 NodeSource 安装 Node.js 20 以提供 `node/npm/npx`（不要安装 Ubuntu 源里的 `npm` 包，避免 `node-*` 依赖冲突）、在安装 Python 依赖前固定删除旧的 `~/.local/opt/blu-agent/.venv`、使用 Ubuntu 系统 Python 3.10/3.11 创建 Python venv、安装 `requirements.txt`、安装或刷新 `lark-cli` 到官方最新版、写入用户级配置、生成启动/检查/配置/卸载命令并前台启动；如果目标机器已有旧版 `lark-cli`，脚本也必须尝试刷新，不能静默沿用旧版授权行为。当前依赖锁定包含 `numpy==1.24.4`，因此 Ubuntu 安装脚本必须优先使用 `/usr/bin/python3.10`，不能使用 Anaconda/conda 的 Python 3.13；已有 `.venv` 不论 Python 版本如何，都应在重跑安装脚本时自动删除并重建，避免旧依赖残留。生成命令包括 `blu-agent-lark-auth` 和 `blu-agent-bot-permissions`：前者只作为显式用户 OAuth 辅助命令，默认没有 scope/domain 时不会发起授权；后者用于输出当前布鲁需要的 bot 应用权限清单。bot 应用权限仍必须在飞书开放平台对应应用中启用、发布并按租户要求审批，本地脚本不能绕过平台审批。真实飞书凭据、模型 API Key 和用户数据不得打进公开包或提交代码；`dist/install_blu_one_click.sh` 包含真实密钥，必须保持在忽略目录内，只给部署人员使用。批量分析表格里的 `Agent_h5数据路径` 必须是布鲁运行机器或数据服务器能访问的路径，迁移到 Ubuntu 后不要继续填写 Windows 的 `D:\...` 路径。

Ubuntu 飞书授权补充：批量行车问题原因分析默认走应用/bot 的 OpenAPI 读写飞书多维表格，不再自动发起 `lark-cli auth login --domain base` 用户授权，避免申请不必要的用户域权限并触发额外审批。`BLU_AGENT_AUTO_LARK_AUTH` 默认必须为 `0`，`BATCH_ROOT_CAUSE_ALLOW_USER_AUTH_FALLBACK` 默认必须为 `0`；只有用户明确要求使用用户身份访问 Base 时才手动运行 `blu-agent-lark-auth`。bot/app 权限仍必须在飞书开放平台对应应用中按最小集合启用、发布并审批。

Ubuntu 安装代理补充：一键安装脚本默认会清理 `127.0.0.1` / `localhost` 这类本机回环代理；安装 Python 依赖时，在未设置 `BLU_AGENT_KEEP_PROXY=1` 的情况下会设置 `PIP_CONFIG_FILE=/dev/null`，并清理 pip/npm 代理变量，避免用户级 `pip.conf` 或 `127.0.0.1:7897` 这类桌面代理导致 `pip` / `npx` 卡死。需要强制不走代理时用 `BLU_AGENT_NO_PROXY=1 ./dist/install_blu_one_click.sh`；只有确认公司代理可用时，才使用 `BLU_AGENT_KEEP_PROXY=1 ./dist/install_blu_one_click.sh`。

Ubuntu 运行时目录补充：不要再使用共享目录 `/tmp/blu_agent`，也不要把布鲁自己的运行产物兜底到 `/tmp/blu_agent_<user>`。上传文件、分析图示、批量行车问题原因分析、问题反馈截图、结果展示、执行器诊断、BLF 转 MDF、轴端扭矩准确性校验、飞书 CLI JSON / 附件中转文件等临时产物，都必须通过 `runtime_paths.get_runtime_subdir(...)` / `ensure_writable_dir(...)` 获取当前用户可写目录；附件和飞书资源下载目标必须再经过 `ensure_writable_download_file_path(...)`，因为下载器可能传入会话目录或无后缀 token 而不是最终文件名；其他具体文件路径必须再经过 `ensure_writable_file_path(...)`，不要直接拼接后写入。默认写入当前用户独占目录（优先 `BLU_AGENT_RUNTIME_DIR`，其次 `BLU_AGENT_RUNTIME`，再按 `$XDG_RUNTIME_DIR/blu-agent` 或 `~/.cache/blu-agent/runtime` 兜底；代码最终兜底为 `~/.cache/blu-agent/runtime-<user>-<pid>`）。进程入口必须调用 `runtime_paths.sanitize_runtime_environment()` / `get_runtime_root()`，把验证过的安全目录重新发布到 `BLU_AGENT_RUNTIME_DIR` 和 `BLU_AGENT_RUNTIME`，避免外部 CLI 或子进程继承历史 `/tmp/blu_agent...`。各能力只能在其 runtime 子目录下创建 `uploads/`、`analysis_results/`、`batch_root_cause/`、`feedback/`、`result_transformer/`、`actuator_handshake/`、`bad_case_plot/`、`root_cause_evidence/`、`blf_mdf/`、`axle_torque_accuracy/`、`file_delivery/`，BLF 转 MDF 和轴端扭矩校验产物不要写回上传目录或数据目录。调用 `lark-cli`、转换器或其他外部 CLI 时，输出文件必须放在已验证可写的父目录下，命令参数尽量传文件名并把 `cwd` 设到该父目录，避免 CLI 把绝对路径、Windows 路径或历史 `/tmp/blu_agent...` 路径误解析成不可写目录。所有 CLI 中转文件执行后必须清理。如果出现 `Permission denied: '/tmp/blu_agent...'`、`Permission denied: '/tmp/blu_agent/uploads/...'`，或当前用户 runtime 目录下历史文件权限异常，优先检查 `~/.config/blu-agent/blu-agent.env` 是否残留 `BLU_AGENT_RUNTIME` / `BLU_AGENT_RUNTIME_DIR` 指向任意 `/tmp/blu_agent...` 路径，并用最新 `dist` 包重装。

2026-08-27 批量 H5 时间覆盖补充：飞书多维表格批量分析和内网 Excel 批量分析即使已填写 `Agent_h5数据路径`，也必须根据 `Agent_问题日期` / `Agent_问题时间` 校验该 H5 是否覆盖目标时间；若已填写路径不覆盖，则先尝试自动重新定位覆盖该时间的 H5，仍找不到时状态保持 `待分析`，结论/概要写入“未找到对应h5数据，请确认数据解析是否完成”，不能写成 `分析失败`。J6B H5 自动查找的最后一段粗略兜底窗口默认 5 分钟，可通过 `J6B_H5_LAST_SEGMENT_COARSE_WINDOW_SECONDS` 调整，避免最后一个文件起始时间误覆盖后续明显不存在的数据。

2026-09-02 批量根因分析 token 优化补充：最终根因模型调用默认只接收 `compact_evidence`，其中包含问题描述线索、候选窗口、窗口内关键证据、全局关键变化和缺失信号；完整 `plan/evidence/window_evidence` 仅保留在本地 context 用于调试。若 compact 结果无法解析、描述匹配度低、结论仍为证据不足、候选窗口未逐项分析，或实际高于 SetSpeed 场景仍把曲率/弯道限速单独作为主因，则只追加一次展开窗口证据复核。候选窗口筛选、H5 读取和信号图生成逻辑不变；内网 Excel 镜像通过 `COPY *.py /app/` 自动带入该共享模块变更，无需同步修改 `deploy/intranet_batch/*`。

2026-09-02 飞书写回失败归类补充：批量行车问题原因分析中，`open.feishu.cn` 的 `SSLEOFError`、连接断开、超时、429/5xx 等属于飞书 OpenAPI/网络写回问题，不得归类为模型接口调用失败。OpenAPI 请求通过 `_openapi_request` 做瞬时错误重试，默认 3 次，可用 `BATCH_ROOT_CAUSE_FEISHU_RETRY_ATTEMPTS` 调整。若分析结果生成后写回失败，群卡片状态显示“写回失败”，表格不应被二次写成“模型接口调用失败”；若写回在分析启动前失败，本条不继续消耗模型 token，并停止本轮批量。

2026-09-03 批量记录扫描补充：`BATCH_ROOT_CAUSE_MAX_ROWS` 只能限制本轮最多处理多少条 `待分析` 记录，不能再限制“只读取表格前 N 条”；默认必须为 0，表示处理扫描范围内所有待分析记录，直到全部跑完或用户发送取消指令。读取多维表格时用 `BATCH_ROOT_CAUSE_SCAN_MAX_ROWS` 控制扫描上限，默认 5000；设为 0 时 OpenAPI 会继续翻页直到表格结束，CLI 兜底因必须传 limit 使用 10000。批量队列先统计所有扫描范围内的 `Agent_问题状态 = 待分析` 行，再把 `JIRA链接` 非空的待分析行排到前面，同一优先级保持表格原顺序；进度和结果卡片必须区分“发现待分析总数”和“本轮计划/实际处理数”。

2026-09-03 批量 BLF 自动解析补充：批量行车问题原因分析自动查找 H5 失败时，会继续在 `BATCH_ROOT_CAUSE_BLF_SEARCH_BASE_DIR` 下按车型、车号、日期和问题时间查找原始 BLF。该目录必须由部署环境配置，不假定固定数据盘路径。BLF 目录名只做大小写不敏感和非字母数字归一化匹配，不要硬编码具体车型目录前后缀；候选 BLF 按文件名起始时间和同目录相邻 BLF 判断覆盖问题时间。解析输出写入 `BATCH_ROOT_CAUSE_H5_SEARCH_BASE_DIR` 下的 parsed 结构：`<车型变体>-<车号>/<YYYYMMDD>-<车型变体>-<车号>_parsed/<blf_stem>_parsed.h5`。Ubuntu 一键安装必须检查 parsed H5 根目录能创建和写入；没有权限时安装脚本先尝试通过 `sudo setfacl` 给当前用户授权，失败时必须停止并给出管理员授权命令。迁移包必须包含所需解析脚本和经授权提供的接口数据库。

后续凡是改到环境变量、启动脚本、部署目录、批量表格字段、依赖库、运行时路径、飞书用户授权 scope 或 bot 应用权限清单，必须同步更新 `deploy/ubuntu_run/env.example`、`deploy/ubuntu_run/install_blu_agent.sh`、`deploy/ubuntu_run/README.md`，重新生成 `dist/Agent_ubuntu_one_click/` 和 `dist/Agent_ubuntu_one_click.tar.gz`，并在本节记录影响点；不能只改 Windows 当前工程。`deploy/ubuntu_run/` 仅作为 Windows 侧打包源码维护，Ubuntu 用户不直接使用；`deploy/ubuntu_run/prepare_ubuntu_project.py` 会在每次打包时重新生成 `dist/install_blu_one_click.sh`，把安装入口复制到生成包的 `Agent/dist/` 下，把迁移说明复制为 `Agent/README_UBUNTU.md`，并在包根生成 `README_UBUNTU_DEPLOY.txt`，最后自动压缩成 `dist/Agent_ubuntu_one_click.tar.gz`，确保私有一键脚本跟当前 `.env`、字段名、模型配置和授权/权限配置一致，避免旧配置滞留。

Windows / Ubuntu Git 同步补充：Git 仓库是代码同步的唯一事实源。每次整理或修改运行代码后，都必须同步检查并更新 `deploy/windows_run/*` 与 `deploy/ubuntu_run/*` 的安装/更新脚本、环境变量模板和说明；两端更新器默认只允许 `git pull --ff-only`，工作区不干净、分支无 upstream 或无法快进时必须停止并提示人工处理，不能 reset、强制覆盖或自动丢弃本地修改。Windows 与 Ubuntu 安装器都要安装当前 checkout 的 `requirements.txt`，保留本机 `.env`/用户配置和外部数据库，不把密钥写入 Git。交付前至少验证 Python 语法、目标能力测试、安装脚本语法和安装包/当前 checkout 中必需模块完整性。

## 内网 Excel 批量分析镜像部署

内网部署目标是“本地持续运行的 Excel 批量分析工作台”，不是飞书机器人迁移。对外入口是浏览器访问 `local_batch_ui.py`，上传同格式 Excel 后点击分析，下载结果包。该形态只保留批量行车问题原因分析，不提供飞书群聊、飞书 Base、反馈记录、文件转换、DTC 查询、执行器查询等对外功能。

部署源码固定维护在：

```text
local_batch_root_cause.py
local_batch_ui.py
deploy/intranet_batch/
```

镜像构建和运行说明见：

```text
deploy/intranet_batch/README.md
deploy/intranet_batch/Dockerfile
deploy/intranet_batch/docker-compose.yml
deploy/intranet_batch/env.example
```

内网镜像构建时可以联网安装 apt/pip 依赖；内网运行时不应再访问 apt、pip、npx、飞书或公网。运行时必须提供容器可访问的 H5 数据挂载和本地/内网 OpenAI 兼容模型 API。`LLM_API_BASE_URL`、`LLM_MODEL`、可选的 `LLM_API_KEY`、`LOCAL_BATCH_PATH_MAP`、`LOCAL_BATCH_DATA_ROOT` 等配置通过 `.env` 或 Secret 注入，不能打进镜像。内网镜像会用 `deploy/intranet_batch/llm_service.py` 覆盖容器内 `/app/llm_service.py`，该覆盖只发生在本镜像内部，不能反向改公共 Agent 或 Ubuntu 部署逻辑。

维护约束：后续任何批量行车问题原因分析相关改动，都要同步评估内网 Excel 适配器和 `deploy/intranet_batch/*` 是否需要更新；但不能因为内网镜像改动而改坏 Ubuntu 飞书机器人部署。除非用户明确要求或改动确实影响 Ubuntu 运行，不要修改 `deploy/ubuntu_run/*`、`dist/Agent_ubuntu_one_click.tar.gz` 或飞书授权相关逻辑。
