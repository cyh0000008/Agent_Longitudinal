# 布鲁 Agent 内网批量分析镜像部署

这个部署形态只保留“批量行车问题原因分析”能力，不启动飞书机器人，不读取飞书多维表格，也不安装或调用 `lark-cli`。

## 运行形态

```text
浏览器上传 Excel
  -> 本地 UI 服务
  -> 读取 Agent_* 列
  -> 逐行调用现有 run_single_batch_analysis(...)
  -> 调用本地模型 API
  -> 输出 分析结果.xlsx + images/ + run_report.json
```

## Excel 格式

输入 Excel 第一行需要包含下面这些列；`JIRA链接` 为可选列，存在且非空时会用于优先排序：

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

只处理 `Agent_问题状态 = 待分析` 的行。若 Excel 中存在 `JIRA链接` 列，且该列非空，这些待分析行会优先处理；同一优先级内仍按原表顺序处理。没有 `JIRA链接` 列时保持原顺序。分析过程中状态会写为 `分析中`，完成后写为 `已分析`，失败写为 `分析失败`。

`Agent_h5数据路径` 可以填写，也可以留空。已填写时会先确认该 H5 是否覆盖 `Agent_问题日期` / `Agent_问题时间`；如果不覆盖，会尝试重新自动定位覆盖目标时间的 H5。留空时会根据 `Agent_车号`、`Agent_车型`、`Agent_问题日期`、`Agent_问题时间` 在 `LOCAL_BATCH_H5_SEARCH_BASE_DIR` 下自动查找 H5。自动查找逻辑和飞书版一致：先按车号/车型/日期和文件起始时间粗筛相邻 H5，再用 H5 内 `timestamp` 覆盖范围确认目标时间。若找不到，状态保持 `待分析`，结论写入“未找到对应h5数据，请确认数据解析是否完成”。

`Agent_问题日期` / `Agent_问题时间` 会用于按 H5 文件起始时间筛选目标问题窗口；历史 Excel 若仍使用 `问题日期` / `问题时间`，程序会兼容读取。

`Agent_分析信号图示` 在本地版中不是飞书附件字段，而是写入结果包里的图片相对路径。

## 外网构建镜像

在可以访问 apt/pip 的构建机上执行：

```bash
cd /path/to/Agent/deploy/intranet_batch
cp env.example .env
docker compose build
docker save blu-agent-local-batch:latest -o blu-agent-local-batch.tar
```

把 `blu-agent-local-batch.tar`、`docker-compose.yml`、`.env`、`blu-agent-local-batch.sh` 拷贝到内网机器。推荐直接使用脚本部署；`docker-compose.yml` 只是保留给已安装 Compose 的环境。

部署包里也包含 `批量分析模板.xlsx`。用户可以基于该模板填写真实问题记录；正式上传前建议删除模板中的示例行，或确保示例行状态不是 `待分析`。

## 内网加载运行

### 推荐：一键脚本

进入内网部署目录后执行：

```bash
bash blu-agent-local-batch.sh deploy /mnt/adas_h5_data
```

其中 `/mnt/adas_h5_data` 换成实际 H5 数据目录。脚本会自动：

```text
检查 Docker 权限
加载 blu-agent-local-batch.tar
创建 runtime / outputs 目录
移除旧容器
启动新容器
检查 http://127.0.0.1:8080/healthz
打印可访问地址
```

常用运维命令：

```bash
bash blu-agent-local-batch.sh status
bash blu-agent-local-batch.sh logs
bash blu-agent-local-batch.sh stop
bash blu-agent-local-batch.sh start
bash blu-agent-local-batch.sh restart /mnt/adas_h5_data
bash blu-agent-local-batch.sh test-api
bash blu-agent-local-batch.sh remove
```

`stop` 只是停止服务，不删除镜像和结果。后续再执行 `start` 即可恢复。长期使用时不需要每天关闭；容器带 `--restart unless-stopped`，机器重启后会自动拉起，除非你手动 `stop` 过。

如果只想验证内网模型 API 是否能被当前镜像调用，不需要准备 Excel 或 H5，直接执行：

```bash
bash blu-agent-local-batch.sh test-api
```

该命令会从容器内部读取 `.env`，调用 `{LLM_API_BASE_URL}/chat/completions`，并打印 Base URL、模型名、是否读取到 key，以及模型返回的前 500 个字符。

### 可选：Docker Compose

如果内网机器已安装 `docker compose` 插件，也可以先按实际 H5 数据目录修改 `docker-compose.yml` 里的挂载：

```yaml
volumes:
  - ./runtime:/runtime
  - ./outputs:/outputs
  - /mnt/adas_h5_data:/data:ro
```

然后运行：

```bash
docker load -i blu-agent-local-batch.tar
docker compose up -d
```

浏览器访问：

```text
http://<server-ip>:8080
```

任务结果会同时保留在宿主机 `./outputs/jobs/<job_id>/output/`，其中包含 `分析结果.xlsx`、`images/` 和 `run_report.json`。分析中间产物写入 `./runtime`。

## 路径映射

如果 Excel 中仍然是 Windows 路径，例如：

```text
E:\ADAS_data\parsed\case.h5
```

而容器内挂载为：

```text
/data/adas_data/parsed/case.h5
```

则在 UI 的“路径映射”里填：

```json
{"E:\\ADAS_data":"/data/adas_data"}
```

也可以写到 `.env`：

```bash
LOCAL_BATCH_PATH_MAP={"E:\\ADAS_data":"/data/adas_data"}
```

## 本地模型 API

内网镜像运行时不访问公网，也不依赖飞书。它只需要能访问一个本地或内网部署的 OpenAI 兼容模型 API：

```text
POST {LLM_API_BASE_URL}/chat/completions
```

如果模型服务就在 Docker 宿主机上，推荐在 `.env` 中配置：

```bash
LLM_API_BASE_URL=http://host.docker.internal:8000/v1
LLM_API_KEY=
LLM_MODEL=local-model
```

此时模型服务需要监听宿主机可达地址，例如 `0.0.0.0:8000` 或宿主机内网 IP；如果模型只监听 `127.0.0.1`，容器通常无法通过 `host.docker.internal` 访问。Linux 内网机也可以把 compose 改成 host network 形态，但这样端口映射写法需要相应删除。

如果模型服务在另一台内网机器上，则改成：

```bash
LLM_API_BASE_URL=http://<model-ip>:<port>/v1
```

`LLM_API_KEY` 在内网镜像中是可选项：本地模型 API 不鉴权时留空；如果网关要求鉴权，再填真实 token。这个可选鉴权只存在于 `deploy/intranet_batch` 镜像适配器里，不改变当前飞书 Agent 和 Ubuntu 部署的公共 `llm_service.py` 行为。

注意：镜像构建机可以联网安装 apt/pip 依赖；加载到完全无网络环境后，运行时不应再访问 apt、pip、npx、飞书或任何公网地址。

## 与 Ubuntu 部署的边界

- Ubuntu 飞书机器人部署仍使用 `deploy/ubuntu_run/`。
- 内网 Excel UI 镜像只维护 `deploy/intranet_batch/`、`local_batch_root_cause.py`、`local_batch_ui.py`。其中 `deploy/intranet_batch/llm_service.py` 是镜像内专用的本地模型 API 适配器。
- 后续改动批量行车问题原因分析的字段、状态、问题类型、结果图示、路径规则或依赖时，必须同步检查本目录；除非明确影响飞书机器人部署，不要改 Ubuntu 一键脚本。
