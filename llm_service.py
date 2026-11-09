from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib import error, request

from agent_skills import (
    SKILL_ACTUATOR_HANDSHAKE_ANALYSIS,
    SKILL_ACTUATOR_REFERENCE_SEARCH,
    SKILL_BATCH_ROOT_CAUSE_ANALYSIS,
    SKILL_KPI_REPORT_ANALYSIS,
    SKILL_BLF_TO_MDF_CONVERSION,
    SKILL_FEEDBACK_REPORT,
    SKILL_DTC_CODE_SEARCH,
    SKILL_FAULT_DIAGNOSIS,
    SKILL_FAULT_TABLE_SEARCH,
    SKILL_PERFORMANCE_ANALYSIS_SETUP,
    SKILL_PERFORMANCE_CAPABILITY_SEARCH,
    SKILL_REQUIREMENT_INTAKE,
    SKILL_RESULT_TRANSFORMER,
    SKILL_ROOT_CAUSE_ANALYSIS,
    SKILL_FUNCTION_EXIT_ANALYSIS,
    SKILL_SMALLTALK_CHAT,
    TASK_BLF_TO_MDF_CONVERSION,
    TASK_KPI_REPORT_ANALYSIS,
    VALID_AGENT_SKILLS,
    VALID_TASK_TYPES,
    build_public_capability_text,
    format_skill_enum_for_prompt,
    format_skills_for_prompt,
    format_task_type_enum_for_prompt,
    get_skill,
    is_skill_enabled,
)
from feishu_config import load_env


SYSTEM_PROMPT = """
你是飞书群里的“布鲁”。

你的工作原则：
1. 优先理解用户真实意图，用自然、简洁、专业的中文回复。
2. 不要机械重复“能力说明”，只有当用户明确在问“你能做什么/支持什么”时，才回答能力范围。
3. 如果用户已经在发起分析，就直接推进，不要把分析请求误判成能力咨询。
4. 能力范围必须以 config/agent_capabilities.xlsx 中 enabled=true 且 visible=true 的配置为准，不要凭旧记忆编造能力清单。
5. 不编造分析结果，不假装已经读取用户未提供的文件内容。
6. 轴扭/轴端扭矩/IActAxleTrq 准确性校验的顺序不能变：先让用户选择车型编号和 Final Ratio；确认后再提示上传 BLF。不要在首轮把 BLF 作为第 1 项材料要求。
"""

BATCH_ROOT_CAUSE_FIXED_COMMAND = "分析飞书多维表格中的问题"
KPI_REPORT_FIXED_COMMAND = "分析飞书多维表格中的KPI报告"

WORKFLOW_PROMPT = """
你是飞书机器人“布鲁”的流程判定助手。你要判断用户这一次消息的真实意图，并输出结构化 JSON。

只输出 JSON，不要解释，不要 Markdown 代码块。

固定格式：
{
  "intent_type": "analysis_request" | "capability_question" | "dev_request" | "clarify",
  "task_type": __TASK_ENUM__ | null,
  "project_type": "J6B" | "8650" | "BMC" | null,
  "project_supported": true | false | null,
  "database_name": string | null,
  "problem_groups": string[],
  "reply": string
}

字段要求：
1. intent_type
- analysis_request：用户已经在发起分析、诊断、排查、查询，不要求把流程说全
- capability_question：用户主要在问“你能看什么/支持什么/有哪些类型”
- dev_request：用户明确点到当前开发中的能力，或点到当前项目暂不支持的能力
- clarify：信息还不完整，但也不是单纯问能力

2. task_type
- 只能是 __TASK_NAMES__ / null

3. project_type
- 只能是 J6B / 8650 / BMC / null
- 如果用户没说项目，就返回 null

4. project_supported
- 仅在 task_type=fault_degradation 时使用
- J6B -> true
- 8650 / BMC -> false
- 未提项目 -> null
- 其他任务可返回 null

5. problem_groups
- 只能从这三个里选：stability_control, acc_control, apa_control
- 不确定就返回 []

6. 重要判定原则
- 只要用户核心意思是“帮我分析/诊断/排查/看一下这个问题”，优先判为 analysis_request
- 不要因为句子里出现“哪些问题”“支持什么”就轻易判成 capability_question；如果用户是在推进当前需求，仍应判为 analysis_request
- 如果用户问的是“APA 能看哪些问题”“你支持哪些故障类型”，这才是 capability_question
- 如果用户是在问“某个功能/模块需要关注哪些 FDA3/FDA7/故障码/DID”，这更像故障诊断范围咨询，优先判为 capability_question 或 clarify，不要直接进入等待上传日志/等待故障输入
- 故障降级诊断只支持 J6B；如果用户明确说 8650 或 BMC 的故障诊断，请判为 dev_request
- 执行器状态判断/执行器握手诊断已可用；规则查询和上传数据分析都判为 analysis_request
- “J6B / 8650 / BMC”的支持限制只作用于故障降级诊断，不要套用到性能问题分析

7. 领域理解
- ACC / 跟车 / 纵向 / 重刹 / 设速 / 回退 -> acc_control
- ABS / TCS / VSE / 稳定 / 附着 -> stability_control
- APA / 泊车 / 停车位 -> apa_control
- fault_event / FDA3 / FDA7 / DID / 故障码 / 62 FD A3 / 62 FD A7 -> 更偏向 fault_degradation
- 握手 / 执行器状态 / actuator -> actuator_handshake

8. reply 风格
- 简洁、自然，像群里协作消息
- analysis_request：给出下一步最直接的动作
- capability_question：回答范围，不要过早催上传
- dev_request：明确说明开发中，并给出当前可替代的可用能力
- 如果是 capability_question，不要自己展开固定能力清单；本地会从 agent_capabilities.xlsx 生成最终对外说明，你只需保持 reply 简短自然。
""".replace("__TASK_ENUM__", format_task_type_enum_for_prompt()).replace(
    "__TASK_NAMES__",
    " / ".join(VALID_TASK_TYPES),
)

POLISH_PROMPT = """
你是车辆问题分析结果润色助手。
请基于结构化结果，输出 2 到 4 句中文总结。
要求：
1. 第一优先级是说清有没有问题。
2. 如果有问题，说清主要问题集中在哪一类、时间点大致情况、建议重点查看哪些图示。
3. 如果没问题，明确说“本次未识别到明显异常”。
4. 不编造数据，不使用 Markdown 列表。
"""

TOOL_FALLBACK_PROMPT = """
你是飞书群里的布鲁。用户提出了一个需求，本地工具没有得到明确结果，或者本地工具只能提供部分线索。
你必须基于用户问题和已知线索直接回复，不允许只说“我来查”“稍等”“我再看看”。

要求：
1. 如果线索不足，要明确说“本地资料里暂未直接命中”，然后给出合理的排查方向或需要补充的信息。
2. 如果用户问车辆/总线/故障/性能相关问题，优先结合已有线索给工程化解释。
3. 不要假装已经读取用户未上传的日志或文件。
4. 回复要短，但必须有实质内容。
5. 如果本地能力暂时不能直接完成，也要给出当前能判断的结论、可能原因、排查方向或需要补充的信息。
"""

ACTUATOR_REFERENCE_PROMPT = """
你是执行器接口文档查询助手。你会拿到用户问题和本地检索到的执行器知识库片段。

回答要求：
1. 只能基于本地检索结果回答，不要编造未命中的文档内容。
2. 如果用户问“需要满足哪些条件”，优先按“触发条件 / 正常应答 / 关键信号”组织。
3. 如果用户问“某信号如何置位/取值”，必须写清信号名、期望值、枚举含义和相关状态。
4. 如果检索结果不足，要明确说“当前知识库没有直接命中”，然后给出最接近的规则或需要补充的关键词。
5. 不要只说“我来查”或“稍等”。要直接给结论。
6. 回复要像工程协作消息，短但信息密度高；可以用小标题和项目符号。
7. 不要大段展示“原始文档依据”，除非它能帮助回答用户问的具体条件。
8. 用户能反向查询和追溯的知识库只有原始接口文档；不要把内部整理产物当成来源展示。
9. 不要输出内部规则 ID、CSV 文件名、机器可读规则包、状态机 ID 或类似 `DRV_ACC_PROPULSION` 的名称。
10. 如果本地片段里出现“状态机”“规则命中”“接口要求命中”等内部检索提示，只能把它翻译成用户能理解的业务表达，例如“驱动系统在 ACC 请求激活后应反馈请求已接受/等待驾驶员输入”，不要原样复述内部名称。
11. 如果用户是在评审执行器接口变更或新需求，例如行泊切换、500ms 超时、降级、刹停、拉 EPB、请求接口迁移，要按“结论先行 / 现有文档能支撑什么 / 当前文档缺口 / 实施关注点 / 风险点 / 建议补齐项”回答。
12. 需求评审类问题不要只罗列信号；要判断需求是否合理、哪些部分可以从现有文档支撑、哪些部分只是工程推断或需要系统需求补充。
13. 如果本地检索没有直接命中某个 flag、阈值、状态或安全动作，必须明确说“当前文档未直接定义”，不要把相近信号包装成确定结论。
"""

FAULT_REFERENCE_PROMPT = """
你是故障表查询助手。你会拿到用户问题和本地 FDA3/FDA7 故障表检索结果。

回答要求：
1. 优先直接回答用户问题，不要只说“我来查”“稍等”。
2. FDA7 是应用层故障，FDA3 是底软故障；如果两类都命中，要分开讲清楚。
3. 如果用户问某功能需要关注哪些故障，按“优先关注项 / 含义 / 可能影响 / 排查方向”组织。
4. 如果用户问信号或报文 miss，要把 `PB_xxx_..._Miss_Fault` 解释成对应 CAN 报文或信号组未收到，并区分 Miss、MAC、ARC 的倾向。
5. 不要编造本地表里没有的故障编号；本地没有直接命中时，要明确说没有直接命中，并给可能关键词、项目差异或排查方向。
6. 回复要像工程协作消息，短但有结论；不要把 Excel 原始列名、内部 score、源码、正则或实现细节甩给用户。
7. 如果本地检索结果已经给出故障表数量、来源文件或 FDA3/FDA7 映射依据，必须保留这些确定信息，不要改写成“可能”“建议提供完整名称”。
"""

DTC_REFERENCE_PROMPT = """
你是 DTC 故障码查询助手。你会拿到用户问题和本地 DTC 表检索结果。

回答要求：
1. 优先直接回答用户问的 DTC 码、系统范围、数量或描述，不要只说“我来查”。
2. PS DTC 表按驱动系统理解；EBCM DTC 表按制动系统理解。
3. DTC 查询与 FDA3/FDA7 故障表不同：用户问 DTC 码、Service DTC、J1979、CustomerDTCID、InternalDTCID 时走 DTC；用户问 FDA3/FDA7 应用层/底软故障时不要混用。
4. 如果本地结果给出表格统计、命中条目或来源 sheet，必须保留确定信息。
5. 没有命中时明确说当前 DTC 表未直接命中，并给出可继续检索的码值、系统范围或关键词，不要编造 DTC。
6. 回复要像工程协作消息，短但有结论；可以保留表格，避免暴露内部 score、正则或代码实现。
"""

PERFORMANCE_REFERENCE_PROMPT = """
你是性能指标知识库查询助手。你会拿到用户问题和本地性能指标知识，包括指标说明，以及从指标实现中提取的关键信号、阈值、枚举、method、事件字段和伪代码逻辑。

回答要求：
1. 优先直接回答用户的问题，不要机械重复“请上传文件”。
2. 如果用户问“有哪些指标/列个表/总表”，保留清晰表格或分组列表，字段至少包含：功能范围、指标、判定规则、关键信号、主要阈值。
3. 如果用户问某个指标“规则/阈值/怎么算/需要哪些信号”，必须写清判定条件、关键输入信号、主要阈值/枚举，以及事件输出关注点。
4. 永远不要把 Python 源码片段、源码行、正则或内部实现长代码直接甩给用户；如果用户问代码实现，也要总结成伪代码或工程步骤。
5. 只基于本地指标知识回答；本地没有命中的地方要说明“当前指标知识库未直接命中”，再给最接近的指标或建议问法。
6. 回复要像工程协作消息，短但信息密度高；可以用小标题、表格和项目符号。
7. 不要说“我来查一下”“稍等”，也不要编造已经跑过数据。
"""

RESULT_TRANSFORMER_PROMPT = """
你是布鲁的结果展示生成器。你会拿到：
1. 用户原始需求
2. 上游本地业务 skill 名称
3. 本地业务 skill 检索/诊断出的可信上下文
4. 已经润色过的基础回复

你的任务不是重新诊断，也不是编造新结论，而是严格按 result_transformer skill 定义，判断是否需要把已有结果转换成更适合飞书展示的产物计划。

result_transformer skill 定义：
__RESULT_TRANSFORMER_SKILL__

只输出 JSON，不要 Markdown 代码块，不要解释。

固定格式：
{
  "should_transform": true | false,
  "title": string,
  "summary": string,
  "artifacts": [
    {
      "type": "rich_card" | "table" | "flowchart_image" | "timeline_image" | "diagram_image" | "markdown_file" | "html_file" | "gif",
      "title": string,
      "description": string,
      "nodes": [{"id": string, "label": string, "kind": "start" | "state" | "decision" | "action" | "end"}],
      "edges": [{"from": string, "to": string, "label": string}],
      "events": [{"time": string, "title": string, "detail": string, "level": "normal" | "warning" | "error"}],
      "columns": [{"key": string, "title": string}],
      "rows": [{string: string}],
      "content": string,
      "frames": [{"title": string, "highlight": string[], "caption": string}]
    }
  ],
  "card_sections": [
    {"title": string, "bullets": [string]}
  ]
}

规划要求：
1. 是否生成产物、生成什么类型，都以 result_transformer skill 定义为准。
2. 用户没有明确要求展示形态转换时，should_transform=false，artifacts=[]。
3. 所有技术内容必须来自本地上下文或基础回复；如果资料不足，产物里要标注“资料不足/需补充”，不要补不存在的信号或条件。
4. 节点、表格和事件用用户能看懂的人话命名，不要使用内部规则 ID、CSV 文件名、状态机 ID 或源码名作为标题。
5. 每个 artifact 标题要短，适合放在飞书卡片里。
6. 尽量控制复杂度：流程图节点 3 到 10 个，表格行 3 到 12 行，时间线事件 3 到 12 个。
""".replace(
    "__RESULT_TRANSFORMER_SKILL__",
    (get_skill(SKILL_RESULT_TRANSFORMER).prompt_block(1) if get_skill(SKILL_RESULT_TRANSFORMER) else ""),
)

SESSION_INTENT_PROMPT = """
你是飞书机器人“布鲁”的会话控制判定助手。
你要判断用户在当前会话里这句话更像哪种操作意图。

只输出 JSON，不要解释。

固定格式：
{
  "action": "cancel" | "status" | "back" | "change_database" | "check_upload" | "new_request" | "continue",
  "reply": string
}

判定要求：
1. cancel
- 用户明确表示不做了、算了、取消、结束这次、先停掉、先别查了、先这样、先不用了

2. status
- 用户在问当前到哪一步了、现在什么状态、进度如何

3. back
- 用户想回到上一步、重新选数据库、重新说需求、返回上一步

4. change_database
- 用户想换数据库、重选数据库版本、改数据库，不是取消整个需求

5. check_upload
- 用户在问“我刚上传了你看到了吗”“附件收到了吗”“怎么还没识别到”
- 这类通常是在催机器人检查最近上传的文件

6. new_request
- 用户虽然当前在一个流程里，但这句话明显是在提出另一个新需求
- 例如“别查故障了，改成性能分析”“换个需求，我要分析 ACC 重刹”

7. continue
- 用户仍在继续当前流程，包括继续补充信息、上传前说明、贴故障码、贴日志、催结果等

补充要求：
- “取消吧”“先不用了”“这个先停一下”通常都算 cancel
- “换数据库”“改成 27.76.1”“重新选数据库”通常优先算 change_database
- “我刚上传了”“你看到附件了吗”“怎么还没识别到文件”通常优先算 check_upload
- “换成性能分析”“我不做这个了，改查 ACC”通常算 new_request
- 用户纠正“不是这个/不对/错了/用错 skill/应该是执行器/不是性能是故障/不是查询是诊断”通常算 new_request，让 DeepSeek 重新选择最终期望的能力。
- 如果拿不准，优先返回 continue
- reply 保持简短自然
"""

MAX_HISTORY_MESSAGES = 12
SUPPORTED_PROBLEM_GROUPS = ("stability_control", "acc_control", "apa_control")
SUPPORTED_INTENT_TYPES = ("analysis_request", "capability_question", "dev_request", "clarify")
SUPPORTED_PROJECT_TYPES = ("J6B", "8650", "BMC")

_histories: dict[str, list[dict[str, str]]] = defaultdict(list)

PROBLEM_GROUP_LABELS = {
    "stability_control": "车身稳定与附着控制",
    "acc_control": "ACC 跟车与纵向控制",
    "apa_control": "APA 泊车控制",
}

PROBLEM_GROUP_CAPABILITIES = {
    "stability_control": "可看 ABS、TCS、VSE 等稳定性和附着控制相关问题。",
    "acc_control": "可看 ACC 舒适性、重刹、回退、控制权、设速等纵向控制问题。",
    "apa_control": "可看 APA 泊车过程中的抖动、换挡、溜车和控制权问题。",
}


def compact_prompt_text(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def compact_recent_turns(turns: Any, limit: int = 8) -> list[dict[str, str]]:
    if not isinstance(turns, list):
        return []
    compacted: list[dict[str, str]] = []
    for item in turns[-limit:]:
        if not isinstance(item, dict):
            continue
        text = compact_prompt_text(item.get("text"), 800)
        if not text:
            continue
        compacted.append(
            {
                "role": str(item.get("role") or "")[:20],
                "text": text,
                "tool": str(item.get("tool") or "")[:80],
                "action": str(item.get("action") or "")[:40],
            }
        )
    return compacted


def compact_last_skill_result(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        "source_skill": str(value.get("source_skill") or "")[:80],
        "title": str(value.get("title") or "")[:80],
        "user_text": compact_prompt_text(value.get("user_text"), 1000),
        "reply_text": compact_prompt_text(value.get("reply_text"), 2000),
        "local_context": compact_prompt_text(value.get("local_context"), 3000),
    }


def is_contextual_followup_text(text: str) -> bool:
    value = (text or "").strip().lower()
    if not value:
        return False
    reference_terms = [
        "上面",
        "刚才",
        "上一条",
        "前面",
        "上述",
        "这个",
        "这些",
        "这几个",
        "这几项",
        "它",
        "它们",
        "那",
        "那些",
        "该流程",
        "该状态",
        "该问题",
        "这136项",
        "136项",
        "继续",
    ]
    followup_terms = ["总结", "概括", "归纳", "分类", "大致", "展开", "更具体", "为什么", "原因", "依据", "根据什么"]
    if any(term in value for term in reference_terms):
        return True
    return len(value) <= 40 and any(term in value for term in followup_terms)


@dataclass
class LLMConfig:
    api_base_url: str
    api_key: str
    model: str
    timeout_seconds: int


@dataclass
class WorkflowDecision:
    intent_type: str = "clarify"
    task_type: str | None = None
    project_type: str | None = None
    project_supported: bool | None = None
    database_name: str | None = None
    problem_groups: list[str] = field(default_factory=list)
    reply: str = ""


@dataclass
class AgentDecision:
    action: str = "reply"
    tool: str | None = None
    task_type: str | None = None
    database_name: str | None = None
    problem_groups: list[str] = field(default_factory=list)
    project_type: str | None = None
    project_supported: bool | None = None
    tool_query: str = ""
    reply: str = ""


@dataclass
class SessionIntentDecision:
    action: str = "continue"
    reply: str = ""


def is_plain_greeting_text(text: str) -> bool:
    value = re.sub(r"[\s，。！？!?,、~～.]+", "", text or "").lower()
    if not value:
        return False
    return value in {
        "你好",
        "您好",
        "在吗",
        "在不在",
        "hello",
        "hi",
        "hey",
        "哈喽",
        "嗨",
    }


def is_smalltalk_chat_text(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if is_plain_greeting_text(raw):
        return True

    lowered = raw.lower()
    technical_tokens = [
        "分析",
        "诊断",
        "故障",
        "降级",
        "执行器",
        "握手",
        "性能",
        "指标",
        "数据库",
        "上传",
        "文件",
        "blf",
        "h5",
        "hdf5",
        "arxml",
        "fda3",
        "fda7",
        "did",
        "fault",
        "acc",
        "apa",
        "aeb",
        "cib",
        "ebcm",
        "ebooster",
        "pdf",
        "ecp",
        "ecm",
    ]
    if any(token in lowered for token in technical_tokens):
        return False

    compact = re.sub(r"[\s，。！？!?、~～,.]+", "", raw).lower()
    smalltalk_exact = {
        "你好",
        "您好",
        "在吗",
        "在不在",
        "hello",
        "hi",
        "hey",
        "谢谢",
        "感谢",
        "辛苦了",
        "好的",
        "收到",
        "明白",
        "可以",
        "你是谁",
        "介绍一下你自己",
        "怎么用你",
        "你能做什么",
        "你现在有哪些能力",
        "你有哪些能力",
        "有哪些能力",
        "现在有哪些能力",
        "你有什么能力",
        "你可以干什么",
        "你可以干哪些事",
        "你能帮我什么",
        "可以干啥",
    }
    if compact in smalltalk_exact:
        return True
    if len(compact) <= 12 and any(token in compact for token in ["你好", "在吗", "谢谢", "辛苦", "怎么用", "你是谁"]):
        return True
    return False


def is_general_capability_intro_text(text: str) -> bool:
    compact = re.sub(r"[\s，。！？!?、~～,.]+", "", (text or "").strip()).lower()
    if not compact:
        return False
    return compact in {
        "你现在有哪些能力",
        "你有哪些能力",
        "有哪些能力",
        "现在有哪些能力",
        "你有什么能力",
        "你有什么功能",
        "有哪些功能",
        "你能做什么",
        "你可以干什么",
        "你可以干哪些事",
        "你能帮我什么",
        "怎么用你",
    }


def smalltalk_reply_for_text(text: str) -> str:
    if is_general_capability_intro_text(text):
        return build_capability_reply([], None)
    return "你好，我在。你可以直接说要查什么或分析什么，我会按需求选择查询、诊断或数据分析能力。"


def is_feedback_report_request(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    compact = re.sub(r"^[\s:：,，。!！@]+", "", value, flags=re.I)
    lowered = compact.lower()
    return bool(
        re.match(r"^(问题反馈|反馈|报错|bug|BUG|纠错|吐槽)\s*[:：\s]", compact)
        or lowered in {"反馈", "报错", "bug", "问题反馈"}
        or re.search(r"(记录|提交|保存).{0,12}(问题反馈|反馈|bug|报错)", compact)
    )


def is_dtc_query_text(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    lowered = value.lower()
    if "fda3" in lowered or "fda7" in lowered:
        return False
    has_dtc_word = any(token in lowered for token in ["dtc", "service dtc", "customerdtc", "internaldtc", "j1979"])
    has_dtc_word = has_dtc_word or "故障码" in value
    has_system_scope = any(
        token in lowered
        for token in ["ebcm", "ebooster", "brake", "制动", "刹车", "ps", "pdf", "ecp", "ecm", "propulsion", "驱动", "动力"]
    )
    has_explicit_code = bool(
        re.search(r"\b[pcbu][0-9a-f]{4}(?:[.\-]?[0-9a-f]{2})?\b", lowered)
        or re.search(r"\b0x[0-9a-f]{2,8}\b", lowered)
    )
    return has_dtc_word and (has_system_scope or has_explicit_code)


def get_llm_config() -> LLMConfig | None:
    load_env()
    if not os.getenv("KIMI_API_KEY", "").strip():
        return None
    return get_kimi_config()


def get_kimi_config() -> LLMConfig:
    load_env()
    api_key = os.getenv("KIMI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未配置 KIMI_API_KEY，不会回退到其他模型。")
    timeout_text = os.getenv("KIMI_TIMEOUT_SECONDS", "120").strip()
    try:
        timeout_seconds = int(timeout_text)
    except ValueError as exc:
        raise ValueError("KIMI_TIMEOUT_SECONDS 必须为正整数。") from exc
    if timeout_seconds <= 0:
        raise ValueError("KIMI_TIMEOUT_SECONDS 必须为正整数。")
    return LLMConfig(
        api_base_url="https://api.kimi.com/coding/v1",
        api_key=api_key,
        model=os.getenv("KIMI_MODEL", "").strip() or "kimi-for-coding",
        timeout_seconds=timeout_seconds,
    )


def get_llm_display_name() -> str:
    load_env()
    return os.getenv("KIMI_MODEL", "").strip() or "kimi-for-coding"


def get_llm_api_unavailable_message() -> str:
    return f"{get_llm_display_name()} API不可用"


def is_llm_api_unavailable_reply(reply: str) -> bool:
    text = str(reply or "").strip()
    if not text:
        return True
    lower = text.lower()
    if "当前聊天回复模型还没有配置好" in text or "llm_api_key" in lower or "kimi_api_key" in lower:
        return True
    if "聊天模型调用失败" not in text:
        return False
    return True


def llm_api_precheck_timeout_seconds() -> int:
    load_env()
    for key in ("BATCH_ROOT_CAUSE_LLM_PRECHECK_TIMEOUT_SECONDS", "LLM_API_PRECHECK_TIMEOUT_SECONDS"):
        value = os.getenv(key, "").strip()
        if not value:
            continue
        try:
            return max(5, int(value))
        except ValueError:
            continue
    config = get_llm_config()
    return config.timeout_seconds if config is not None else 20


def probe_llm_api_available(
    timeout_seconds: int | None = None,
    *,
    config: LLMConfig | None = None,
    api_protocol: str | None = None,
) -> tuple[bool, str]:
    reply = call_chat_completion(
        [
            {"role": "system", "content": "你是接口连通性测试助手。"},
            {"role": "user", "content": "只回复：OK"},
        ],
        timeout_seconds=(
            timeout_seconds if timeout_seconds is not None
            else config.timeout_seconds if config is not None
            else llm_api_precheck_timeout_seconds()
        ),
        config=config,
        api_protocol=api_protocol,
    )
    if is_llm_api_unavailable_reply(reply):
        if config is not None:
            detail = str(reply or "模型返回空响应").replace(config.api_key, "[REDACTED]")
            return False, f"{config.model} API不可用：{detail[:600]}"
        return False, get_llm_api_unavailable_message()
    return True, ""


def ensure_llm_api_available(timeout_seconds: int | None = None) -> None:
    ok, message = probe_llm_api_available(timeout_seconds=timeout_seconds)
    if not ok:
        raise RuntimeError(message or get_llm_api_unavailable_message())


def clear_history(conversation_key: str) -> None:
    _histories.pop(conversation_key, None)


def generate_group_reply(conversation_key: str, user_text: str) -> str:
    history = _histories[conversation_key][-MAX_HISTORY_MESSAGES:]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": user_text},
    ]
    reply = call_chat_completion(messages)
    if not reply:
        return "我这边暂时没生成可用回复。你可以直接说需求，比如：分析 ACC 重刹，用最新数据库，我上传 h5 文件。"

    _histories[conversation_key] = [
        *history,
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": reply},
    ]
    _histories[conversation_key] = _histories[conversation_key][-MAX_HISTORY_MESSAGES:]
    return reply


def generate_tool_fallback_reply(
    user_text: str,
    tool_name: str | None = None,
    local_context: str = "",
) -> str:
    prompt = (
        f"用户问题：{user_text}\n"
        f"尝试调用的本地能力：{tool_name or '无'}\n"
        f"本地检索/工具线索：{local_context or '本地工具没有返回明确结果。'}"
    )
    reply = call_chat_completion(
        [
            {"role": "system", "content": TOOL_FALLBACK_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )
    if not reply or "调用失败" in reply or "还没有配置好" in reply:
        return (
            "本地资料里暂时没有直接命中的结果。"
            "我建议先确认对应报文/信号是否在当前数据库和故障表中存在，再看发送端 ECU、网关路由、总线链路、报文周期/超时监控是否异常。"
        )
    return reply.strip()


def polish_actuator_reference_answer(
    user_text: str,
    local_context: str,
    fallback_reply: str,
) -> str:
    prompt = (
        f"用户问题：{user_text}\n\n"
        f"本地检索结果：\n{local_context or '本地知识库没有返回明确结果。'}"
    )
    reply = call_chat_completion(
        [
            {"role": "system", "content": ACTUATOR_REFERENCE_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )
    if not reply or "调用失败" in reply or "还没有配置好" in reply:
        return fallback_reply
    value = reply.strip()
    if not value or value in {"稍等", "我来查一下", "我帮你查一下"}:
        return fallback_reply
    return value


def polish_fault_reference_answer(
    user_text: str,
    local_context: str,
    fallback_reply: str,
) -> str:
    prompt = (
        f"用户问题：{user_text}\n\n"
        f"本地故障表检索结果：\n{local_context or '本地 FDA3/FDA7 故障表没有返回明确结果。'}"
    )
    reply = call_chat_completion(
        [
            {"role": "system", "content": FAULT_REFERENCE_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )
    if not reply or "调用失败" in reply or "还没有配置好" in reply:
        return fallback_reply
    value = reply.strip()
    if not value or value in {"稍等", "我来查一下", "我帮你查一下"}:
        return fallback_reply
    return value


def polish_dtc_reference_answer(
    user_text: str,
    local_context: str,
    fallback_reply: str,
) -> str:
    prompt = (
        f"用户问题：{user_text}\n\n"
        f"本地 DTC 表检索结果：\n{local_context or '本地 DTC 表没有返回明确结果。'}"
    )
    reply = call_chat_completion(
        [
            {"role": "system", "content": DTC_REFERENCE_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )
    if not reply or "调用失败" in reply or "还没有配置好" in reply:
        return fallback_reply
    value = reply.strip()
    if not value or value in {"稍等", "我来查一下", "我帮你查一下"}:
        return fallback_reply
    return value


def polish_performance_capability_answer(
    user_text: str,
    local_context: str,
    fallback_reply: str,
) -> str:
    prompt = (
        f"用户问题：{user_text}\n\n"
        f"本地性能指标知识：\n{local_context or '本地性能指标知识库没有返回明确结果。'}"
    )
    reply = call_chat_completion(
        [
            {"role": "system", "content": PERFORMANCE_REFERENCE_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )
    if not reply or "调用失败" in reply or "还没有配置好" in reply:
        return fallback_reply
    value = reply.strip()
    if not value or value in {"稍等", "我来查一下", "我帮你查一下"}:
        return fallback_reply
    return value


def build_result_transform_plan(
    user_text: str,
    source_skill: str,
    local_context: str,
    base_reply: str,
) -> dict[str, Any]:
    """Ask DeepSeek to plan post-processing artifacts for a trusted local result."""
    prompt = json.dumps(
        {
            "user_text": user_text,
            "source_skill": source_skill,
            "local_context": local_context[:12000],
            "base_reply": base_reply[:6000],
        },
        ensure_ascii=False,
    )
    raw = call_chat_completion(
        [
            {"role": "system", "content": RESULT_TRANSFORMER_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )
    parsed = parse_json_object(raw or "")
    if not isinstance(parsed, dict):
        return {
            "should_transform": False,
            "title": "",
            "summary": "",
            "artifacts": [],
            "card_sections": [],
        }
    return normalize_result_transform_plan(parsed)


def normalize_result_transform_plan(plan: dict[str, Any]) -> dict[str, Any]:
    supported_types = {
        "rich_card",
        "table",
        "flowchart_image",
        "timeline_image",
        "diagram_image",
        "markdown_file",
        "html_file",
        "gif",
    }
    artifacts = []
    for item in plan.get("artifacts") or []:
        if not isinstance(item, dict):
            continue
        artifact_type = str(item.get("type") or "").strip()
        if artifact_type not in supported_types:
            continue
        normalized = dict(item)
        normalized["type"] = artifact_type
        normalized["title"] = str(item.get("title") or artifact_type).strip()[:80]
        normalized["description"] = str(item.get("description") or "").strip()[:500]
        artifacts.append(normalized)
    sections = []
    for section in plan.get("card_sections") or []:
        if not isinstance(section, dict):
            continue
        title = str(section.get("title") or "").strip()[:80]
        bullets = [str(value).strip() for value in section.get("bullets") or [] if str(value).strip()]
        if title or bullets:
            sections.append({"title": title, "bullets": bullets[:8]})
    return {
        "should_transform": bool(plan.get("should_transform")) and bool(artifacts or sections),
        "title": str(plan.get("title") or "").strip()[:80],
        "summary": str(plan.get("summary") or "").strip()[:800],
        "artifacts": artifacts[:8],
        "card_sections": sections[:6],
    }


AGENT_ORCHESTRATOR_PROMPT_TEMPLATE = """
你是飞书机器人“布鲁”的总控智能体。所有用户消息都先由你理解，再决定是否调用本地能力。
本地规则和函数只是工具，不是决策者。你要优先理解用户真实意图、上下文和当前会话状态。

只能输出 JSON，不要 Markdown，不要解释。
格式：
{
  "action": "reply" | "call_tool" | "ask" | "cancel" | "status" | "new_request",
  "tool": __SKILL_ENUM__ | null,
  "task_type": __TASK_ENUM__ | null,
  "database_name": string | null,
  "problem_groups": string[],
  "project_type": "J6B" | "8650" | "BMC" | null,
  "project_supported": true | false | null,
  "tool_query": string,
  "reply": string
}

本地能力：
__SKILL_DESCRIPTIONS__

reply 能力：
- 本地没有必要工具时，你可以自己自然回复，但不要编造已经分析过文件或日志。

关键判断：
- current_session.recent_turns、last_skill_result、last_fault_reference_context 都是同一用户在同一群里的短期上下文。判断用户意图时必须结合它们，尤其是“上面/刚才/这个/这些/这136项/继续/总结一下/更具体/画成图/为什么”等承接上一轮的说法。
- 如果用户在追问上一轮结果，要把上一轮主题补进 tool_query，让下游 skill 收到完整问题；不要只把“这几个/总结一下/为什么”这种残缺指代传下去。
- 如果用户明确换了需求或纠正你“不是这个/应该是另一个”，要相信最新消息并切到新的 skill；只有语义不完整时才用上下文补全，不要让旧上下文污染明确的新需求。
- 通用别名：FDA7 = 应用层故障，FDA3 = 底软故障；这两个说法在所有故障表查询/诊断上下文里等价。
- 所有 skill 的 when_to_use / 示例都是语义触发，不是字面模板；用户只要表达的是同类需求，就应该选择对应 skill。
- 当前主要应用是资料查询、规则解释、需求评审和能力咨询。不要因为用户说“分析/看看/帮我看”就默认进入上传数据问题分析。
- 只有用户明确说要“上传数据/基于数据/数据中出现某问题的原因/从这段 blf/h5/hdf5 里定位问题”，并且问题现象、分析对象或归因目标足够清楚时，才进入诊断分析类 skill。
- 如果用户只是问“能不能分析”“需要看哪些信号”“有哪些原因”“这个需求是否合理”“流程是什么”“状态怎么跳转”，优先选择查询类 skill 或直接回复，不要等待上传文件。
- requirement_intake 是入口澄清 skill：只有当用户需求无法稳定落到唯一业务 skill，或需要确认多个可行能力时才使用。用户一旦表达出明确查询/诊断/分析目标，就不要停在 requirement_intake，要直接选择对应业务 skill。
- result_transformer 是结果展示生成 skill，不是入口业务 skill。用户要求“画图/流程图/表格/报告/文件/GIF”等展示形态转换时，仍要先选择能产出可信内容的业务 skill，例如 actuator_reference_search / root_cause_analysis / actuator_handshake_analysis；本地会在业务 skill 结果后再调用 result_transformer。
- 用户说“把上面的/刚才的/这个结果画成流程图、表格、图示”等承接上一轮结果的需求时，不要进入 requirement_intake；应复用上一轮业务结果进入 result_transformer 展示链路。
- 用户在澄清阶段给出新的明确需求，哪怕和上一轮不同，也要按最新需求重新分发；不要把旧 session 强行拼进去导致继续追问旧问题。
- 用户问“制动/驱动/EBCM/PS/PDF/ECP/ECM 的 DTC 码是什么/有多少/某个 DTC 含义/某个描述对应什么 DTC”，是 dtc_code_search；不要和 FDA3/FDA7 故障表混淆。
- 性能分析能力查询当前在能力开关表中关闭。用户问性能指标规则、阈值、关注信号或问题类型表时，不要进入等待上传文件；应说明该查询入口当前关闭，或者在用户明确上传数据做行车问题原因分析时走 root_cause_analysis。
- 用户问“导致制动断握手有哪些原因”“EBCM/ebooster 握手失败看哪些信号”“驱动/PDF/ECP/ECM 断握手原因”“执行器降级/状态不闭环怎么查”，是 actuator_reference_search。
- 用户问执行器文档里的某个特定条件、设计要求、接口状态流转、例子，或“ACC 下驱动握手需要满足哪些条件”“某信号在握手状态下该如何置位/取值”，也是 actuator_reference_search。
- 用户问“某场景下制动/驱动/执行器握手流程、状态跳转流程、状态流转、流程图”，且没有上传数据或要求从数据里识别问题，也是 actuator_reference_search；不要进入 actuator_handshake_analysis 等待上传。
- 用户明确说上传数据、h5/hdf5/blf、总线数据、跑一下数据、从数据里识别问题时，如果没有明确指向执行器/握手/制动响应/驱动响应/降级/闭环，也没有明确指向性能/重刹/APA/ACC/稳定性指标，不要强行选择某一个分析工具；应 action=call_tool、tool=requirement_intake，询问用户是要“性能分析”“执行器分析”还是“两个都分析”。
- 用户上传或准备上传 `.h5` 数据，并希望解释顿挫、重制动、不合理低速、误制动、漏制动、未达到 set speed、溜车、跟车距离异常等现象的原因，尤其问“是执行器响应问题还是算法/控制侧问题”“帮我分析原因/归因”“为什么导致这个问题”，是 root_cause_analysis。行车问题原因分析当前仅支持 `.h5`；用户手头是 `.blf`、`.hdf5` 或其他格式时，要提示先转换成标准 `.h5`。
- 用户明确说“两条都看/两个都分析/性能和执行器都分析/综合判断/行车问题原因分析/综合归因”，也是 root_cause_analysis。
- 用户问 ACC/LCC/自适应巡航/巡航/车道居中的功能退出、退出原因、退出原因码、退出码、退出条件、抑制原因、临时退出，或明确说“行车问题原因分析里面定义的 ACC/LCC 功能退出原因”，是 function_exit_analysis。没有上传数据时先按功能退出规则/原因码配置查询回答；明确基于 H5/数据/上传文件时才等待 H5。不要走 actuator_reference_search/actuator_handshake_analysis。用户只是问 ACC/LCC 非退出相关的状态流程、条件、信号置位或设计要求时，才走 actuator_reference_search。
- 用户问“核对/校验/验证/复算/判断 轴扭/轴端扭矩/IActAxleTrq/ActAxleTrq 准确性/准不准/较准/不准”，是 axle_torque_accuracy_analysis；不要作为普通 reply 自己列材料清单，也不要走 blf_to_mdf_conversion。
- 轴端扭矩准确性校验的交互顺序是硬约束：首轮只能提示用户先选择车型编号和 Final Ratio，不能同时要求上传 BLF；每次新的轴扭校验都必须重新选择车型/Final Ratio，不能继承上一轮车型，也不能因为首轮文本里出现 `1/2/3/C1UL/3.47` 就跳过确认；车型/Final Ratio 确认后，下一轮才提示上传 `.blf`。
- 轴端扭矩车型选项必须按这个顺序：1. 358-2（Final Ratio 3.47）；2. C1UL（Final Ratio 3.47）；3. E2LB-2（Final Ratio 3.17）；4. 其他（需输入 Final Ratio）。选择 4 时，先让用户输入 Final Ratio，并列出已有车型的 Final Ratio 值。
- 用户明确说“分析这段数据为什么没有响应”“制动/驱动没有响应”“断握手/降级/状态不闭环”，是 actuator_handshake_analysis。
- 用户明确要求“BLF 转 MDF / blf 转成 mdf / 导出 MDF / 生成 MDF / 格式转换”，是 blf_to_mdf_conversion；这只是格式转换，不是问题诊断。用户问 BLF 数据中为什么出问题时不要选它。
- 用户说“取消、算了、不用了”，是 cancel。
- 用户已经在某个会话中，但提出明显不同的新需求，返回 new_request。
- 用户用“反馈：...”“报错：...”“bug：...”“问题反馈：...”等固定反馈语法，或明确要求记录布鲁回复问题/没反应/体验问题，选择 feedback_report；不要进入业务分析。
- 用户明确纠正“不是这个/不对/用错了/应该是另一个能力/不是性能是执行器/不是查询是诊断”时，必须相信最新反馈，重新选择最终期望的 skill，不要被旧 session 或兜底判断锁住。
- problem_groups 只能从 stability_control、acc_control、apa_control 中选择，不确定返回 []。
- reply 要简短、自然、像群聊协作消息。需要本地工具时，reply 可以为空或只写一句过渡。
- 如果你只是想说“我来查/稍等/我看看”，不要选择 reply；应该选择 call_tool。选择 reply 时必须直接回答用户问题。
"""


def build_agent_orchestrator_prompt() -> str:
    return (
        AGENT_ORCHESTRATOR_PROMPT_TEMPLATE.replace("__SKILL_ENUM__", format_skill_enum_for_prompt())
        .replace("__TASK_ENUM__", format_task_type_enum_for_prompt())
        .replace("__SKILL_DESCRIPTIONS__", format_skills_for_prompt())
    )


COMMON_RESPONSE_GUARDRAIL = (
    "\n通用输出底线：永远不要把 Python 源码片段、源码行、正则、内部检索 score、"
    "Excel 原始列名或冗长内部实现细节直接发给用户；需要解释实现时，只能总结成伪代码、"
    "工程步骤、判定条件或排查建议。"
)
for _prompt_name in (
    "SYSTEM_PROMPT",
    "TOOL_FALLBACK_PROMPT",
    "ACTUATOR_REFERENCE_PROMPT",
    "FAULT_REFERENCE_PROMPT",
    "DTC_REFERENCE_PROMPT",
    "PERFORMANCE_REFERENCE_PROMPT",
    "RESULT_TRANSFORMER_PROMPT",
    "AGENT_ORCHESTRATOR_PROMPT_TEMPLATE",
):
    globals()[_prompt_name] = globals()[_prompt_name] + COMMON_RESPONSE_GUARDRAIL


def decide_agent_action(
    user_text: str,
    database_names: list[str],
    session_state: dict[str, Any] | None = None,
) -> AgentDecision:
    session_state = session_state or {}
    if is_smalltalk_chat_text(user_text):
        return AgentDecision(
            action="call_tool",
            tool=SKILL_SMALLTALK_CHAT,
            task_type=None,
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query="",
            reply=smalltalk_reply_for_text(user_text),
        )

    if is_batch_root_cause_analysis_intent(user_text):
        return AgentDecision(
            action="call_tool",
            tool=SKILL_BATCH_ROOT_CAUSE_ANALYSIS,
            task_type="batch_root_cause_analysis",
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query=user_text,
            reply="",
        )

    if is_kpi_report_analysis_intent(user_text):
        return AgentDecision(
            action="call_tool",
            tool=SKILL_KPI_REPORT_ANALYSIS,
            task_type=TASK_KPI_REPORT_ANALYSIS,
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query=user_text,
            reply="",
        )

    if is_kpi_report_analysis_near_miss(user_text):
        return AgentDecision(
            action="ask",
            tool=None,
            task_type=None,
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query=user_text,
            reply=kpi_report_near_miss_reply(),
        )

    if is_batch_root_cause_analysis_near_miss(user_text):
        return AgentDecision(
            action="ask",
            tool=None,
            task_type=None,
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query=user_text,
            reply=batch_root_cause_near_miss_reply(),
        )

    if is_blf_to_mdf_conversion_intent(user_text):
        return AgentDecision(
            action="call_tool",
            tool=SKILL_BLF_TO_MDF_CONVERSION,
            task_type=TASK_BLF_TO_MDF_CONVERSION,
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query=user_text,
            reply="",
        )

    fallback = heuristic_agent_decision(user_text, database_names, session_state)
    if fallback.tool and not is_skill_enabled(fallback.tool):
        fallback = AgentDecision(
            action="reply",
            tool=None,
            task_type=None,
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query=user_text,
            reply="这个能力当前已在配置表里关闭，暂时不能处理。可以联系维护者打开后再试。",
        )
    raw = call_chat_completion(
        [
            {"role": "system", "content": build_agent_orchestrator_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "user_text": user_text,
                        "current_session": {
                            "stage": session_state.get("stage"),
                            "task_type": session_state.get("task_type"),
                            "database": session_state.get("database"),
                            "problem_groups": session_state.get("problem_groups") or [],
                            "project_type": session_state.get("project_type"),
                            "last_fault_reference_context": session_state.get("fault_reference_context"),
                            "recent_turns": compact_recent_turns(session_state.get("recent_turns")),
                            "last_skill_result": compact_last_skill_result(session_state.get("last_skill_result")),
                            "has_recent_file": bool(session_state.get("file_key")),
                            "file_name": session_state.get("file_name"),
                        },
                        "available_databases": database_names[:80],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
    )
    parsed = parse_json_object(raw or "")
    if not isinstance(parsed, dict):
        print(f"[agent-orchestrator] fallback raw={raw!r} text={user_text!r}", flush=True)
        return fallback
    return merge_agent_decision(parsed, fallback, database_names, user_text)


def is_skill_correction_text(user_text: str) -> bool:
    text = (user_text or "").lower()
    return any(
        token in text
        for token in [
            "不是",
            "不对",
            "错了",
            "用错",
            "选错",
            "判断错",
            "应该是",
            "不是性能",
            "不是执行器",
            "不是故障",
            "不是查询",
            "不是诊断",
            "是执行器",
            "是性能",
            "是故障",
            "切到",
            "换到",
            "改成",
            "别用这个",
            "换个 skill",
            "换个skill",
            "wrong skill",
        ]
    )


def is_actuator_reference_query_text(user_text: str) -> bool:
    value = (user_text or "").lower()
    if not value:
        return False
    if is_function_exit_analysis_intent(user_text):
        return False
    data_or_file_terms = [
        "上传",
        "附件",
        "文件",
        "总线数据",
        "这段数据",
        "数据里",
        "数据里面",
        ".blf",
        ".h5",
        ".hdf5",
        "blf",
        "h5",
        "hdf5",
        "跑一下",
        "跑一遍",
        "从数据",
    ]
    actuator_terms = [
        "执行器",
        "握手",
        "制动",
        "刹车",
        "驻车",
        "电子驻车",
        "驱动",
        "转向",
        "方向盘",
        "换挡",
        "换档",
        "挡位",
        "档位",
        "ebcm",
        "ebooster",
        "brake",
        "pdf",
        "ecp",
        "ecm",
        "eps",
        "etrs",
        "epb",
        "steer",
        "steering",
        "shift",
        "gear",
        "range",
        "aeb",
        "cib",
        "acc",
        "apa",
        "srp",
        "hpp",
    ]
    review_terms = [
        "需求",
        "变更",
        "合理",
        "合理性",
        "可行",
        "可行性",
        "实施",
        "实现",
        "方案",
        "风险",
        "风险点",
        "关注点",
        "建议",
        "评审",
        "设计",
        "改动",
        "接口变更",
        "行泊",
        "行/泊",
        "行车接口",
        "泊车接口",
        "500ms",
        "超时",
    ]
    if any(term in value for term in actuator_terms) and any(term in value for term in review_terms):
        return True
    if any(term in value for term in data_or_file_terms):
        return False
    reference_terms = [
        "流程",
        "流程图",
        "状态跳转",
        "状态流转",
        "条件",
        "置位",
        "取值",
        "设计要求",
        "文档",
        "查询",
        "咨询",
        "查一下",
        "了解",
        "哪些",
        "原因",
        "规则",
        "接收",
        "收到",
        "接受",
        "请求",
        "释放",
        "响应",
        "反馈",
        "是否",
        "会不会",
        "能不能",
        "能否",
        "应该",
        "状态",
        "场景",
        "吗",
        "怎么看",
        "怎么",
    ]
    return any(term in value for term in actuator_terms) and any(term in value for term in reference_terms)


def merge_agent_decision(
    parsed: dict[str, Any],
    fallback: AgentDecision,
    database_names: list[str],
    user_text: str = "",
) -> AgentDecision:
    action = str(parsed.get("action") or "").strip()
    if action not in {"reply", "call_tool", "ask", "cancel", "status", "new_request"}:
        action = fallback.action

    tool = parsed.get("tool")
    if tool not in VALID_AGENT_SKILLS or not is_skill_enabled(tool):
        tool = fallback.tool if fallback.tool in VALID_AGENT_SKILLS and is_skill_enabled(fallback.tool) else None

    task_type = parsed.get("task_type")
    if task_type not in VALID_TASK_TYPES:
        task_type = fallback.task_type

    database_name = parsed.get("database_name")
    if database_name not in database_names:
        database_name = fallback.database_name

    groups = normalize_problem_groups(parsed.get("problem_groups"))
    if not groups:
        groups = fallback.problem_groups

    project_type = str(parsed.get("project_type") or "").strip().upper()
    if project_type not in SUPPORTED_PROJECT_TYPES:
        project_type = fallback.project_type

    project_supported = parsed.get("project_supported")
    if not isinstance(project_supported, bool):
        project_supported = fallback.project_supported

    if is_batch_root_cause_analysis_intent(user_text):
        action = "call_tool"
        tool = SKILL_BATCH_ROOT_CAUSE_ANALYSIS
        task_type = "batch_root_cause_analysis"

    if is_kpi_report_analysis_intent(user_text):
        action = "call_tool"
        tool = SKILL_KPI_REPORT_ANALYSIS
        task_type = TASK_KPI_REPORT_ANALYSIS

    if is_blf_to_mdf_conversion_intent(user_text):
        action = "call_tool"
        tool = SKILL_BLF_TO_MDF_CONVERSION
        task_type = TASK_BLF_TO_MDF_CONVERSION

    if is_function_exit_analysis_intent(user_text):
        action = "call_tool"
        tool = SKILL_FUNCTION_EXIT_ANALYSIS
        task_type = "function_exit_analysis"

    if tool in {SKILL_REQUIREMENT_INTAKE, SKILL_ACTUATOR_HANDSHAKE_ANALYSIS} and is_actuator_reference_query_text(user_text):
        action = "call_tool"
        tool = SKILL_ACTUATOR_REFERENCE_SEARCH
        task_type = "actuator_handshake"

    if tool == SKILL_SMALLTALK_CHAT:
        action = "reply"
        task_type = None

    if tool == SKILL_BATCH_ROOT_CAUSE_ANALYSIS and not is_batch_root_cause_analysis_intent(user_text):
        action = fallback.action
        tool = fallback.tool
        task_type = fallback.task_type

    if tool == SKILL_KPI_REPORT_ANALYSIS and not is_kpi_report_analysis_intent(user_text):
        action = fallback.action
        tool = fallback.tool
        task_type = fallback.task_type

    protected_tools = {
        SKILL_SMALLTALK_CHAT,
        SKILL_FUNCTION_EXIT_ANALYSIS,
        SKILL_BATCH_ROOT_CAUSE_ANALYSIS,
        SKILL_KPI_REPORT_ANALYSIS,
        SKILL_BLF_TO_MDF_CONVERSION,
    }
    allow_fallback_override = not is_skill_correction_text(user_text) and tool not in protected_tools
    if (
        allow_fallback_override
        and fallback.action == "call_tool"
        and fallback.tool == SKILL_ROOT_CAUSE_ANALYSIS
        and action not in {"cancel", "status", "new_request"}
    ):
        action = "call_tool"
        tool = SKILL_ROOT_CAUSE_ANALYSIS
        task_type = "root_cause_analysis"

    if (
        allow_fallback_override
        and tool not in {SKILL_REQUIREMENT_INTAKE, SKILL_ROOT_CAUSE_ANALYSIS, SKILL_FUNCTION_EXIT_ANALYSIS, SKILL_BATCH_ROOT_CAUSE_ANALYSIS, SKILL_KPI_REPORT_ANALYSIS, SKILL_BLF_TO_MDF_CONVERSION}
        and fallback.action == "call_tool"
        and fallback.tool == SKILL_FAULT_TABLE_SEARCH
        and is_skill_enabled(SKILL_FAULT_TABLE_SEARCH)
        and action not in {"cancel", "status", "new_request"}
    ):
        action = "call_tool"
        tool = SKILL_FAULT_TABLE_SEARCH
        task_type = "fault_degradation"
    if (
        allow_fallback_override
        and tool not in {SKILL_REQUIREMENT_INTAKE, SKILL_ROOT_CAUSE_ANALYSIS, SKILL_FUNCTION_EXIT_ANALYSIS, SKILL_BATCH_ROOT_CAUSE_ANALYSIS, SKILL_KPI_REPORT_ANALYSIS, SKILL_BLF_TO_MDF_CONVERSION}
        and fallback.action == "call_tool"
        and fallback.tool == SKILL_PERFORMANCE_CAPABILITY_SEARCH
        and action not in {"cancel", "status", "new_request"}
    ):
        action = "call_tool"
        tool = SKILL_PERFORMANCE_CAPABILITY_SEARCH
        task_type = "performance_analysis"
    if (
        allow_fallback_override
        and tool not in {SKILL_REQUIREMENT_INTAKE, SKILL_ROOT_CAUSE_ANALYSIS, SKILL_FUNCTION_EXIT_ANALYSIS, SKILL_BATCH_ROOT_CAUSE_ANALYSIS, SKILL_KPI_REPORT_ANALYSIS, SKILL_BLF_TO_MDF_CONVERSION}
        and fallback.action == "call_tool"
        and fallback.tool in {SKILL_ACTUATOR_REFERENCE_SEARCH, SKILL_ACTUATOR_HANDSHAKE_ANALYSIS}
        and action not in {"cancel", "status", "new_request"}
    ):
        action = "call_tool"
        tool = fallback.tool
        task_type = "actuator_handshake"

    if tool == SKILL_RESULT_TRANSFORMER and fallback.tool and fallback.tool != SKILL_RESULT_TRANSFORMER:
        action = "call_tool"
        tool = fallback.tool
        task_type = fallback.task_type

    if tool == SKILL_REQUIREMENT_INTAKE:
        action = "call_tool"
        task_type = None

    if tool and not is_skill_enabled(tool):
        return AgentDecision(
            action="reply",
            tool=None,
            task_type=None,
            database_name=database_name,
            problem_groups=groups,
            project_type=project_type,
            project_supported=project_supported,
            tool_query=str(parsed.get("tool_query") or "").strip() or fallback.tool_query,
            reply="这个能力当前已在配置表里关闭，暂时不能处理。可以联系维护者打开后再试。",
        )

    return AgentDecision(
        action=action,
        tool=tool,
        task_type=task_type,
        database_name=database_name,
        problem_groups=groups,
        project_type=project_type,
        project_supported=project_supported,
        tool_query=str(parsed.get("tool_query") or "").strip() or fallback.tool_query,
        reply=str(parsed.get("reply") or "").strip() or fallback.reply,
    )


def heuristic_agent_decision(user_text: str, database_names: list[str], session_state: dict[str, Any]) -> AgentDecision:
    if is_feedback_report_request(user_text):
        return AgentDecision(
            action="call_tool",
            tool=SKILL_FEEDBACK_REPORT,
            task_type=None,
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query=user_text,
            reply="",
        )

    if is_smalltalk_chat_text(user_text):
        return AgentDecision(
            action="call_tool",
            tool=SKILL_SMALLTALK_CHAT,
            task_type=None,
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query="",
            reply=smalltalk_reply_for_text(user_text),
        )

    if is_blf_to_mdf_conversion_intent(user_text):
        return AgentDecision(
            action="call_tool",
            tool=SKILL_BLF_TO_MDF_CONVERSION,
            task_type=TASK_BLF_TO_MDF_CONVERSION,
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query=user_text,
            reply="",
        )

    if is_function_exit_analysis_intent(user_text, session_state):
        return AgentDecision(
            action="call_tool",
            tool=SKILL_FUNCTION_EXIT_ANALYSIS,
            task_type="function_exit_analysis",
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query=user_text,
            reply="",
        )

    if is_batch_root_cause_analysis_intent(user_text):
        return AgentDecision(
            action="call_tool",
            tool=SKILL_BATCH_ROOT_CAUSE_ANALYSIS,
            task_type="batch_root_cause_analysis",
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query=user_text,
            reply="",
        )

    if is_kpi_report_analysis_intent(user_text):
        return AgentDecision(
            action="call_tool",
            tool=SKILL_KPI_REPORT_ANALYSIS,
            task_type=TASK_KPI_REPORT_ANALYSIS,
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query=user_text,
            reply="",
        )

    if is_kpi_report_analysis_near_miss(user_text):
        return AgentDecision(
            action="ask",
            tool=None,
            task_type=None,
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query=user_text,
            reply=kpi_report_near_miss_reply(),
        )

    if is_batch_root_cause_analysis_near_miss(user_text):
        return AgentDecision(
            action="ask",
            tool=None,
            task_type=None,
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query=user_text,
            reply=batch_root_cause_near_miss_reply(),
        )

    if is_root_cause_analysis_intent(user_text, session_state):
        groups = infer_problem_groups(user_text) or normalize_problem_groups(session_state.get("problem_groups"))
        return AgentDecision(
            action="call_tool",
            tool=SKILL_ROOT_CAUSE_ANALYSIS,
            task_type="root_cause_analysis",
            database_name=session_state.get("database"),
            problem_groups=groups,
            tool_query=user_text,
            reply="",
        )

    if is_dtc_query_text(user_text):
        return AgentDecision(
            action="call_tool",
            tool=SKILL_DTC_CODE_SEARCH,
            task_type="fault_degradation",
            database_name=session_state.get("database"),
            problem_groups=[],
            tool_query=user_text,
            reply="",
        )

    last_result = session_state.get("last_skill_result") or {}
    last_tool = last_result.get("source_skill")
    last_skill = get_skill(last_tool) if last_tool else None
    if last_skill and last_skill.category == "query" and is_contextual_followup_text(user_text):
        previous_question = str(last_result.get("user_text") or "").strip()
        return AgentDecision(
            action="call_tool",
            tool=last_tool,
            task_type=last_skill.task_type,
            database_name=session_state.get("database"),
            problem_groups=normalize_problem_groups(session_state.get("problem_groups")),
            tool_query=f"{previous_question}\n用户追问：{user_text}".strip(),
            reply="",
        )

    if is_actuator_reference_query_text(user_text):
        return AgentDecision(
            action="call_tool",
            tool=SKILL_ACTUATOR_REFERENCE_SEARCH,
            task_type="actuator_handshake",
            database_name=session_state.get("database"),
            problem_groups=normalize_problem_groups(session_state.get("problem_groups")),
            tool_query=user_text,
            reply="",
        )

    if is_performance_capability_question(user_text, session_state):
        groups = infer_problem_groups(user_text) or normalize_problem_groups(session_state.get("problem_groups"))
        return AgentDecision(
            action="call_tool",
            tool=SKILL_PERFORMANCE_CAPABILITY_SEARCH,
            task_type="performance_analysis",
            database_name=session_state.get("database"),
            problem_groups=groups,
            tool_query=user_text,
            reply="",
        )

    workflow = heuristic_workflow_decision(
        user_text,
        database_names,
        session_state.get("task_type"),
        session_state.get("database"),
        session_state.get("problem_groups"),
    )
    action = "call_tool" if workflow.task_type else "reply"
    tool = None
    if workflow.task_type == "fault_degradation":
        tool = SKILL_FAULT_TABLE_SEARCH if is_fault_scope_question(user_text) else SKILL_FAULT_DIAGNOSIS
    elif workflow.task_type == "performance_analysis":
        tool = SKILL_PERFORMANCE_ANALYSIS_SETUP
    elif workflow.task_type == "actuator_handshake":
        tool = (
            SKILL_ACTUATOR_HANDSHAKE_ANALYSIS
            if is_actuator_analysis_intent(user_text, session_state)
            else SKILL_ACTUATOR_REFERENCE_SEARCH
        )
    if workflow.intent_type == "dev_request":
        action = "call_tool" if tool else "reply"
    if workflow.intent_type == "capability_question" and not tool:
        action = "reply"
    return AgentDecision(
        action=action,
        tool=tool,
        task_type=workflow.task_type,
        database_name=workflow.database_name,
        problem_groups=workflow.problem_groups,
        project_type=workflow.project_type,
        project_supported=workflow.project_supported,
        tool_query=user_text,
        reply=workflow.reply,
    )


def analyze_workflow_request(
    user_text: str,
    database_names: list[str],
    current_task: str | None = None,
    current_database: str | None = None,
    current_problem_groups: list[str] | None = None,
) -> WorkflowDecision:
    heuristic = heuristic_workflow_decision(
        user_text,
        database_names,
        current_task,
        current_database,
        current_problem_groups,
    )

    raw = call_chat_completion(
        [
            {"role": "system", "content": WORKFLOW_PROMPT},
            {
                "role": "user",
                "content": build_workflow_user_prompt(
                    user_text,
                    database_names,
                    current_task,
                    current_database,
                    current_problem_groups,
                ),
            },
        ]
    )
    parsed = parse_json_object(raw or "")
    if not isinstance(parsed, dict):
        print(
            "[workflow-llm] fallback=heuristic "
            f"user_text={user_text!r} raw={raw!r} "
            f"heuristic_intent={heuristic.intent_type} heuristic_task={heuristic.task_type} "
            f"heuristic_project={heuristic.project_type} heuristic_supported={heuristic.project_supported} "
            f"heuristic_db={heuristic.database_name} heuristic_groups={heuristic.problem_groups}",
            flush=True,
        )
        return heuristic

    decision = merge_llm_decision(parsed, heuristic, database_names, user_text)
    print(
        "[workflow-llm] "
        f"user_text={user_text!r} "
        f"parsed_intent={parsed.get('intent_type')!r} parsed_task={parsed.get('task_type')!r} "
        f"parsed_project={parsed.get('project_type')!r} parsed_supported={parsed.get('project_supported')!r} "
        f"parsed_db={parsed.get('database_name')!r} parsed_groups={parsed.get('problem_groups')!r} "
        f"final_intent={decision.intent_type} final_task={decision.task_type} "
        f"final_project={decision.project_type} final_supported={decision.project_supported} "
        f"final_db={decision.database_name} final_groups={decision.problem_groups}",
        flush=True,
    )
    return decision


def analyze_session_message(
    user_text: str,
    current_stage: str | None = None,
    current_task: str | None = None,
) -> SessionIntentDecision:
    heuristic = heuristic_session_intent(user_text, current_stage, current_task)
    raw = call_chat_completion(
        [
            {"role": "system", "content": SESSION_INTENT_PROMPT},
            {
                "role": "user",
                "content": (
                    f"用户消息：{user_text}\n"
                    f"当前阶段：{current_stage or 'null'}\n"
                    f"当前任务：{current_task or 'null'}"
                ),
            },
        ]
    )
    parsed = parse_json_object(raw or "")
    if not isinstance(parsed, dict):
        return heuristic

    action = str(parsed.get("action") or "").strip()
    if action not in {"cancel", "status", "back", "change_database", "check_upload", "new_request", "continue"}:
        action = heuristic.action
    reply = str(parsed.get("reply") or "").strip() or heuristic.reply
    return SessionIntentDecision(action=action, reply=reply)


def merge_llm_decision(
    parsed: dict[str, Any],
    heuristic: WorkflowDecision,
    database_names: list[str],
    user_text: str = "",
) -> WorkflowDecision:
    intent_type = str(parsed.get("intent_type") or "").strip()
    if intent_type not in SUPPORTED_INTENT_TYPES:
        intent_type = heuristic.intent_type

    task_type = parsed.get("task_type")
    if task_type not in VALID_TASK_TYPES:
        task_type = heuristic.task_type

    project_type = str(parsed.get("project_type") or "").strip().upper()
    if project_type not in SUPPORTED_PROJECT_TYPES:
        project_type = heuristic.project_type

    project_supported = parsed.get("project_supported")
    if not isinstance(project_supported, bool):
        project_supported = heuristic.project_supported

    database_name = parsed.get("database_name")
    if database_name not in database_names:
        database_name = heuristic.database_name

    groups = normalize_problem_groups(parsed.get("problem_groups"))
    if not groups:
        groups = heuristic.problem_groups

    reply = str(parsed.get("reply") or "").strip() or heuristic.reply

    if (
        intent_type == "capability_question"
        and heuristic.intent_type == "analysis_request"
        and has_strong_analysis_intent(user_text)
    ):
        intent_type = "analysis_request"

    if task_type == "fault_degradation":
        if project_type == "J6B":
            project_supported = True
        elif project_type in {"8650", "BMC"}:
            project_supported = False
            if intent_type == "analysis_request":
                intent_type = "dev_request"
        elif heuristic.project_supported is not None and project_supported is None:
            project_supported = heuristic.project_supported
    else:
        project_supported = None if project_supported is None else project_supported

    return WorkflowDecision(
        intent_type=intent_type,
        task_type=task_type,
        project_type=project_type,
        project_supported=project_supported,
        database_name=database_name,
        problem_groups=groups,
        reply=reply,
    )


def heuristic_workflow_decision(
    user_text: str,
    database_names: list[str],
    current_task: str | None = None,
    current_database: str | None = None,
    current_problem_groups: list[str] | None = None,
) -> WorkflowDecision:
    text = user_text or ""
    if is_plain_greeting_text(text):
        return WorkflowDecision(
            intent_type="clarify",
            task_type=None,
            project_type=None,
            project_supported=None,
            database_name=current_database,
            problem_groups=[],
            reply="你好，我在。你可以直接说要查什么或分析什么。",
        )

    normalized = normalize_text(text)
    task_type = current_task
    project_type = infer_project_type(text)
    project_supported = None
    database_name = current_database
    problem_groups = normalize_problem_groups(current_problem_groups)

    requested_groups = infer_problem_groups(text)
    if requested_groups:
        problem_groups = requested_groups

    matched_database = match_database_name(text, database_names)
    if matched_database:
        database_name = matched_database

    if project_type == "J6B":
        project_supported = True
    elif project_type in {"8650", "BMC"}:
        project_supported = False

    keyword_task_type = infer_keyword_task_type(text, current_task)

    if is_fault_scope_question(text):
        return WorkflowDecision(
            intent_type="capability_question",
            task_type="fault_degradation",
            project_type=project_type,
            project_supported=project_supported,
            database_name=database_name,
            problem_groups=problem_groups,
            reply=build_fault_scope_reply(text, project_type),
        )

    if is_capability_question(text):
        return WorkflowDecision(
            intent_type="capability_question",
            task_type=keyword_task_type,
            project_type=project_type,
            project_supported=project_supported if keyword_task_type == "fault_degradation" else None,
            database_name=database_name,
            problem_groups=problem_groups,
            reply=build_capability_reply(problem_groups, keyword_task_type),
        )

    task_type = keyword_task_type
    if task_type is None and should_default_to_performance_analysis(text, normalized, problem_groups):
        task_type = "performance_analysis"

    if task_type == "actuator_handshake":
        return WorkflowDecision(
            intent_type="analysis_request",
            task_type=task_type,
            project_type=project_type,
            project_supported=None,
            database_name=database_name,
            problem_groups=problem_groups,
            reply="已识别为执行器握手诊断。你可以直接问断握手/降级原因，也可以上传 `.blf` / `.h5` / `.hdf5` 总线数据，我会按原始接口文档检查执行器响应是否闭环。",  # noqa: E501
        )

    if task_type == "root_cause_analysis":
        return WorkflowDecision(
            intent_type="analysis_request",
            task_type=task_type,
            project_type=project_type,
            project_supported=None,
            database_name=database_name,
            problem_groups=problem_groups,
            reply=(
                "已识别为行车问题原因分析。请上传标准 `.h5` 总线数据，"
                "如果手头是 `.blf`、`.hdf5` 或其他格式，请先转换成 `.h5`。"
                "我会同时检查性能表现和执行器接口响应，判断更像执行器侧还是算法/控制侧。"
            ),
        )

    if task_type == "fault_degradation":
        if project_supported is False:
            return WorkflowDecision(
                intent_type="dev_request",
                task_type=task_type,
                project_type=project_type,
                project_supported=False,
                database_name=database_name,
                problem_groups=problem_groups,
                reply=f"故障降级诊断目前仅支持 J6B 项目，{project_type} 项目还在开发中。你可以继续描述真实需求，我会按当前能力配置重新判断可用入口。",  # noqa: E501
            )
        return WorkflowDecision(
            intent_type="analysis_request",
            task_type=task_type,
            project_type=project_type,
            project_supported=project_supported,
            database_name=database_name,
            problem_groups=problem_groups,
            reply="已识别为故障降级诊断。你可以直接上传 slog / DID 日志文件，或者直接粘贴 fault_event、62 FD A3 / 62 FD A7 字节串、FDA3/FDA7 故障码给我。",  # noqa: E501
        )

    if task_type == "performance_analysis":
        if database_name:
            reply = (
                f"已识别为性能问题分析，先按 `{database_name}` 处理。"
                "请直接上传 `.blf`、`.h5` 或 `.hdf5` 文件，我会自动开始分析。"
            )
        else:
            reply = (
                "已识别为性能问题分析。数据库如果你不指定，我会默认选最新版本；"
                "你也可以直接点名某个数据库版本，或者上传新的 `.arxml` 数据库文件。"
            )
        return WorkflowDecision(
            intent_type="analysis_request",
            task_type=task_type,
            project_type=project_type,
            project_supported=None,
            database_name=database_name,
            problem_groups=problem_groups,
            reply=reply,
        )

    if database_name:
        return WorkflowDecision(
            intent_type="clarify",
            task_type=task_type,
            project_type=project_type,
            project_supported=project_supported,
            database_name=database_name,
            problem_groups=problem_groups,
            reply=f"我已经识别到数据库 `{database_name}`。你可以继续说想查什么、分析什么，或者直接上传对应文件；我会按当前能力配置选择可用流程。",  # noqa: E501
        )

    return WorkflowDecision(
        intent_type="clarify",
        task_type=task_type,
        project_type=project_type,
        project_supported=project_supported,
        database_name=database_name,
        problem_groups=problem_groups,
        reply=build_public_capability_text(problem_groups, task_type),
    )


def build_capability_reply(problem_groups: list[str], task_type: str | None = None) -> str:
    return build_public_capability_text(problem_groups, task_type)


def is_actuator_analysis_intent(user_text: str, session_state: dict[str, Any] | None = None) -> bool:
    session_state = session_state or {}
    value = (user_text or "").lower()
    if (
        session_state.get("task_type") == "actuator_handshake"
        and session_state.get("stage") == "waiting_file"
        and has_explicit_data_problem_analysis_intent(user_text)
    ):
        return True

    file_tokens = [
        ".blf",
        ".h5",
        ".hdf5",
        "blf",
        "h5",
        "hdf5",
        "上传",
        "文件",
        "附件",
        "总线数据",
        "这段数据",
        "数据里",
        "数据里面",
        "can数据",
        "跑一下",
        "跑一遍",
        "检查一下数据",
    ]
    analysis_tokens = [
        "分析",
        "诊断",
        "识别",
        "检查",
        "看一下",
        "帮我看",
        "问题点",
        "为什么没有响应",
        "为什么没响应",
        "为什么不响应",
        "没有响应",
        "没响应",
        "不响应",
        "断握手了",
        "功能降级了",
        "不闭环",
        "未闭环",
        "不符合设计",
    ]
    has_file_or_data = any(token in value for token in file_tokens)
    has_analysis = any(token in value for token in analysis_tokens)
    return has_file_or_data and has_analysis and has_explicit_data_problem_analysis_intent(user_text)


def is_blf_to_mdf_conversion_intent(user_text: str) -> bool:
    value = (user_text or "").lower()
    if "blf" not in value:
        return False
    conversion_terms = [
        "mdf",
        "mf4",
        "转换",
        "转成",
        "转为",
        "转一下",
        "导出",
        "生成",
        "格式转换",
    ]
    diagnosis_terms = ["原因", "为什么", "诊断", "分析问题", "行车问题", "顿挫", "重制动", "误制动"]
    return any(term in value for term in conversion_terms) and not any(term in value for term in diagnosis_terms)


def is_function_exit_analysis_intent(user_text: str, session_state: dict[str, Any] | None = None) -> bool:
    session_state = session_state or {}
    value = (user_text or "").lower()
    if not value:
        return False
    if (
        session_state.get("task_type") == "function_exit_analysis"
        and session_state.get("stage") == "waiting_file"
    ):
        return True

    scope_terms = ["acc", "lcc", "自适应巡航", "巡航", "车道居中"]
    exit_terms = [
        "功能退出",
        "退出原因",
        "退出原因码",
        "原因码",
        "退出码",
        "退出条件",
        "为什么退出",
        "退出点",
        "抑制原因",
        "临时退出",
        "退出了",
        "退出触发",
        "退出规则",
        "退出定义",
    ]
    context_terms = [
        "行车问题原因分析",
        "功能退出分析",
        "退出原因分析",
        "acc/lcc功能退出",
        "acc 功能退出",
        "lcc 功能退出",
    ]
    correction_terms = ["不是执行器", "不是握手", "基于h5", "h5的功能退出", "功能退出原因码"]

    has_scope = any(term in value for term in scope_terms)
    has_exit = any(term in value for term in exit_terms)
    has_context = any(term in value for term in context_terms)
    if any(term in value for term in correction_terms) and (has_exit or has_context):
        return True
    return has_exit and (has_scope or has_context or "功能退出" in value)


def _current_user_utterance_text(user_text: str) -> str:
    value = str(user_text or "").strip()
    if "用户更正：" in value:
        value = value.rsplit("用户更正：", 1)[-1].strip()
    if "引用消息上下文：" in value:
        value = value.split("引用消息上下文：", 1)[0].strip()
    return value


def _normalize_fixed_command_text(user_text: str) -> str:
    value = _current_user_utterance_text(user_text)
    value = re.sub(r"<at[^>]*>.*?</at>", "", value, flags=re.IGNORECASE)
    value = re.sub(
        r"^(?:@)?(?:布鲁Mate|布鲁|行车分析助手|行车Agent|_user_1)\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+", "", value)
    return value.strip("，,。.!！?？；;：:")


def is_batch_root_cause_analysis_intent(user_text: str) -> bool:
    return _normalize_fixed_command_text(user_text) == BATCH_ROOT_CAUSE_FIXED_COMMAND


def is_batch_root_cause_analysis_near_miss(user_text: str) -> bool:
    if is_batch_root_cause_analysis_intent(user_text):
        return False
    normalized = _normalize_fixed_command_text(user_text)
    if not normalized:
        return False
    value = normalized.lower()
    if BATCH_ROOT_CAUSE_FIXED_COMMAND in normalized:
        return True

    has_table = any(term in value for term in ["飞书多维表格", "多维表格", "bitable", "base"])
    if not has_table and "表格" in normalized:
        has_table = any(term in normalized for term in ["飞书", "多维", "批量"])
    if not has_table:
        return False

    has_action = any(term in normalized for term in ["分析", "处理", "跑", "执行", "批量", "回写"])
    has_problem = any(term in normalized for term in ["问题", "待分析", "结论", "原因", "行车问题"])
    return has_action and has_problem


def batch_root_cause_near_miss_reply() -> str:
    return (
        "我看这句话像是在说飞书多维表格里的待分析问题，但这个入口为了避免误触发，"
        "只能用固定指令启动。\n\n"
        f"正确说法是：`{BATCH_ROOT_CAUSE_FIXED_COMMAND}`\n\n"
        "如果你确实要批量处理表格，请重新发送上面这句，我会读取配置好的多维表格并显示处理进度。"
        "如果不是，请直接告诉我真正要做什么，比如查询执行器文档、分析上传数据、查看功能退出原因等。"
    )


def is_kpi_report_analysis_intent(user_text: str) -> bool:
    return _normalize_fixed_command_text(user_text) == KPI_REPORT_FIXED_COMMAND


def is_kpi_report_analysis_near_miss(user_text: str) -> bool:
    if is_kpi_report_analysis_intent(user_text):
        return False
    normalized = _normalize_fixed_command_text(user_text)
    if not normalized:
        return False
    value = normalized.lower()
    has_table = any(term in value for term in ("飞书多维表格", "多维表格", "bitable", "base"))
    has_kpi = "kpi" in value or "指标报告" in normalized or "经营报告" in normalized
    has_action = any(term in normalized for term in ("分析", "生成", "处理", "执行", "回写"))
    return has_table and has_kpi and has_action


def kpi_report_near_miss_reply() -> str:
    return (
        "我看这句话像是在要求分析多维表格里的 KPI。为了避免误回写，批量入口只能用固定指令启动。\n\n"
        f"正确说法是：`{KPI_REPORT_FIXED_COMMAND}`\n\n"
        "重新发送这句后，我会只读取待分析记录的报告内容，并把 AI 分析、原文可证实原因和改进建议写回对应列。"
    )


def is_root_cause_analysis_intent(user_text: str, session_state: dict[str, Any] | None = None) -> bool:
    session_state = session_state or {}
    value = (user_text or "").lower()
    if not value:
        return False
    if (
        session_state.get("task_type") == "root_cause_analysis"
        and session_state.get("stage") == "waiting_file"
        and has_explicit_data_problem_analysis_intent(user_text)
    ):
        return True

    data_terms = [
        "上传",
        "文件",
        "附件",
        "数据",
        "总线",
        "这段",
        "下面",
        ".blf",
        ".h5",
        ".hdf5",
        "blf",
        "h5",
        "hdf5",
    ]
    cause_terms = [
        "原因",
        "导致",
        "为什么",
        "归因",
        "根因",
        "是哪边",
        "哪边",
        "执行器还是算法",
        "算法还是执行器",
        "控制侧",
        "策略侧",
        "响应侧",
        "两个都分析",
        "两条都看",
        "综合判断",
        "综合分析",
        "行车问题原因分析",
        "综合归因",
    ]
    phenomenon_terms = [
        "顿挫",
        "颠簸",
        "抖动",
        "重制动",
        "重刹",
        "急减速",
        "不合理低速",
        "误制动",
        "漏制动",
        "未达到",
        "setspeed",
        "set speed",
        "设速",
        "溜车",
        "跟车距离",
        "车距异常",
        "距离异常",
    ]
    has_data = any(term in value for term in data_terms)
    has_cause = any(term in value for term in cause_terms)
    has_phenomenon = any(term in value for term in phenomenon_terms)
    explicit_data_analysis = has_explicit_data_problem_analysis_intent(user_text)
    return (explicit_data_analysis and has_data and has_cause and has_phenomenon) or (
        has_data and any(term in value for term in ["两个都分析", "两条都看", "综合判断", "行车问题原因分析", "综合归因"])
    )


def has_explicit_data_problem_analysis_intent(text: str) -> bool:
    value = (text or "").lower()
    if not value:
        return False
    data_terms = [
        "上传",
        "文件",
        "附件",
        "数据",
        "总线",
        "这段",
        "下面",
        ".blf",
        ".h5",
        ".hdf5",
        "blf",
        "h5",
        "hdf5",
    ]
    analysis_terms = [
        "分析",
        "诊断",
        "排查",
        "定位",
        "识别",
        "检查",
        "跑一下",
        "跑一遍",
    ]
    problem_terms = [
        "问题",
        "异常",
        "原因",
        "为什么",
        "导致",
        "根因",
        "归因",
        "顿挫",
        "颠簸",
        "抖动",
        "重制动",
        "重刹",
        "急减速",
        "误制动",
        "漏制动",
        "不合理低速",
        "未达到",
        "setspeed",
        "set speed",
        "溜车",
        "跟车距离",
        "没有响应",
        "没响应",
        "不响应",
        "断握手",
        "降级",
        "不闭环",
        "未闭环",
    ]
    explicit_phrases = [
        "分析数据中",
        "分析这段数据",
        "分析下面数据",
        "基于数据分析",
        "从数据里分析",
        "从数据中分析",
        "数据中出现",
        "数据里出现",
        "上传数据分析",
        "上传文件分析",
        "上传 h5 分析",
        "上传 blf 分析",
        "我上传",
    ]
    has_data = any(term in value for term in data_terms)
    has_analysis = any(term in value for term in analysis_terms)
    has_problem = any(term in value for term in problem_terms)
    has_explicit_phrase = any(phrase in value for phrase in explicit_phrases)
    return has_data and has_problem and (has_analysis or has_explicit_phrase)


def infer_keyword_task_type(user_text: str, current_task: str | None = None) -> str | None:
    value = (user_text or "").lower()
    if is_function_exit_analysis_intent(user_text):
        return "function_exit_analysis"
    if is_root_cause_analysis_intent(user_text):
        return "root_cause_analysis"
    if any(token in value for token in ["执行器", "握手", "actuator", "制动断", "驱动断"]):
        return "actuator_handshake"
    actuator_aliases = ["aeb", "cib", "ebcm", "ebooster", "pdf", "ecp", "ecm", "制动", "刹车", "制动系统", "驱动系统", "动力系统"]
    actuator_context = [
        "握手",
        "断握手",
        "降级",
        "不闭环",
        "未闭环",
        "响应",
        "响应异常",
        "没有响应",
        "没响应",
        "无响应",
        "不响应",
        "未响应",
        "request honored",
        "denied",
        "failed",
        "limited",
        "状态",
        "流程",
        "跳转",
        "流转",
        "条件",
        "置位",
        "取值",
        "设计要求",
    ]
    if any(token in value for token in actuator_aliases) and any(token in value for token in actuator_context):
        return "actuator_handshake"
    if any(
        token in value
        for token in [
            "故障诊断",
            "故障降级",
            "fault_event",
            "did",
            "fda3",
            "fda7",
            "故障码",
            "miss",
            "报文丢失",
            "信号丢失",
            "总线信号",
            "62fda3",
            "62fda7",
            "62 fd a3",
            "62 fd a7",
        ]
    ):
        return "fault_degradation"
    if any(token in value for token in ["性能", "performance"]):
        if has_explicit_data_problem_analysis_intent(user_text):
            return "performance_analysis"
        return current_task
    return current_task


def is_fault_scope_question(text: str) -> bool:
    value = (text or "").lower()
    if is_bus_miss_reference_question(value):
        return True
    has_fault_tokens = any(token in value for token in ["fda3", "fda7", "did", "故障码", "故障条目", "故障类型", "相关故障", "故障", "fault_event"])
    if not has_fault_tokens:
        return False
    question_tokens = ["哪些", "哪几条", "哪几个", "哪类", "关注", "需要关注", "不清楚", "找一下", "帮我找", "怎么看", "怎么看待", "先看", "重点看", "有什么"]
    scope_tokens = ["功能", "模块", "环视", "泊车", "停车", "acc", "apa", "rpa", "域控", "系统"]
    has_question = any(token in value for token in question_tokens) or "？" in value or "?" in value
    has_scope = any(token in value for token in scope_tokens)
    has_strong_upload_intent = any(token in value for token in ["上传", "日志", "字节串", "发你", "贴给你"])
    return has_fault_tokens and has_question and has_scope and not has_strong_upload_intent


def is_bus_miss_reference_question(text: str) -> bool:
    value = text or ""
    has_frame_id = bool(re.search(r"(?<![0-9a-f])(?:0x)?[0-9a-f]{3}(?![0-9a-f])", value, re.IGNORECASE))
    has_miss_intent = any(token in value.lower() for token in ["miss", "丢失", "收不到", "未收到", "报文", "总线", "信号"])
    has_question = any(token in value for token in ["原因", "导致", "可能", "为什么", "是什么", "排查"]) or "?" in value or "？" in value
    return has_frame_id and has_miss_intent and has_question


def build_fault_scope_reply(text: str, project_type: str | None) -> str:
    project_hint = ""
    if project_type == "J6B":
        project_hint = "如果这是 J6B 项目，我可以继续按 J6B 故障诊断口径帮你收窄范围。"
    elif project_type in {"8650", "BMC"}:
        project_hint = "不过故障降级诊断当前正式支持范围还是 J6B，8650 和 BMC 还在开发中。"
    return (
        "这更像是在咨询该功能需要重点关注哪些故障项，不一定要立刻上传日志。"
        "你可以直接告诉我是哪个项目、哪个模块，或者贴几个你已经看到的 FDA3/FDA7 编号，我先帮你梳理重点关注项。"
        + project_hint
    )


def heuristic_session_intent(
    user_text: str,
    current_stage: str | None = None,
    current_task: str | None = None,
) -> SessionIntentDecision:
    text = (user_text or "").strip().lower()
    if not text:
        return SessionIntentDecision()

    cancel_patterns = [
        "取消",
        "取消吧",
        "算了",
        "先不用",
        "先不用了",
        "不做了",
        "结束这次",
        "结束吧",
        "先停",
        "先停掉",
        "先别查",
    ]
    if any(token in text for token in cancel_patterns):
        return SessionIntentDecision(action="cancel", reply="好的，这次先帮你停在这里。")

    if "状态" in text or "进度" in text or "到哪一步" in text:
        return SessionIntentDecision(action="status", reply="我给你看一下当前状态。")

    back_patterns = ["返回", "上一步", "重新选数据库", "重新选择数据库", "重选数据库", "重新说需求"]
    if any(token in text for token in back_patterns):
        return SessionIntentDecision(action="back", reply="好，我带你回上一步。")

    database_patterns = ["换数据库", "改数据库", "换个数据库", "重新选库", "数据库改成", "数据库换成", "库改成"]
    if any(token in text for token in database_patterns):
        return SessionIntentDecision(action="change_database", reply="好，我们把数据库切一下。")

    upload_check_patterns = ["我上传了", "刚上传", "收到了吗", "看到附件", "看到文件", "没识别到", "怎么还没反应", "怎么没反应"]
    if any(token in text for token in upload_check_patterns):
        return SessionIntentDecision(action="check_upload", reply="我帮你检查一下最近上传的文件。")

    new_request_patterns = [
        "改成",
        "换成",
        "换个需求",
        "重新来一个",
        "不做这个了",
        "别查这个了",
        "不是",
        "不对",
        "错了",
        "用错",
        "应该是",
        "别用这个",
        "不是性能",
        "不是故障",
        "不是查询",
        "不是诊断",
    ]
    if any(token in text for token in new_request_patterns):
        return SessionIntentDecision(action="new_request", reply="好，我按新的需求重新开始。")

    if current_task == "fault_degradation" and any(token in text for token in ["性能分析", "acc", "apa", "abs", "tcs", "vse"]):
        return SessionIntentDecision(action="new_request", reply="好，我切到新的分析需求。")

    return SessionIntentDecision(action="continue", reply="")


def is_capability_question(text: str) -> bool:
    value = (text or "").lower()
    question_tokens = ["哪些", "什么", "支持", "有哪些", "哪几类", "哪类", "能看什么", "可以看什么"]
    capability_tokens = ["问题", "类型", "内容", "场景", "能分析", "可以分析", "能看", "可以看", "支持看"]
    strong_scope_patterns = [
        "哪些问题",
        "哪些类型",
        "支持哪些",
        "可以分析哪些",
        "能分析哪些",
        "你能看哪些",
    ]
    if any(pattern in value for pattern in strong_scope_patterns):
        return True
    if has_strong_analysis_intent_from_text(value):
        return False
    has_question = any(token in value for token in question_tokens) or "？" in value or "?" in value
    has_capability_scope = any(token in value for token in capability_tokens)
    return has_question and has_capability_scope


def has_strong_analysis_intent_from_text(text: str) -> bool:
    value = (text or "").lower()
    return any(
        token in value
        for token in [
            "我要分析",
            "我想分析",
            "我想要分析",
            "分析一个",
            "帮我分析",
            "做个分析",
            "做一下分析",
            "开始分析",
            "帮我诊断",
            "帮我排查",
            "我上传",
            "我给你上传",
        ]
    )


def has_strong_analysis_intent(text: str) -> bool:
    return has_strong_analysis_intent_from_text(text)


def should_default_to_performance_analysis(text: str, normalized: str, problem_groups: list[str]) -> bool:
    if not text or is_capability_question(text):
        return False
    if not problem_groups:
        return False
    return has_explicit_data_problem_analysis_intent(text)


def polish_analysis_summary(
    selected_problem_groups: list[str],
    status: str,
    problem_titles: list[str],
    event_count: int,
    image_count: int,
    warnings: list[str],
    fallback_summary: str,
) -> str:
    if status != "ok":
        return fallback_summary

    scope_text = format_problem_group_scope(selected_problem_groups)
    problem_text = "、".join(problem_titles[:5]) if problem_titles else "未识别到问题"
    warning_text = "；".join(warnings[:4]) if warnings else "无"
    prompt = (
        f"问题范围：{scope_text}\n"
        f"问题状态：{status}\n"
        f"识别到的问题：{problem_text}\n"
        f"问题数量：{len(problem_titles)}\n"
        f"问题时间点数量：{event_count}\n"
        f"图示数量：{image_count}\n"
        f"告警或提示：{warning_text}\n"
        f"保底总结：{fallback_summary}"
    )
    reply = call_chat_completion(
        [
            {"role": "system", "content": POLISH_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )
    return reply.strip() if reply and "调用失败" not in reply else fallback_summary


def build_workflow_user_prompt(
    user_text: str,
    database_names: list[str],
    current_task: str | None,
    current_database: str | None,
    current_problem_groups: list[str] | None,
) -> str:
    return (
        f"用户消息：{user_text}\n"
        f"当前 task_type：{current_task or 'null'}\n"
        f"当前 database_name：{current_database or 'null'}\n"
        f"当前 problem_groups：{','.join(current_problem_groups or []) or 'null'}\n"
        f"可选数据库：{'; '.join(database_names[:50])}"
    )


def call_chat_completion(
    messages: list[dict[str, str]],
    *,
    response_format: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
    config: LLMConfig | None = None,
    api_protocol: str | None = None,
) -> str:
    config = config if config is not None else get_llm_config()
    if config is None:
        return "当前聊天回复模型还没有配置好。请先配置 `KIMI_API_KEY`。"

    timeout = timeout_seconds if timeout_seconds is not None else config.timeout_seconds
    use_anthropic = (
        api_protocol in {"anthropic", "claude"}
        if api_protocol is not None
        else _should_use_anthropic_messages_api(config.api_base_url)
    )
    if use_anthropic:
        return _call_anthropic_messages_api(
            config,
            messages,
            response_format=response_format,
            timeout_seconds=timeout,
        )

    payload = {"model": config.model, "messages": messages, "stream": False}
    if response_format:
        payload["response_format"] = response_format
    max_tokens = _openai_chat_max_tokens()
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if _should_disable_openai_thinking(config):
        payload["thinking"] = {"type": "disabled"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    api_request = request.Request(
        url=f"{config.api_base_url}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
        },
        method="POST",
    )

    try:
        with request.urlopen(api_request, timeout=timeout) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return f"聊天模型调用失败：HTTP {exc.code}。{trim_error(detail)}"
    except Exception as exc:
        return f"聊天模型调用失败：{exc}"

    return extract_reply_text(response_data).strip()


def _should_use_anthropic_messages_api(api_base_url: str) -> bool:
    normalized = (api_base_url or "").strip().rstrip("/").lower()
    if normalized == "https://api.kimi.com/coding/v1":
        return False
    protocol = os.getenv("LLM_API_PROTOCOL", "").strip().lower()
    if protocol in {"anthropic", "claude"}:
        return True
    return normalized.endswith("/anthropic") or "/anthropic/" in normalized


def _openai_chat_max_tokens() -> int | None:
    value = os.getenv("LLM_MAX_TOKENS", "").strip()
    if not value:
        return None
    try:
        return max(256, int(value))
    except ValueError:
        return None


def _should_disable_openai_thinking(config: LLMConfig) -> bool:
    configured = os.getenv("LLM_DISABLE_THINKING", os.getenv("LLM_THINKING_DISABLED", "")).strip().lower()
    if configured in {"1", "true", "yes", "y", "on"}:
        return True
    if configured in {"0", "false", "no", "n", "off"}:
        return False
    base_url = (config.api_base_url or "").strip().lower()
    model = (config.model or "").strip().lower()
    return "api.kimi.com/coding" in base_url or model.startswith("kimi-")


def _anthropic_messages_url(api_base_url: str) -> str:
    base = (api_base_url or "").strip().rstrip("/")
    lowered = base.lower()
    if lowered.endswith("/v1/messages") or lowered.endswith("/messages"):
        return base
    if lowered.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


def _call_anthropic_messages_api(
    config: LLMConfig,
    messages: list[dict[str, str]],
    *,
    response_format: dict[str, Any] | None = None,
    timeout_seconds: int,
) -> str:
    system_parts: list[str] = []
    anthropic_messages: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "user").strip().lower()
        content = str(message.get("content") or "")
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        if anthropic_messages and anthropic_messages[-1]["role"] == role:
            anthropic_messages[-1]["content"] += "\n\n" + content
        else:
            anthropic_messages.append({"role": role, "content": content})

    if response_format:
        system_parts.append("请只返回一个合法 JSON object，不要输出 Markdown、代码块或解释性前后缀。")
    if not anthropic_messages:
        anthropic_messages.append({"role": "user", "content": "请回复。"})

    try:
        max_tokens = max(256, int(os.getenv("LLM_MAX_TOKENS", "4096").strip() or "4096"))
    except ValueError:
        max_tokens = 4096
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": anthropic_messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    api_request = request.Request(
        url=_anthropic_messages_url(config.api_base_url),
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": config.api_key,
            "Authorization": f"Bearer {config.api_key}",
            "anthropic-version": os.getenv("ANTHROPIC_VERSION", "2023-06-01").strip() or "2023-06-01",
        },
        method="POST",
    )

    try:
        with request.urlopen(api_request, timeout=timeout_seconds) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return f"聊天模型调用失败：HTTP {exc.code}。{trim_error(detail)}"
    except Exception as exc:
        return f"聊天模型调用失败：{exc}"

    return extract_anthropic_reply_text(response_data).strip()


def extract_anthropic_reply_text(response_data: dict[str, Any]) -> str:
    content = response_data.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" or "text" in item:
                    text_parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                text_parts.append(item)
        return "\n".join(part for part in text_parts if part)
    completion = response_data.get("completion")
    if isinstance(completion, str):
        return completion
    return ""


def extract_reply_text(response_data: dict[str, Any]) -> str:
    choices = response_data.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
        return "\n".join(part for part in text_parts if part)
    return ""


def parse_json_object(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.DOTALL).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def match_database_name(user_text: str, database_names: list[str]) -> str | None:
    lowered = user_text.lower().strip()
    normalized = normalize_text(user_text)
    exact = [name for name in database_names if name.lower() == lowered]
    if exact:
        return exact[0]
    contains = [name for name in database_names if lowered and lowered in name.lower()]
    if len(contains) == 1:
        return contains[0]
    normalized_matches = [name for name in database_names if normalized and normalized in normalize_text(name)]
    if len(normalized_matches) == 1:
        return normalized_matches[0]
    return None


def infer_problem_groups(user_text: str) -> list[str]:
    text = (user_text or "").lower()
    groups: list[str] = []
    if any(token in text for token in ["abs", "tcs", "vse", "稳定", "附着", "车身稳定"]):
        groups.append("stability_control")
    if any(
        token in text
        for token in [
            "acc",
            "跟车",
            "纵向",
            "重刹",
            "重制动",
            "急减速",
            "急刹",
            "误制动",
            "误刹",
            "setspeed",
            "set speed",
            "设速",
            "稳速",
            "不合理低速",
            "低速异常",
            "溜车",
            "回退",
        ]
    ):
        groups.append("acc_control")
    if any(token in text for token in ["apa", "泊车", "停车位"]):
        groups.append("apa_control")
    return normalize_problem_groups(groups)


def is_performance_capability_question(user_text: str, session_state: dict[str, Any] | None = None) -> bool:
    text = (user_text or "").lower()
    session_state = session_state or {}
    current_task = session_state.get("task_type")
    has_performance_context = current_task == "performance_analysis" or any(
        token in text for token in ["性能", "performance", "指标", "metric", "acc", "apa", "abs", "tcs", "vse"]
    )
    if not has_performance_context:
        return False
    detail_intent = any(
        token in text
        for token in [
            "哪些分析",
            "哪些问题",
            "问题条目",
            "具体问题",
            "具体点",
            "更具体",
            "能识别",
            "可以识别",
            "支持什么",
            "看什么",
            "分析什么",
            "有什么",
            "有哪些",
            "指标",
            "规则",
            "阈值",
            "判定",
            "信号",
            "列表",
            "表格",
            "总表",
            "问题类型",
            "关注信号",
            "算法请求",
            "执行器请求",
            "筛选代码",
            "预设表",
        ]
    )
    upload_or_run_intent = any(
        token in text
        for token in ["上传", "文件", ".h5", ".hdf5", ".blf", "开始分析", "帮我分析", "跑一下"]
    )
    return detail_intent and not upload_or_run_intent


def infer_project_type(user_text: str) -> str | None:
    text = (user_text or "").upper()
    if "J6B" in text:
        return "J6B"
    if "8650" in text:
        return "8650"
    if "BMC" in text:
        return "BMC"
    return None


def normalize_problem_groups(groups: Any) -> list[str]:
    if not isinstance(groups, list):
        return []
    return [str(group) for group in groups if str(group) in SUPPORTED_PROBLEM_GROUPS]


def format_problem_group_scope(groups: list[str] | None) -> str:
    normalized = [group for group in (groups or []) if group in PROBLEM_GROUP_LABELS]
    if not normalized:
        return "全部问题类型"
    return "、".join(PROBLEM_GROUP_LABELS[group] for group in normalized)


def normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def trim_error(detail: str, limit: int = 180) -> str:
    cleaned = " ".join(detail.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."
