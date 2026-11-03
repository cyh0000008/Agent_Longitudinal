# 布鲁 Agent

面向 ADAS / 行车数据分析场景的 Python 项目，包含飞书群机器人、本地批量分析界面、诊断与查询能力，以及 CAN 日志转换工具。

## 主要组成

- `main.py`：飞书事件入口、会话管理和能力调度。
- `llm_service.py`、`agent_skills.py`：自然语言意图编排和本地能力定义。
- `performance_analysis.py`、`root_cause_analysis.py`、`fault_diagnosis.py` 等：分析、诊断与查询实现。
- `local_batch_ui.py`、`local_batch_root_cause.py`：本地 Excel 批量分析入口。
- `FileConverter/`：BLF 与 MDF 转换及校验工具。
- `deploy/windows_run/`、`deploy/ubuntu_run/`：基于 Git 同步的 Windows / Ubuntu 飞书机器人安装与更新脚本。
- `deploy/intranet_batch/`：内网批量分析部署说明。

更详细的能力边界和开发约定见 [AGENT.md](AGENT.md)，当前状态参考 [PROJECT_STATUS.md](PROJECT_STATUS.md)。

## 环境准备

建议使用 Python 3.10 或 3.11。安装依赖：

```powershell
python -m pip install -r requirements.txt
```

本地运行前，按需配置 `.env` 中的飞书和模型 API 参数。可从部署目录的 `env.example` 参考变量名称；不要将真实 `.env`、密钥或访问令牌提交到 Git。

批处理指标工具使用机器本地的 `analysis_engine/config.json`。可复制 `analysis_engine/config.example.json` 并按实际数据目录修改。`config/` 中的运行配置和车辆数据文件需从获准来源单独提供，不纳入此仓库。

启动飞书机器人：

```powershell
python check_feishu_credentials.py
python main.py
```

飞书应用需要启用机器人、长连接事件订阅，并配置相应消息权限。Windows 部署见 [deploy/windows_run/README.md](deploy/windows_run/README.md)，Ubuntu 部署见 [deploy/ubuntu_run/README.md](deploy/ubuntu_run/README.md)。

KPI 分析仅输出无具体数字的定性摘要到“KPI报告AI分析结果”。“KPI报告AI分析图示”（附件列）保存本地依据原始 Excel 绘制的仪表盘：准出饼图、MPI/CPI 变化分布柱图、全部指标版本对比与明细表，图中保留真实数值和样本标注。绿色表头为当前版本，无基线只展示当前问题，不虚构提升。统一宽度、配色和版式，高度随指标数量增加。模型输入使用紧凑指标数组，不调用模型压缩或画图。图示上传失败不更新该行，临时图自动清理。中文字体需微软雅黑或 Noto Sans CJK。外部模型联调仅使用非敏感合成数据，不发送内部报告。

本地批量分析部署见 [deploy/intranet_batch/README.md](deploy/intranet_batch/README.md)。

## 外部数据与同步范围

KPI 重点对比仅保留相对变化达到 50% 且绝对差值大于 6 的指标，或准出结论改变的指标；AI 输入和图表使用相同筛选。ABS/TCS/VSE 关联 ACC 退出指标不进入分析或图表。下界值、零事件或缺失值不直接判升降；无基线只展示当前未准出项。准出分布总览仍使用其余全部有效指标。

Git 仓库只用于同步代码、项目文档和配置模板。运行所需的车辆日志、故障/DTC 表、ARXML/DBC 数据库、机器接口资料、运行配置、分析结果、临时文件、部署包和本地凭据不纳入版本控制。首次在另一台机器部署时，需要通过获准的数据渠道提供相应数据文件，并在本机配置环境变量。
