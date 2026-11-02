# 布鲁 Ubuntu 22.04 一键迁移说明

当前只保留一种推荐迁移方式：在 Windows 工程里生成完整迁移包，把 `Agent_ubuntu_one_click.tar.gz` 拷贝到 Ubuntu，解压后运行包内安装脚本。
不要只拷贝 `install_blu_one_click.sh`。这个脚本依赖完整工程目录。

## 1. 在 Windows 生成迁移包

在 Windows 当前工程目录执行：

```powershell
cd <Windows工程目录>
python deploy\ubuntu_run\prepare_ubuntu_project.py
```

生成结果：

```text
<Windows工程目录>\dist\Agent_ubuntu_one_click\Agent
<Windows工程目录>\dist\Agent_ubuntu_one_click.tar.gz
```

Ubuntu 侧只需要拷贝 `Agent_ubuntu_one_click.tar.gz`。

## 2. 在 Ubuntu 一键安装

```bash
cd /home/<user>
tar -xzf Agent_ubuntu_one_click.tar.gz
cd Agent_ubuntu_one_click/Agent
chmod +x dist/install_blu_one_click.sh
./dist/install_blu_one_click.sh
```

脚本会自动完成：

1. 安装系统依赖。
2. 删除旧的 `~/.local/opt/blu-agent/.venv`，避免 Python 版本或依赖残留。
3. 使用 Ubuntu 系统 Python 3.10/3.11 创建虚拟环境。
4. 安装 `requirements.txt`。
5. 安装或刷新 `lark-cli`。
6. 写入用户级配置。
7. 生成启动、检查、授权、权限清单等命令。

安装后常用命令：

```bash
blu-agent-start
blu-agent-doctor
blu-agent-lark-auth
blu-agent-bot-permissions
blu-agent-configure
blu-agent-uninstall
```

如果提示找不到命令，执行：

```bash
source ~/.bashrc
```

### 安装当前仓库版本并启动

第一次先克隆私有仓库：

```bash
git clone git@github.com:cyh0000008/Agent_Longitudinal.git
cd Agent_Longitudinal
```

先手动获取想安装的代码版本：首次用 `git clone`；后续更新时在仓库目录执行 `git pull --ff-only origin main`。完成后，停止旧的机器人进程（如果正在运行，在其终端按 `Ctrl+C`），再执行安装脚本：

```bash
bash deploy/ubuntu_run/update_blu_agent.sh
```

脚本不会访问 Git 或拉取代码；它只安装当前本地仓库版本、刷新依赖、保留用户级 `.env`，运行健康检查后以前台方式启动。按 `Ctrl+C` 可停止机器人。请勿同时运行多个实例，也不要同时在 Windows 和 Ubuntu 启动同一个机器人。

只安装当前仓库版本、不启动机器人：

```bash
bash deploy/ubuntu_run/update_blu_agent.sh --no-start
```

首次安装或更新时如果缺少凭据，安装器会提示输入。配置文件保存在 `~/.config/blu-agent/blu-agent.env`，该文件不会被 Git 更新覆盖。车辆数据、数据库及机器专属运行配置也不在仓库中，需要提前通过获准渠道放到 Ubuntu 并配置对应路径。

## 3. 凭据和环境变量

迁移到 Ubuntu 后仍使用同一个飞书机器人和同一个模型 API 配置。下面这些值不需要因为换机器而改变：

```text
FEISHU_APP_ID
FEISHU_APP_SECRET
LLM_API_KEY
LLM_API_BASE_URL
LLM_MODEL
```

配置文件位置：

```text
~/.config/blu-agent/blu-agent.env
```

注意：不要让 Windows 和 Ubuntu 同时运行同一个机器人后端，否则群里会重复回复。

## 4. 飞书用户授权

如果多维表格或问题反馈报错：

```text
need_user_authorization
token_missing
requires scope
```

在 Ubuntu 上执行：

```bash
blu-agent-lark-auth
```

一键安装脚本默认不会自动执行这一步。批量行车问题原因分析优先使用应用/bot 的 OpenAPI 权限，不申请用户 OAuth；只有明确需要用户身份访问其他资源时，再手动执行 `blu-agent-lark-auth`。

如确需用户授权，应优先按 lark-cli 报错中的具体 missing scope 做最小授权，不要用宽泛的 `--domain base` 去申请整组 Base 用户权限。

## 5. 飞书机器人应用权限

如果错误指向 “bot/app lacks scope” 或飞书开放平台权限不足，执行：

```bash
blu-agent-bot-permissions
```

它会打印当前布鲁需要的机器人应用权限清单。然后到飞书开放平台中，对同一个 `FEISHU_APP_ID` 的应用开启对应权限、发布版本，并按企业要求完成审批。
批量行车问题分析会创建字段、回写记录并上传附件，因此需要同时开启多维表格记录、字段和附件相关权限。

本地脚本不能绕过飞书后台审批。

## 6. 运行时目录

Ubuntu 上所有上传文件、分析图、转换产物和批量分析临时文件都会写入当前用户自己的运行时目录：

```text
$XDG_RUNTIME_DIR/blu-agent
```

如果没有 `XDG_RUNTIME_DIR`，默认使用：

```text
~/.cache/blu-agent/runtime
```

不要把 `BLU_AGENT_RUNTIME_DIR` 或 `BLU_AGENT_RUNTIME` 设置为 `/tmp/blu_agent`、`/tmp/blu_agent_<用户名>` 或任何 `/tmp/blu_agent...` 旧运行目录。这类共享临时目录可能被 root 或其他用户创建，导致：

```text
[Errno 13] Permission denied
```

安装脚本和启动脚本会自动忽略旧的 `/tmp/blu_agent...` 配置，并创建以下子目录：

```text
uploads
analysis_results
batch_root_cause
feedback
result_transformer
actuator_handshake
bad_case_plot
root_cause_evidence
blf_mdf
axle_torque_accuracy
file_delivery
```

飞书附件下载和消息资源下载有一个额外约束：下载目标必须是“具体文件路径”，不能只传会话目录或无后缀 token。代码里统一通过 `ensure_writable_download_file_path(...)` 处理这类目标；它会把旧 `/tmp/blu_agent...` 路径迁移到当前用户 runtime，并在目标看起来像目录时自动追加真实文件名。新增下载、转换、批量分析、问题反馈等能力时，不要直接把 `uploads/<session_token>` 或 `/tmp/blu_agent/...` 当作输出文件传给 `lark-cli`、转换器或其他外部工具。

## 7. 批量行车问题原因分析

固定唤醒语句：

```text
@布鲁 分析飞书多维表格中的问题
```

运行中取消固定语句：

```text
@布鲁 取消当前问题分析
```

取消只作用于同一用户在同一群聊里正在运行的批量行车问题原因分析；已经完成回写的行不会回滚，正在分析但未完成的行会恢复为 `待分析`。

运行中的批量分析进度卡会直接显示取消方式；收到取消指令后，同一张卡会先显示 `取消中`，最终更新为 `已取消`，只有原进度卡更新失败时才会额外发送兜底提示。

多维表格里的关键业务列必须保留 `Agent_` 前缀；`JIRA链接` 是可选优先级列，按原名保留：

```text
Agent_问题描述
Agent_h5数据路径
Agent_问题类型
Agent_问题状态
Agent_问题日期
Agent_问题时间
Agent_车号
Agent_车型
JIRA链接
Agent_分析结论
Agent_结论概要
Agent_分析信号图示
```

只会分析 `Agent_问题状态 = 待分析` 的行。读取多维表格时，`BATCH_ROOT_CAUSE_SCAN_MAX_ROWS` 控制最多扫描多少条记录，默认 5000；`BATCH_ROOT_CAUSE_MAX_ROWS` 控制本轮最多处理多少条待分析记录，默认 0，表示处理扫描范围内所有待分析记录，直到全部跑完或用户发送取消指令。若表中存在 `JIRA链接` 列，且该列非空，这些待分析行会优先处理；同一优先级内仍按表格原顺序处理。没有 `JIRA链接` 列时保持原顺序。

`Agent_问题日期` / `Agent_问题时间` 用于在 H5 文件起始时间基础上筛选更接近真实问题发生时刻的候选窗口；历史表还未改名前，程序会兼容读取旧列名 `问题日期` / `问题时间`。

`Agent_车号` 必须使用数据源要求的完整车号格式，不要只填写不完整的短号。

`Agent_h5数据路径` 可以手动填写，也可以留空。已填写时会直接使用该路径；留空时，布鲁会根据 `Agent_车号`、`Agent_车型`、`Agent_问题日期`、`Agent_问题时间` 在 `BATCH_ROOT_CAUSE_H5_SEARCH_BASE_DIR` 下自动查找解析后的 H5，并把找到的绝对路径回写到 `Agent_h5数据路径`。自动查找不会把问题时间卡死在文件名同一分钟，而是先按车号/车型/日期和 H5 文件起始时间粗筛相邻文件，再读取 H5 内 `timestamp` 覆盖范围确认目标时间是否在该文件里。如果未找到对应 H5，会继续在 `BATCH_ROOT_CAUSE_BLF_SEARCH_BASE_DIR` 下按同样的车型、车号、日期和问题时间查找原始 BLF；找到后解析成 `<blf文件名>_parsed.h5`，输出到 `BATCH_ROOT_CAUSE_H5_SEARCH_BASE_DIR` 下对应的车型/车号目录和日期行程目录，再继续原来的 H5 分析和回写。若 H5 和 BLF 都找不到，`Agent_问题状态` 仍保持 `待分析`，`Agent_分析结论` 写入 `未找到对应h5数据，请确认数据解析是否完成`。自动查找仍要求最终路径是布鲁当前运行这台 Ubuntu 机器能访问的路径，不要填写用户自己 Windows 电脑上的路径。

一键安装时会检查 `BATCH_ROOT_CAUSE_H5_SEARCH_BASE_DIR` 是否可创建目录和写文件；如果没有权限，安装脚本会通过 `sudo` 尝试创建目录并用 ACL 给当前 Ubuntu 用户授权。若自动授权失败，脚本会停止并打印需要管理员执行的 `mkdir/setfacl` 命令。BLF 根目录只需要可读权限；如果 `BATCH_ROOT_CAUSE_BLF_SEARCH_BASE_DIR` 不可读，安装和 `blu-agent-doctor` 会给出警告。

飞书多维表格读取和写回走 `open.feishu.cn` OpenAPI。若遇到 `SSLEOFError`、连接断开、超时、429 或 5xx，会按 `BATCH_ROOT_CAUSE_FEISHU_RETRY_ATTEMPTS` 重试，默认 3 次。重试后仍失败时，群卡片会显示“写回失败”；这类错误属于飞书 OpenAPI/网络出口问题，不会再写成模型接口调用失败。

`Agent_问题类型` 当前覆盖纵向问题、`横向-猛打` 和 `功能退出`；`横向-猛打` 会检查 LCC 激活、方向盘角、转向请求和车道 C0/C1，并回写对应信号图。

### 7.1 单个上传 H5 与批量表格的一致性

Ubuntu 部署后，用户在飞书群里直接上传单个 `.h5` 做行车问题原因分析，和“分析飞书多维表格中的问题”使用同一个单条分析核心：

```text
batch_root_cause_analysis.run_single_batch_analysis(...)
```

只要环境变量保持：

```text
BATCH_ROOT_CAUSE_ANALYSIS_MODE=deepseek_investigation
```

两条入口都会走 `deepseek_root_cause_analysis.py` 的模型调查式链路。本地负责识别问题类型、按预设和问题描述生成信号查询计划、读取 H5 并压缩证据摘要、生成信号图；候选窗口、根因因素、结论概要和关键证据信号由当前配置的 `LLM_MODEL` 组织。

安装后可用下面命令确认链路没有漏文件或退回旧逻辑：

```bash
blu-agent-doctor
```

输出里应包含：

```text
[ OK ] DeepSeek root-cause mode
[ OK ] Root-cause single/batch shared chain
```

## 8. 轴端扭矩准确性校验

用户在群里提出“校验轴扭 / 轴端扭矩准不准 / 复算 IActAxleTrq”后，布鲁会等待上传 `.blf`，再使用 ECM/CCB ARXML 解目标报文并复算轴端扭矩。

关键配置：

```text
AXLE_TORQUE_ARXML
AXLE_TORQUE_OUTPUT_SCALE=1
AXLE_TORQUE_ABS_ERROR_THRESHOLD_NM=120
AXLE_TORQUE_MAE_ERROR_THRESHOLD_NM=90
AXLE_TORQUE_P95_ERROR_THRESHOLD_NM=240
AXLE_TORQUE_PASS_RATE_THRESHOLD=0.78
AXLE_TORQUE_PROGRESS_INTERVAL_SECONDS=2
```

`AXLE_TORQUE_ARXML` 可留空；留空时会优先找工程 `database/` 下的 ECM ARXML。若 Ubuntu 侧没有默认 ECM 数据库，使用该能力前可以先在群里上传新的 `.arxml`，再上传 `.blf`。

用户必须在飞书里先按编号选择车型/配置，再上传 BLF：`1=358-2`，Final Ratio `3.47`；`2=C1UL`，Final Ratio `3.47`；`3=E2LB-2`，Final Ratio `3.17`；`4=其他车型`，必须继续输入 Final Ratio。每次新的轴端扭矩校验都要重新选择车型/Final Ratio，不继承上一轮选择；未选择车型或缺少其他车型 Final Ratio 时不会默认使用 3.47。

飞书进度卡会在同一条卡片内实时刷新下载、BLF 扫描百分比、目标报文解析百分比、已读帧数、候选/匹配帧数，以及信号抽取、轴扭复算和图示生成阶段。需要降低刷新频率时可调大 `AXLE_TORQUE_PROGRESS_INTERVAL_SECONDS`。

轴端扭矩校验默认直接从 BLF 解目标报文到内存信号，不再先生成临时 MDF；普通“BLF 转 MDF”仍是独立能力。个别 BLF 的文件头绝对起始时间即使不在 MDF v4 可写范围内，也不会影响轴扭校验的相对时间轴分析。

当前已确认 `IActAxleTrq` 就是真实轴端扭矩，因此默认 `AXLE_TORQUE_OUTPUT_SCALE=1`，不再对文档公式复算结果做 0.5 折算。只有后续明确要按单侧/折半口径临时对比时，才手动覆盖该变量。

R 档已按项目确认值参与计算：档位编码 14，`i_gear=2.96`，`η_trans=1`，按正值进入复算公式；飞书图示中 R 档显示为 `-1`，避免把编码 14 误读成 14 挡。

说明书定义的最小动态输入包含动力流状态。优先读取 078 `PTEI_Transmission_Torque_Request_078_M` 中的 `TEPI_TrnsEngdPrFlwSt / TEPI_TrnsEngdPrFlwStV`；若 BLF 为 ICE 变体，可读取 076 `PTEI_Transmission_Torque_Request_076_M` 中的 `TEPI_TrnsEngdPrFlwSt_ICE / TEPI_TrnsEngdPrFlwStV_ICE`（含 ARXML 解码后的 `I` 前缀等价名）。两套都缺失时必须返回证据不足，不能用前进档位替代动力流状态继续给准/不准结论。

## 9. 常见问题

### 9.1 Permission denied: /tmp/blu_agent...

原因通常是旧版本使用了共享目录 `/tmp/blu_agent` 或 `/tmp/blu_agent_<用户名>`，该目录被 root 或其他用户占用。

处理：

```bash
grep -E 'BLU_AGENT_RUNTIME(_DIR)?|BLU_LARK_AUTH_SCOPES' ~/.config/blu-agent/blu-agent.env
```

确认里面不要出现任何 `/tmp/blu_agent...` 路径或 `BLU_LARK_AUTH_SCOPES`。重新运行安装脚本即可自动修正。

### 8.2 pip / npx 被本机代理卡住

如果安装时反复出现 `ProxyError`、`127.0.0.1:7897`、`_ssl.c:990`，或一直重试 `/simple/pip/`，通常是 Ubuntu shell 或用户级 `pip.conf` 里残留了本机代理配置。最新版安装脚本默认会清理 `127.0.0.1` / `localhost` 这类回环代理，并在未设置 `BLU_AGENT_KEEP_PROXY=1` 时让 pip 忽略用户级配置：

```bash
PIP_CONFIG_FILE=/dev/null
```

正常情况下直接重跑：

```bash
./dist/install_blu_one_click.sh
```

如果仍希望强制不走代理：

```bash
BLU_AGENT_NO_PROXY=1 ./dist/install_blu_one_click.sh
```

只有确认公司代理可用时，才保留代理：

```bash
BLU_AGENT_KEEP_PROXY=1 ./dist/install_blu_one_click.sh
```

### 8.3 Python 3.13 安装 numpy 失败

不要使用 Conda base 的 Python 3.13 创建环境。安装脚本会优先使用系统 Python 3.10/3.11，并在安装前删除旧 `.venv`。

### 8.4 多维表格提示需要授权

运行：

```bash
blu-agent-lark-auth
```

### 8.5 机器人权限不足

运行：

```bash
blu-agent-bot-permissions
```

然后在飞书开放平台给当前机器人应用补权限并发布版本。

## 9. 后续维护约束

如果后续代码改动影响 Ubuntu 迁移、启动脚本、环境变量、运行时目录、依赖或授权方式，必须同步更新：

```text
deploy/ubuntu_run/*
AGENT.md
dist/Agent_ubuntu_one_click.tar.gz
```

重新打包命令：

```powershell
python deploy\ubuntu_run\prepare_ubuntu_project.py
```
