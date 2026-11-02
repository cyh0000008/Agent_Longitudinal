from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


TASK_FAULT_DEGRADATION = "fault_degradation"
TASK_ACTUATOR_HANDSHAKE = "actuator_handshake"
TASK_PERFORMANCE_ANALYSIS = "performance_analysis"
TASK_ROOT_CAUSE_ANALYSIS = "root_cause_analysis"
TASK_BATCH_ROOT_CAUSE_ANALYSIS = "batch_root_cause_analysis"
TASK_FUNCTION_EXIT_ANALYSIS = "function_exit_analysis"
TASK_BLF_TO_MDF_CONVERSION = "blf_to_mdf_conversion"
TASK_AXLE_TORQUE_ACCURACY = "axle_torque_accuracy"

SKILL_CATEGORY_QUERY = "query"
SKILL_CATEGORY_DIAGNOSTIC_ANALYSIS = "diagnostic_analysis"
SKILL_CATEGORY_GENERAL = "general"
SKILL_CATEGORY_POSTPROCESS = "postprocess"
SKILL_CATEGORY_FILE_CONVERSION = "file_conversion"

SKILL_REQUIREMENT_INTAKE = "requirement_intake"
SKILL_RESULT_TRANSFORMER = "result_transformer"
SKILL_SMALLTALK_CHAT = "smalltalk_chat"
SKILL_FEEDBACK_REPORT = "feedback_report"
SKILL_FAULT_TABLE_SEARCH = "fault_table_search"
SKILL_DTC_CODE_SEARCH = "dtc_code_search"
SKILL_FAULT_DIAGNOSIS = "fault_diagnosis"
SKILL_PERFORMANCE_CAPABILITY_SEARCH = "performance_capability_search"
SKILL_PERFORMANCE_ANALYSIS_SETUP = "performance_analysis_setup"
SKILL_ACTUATOR_REFERENCE_SEARCH = "actuator_reference_search"
SKILL_ACTUATOR_HANDSHAKE_ANALYSIS = "actuator_handshake_analysis"
SKILL_ROOT_CAUSE_ANALYSIS = "root_cause_analysis"
SKILL_BATCH_ROOT_CAUSE_ANALYSIS = "batch_root_cause_analysis"
SKILL_FUNCTION_EXIT_ANALYSIS = "function_exit_analysis"
SKILL_BLF_TO_MDF_CONVERSION = "blf_to_mdf_conversion"
SKILL_AXLE_TORQUE_ACCURACY_ANALYSIS = "axle_torque_accuracy_analysis"


ROOT = Path(__file__).resolve().parent
CAPABILITY_CONFIG_PATH = ROOT / "config" / "agent_capabilities.xlsx"

# Local-only skills are intentionally not exposed to DeepSeek. They are entered
# only by exact local commands or internal code paths.
LOCAL_ONLY_AGENT_SKILLS = {SKILL_BATCH_ROOT_CAUSE_ANALYSIS}
LOCAL_ONLY_TASK_TYPES = {TASK_BATCH_ROOT_CAUSE_ANALYSIS}


TASK_TYPE_LABELS = {
    TASK_FAULT_DEGRADATION: "故障降级诊断",
    TASK_ACTUATOR_HANDSHAKE: "执行器握手诊断",
    TASK_PERFORMANCE_ANALYSIS: "性能问题分析",
    TASK_ROOT_CAUSE_ANALYSIS: "行车问题原因分析",
    TASK_BATCH_ROOT_CAUSE_ANALYSIS: "批量行车问题原因分析",
    TASK_FUNCTION_EXIT_ANALYSIS: "ACC/LCC功能退出原因分析",
    TASK_BLF_TO_MDF_CONVERSION: "BLF 转 MDF 转换",
    TASK_AXLE_TORQUE_ACCURACY: "轴端扭矩准确性校验",
}


@dataclass(frozen=True)
class AgentSkill:
    """A local capability that DeepSeek can choose, while Python executes it."""

    name: str
    title: str
    category: str
    task_type: str | None
    summary: str
    when_to_use: tuple[str, ...] = field(default_factory=tuple)
    input_contract: tuple[str, ...] = field(default_factory=tuple)
    execution: str = ""
    limits: tuple[str, ...] = field(default_factory=tuple)
    examples: tuple[str, ...] = field(default_factory=tuple)

    def prompt_block(self, index: int) -> str:
        lines = [
            f"{index}. {self.name}（{self.title}）",
            f"- category: {self.category}",
            f"- task_type: {self.task_type or 'null'}",
            f"- 作用: {self.summary}",
            "- 触发方式: 语义匹配，不要求用户原话和示例完全一致",
        ]
        if self.when_to_use:
            lines.append(f"- 适合: {'；'.join(self.when_to_use)}")
        if self.input_contract:
            lines.append(f"- 输入: {'；'.join(self.input_contract)}")
        if self.execution:
            lines.append(f"- 执行: {self.execution}")
        if self.limits:
            lines.append(f"- 边界: {'；'.join(self.limits)}")
        if self.examples:
            lines.append(f"- 示例: {'；'.join(self.examples)}")
        return "\n".join(lines)


AGENT_SKILLS: tuple[AgentSkill, ...] = (
    AgentSkill(
        name=SKILL_REQUIREMENT_INTAKE,
        title="需求识别与澄清",
        category=SKILL_CATEGORY_GENERAL,
        task_type=None,
        summary="当用户需求不够明确、可能落到多个能力，或用户正在纠正上一轮理解时，先识别/澄清真实目标，再切入具体业务 skill。",
        when_to_use=(
            "用户描述的是一个问题或数据，但无法稳定判断该走性能分析、执行器分析、故障诊断还是查询",
            "用户说“不是这个/不对/不是分析/换个需求”等纠正语，但新意图还需要重新理解",
            "用户只给了现象、对象或一句泛泛需求，还缺少文件、问题类型、查询对象、项目或分析范围",
            "需要让用户在多个可行 skill 之间做选择，例如性能分析、执行器分析、两个都分析",
        ),
        input_contract=(
            "自然语言需求或纠正信息",
            "可结合当前会话中上一条待澄清请求",
        ),
        execution="不直接执行业务分析；只输出最少必要澄清问题，或在用户补足信息后交给具体业务 skill。澄清问题要给出可直接回复的选项。",
        limits=(
            "用户需求已经明确时不要停在本 skill，应直接选择对应业务 skill",
            "不要把澄清阶段写成固定规则树；用户一旦给出明确新需求，就立刻切到新 skill",
        ),
        examples=(
            "帮我分析这段数据有没有异常",
            "不是这个，我想查一下 ACC 状态跳转",
            "这个问题能不能看一下",
        ),
    ),
    AgentSkill(
        name=SKILL_RESULT_TRANSFORMER,
        title="结果展示生成",
        category=SKILL_CATEGORY_POSTPROCESS,
        task_type=None,
        summary="对已经由本地业务 skill 产出的可信结果做展示形态转换，例如生成流程图、时序图、结构化表格、Markdown/HTML 报告、动图或更适合飞书卡片展示的摘要。",
        when_to_use=(
            "用户明确要求把查询或诊断结果画成图、流程图、状态跳转图、时序图、可视化图或用图表示",
            "用户明确要求把本地结果整理成表格、清单、报告、文件、HTML、Markdown 或 GIF 动图",
            "用户说“把上面的/刚才的/上一条/这个结果画成流程图/表格/图示”等承接上一轮结果的展示要求",
            "用户要求对某个业务 skill 的结果做展示形态转换，而不是重新查询或重新诊断",
            "需要先调用本地业务 skill 获取可信信息，再让 DeepSeek 规划展示结构和产物",
        ),
        input_contract=(
            "用户原始需求",
            "来源业务 skill 名称",
            "本地业务 skill 输出的可信上下文",
            "已润色的基础回复",
        ),
        execution="DeepSeek 只生成产物计划和结构化内容；本地 Python 负责实际渲染图片、动图、表格或文件。它不是入口业务 skill，不能跳过原始查询/诊断能力直接编造资料。",
        limits=(
            "必须依赖上游业务 skill 的结果，不可脱离本地资料生成技术结论",
            "如果用户明确用“上面的/刚才的/这个”承接上一轮结果，应优先复用上一轮业务 skill 的可信结果，不要重新进入需求识别",
            "不能让 DeepSeek 直接读写工程文件或执行系统命令",
            "当前未实现的产物类型要优雅降级为卡片文本，不要让流程失败",
            "用户只问“流程”“状态跳转流程”“过程”“条件”“规则”时，默认只需要文字说明，不要生成图片",
            "只有用户明确说“画图”“流程图”“状态图”“图示”“用图表示”“表格”“报告”“文件”“GIF/动图”等展示形态时，才 should_transform=true",
            "面向用户的回复里不要出现 result_transformer、后处理 skill、展示生成 skill 这类内部术语",
        ),
        examples=(
            "把 ACC 状态下驱动系统状态跳转流程用流程图表示出来",
            "把这些故障条目整理成表格",
            "生成一张信号流 GIF",
            "把诊断结论导出成 Markdown 报告",
        ),
    ),
    AgentSkill(
        name=SKILL_SMALLTALK_CHAT,
        title="日常沟通",
        category=SKILL_CATEGORY_GENERAL,
        task_type=None,
        summary="处理打招呼、感谢、轻量咨询、日常沟通和非技术闲聊；不进入故障、性能或执行器流程。",
        when_to_use=(
            "用户只是打招呼、问在不在、表示感谢或简单寒暄",
            "用户在咨询机器人身份、怎么开始、有哪些能力、能做什么、可以怎样提问，但没有明确要求查文档或跑数据",
            "用户的问题不需要调用本地故障表、性能指标、执行器规则或上传文件分析能力",
        ),
        input_contract=(
            "自然语言短消息",
            "可以结合当前会话状态给一句友好的引导",
        ),
        execution="由 DeepSeek 直接生成自然、简短的群聊回复；如果用户顺带提出明确技术需求，应切换到对应查询或诊断 skill。",
        limits=(
            "不要把普通寒暄误判为执行器查询、性能分析或故障诊断",
            "能力介绍类问题不继承上一轮业务 skill，不要因为上一轮查过执行器就继续走执行器查询",
            "不要编造已经执行过的本地分析结果",
        ),
        examples=(
            "你好",
            "在吗",
            "你是谁",
            "怎么用你",
            "谢谢",
        ),
    ),
    AgentSkill(
        name=SKILL_FEEDBACK_REPORT,
        title="问题反馈记录",
        category=SKILL_CATEGORY_GENERAL,
        task_type=None,
        summary="当用户明确指出布鲁回复不对、没反应、用错能力、格式难看、结果异常等问题时，收集最近几轮同一用户同一群聊上下文，生成对话快照，并写入配置好的问题反馈多维表格。",
        when_to_use=(
            "用户用固定反馈语法，例如“反馈：...”“报错：...”“bug：...”“问题反馈：...”",
            "用户要求把当前对话问题、布鲁回复问题、没反应、错误结果、体验问题记录到反馈表",
            "用户明确说“把这段对话反馈一下”“记录这个问题”“提交问题反馈”",
        ),
        input_contract=(
            "反馈说明文本",
            "最近同一 open_id + chat_id 的用户与布鲁对话摘要",
            "可选：当前触发消息 id 和最近一次业务 skill 结果",
        ),
        execution="本地 Python 生成对话快照图片，使用 FEEDBACK_BASE_TOKEN / FEEDBACK_TABLE_ID 写入指定飞书多维表格；如配置了附件字段，则尝试把快照追加到附件字段。",
        limits=(
            "这不是业务诊断 skill，不要进入性能、执行器或故障分析流程",
            "未配置目标 Base 时，只能提示需要配置或先运行初始化脚本，不要静默丢失反馈",
            "不要把用户上传的大文件或分析产物长期保存到反馈记录里，只记录上下文摘要和快照",
        ),
        examples=(
            "反馈：刚才把执行器查询误判成性能分析了",
            "报错：上传 h5 后没有反应",
            "bug：这个回复格式里还有 Markdown 表格原文",
        ),
    ),
    AgentSkill(
        name=SKILL_FAULT_TABLE_SEARCH,
        title="故障表查询",
        category=SKILL_CATEGORY_QUERY,
        task_type=TASK_FAULT_DEGRADATION,
        summary="基于项目内 fault_tables/FDA3/FDA7 故障表回答文档咨询类问题，直接给出故障项、含义和排查方向。",
        when_to_use=(
            "用户问某功能要关注哪些 FDA3/FDA7/DID/故障码",
            "用户问某个 FDA3/FDA7 编号或故障码含义",
            "用户问环视/APA/ACC 相关故障有哪些",
            "用户问 0D7/1B6/1F0/1AD 等总线信号 miss/报文丢失可能原因",
            "用户问 FDA3/FDA7 故障表有多少项、来自哪个文档、依据什么判断或 FDA3/FDA7 分别是什么",
        ),
        input_contract=(
            "自然语言查询",
            "可以带功能名、报文 ID、故障码或上下文",
        ),
        execution="读取项目内 fault_tables 故障表索引并返回匹配条目，再交给 DeepSeek 用人话组织；找不到时基于已知线索给可操作排查方向，但不能编造故障编号，也不要暴露 Excel 原始列名或内部检索细节。",
        limits=(
            "FDA7 对应应用层故障，FDA3 对应底软故障",
            "故障表数量、来源文件、DID 字节到 FDA3/FDA7 的映射关系都属于本 skill 的查询范围",
            "这是查询能力，不要求用户上传日志",
        ),
        examples=(
            "针对环视功能，需要关注哪些 FDA3",
            "0D7/1B6 信号丢失可能是什么原因",
        ),
    ),
    AgentSkill(
        name=SKILL_DTC_CODE_SEARCH,
        title="DTC故障码查询",
        category=SKILL_CATEGORY_QUERY,
        task_type=TASK_FAULT_DEGRADATION,
        summary="基于项目内 dtc_tables/PS DTC.xlsx 和 dtc_tables/EBCM DTC.xlsx 查询驱动系统、制动系统相关 DTC 码、描述、内部编号、J1979 映射和所属表。",
        when_to_use=(
            "用户问制动系统、EBCM、ebooster、brake 相关 DTC 码含义、数量、描述或匹配项",
            "用户问驱动系统、PS、PDF、ECP、ECM、propulsion 相关 DTC 码含义、数量、描述或匹配项",
            "用户直接给出 P/C/B/U 开头的 DTC 码、Service DTC、CustomerDTCID、InternalDTCID 或故障描述关键词，希望查表解释",
            "用户问某一类 DTC 码有哪些、某个 DTC 属于制动还是驱动、DTC 表里是否有某个条目",
        ),
        input_contract=(
            "自然语言查询",
            "可带系统范围：制动 / 驱动 / EBCM / ebooster / PS / PDF / ECP / ECM",
            "可带 DTC 码、内部 ID、J1979 码、英文故障描述关键词",
        ),
        execution="读取 dtc_tables 下的 DTC Excel 表，自动识别表头并做全字段检索；本地返回命中条目和表格统计，再交给 DeepSeek 组织成用户能看懂的查询回复。",
        limits=(
            "这是 DTC 表查询能力，不等于 FDA3/FDA7 故障表查询；FDA7/FDA3 仍走 fault_table_search 或 fault_diagnosis",
            "PS DTC 表按驱动系统理解，EBCM DTC 表按制动系统理解",
            "没有直接命中时只能说明未命中，并给出可继续检索的关键词或系统范围，不要编造 DTC 条目",
            "不要要求用户上传日志；只有用户明确给日志/字节串做诊断时才切到 fault_diagnosis",
        ),
        examples=(
            "C0502.00 是什么故障",
            "制动系统 DTC 里有没有轮速传感器开路",
            "驱动系统 DTC 表里 WSS_OPEN_FRONT_LEFT 对应什么码",
            "EBCM DTC 一共有多少项",
        ),
    ),
    AgentSkill(
        name=SKILL_FAULT_DIAGNOSIS,
        title="故障降级诊断",
        category=SKILL_CATEGORY_DIAGNOSTIC_ANALYSIS,
        task_type=TASK_FAULT_DEGRADATION,
        summary="基于项目内 fault_tables 解析用户给出的 slog、DID、fault_event、FDA3/FDA7 索引或整段诊断字节，定位故障原因。",
        when_to_use=(
            "用户贴 fault_event 日志行",
            "用户贴 62 FD A3 / 62 FD A7 响应字节串",
            "用户上传 slog 或 DID 日志文件",
            "用户直接查询 FDA3[n] / FDA7[n] / byte bit",
        ),
        input_contract=(
            "文本故障输入",
            "或 slog/DID 文件附件",
        ),
        execution="下载或解析输入后调用故障诊断解析器，基于项目内 fault_tables/FDA3/FDA7 输出诊断结论，再交给 DeepSeek 组织成可读的工程解释。",
        limits=(
            "故障降级诊断目前只支持 J6B 项目",
            "8650 和 BMC 项目仍在开发中",
            "FDA7 对应应用层故障，FDA3 对应底软故障",
        ),
        examples=(
            "FDA7[62] 是什么故障",
            "解析这段 62 FD A7 ...",
        ),
    ),
    AgentSkill(
        name=SKILL_PERFORMANCE_CAPABILITY_SEARCH,
        title="性能分析能力查询",
        category=SKILL_CATEGORY_QUERY,
        task_type=TASK_PERFORMANCE_ANALYSIS,
        summary="基于本地性能问题预设表、指标说明和指标实现摘要回答性能分析能识别哪些问题、每个问题类型该看哪些信号、每个指标的规则/阈值/信号、以及按功能范围列指标表。",
        when_to_use=(
            "用户问性能分析支持哪些具体问题",
            "用户问 ACC/稳定性控制可以看哪些指标",
            "用户问某个性能指标的具体规则、判定条件、阈值、枚举或需要哪些信号",
            "用户希望列出总的指标表、每个指标规则表，或某个功能相关的指标表",
            "用户问顿挫、重制动、误制动、溜车、稳速未达 Setspeed、不合理低速、跟车距离等问题类型分别关注哪些实车/算法请求/执行器请求信号",
            "用户问性能问题类型对应哪些关联问题筛选代码，或让列一张性能问题预设表",
            "用户问性能分析指标实现、指标说明、method、parameters 或事件输出字段",
            "用户只是了解能力，不是在上传数据分析",
        ),
        input_contract=(
            "自然语言能力咨询",
            "可带 ACC、ABS、TCS、VSE 等范围词",
            "可带具体指标名称、问题现象、问题类型、规则/阈值/信号/表格等查询意图",
        ),
        execution="先读取 config/performance_problem_presets.xlsx 这一张可维护预设表，再检索 METRIC_DESCRIPTIONS、PROBLEM_GROUP_DEFINITIONS 和 analysis_engine/metrics/code 指标实现，提取 REQUIRED_SIGNALS、DEFAULT 阈值、枚举、method、parameters、事件字段和伪代码后交给 DeepSeek 组织回复。永远不要把源码片段直接发给用户。",
        limits=(
            "只回答能力和条目，不进入等待上传文件",
            "性能分析不受 J6B/8650/BMC 限制",
        ),
        examples=(
            "性能分析可以帮我识别哪些问题",
            "ACC 具体有哪些指标",
            "每个指标具体规则是什么，列个表",
            "ACC 急减速/重刹的判定阈值是什么",
            "重制动需要看哪些算法请求和执行器响应信号",
        ),
    ),
    AgentSkill(
        name=SKILL_PERFORMANCE_ANALYSIS_SETUP,
        title="性能问题分析",
        category=SKILL_CATEGORY_DIAGNOSTIC_ANALYSIS,
        task_type=TASK_PERFORMANCE_ANALYSIS,
        summary="在用户明确要求基于上传数据跑性能指标时，配置性能问题分析任务并等待或接收 .blf/.h5/.hdf5 数据。",
        when_to_use=(
            "用户明确说要上传或已经上传 blf/h5/hdf5，并要求跑性能问题分析",
            "用户明确说“分析数据中的 ACC 重刹/稳定性控制等性能问题”",
            "用户明确说要基于某个数据库版本和某个数据文件跑性能指标",
            "用户明确要求按性能分析引擎检查顿挫、重制动、误制动、溜车、稳速未达 Setspeed、不合理低速、跟车距离等现象",
        ),
        input_contract=(
            "可选数据库名或版本",
            "可选问题组 stability_control / acc_control",
            "可选问题类型：顿挫 / 重制动 / 误制动 / 溜车 / 稳速未达 Setspeed / 不合理低速 / 跟车距离",
            "分析文件 .blf/.h5/.hdf5",
        ),
        execution="选择数据库，记录用户需求；分析前读取并校验 config/performance_problem_presets.xlsx，根据用户描述的问题类型筛选关联指标，再下载数据文件并调用性能分析引擎。",
        limits=(
            "用户未指定数据库时默认使用最新数据库",
            "如果用户上传新的数据库，必须是 .arxml 文件并保存到 database",
            "用户只是问规则、指标、能看哪些问题、关注哪些信号时，不要进入本 skill；当前 performance_capability_search 已关闭，应按能力开关提示入口暂不开放",
            "用户只是笼统说“帮我看看/分析一下/有没有问题”时不要直接进入本 skill，应先澄清或走查询",
        ),
        examples=(
            "帮我分析 ACC 重制动，用最新数据库",
        ),
    ),
    AgentSkill(
        name=SKILL_ACTUATOR_REFERENCE_SEARCH,
        title="执行器查询",
        category=SKILL_CATEGORY_QUERY,
        task_type=TASK_ACTUATOR_HANDSHAKE,
        summary="基于执行器接口原始文档和机器可读规则，回答制动、驱动、转向、换挡执行器握手、降级、状态闭环、接口变更和需求合理性评审相关的资料查询。",
        when_to_use=(
            "用户问导致制动/驱动/转向/换挡断握手有哪些原因",
            "用户问 EBCM/ebooster/PDF/ECP/ECM/EPS/ETRS 相关接口信号",
            "用户问 AEB/ACC 制动没有响应、驱动不响应一般要看哪些条件",
            "用户问方向盘、转向、EPS、转角叠加、转向扭矩叠加相关接口条件或信号置位",
            "用户问换挡、挡位、ETRS、P/R/N/D、APA 目标挡位请求相关接口条件或信号置位",
            "用户问某功能场景下执行器握手需要满足哪些条件",
            "用户问某信号在握手、降级、激活、退出等状态下应该如何置位或取值",
            "用户问文档中的某个特定条件、规则、例子、状态机跳转或设计要求",
            "用户提出执行器接口变更、行泊切换、超时降级、刹停/拉 EPB 等新需求，希望评估需求合理性、实施关注点或风险点",
            "用户要求结合现有 EBCM/PS/EPS/ETRS 接口文档评审一个执行器相关方案，而不是上传数据跑诊断",
            "用户问执行器/制动/驱动在 ACC/APA/AEB 等场景下的流程、状态流转、状态跳转或流程图，但没有上传数据或要求跑数据",
            "用户没有上传数据，只是在查文档、规则、原因、条件、关键信号或设计要求",
        ),
        input_contract=(
            "自然语言规则查询",
            "可带系统对象、功能场景、信号名或问题现象",
        ),
        execution="查询 ADAS_interface_machine_readable_rules 和执行器原始文档整理结果；普通查询返回相关原因、条件和关键信号，需求评审类查询返回合理性判断、现有文档支撑点、文档缺口、实施关注点和风险点。",
        limits=(
            "制动执行器/制动接口同义词包括 EBCM、ebooster、brake；AEB/CIB 不是制动系统，只能作为功能场景或制动请求来源理解",
            "驱动系统同义词包括 PDF、ECP、ECM",
            "转向系统同义词包括 EPS、steering、方向盘、转向、转角叠加、转向扭矩叠加",
            "换挡系统同义词包括 ETRS、shift、gear、range、换挡、换档、挡位、档位",
            "这是查询能力，不要求用户上传总线数据",
            "需求评审类问题要优先分析合理性、文档支撑和风险，不要只罗列信号；如果文档没有直接定义某个 flag、阈值或状态，必须明确标成文档缺口",
            "不要因为用户提到“握手/流程图/状态跳转”就进入执行器分析；只有用户明确说上传数据、这段数据、跑数据或从数据里识别问题时才进入执行器分析",
        ),
        examples=(
            "导致制动断握手的有哪些原因",
            "驱动系统 ACC 握手失败要看哪些信号",
            "APA 下 EPS 转向握手需要看哪些信号",
            "APA 换挡控制激活时 ETRS 应该怎么响应",
            "ACC 下驱动握手需要满足哪些条件",
            "某个信号在握手状态下应该怎么置位",
            "评审一下 ACC/HPP 改用行车制动接口这个需求是否合理，需要注意哪些风险",
        ),
    ),
    AgentSkill(
        name=SKILL_ACTUATOR_HANDSHAKE_ANALYSIS,
        title="执行器分析",
        category=SKILL_CATEGORY_DIAGNOSTIC_ANALYSIS,
        task_type=TASK_ACTUATOR_HANDSHAKE,
        summary="基于执行器文档和整理好的规则包，对用户上传的总线数据识别断握手、降级、状态未按设计闭环等问题点。",
        when_to_use=(
            "用户上传 BLF/H5/HDF5 总线数据要求检查执行器响应",
            "用户说分析这段 AEB/ACC/APA 数据里制动或驱动为什么没有响应",
            "用户要求从数据里判断断握手、功能降级、状态未闭环、不符合设计需求",
            "用户已经处于等待执行器分析文件阶段并继续补充上传/数据相关说明",
        ),
        input_contract=(
            ".blf/.h5/.hdf5 总线数据",
            "BLF 会结合当前 ARXML 数据库解码",
            "可选数据库版本或新上传 .arxml 数据库",
        ),
        execution="下载总线数据，结合数据库和 ADAS_interface_machine_readable_rules 跑规则诊断，返回问题点、证据信号、时间窗和图示。",
        limits=(
            "制动执行器/制动接口同义词包括 EBCM、ebooster、brake；AEB/CIB 不是制动系统，只能作为功能场景或制动请求来源理解",
            "驱动系统同义词包括 PDF、ECP、ECM",
            "没有数据时进入等待上传，不直接编造诊断结论",
        ),
        examples=(
            "帮我分析为什么这段 AEB 数据里面制动没有响应",
            "上传 blf 后检查执行器是否断握手",
        ),
    ),
    AgentSkill(
        name=SKILL_FUNCTION_EXIT_ANALYSIS,
        title="ACC/LCC功能退出原因分析",
        category=SKILL_CATEGORY_DIAGNOSTIC_ANALYSIS,
        task_type=TASK_FUNCTION_EXIT_ANALYSIS,
        summary="查询或分析 ACC/LCC 功能退出原因。无文件时读取 config/acc_lcc_exit_reason_rules.xlsx 回答退出原因、退出码、退出条件和配置里是否包含某类退出；明确基于数据时再等待 H5 并定位退出点。",
        when_to_use=(
            "用户问 ACC/LCC 功能退出原因、退出原因码、退出码、退出条件、抑制原因、临时退出，或问行车问题原因分析里是否定义了某个功能退出原因",
            "用户问当前功能退出配置里有没有某个原因，例如轮速方向反向 reverse 是否会导致 ACC/LCC 退出",
            "用户明确要求基于上传数据、H5/HDF5、这段数据分析 ACC/LCC 功能退出原因、为什么退出、退出点、退出码或抑制原因",
            "用户说要分析这段数据里 ACC/LCC 退出、临时退出、功能退出、巡航退出、车道居中退出的原因",
            "用户已经处于等待 ACC/LCC 功能退出分析文件阶段并继续上传 H5/HDF5 数据",
        ),
        input_contract=(
            "无文件查询：用户的文字问题，以及 config/acc_lcc_exit_reason_rules.xlsx",
            "数据分析：.h5/.hdf5 总线数据",
            "可选：只看 ACC 或只看 LCC",
            "可选：用户指定的时间窗、退出点或退出现象描述",
        ),
        execution=(
            "无文件查询时直接读取 config/acc_lcc_exit_reason_rules.xlsx，回答配置中定义了哪些退出原因、某个原因是否已覆盖、还缺什么信号或定义；"
            "明确基于 H5 数据时，匹配 H5 信号，定位激活态退出跳变，并解码退出原因、抑制条件和关键证据。"
        ),
        limits=(
            "当前不直接解析 BLF；BLF 需要先解码成 H5",
            "用户只是问 ACC/LCC 非退出相关的状态流程、条件、信号置位或设计要求时，应走 actuator_reference_search，不进入等待上传",
            "用户问顿挫、重制动、误制动、跟车距离等行车现象原因时，应走 root_cause_analysis",
            "信号缺失时只能说明缺证据，不要编造退出原因",
        ),
        examples=(
            "ACC/LCC 功能退出原因有哪些",
            "行车问题原因分析里定义的 ACC 功能退出原因，有没有因为轮速方向反向 reverse 导致的",
            "上传 h5，帮我分析这段 ACC 为什么退出",
            "基于这段数据看一下 LCC 临时退出原因",
            "这个 H5 里 ACC 退出点和退出码帮我找一下",
        ),
    ),
    AgentSkill(
        name=SKILL_BATCH_ROOT_CAUSE_ANALYSIS,
        title="批量行车问题原因分析",
        category=SKILL_CATEGORY_DIAGNOSTIC_ANALYSIS,
        task_type=TASK_BATCH_ROOT_CAUSE_ANALYSIS,
        summary="固定指令“分析飞书多维表格中的问题”触发后，读取配置好的飞书多维表格，只处理待分析记录；根据布鲁运行机器/服务器上的绝对数据路径和问题类型调用行车问题原因分析或 ACC/LCC 功能退出分析，并把状态、概要、完整结论和信号图示回写到表格。",
        when_to_use=(
            "用户发送固定指令：分析飞书多维表格中的问题",
        ),
        input_contract=(
            "已配置 BATCH_ROOT_CAUSE_BASE_TOKEN 和 BATCH_ROOT_CAUSE_TABLE_ID",
            "多维表格关键字段：Agent_问题描述、Agent_h5数据路径、Agent_问题类型、Agent_问题状态、Agent_结论概要、Agent_分析结论、Agent_分析信号图示",
            "只有 Agent_问题状态 为待分析才会处理；空白状态不会处理；处理中写为分析中，成功后写为已分析，失败后写为分析失败",
            "Agent_问题类型必须来自 config/performance_problem_presets.xlsx 当前覆盖的问题类型；额外支持功能退出",
            "Agent_h5数据路径必须是布鲁运行机器/服务器上的 .h5 绝对路径；不支持 .blf/.hdf5、填表人电脑本地路径、相对路径或只填文件名",
            "表格可以增加其他业务列，但不能改名或删除上述 Agent_ 关键字段",
        ),
        execution=(
            "本地 Python 读取飞书多维表格记录；只对 Agent_问题状态 为待分析的行，按 Agent_问题类型 调用行车问题原因分析；"
            "功能退出行调用 function_exit_analysis；将一句话概要写入 Agent_结论概要，完整结论写入 Agent_分析结论，"
            "将生成的信号图上传到 Agent_分析信号图示，并用飞书进度卡显示当前处理进度。"
        ),
        limits=(
            "这是批量入口，不处理用户直接上传的单个文件；单个文件仍走 root_cause_analysis",
            "只有固定指令“分析飞书多维表格中的问题”可以直接触发本 skill；其他关于批量表格、Base 或回写的说法只做咨询或澄清，不直接执行",
            "不会修改或删除表格中的原始 h5 路径文件，只会清理本次生成的临时图示",
            "Base 未配置或飞书权限不足时必须明确提示，不要静默失败",
            "不处理 Agent_问题状态 不是待分析的记录；用户要重跑时，应先把该行 Agent_问题状态 改回待分析",
            "不会根据文件名自动搜索数据文件；表格路径不存在时直接标记失败并提示修正为服务器绝对路径",
            "批量入口中的所有问题类型都只支持 .h5；.blf/.hdf5 需要先转换成标准 H5",
        ),
        examples=(
            "分析飞书多维表格中的问题",
        ),
    ),
    AgentSkill(
        name=SKILL_AXLE_TORQUE_ACCURACY_ANALYSIS,
        title="轴端扭矩准确性校验",
        category=SKILL_CATEGORY_DIAGNOSTIC_ANALYSIS,
        task_type=TASK_AXLE_TORQUE_ACCURACY,
        summary="轴端扭矩准确性校验入口。必须先让用户选择车型编号和 Final Ratio；车型/Final Ratio 确认后，才提示上传 .blf 总线日志，再结合 ECM/CCB ARXML 复算轴端扭矩并与 IActAxleTrq 对比。",
        when_to_use=(
            "用户上传 BLF，要求判断轴端扭矩、轴扭、ActAxleTrq 是否准确",
            "用户说轴扭不准、轴扭较准、校验轴端扭矩、复算轴端扭矩、发动机扭矩到轴端扭矩",
            "用户希望用最新 ARXML 解 BLF，并输出计算扭矩、实车扭矩、误差和异常时间窗口",
        ),
        input_contract=(
            "交互顺序不能变：第一步只提示用户选择车型编号/Final Ratio，不要同时要求上传 BLF；第二步在车型/Final Ratio 确认后，才提示上传 .blf 文件",
            "每次发起新的轴端扭矩校验都必须重新选择车型/Final Ratio，不能继承上一轮 session 的车型，也不能在首轮文本里识别到车型后直接跳过确认",
            "车型选项必须这样展示：1. 358-2（Final Ratio 3.47）；2. C1UL（Final Ratio 3.47）；3. E2LB-2（Final Ratio 3.17）；4. 其他（需输入 Final Ratio）",
            "如果用户选择 4 或其他车型，必须先提示输入 Final Ratio，并列出已有车型的 Final Ratio 值；收到 Final Ratio 后才提示上传 BLF",
            "一个 .blf 文件只能在车型/Final Ratio 确认后再要求用户上传；用户提前上传 BLF 时也要先回到车型/Final Ratio 确认",
            "一个 ECM/CCB ARXML；不指定时优先使用 database 目录中的 Clea_Mode_Year23_ECM_V27.80.1.arxml 或更新 ECM ARXML",
            "可选环境变量 AXLE_TORQUE_OUTPUT_SCALE；当前默认 1，IActAxleTrq 已确认为真实轴端扭矩，不做 0.5 折算",
        ),
        execution=(
            "首轮只生成车型/Final Ratio 选择提示；不要把 .blf 放在首轮待办里。"
            "本地 Python 复用 FileConverter/blf_to_mdf.py 的 BLF 解析、通道路由和信号解码函数，只解目标报文 071/073/0CB/0C8/0D7/076/078/08D，"
            "再按 TC Ratio、实际档位速比、车型主减速比和传动效率复算轴端扭矩；"
            "输出 MAE、P95、通过率、异常窗口、信号来源和轴端扭矩图示；说明书只定义复算公式和输入边界，不定义准/不准阈值。"
        ),
        limits=(
            "当前只判断 D1-D9 前进档和 R 档；R 档编码 14，i=2.96，η=1，按正值参与复算；D10、P/N、无有效档位样本不做准度判定",
            "动力流状态优先来自 078；若 BLF 为 ICE 变体，可使用 076 中的 TEPI_TrnsEngdPrFlwSt_ICE / V_ICE；两者都缺失时必须提示证据不足，不能退化为按档位和有效性信号门控",
            "如果 ECM ARXML 缺失或 0CB 涡轮转速信号解不出，只能提示证据不足，不能编造结论",
            "这不是 BLF 转 MDF 文件交付能力；如果用户只是要 mdf 文件，应走 blf_to_mdf_conversion",
        ),
        examples=(
            "核对轴扭准确性",
            "校验轴端扭矩准不准",
            "上传 blf，帮我判断轴端扭矩准不准",
            "用最新 arxml 解析这个 blf，校验 IActAxleTrq",
            "轴扭较准和轴扭不准两段数据帮我验证一下",
        ),
    ),
    AgentSkill(
        name=SKILL_BLF_TO_MDF_CONVERSION,
        title="BLF 转 MDF",
        category=SKILL_CATEGORY_FILE_CONVERSION,
        task_type=TASK_BLF_TO_MDF_CONVERSION,
        summary="把用户上传的 .blf 总线日志按 .arxml 数据库解析并转换成未压缩 .mdf 文件，再把转换结果回传给用户。",
        when_to_use=(
            "用户明确要求把 BLF 转成 MDF、导出 MDF、生成 MDF 或做 BLF/MDF 格式转换",
            "用户说手头有 .blf，希望转成 .mdf 后给其他工具使用",
            "用户指定或上传 .arxml 数据库，只是为了完成 BLF 解码转换，不是在做问题原因分析",
        ),
        input_contract=(
            "一个 .blf 文件",
            "一个 .arxml 数据库；不指定时默认使用 Clea_Mode_Year23_ADM_HC_V27.80.0.arxml",
            "用户可以先上传新的 .arxml，也可以说明使用 Agent database 目录中已有的数据库",
        ),
        execution=(
            "调用工程内 FileConverter/blf_to_mdf.py 完成转换",
            "先根据 CAN ID、DLC、帧签名和 Cluster 覆盖率，把 BLF 物理通道映射到 ARXML 逻辑网络",
            "同一个 CAN ID 只使用当前物理通道所属逻辑网络的帧定义解码，不跨网络猜测或复用",
            "每个逻辑信号只输出一次，不按物理通道复制信号，也不添加 CAN0/CAN1/CAN6 前缀",
            "MDF 内容默认不压缩；如果飞书直传受限，仅在传输层打包 ZIP，解压后仍是未压缩 MDF",
            "转换成功并回传后，清理本次上传的 BLF 和生成的临时产物",
        ),
        limits=(
            "这只是文件格式转换，不输出问题诊断结论",
            "只接受 .blf 输入；如果用户上传 h5/hdf5，应提示该能力不处理 H5",
            "如果默认数据库不存在，必须提示用户指定或上传 .arxml",
            "如果用户问的是数据中为什么出问题、顿挫/重制动/退出原因，不要选本 skill，应选对应分析 skill",
        ),
        examples=(
            "帮我把这个 blf 转成 mdf",
            "用 27.80.0 HC 数据库把 blf 导出 mdf",
            "我上传一个 blf，帮我生成 mdf 文件",
        ),
    ),
    AgentSkill(
        name=SKILL_ROOT_CAUSE_ANALYSIS,
        title="行车问题原因分析",
        category=SKILL_CATEGORY_DIAGNOSTIC_ANALYSIS,
        task_type=TASK_ROOT_CAUSE_ANALYSIS,
        summary="对同一段总线数据同时调用性能问题分析和执行器分析，并补充读取算法请求侧、算法请求诱因与执行器响应侧信号，形成现象层、请求侧、请求诱因、响应侧的原因证据链。",
        when_to_use=(
            "用户上传或准备上传 .h5 数据，希望分析某个现象是什么原因导致",
            "用户明确问是执行器响应问题、制动/驱动响应问题，还是算法控制侧/策略侧问题",
            "用户希望定位顿挫、重制动、不合理低速、误制动、漏制动、未达到 set speed、溜车、跟车距离异常等现象的根因",
            "用户说要把性能问题和执行器响应放在一起看，或说两个都分析、综合判断、帮我分析原因或归因",
            "用户想判断算法请求本身是否异常，还是执行器没有按请求响应",
            "用户没有明确只要性能指标结果或只要执行器接口结果，而是要解释这段数据为什么出现该现象",
        ),
        input_contract=(
            ".h5 总线数据",
            "如果原始数据是 .blf、.hdf5 或其他格式，需要先转换成标准 .h5",
            "自然语言问题现象，例如顿挫、重制动、误制动、漏制动、未达到 set speed、溜车、跟车距离异常",
        ),
        execution=(
            "先跑性能问题分析，再跑执行器分析；本地原因分析层汇总两侧证据，输出结论、证据时间窗、"
            "执行器侧/算法侧判断和后续排查建议。输出必须回答用户真正关心的“为什么”："
            "现象是否成立、同一时间窗内请求侧是否主动给出目标/扭矩/制动类型、执行器响应是否闭环、"
            "如果请求侧主动给出请求，还要继续看跟车目标、Cut-in 目标、曲率限速、SetSpeed 等场景输入是否能解释请求原因；"
            "当前更像请求侧触发还是响应侧异常，以及还缺哪些关键信号。最终表述要先用自然语言给判断，再展开证据，不要只堆字段。"
        ),
        limits=(
            "不是替代性能分析或执行器分析，而是编排二者并做原因分析汇总",
            "如果用户明确只问性能指标规则，当前 performance_capability_search 已关闭，应提示能力入口暂不开放；明确只查执行器文档，走 actuator_reference_search",
            "没有上传数据时进入等待上传，不编造数据结论",
            "不要把“匹配到几个信号”当成结论；匹配数量只能作为覆盖状态，必须结合时间窗和值变化解释因果链",
            "所有现象归因都必须先检查该现象对应的关键现象层、请求侧、请求诱因和响应侧信号是否齐全；关键量缺失时只能输出“现象成立但归因证据不足，需要补充信号”，不能用其他非关键匹配信号代替",
            "顿挫、重制动、稳速未达 Setspeed、不合理低速、误制动等场景必须在请求侧成立后继续检查目标、曲率限速和设速变化；请求侧先突变且诱因成立则偏算法按场景主动请求，响应侧无请求却激活或响应异常则偏执行器/接口",
            "用户可见结论要拟人化：例如“我会先从请求侧查起”“现在证据还差响应侧，先别定根因”，不要输出机械的字段说明。",
        ),
        examples=(
            "帮我分析这段数据为什么会顿挫，是执行器还是算法侧",
            "这段 ACC 重制动帮我分析原因，看是不是制动响应导致",
            "上传 h5，分析未达到 set speed 是控制侧还是执行器侧问题",
            "这个跟车距离异常，把性能和执行器都看一下",
        ),
    ),
)


SKILLS_BY_NAME = {skill.name: skill for skill in AGENT_SKILLS}
VALID_AGENT_SKILLS = tuple(SKILLS_BY_NAME)
VALID_TASK_TYPES = tuple(TASK_TYPE_LABELS)
QUERY_SKILLS = tuple(skill.name for skill in AGENT_SKILLS if skill.category == SKILL_CATEGORY_QUERY)
DIAGNOSTIC_ANALYSIS_SKILLS = tuple(
    skill.name for skill in AGENT_SKILLS if skill.category == SKILL_CATEGORY_DIAGNOSTIC_ANALYSIS
)
FILE_CONVERSION_SKILLS = tuple(
    skill.name for skill in AGENT_SKILLS if skill.category == SKILL_CATEGORY_FILE_CONVERSION
)


_CAPABILITY_CACHE_MTIME: float | None = None
_CAPABILITY_CACHE: dict[str, dict[str, object]] = {}
_TRUE_VALUES = {"1", "true", "yes", "y", "on", "enable", "enabled", "是", "启用", "开启", "打开"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "disable", "disabled", "否", "停用", "禁用", "关闭"}


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _config_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    text = _cell_text(value).lower()
    if not text:
        return default
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return default


def load_agent_capability_config() -> dict[str, dict[str, object]]:
    """Read the optional Excel capability switch table.

    Missing or unreadable config means all registered skills stay enabled. The
    cache is keyed by file mtime so editing the table can take effect without a
    code change; restarting the bot is still the safest operational path.
    """

    global _CAPABILITY_CACHE_MTIME, _CAPABILITY_CACHE
    if not CAPABILITY_CONFIG_PATH.exists():
        _CAPABILITY_CACHE_MTIME = None
        _CAPABILITY_CACHE = {}
        return {}
    try:
        mtime = CAPABILITY_CONFIG_PATH.stat().st_mtime
        if _CAPABILITY_CACHE_MTIME == mtime:
            return _CAPABILITY_CACHE
        from openpyxl import load_workbook

        workbook = load_workbook(CAPABILITY_CONFIG_PATH, read_only=True, data_only=True)
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            _CAPABILITY_CACHE_MTIME = mtime
            _CAPABILITY_CACHE = {}
            return {}
        header = [_cell_text(value).lower() for value in rows[0]]
        try:
            name_idx = header.index("skill_name")
        except ValueError:
            name_idx = 0
        enabled_idx = header.index("enabled") if "enabled" in header else -1
        visible_idx = header.index("visible") if "visible" in header else -1
        config: dict[str, dict[str, object]] = {}
        for row in rows[1:]:
            if not row:
                continue
            skill_name = _cell_text(row[name_idx] if name_idx < len(row) else "")
            if not skill_name:
                continue
            if skill_name not in SKILLS_BY_NAME:
                continue
            enabled_value = row[enabled_idx] if 0 <= enabled_idx < len(row) else True
            visible_value = row[visible_idx] if 0 <= visible_idx < len(row) else True
            config[skill_name] = {
                "enabled": _config_bool(enabled_value, True),
                "visible": _config_bool(visible_value, True),
            }
        _CAPABILITY_CACHE_MTIME = mtime
        _CAPABILITY_CACHE = config
        return config
    except Exception:
        return {}


def is_skill_enabled(name: str | None) -> bool:
    if not name:
        return False
    if name not in SKILLS_BY_NAME:
        return False
    config = load_agent_capability_config().get(name, {})
    return bool(config.get("enabled", True))


def is_skill_visible(name: str | None) -> bool:
    if not is_skill_enabled(name):
        return False
    if name in LOCAL_ONLY_AGENT_SKILLS:
        return False
    config = load_agent_capability_config().get(name or "", {})
    return bool(config.get("visible", True))


def enabled_agent_skills(include_hidden: bool = False) -> tuple[AgentSkill, ...]:
    return tuple(
        skill
        for skill in AGENT_SKILLS
        if is_skill_enabled(skill.name) and (include_hidden or is_skill_visible(skill.name))
    )


def valid_agent_skill_names(include_hidden: bool = True) -> tuple[str, ...]:
    return tuple(skill.name for skill in enabled_agent_skills(include_hidden=include_hidden))


def get_skill(name: str | None) -> AgentSkill | None:
    return SKILLS_BY_NAME.get(name or "")


def skill_title(name: str | None) -> str:
    skill = get_skill(name)
    return skill.title if skill else (name or "")


def skill_task_type(name: str | None) -> str | None:
    skill = get_skill(name)
    return skill.task_type if skill else None


def task_type_options() -> list[tuple[str, str]]:
    return list(TASK_TYPE_LABELS.items())


PUBLIC_CAPABILITY_CATEGORY_LABELS = {
    SKILL_CATEGORY_QUERY: "资料查询",
    SKILL_CATEGORY_DIAGNOSTIC_ANALYSIS: "数据诊断与原因分析",
    SKILL_CATEGORY_FILE_CONVERSION: "文件转换",
    SKILL_CATEGORY_POSTPROCESS: "结果展示",
    SKILL_CATEGORY_GENERAL: "日常协作",
}

PUBLIC_CAPABILITY_CATEGORY_ORDER = (
    SKILL_CATEGORY_QUERY,
    SKILL_CATEGORY_DIAGNOSTIC_ANALYSIS,
    SKILL_CATEGORY_FILE_CONVERSION,
    SKILL_CATEGORY_POSTPROCESS,
    SKILL_CATEGORY_GENERAL,
)


def _capability_sheet_rows() -> list[dict[str, str]]:
    """Return enabled + visible public capability rows from agent_capabilities.xlsx."""

    if not CAPABILITY_CONFIG_PATH.exists():
        return []
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(CAPABILITY_CONFIG_PATH, read_only=True, data_only=True)
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
    except Exception:
        return []
    if not rows:
        return []
    header = [_cell_text(value).lower() for value in rows[0]]

    def idx(name: str) -> int:
        return header.index(name) if name in header else -1

    name_idx = idx("skill_name")
    enabled_idx = idx("enabled")
    visible_idx = idx("visible")
    category_idx = idx("category")
    task_type_idx = idx("task_type")
    title_idx = idx("title")
    scope_idx = idx("user_visible_scope")
    notes_idx = idx("notes")
    if name_idx < 0:
        return []

    public_rows: list[dict[str, str]] = []
    for row in rows[1:]:
        if not row:
            continue
        skill_name = _cell_text(row[name_idx] if name_idx < len(row) else "")
        skill = SKILLS_BY_NAME.get(skill_name)
        if not skill:
            continue
        enabled_value = row[enabled_idx] if 0 <= enabled_idx < len(row) else True
        visible_value = row[visible_idx] if 0 <= visible_idx < len(row) else True
        if not _config_bool(enabled_value, True):
            continue
        if not _config_bool(visible_value, True):
            continue
        if skill_name in LOCAL_ONLY_AGENT_SKILLS:
            continue
        public_rows.append(
            {
                "skill_name": skill_name,
                "category": _cell_text(row[category_idx] if 0 <= category_idx < len(row) else "")
                or skill.category,
                "task_type": _cell_text(row[task_type_idx] if 0 <= task_type_idx < len(row) else "")
                or (skill.task_type or ""),
                "title": _cell_text(row[title_idx] if 0 <= title_idx < len(row) else "") or skill.title,
                "scope": _cell_text(row[scope_idx] if 0 <= scope_idx < len(row) else "") or skill.summary,
                "notes": _cell_text(row[notes_idx] if 0 <= notes_idx < len(row) else ""),
            }
        )
    return public_rows


def public_agent_capabilities() -> tuple[dict[str, str], ...]:
    rows = _capability_sheet_rows()
    if rows:
        return tuple(rows)
    return tuple(
        {
            "skill_name": skill.name,
            "category": skill.category,
            "task_type": skill.task_type or "",
            "title": skill.title,
            "scope": skill.summary,
            "notes": "",
        }
        for skill in enabled_agent_skills(include_hidden=False)
    )


def build_public_capability_text(problem_groups: list[str] | None = None, task_type: str | None = None) -> str:
    rows = list(public_agent_capabilities())
    if not rows:
        return "我现在没有读取到已开放的能力配置，请先检查 config/agent_capabilities.xlsx。"

    lines = ["我会先理解你的需求，再自动选择合适能力。当前对外开放的是："]
    category_index = 1
    for category in PUBLIC_CAPABILITY_CATEGORY_ORDER:
        category_rows = [row for row in rows if row.get("category") == category]
        if not category_rows:
            continue
        lines.append("")
        lines.append(f"{category_index}. {PUBLIC_CAPABILITY_CATEGORY_LABELS.get(category, category)}")
        category_index += 1
        for row in category_rows:
            title = row.get("title") or row.get("skill_name") or "未命名能力"
            scope = (row.get("scope") or "").strip()
            detail = scope.rstrip("。")
            if detail:
                lines.append(f"- {title}：{detail}。")
            else:
                lines.append(f"- {title}")

    if problem_groups or task_type:
        lines.append("")
        lines.append("如果你刚才问的是某个具体场景，我会按已开放能力继续判断；被配置为关闭或隐藏的入口不会被展示。")
    else:
        lines.append("")
        lines.append("你直接说想查什么、分析什么，或者按对应能力上传文件即可；配置表里关闭或隐藏的能力不会作为入口展示。")
    return "\n".join(lines)


def format_skill_enum_for_prompt() -> str:
    return " | ".join(f'"{name}"' for name in valid_agent_skill_names(include_hidden=False))


def format_task_type_enum_for_prompt() -> str:
    return " | ".join(f'"{name}"' for name in VALID_TASK_TYPES if name not in LOCAL_ONLY_TASK_TYPES)


def format_skills_for_prompt() -> str:
    skills_for_prompt = enabled_agent_skills(include_hidden=False)
    general_blocks = [
        skill.prompt_block(index)
        for index, skill in enumerate(
            (skill for skill in skills_for_prompt if skill.category == SKILL_CATEGORY_GENERAL),
            start=1,
        )
    ]
    query_blocks = [
        skill.prompt_block(index)
        for index, skill in enumerate(
            (skill for skill in skills_for_prompt if skill.category == SKILL_CATEGORY_QUERY),
            start=1,
        )
    ]
    analysis_blocks = [
        skill.prompt_block(index)
        for index, skill in enumerate(
            (skill for skill in skills_for_prompt if skill.category == SKILL_CATEGORY_DIAGNOSTIC_ANALYSIS),
            start=1,
        )
    ]
    conversion_blocks = [
        skill.prompt_block(index)
        for index, skill in enumerate(
            (skill for skill in skills_for_prompt if skill.category == SKILL_CATEGORY_FILE_CONVERSION),
            start=1,
        )
    ]
    postprocess_blocks = [
        skill.prompt_block(index)
        for index, skill in enumerate(
            (skill for skill in skills_for_prompt if skill.category == SKILL_CATEGORY_POSTPROCESS),
            start=1,
        )
    ]
    return (
        "通用沟通类 skills:\n"
        + "\n\n".join(general_blocks)
        + "\n\n" +
        "查询类 skills：\n"
        + "\n\n".join(query_blocks)
        + "\n\n诊断分析类 skills：\n"
        + "\n\n".join(analysis_blocks)
        + "\n\n文件转换类 skills：\n"
        + "\n\n".join(conversion_blocks)
        + "\n\n结果展示生成类 skills：\n"
        + "\n\n".join(postprocess_blocks)
    )
