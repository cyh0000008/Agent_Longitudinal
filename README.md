# 布鲁 Agent

面向 ADAS / 行车数据分析场景的 Python 项目，包含飞书群机器人、本地批量分析界面、诊断与查询能力，以及 CAN 日志转换工具。

## 主要组成

- `main.py`：飞书事件入口、会话管理和能力调度。
- `llm_service.py`、`agent_skills.py`：自然语言意图编排和本地能力定义。
- `performance_analysis.py`、`root_cause_analysis.py`、`fault_diagnosis.py` 等：分析、诊断与查询实现。
- `local_batch_ui.py`、`local_batch_root_cause.py`：本地 Excel 批量分析入口。
- `FileConverter/`：BLF 与 MDF 转换及校验工具。
- `deploy/`：Ubuntu 飞书机器人和内网批量分析部署说明。

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

飞书应用需要启用机器人、长连接事件订阅，并配置相应消息权限。完整部署步骤见 [deploy/ubuntu_run/README.md](deploy/ubuntu_run/README.md)。

本地批量分析部署见 [deploy/intranet_batch/README.md](deploy/intranet_batch/README.md)。

## 外部数据与同步范围

Git 仓库只用于同步代码、项目文档和配置模板。运行所需的车辆日志、故障/DTC 表、ARXML/DBC 数据库、机器接口资料、运行配置、分析结果、临时文件、部署包和本地凭据不纳入版本控制。首次在另一台机器部署时，需要通过获准的数据渠道提供相应数据文件，并在本机配置环境变量。
