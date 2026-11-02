#!/usr/bin/env bash
set -euo pipefail

APP_NAME="blu-agent-local-batch"
IMAGE_NAME="${IMAGE_NAME:-blu-agent-local-batch:latest}"
IMAGE_TAR="${IMAGE_TAR:-blu-agent-local-batch.tar}"
ENV_FILE="${ENV_FILE:-.env}"
CONTAINER_NAME="${CONTAINER_NAME:-blu-agent-local-batch}"
HOST_PORT="${HOST_PORT:-8080}"
RUNTIME_DIR="${RUNTIME_DIR:-runtime}"
OUTPUTS_DIR="${OUTPUTS_DIR:-outputs}"
DEFAULT_H5_DIR="${H5_DATA_DIR:-h5_data}"

DOCKER_CMD=()

usage() {
  cat <<'EOF'
Usage:
  bash blu-agent-local-batch.sh deploy [h5_data_dir]
  bash blu-agent-local-batch.sh restart [h5_data_dir]
  bash blu-agent-local-batch.sh start
  bash blu-agent-local-batch.sh stop
  bash blu-agent-local-batch.sh status
  bash blu-agent-local-batch.sh logs
  bash blu-agent-local-batch.sh remove
  bash blu-agent-local-batch.sh test-api

Examples:
  bash blu-agent-local-batch.sh deploy
  bash blu-agent-local-batch.sh deploy /mnt/adas_h5_data
  bash blu-agent-local-batch.sh logs
  bash blu-agent-local-batch.sh stop
  bash blu-agent-local-batch.sh test-api

Environment overrides:
  HOST_PORT=18080 bash blu-agent-local-batch.sh deploy /mnt/adas_h5_data
  CONTAINER_NAME=blu-agent-test bash blu-agent-local-batch.sh deploy
EOF
}

log() {
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

die() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

detect_docker() {
  command -v docker >/dev/null 2>&1 || die "未找到 docker 命令，请先安装 Docker。"
  if docker info >/dev/null 2>&1; then
    DOCKER_CMD=(docker)
    return
  fi
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    DOCKER_CMD=(sudo docker)
    return
  fi
  die "Docker 已安装但当前用户无法访问 daemon。请使用 sudo，或执行：sudo usermod -aG docker \$USER 后重新登录。"
}

docker_call() {
  "${DOCKER_CMD[@]}" "$@"
}

abs_path() {
  local path="$1"
  mkdir -p "$path"
  (cd "$path" && pwd -P)
}

container_exists() {
  [ -n "$(docker_call ps -aq -f "name=^/${CONTAINER_NAME}$")" ]
}

load_image_if_needed() {
  if [ -f "$IMAGE_TAR" ]; then
    log "加载镜像：$IMAGE_TAR"
    docker_call load -i "$IMAGE_TAR"
    return
  fi
  if docker_call image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    log "镜像包不存在，沿用本机已有镜像：$IMAGE_NAME"
    return
  fi
  die "未找到镜像包：$IMAGE_TAR，也未找到本机镜像：$IMAGE_NAME"
}

deploy() {
  local h5_dir="${1:-$DEFAULT_H5_DIR}"
  [ -f "$ENV_FILE" ] || die "未找到配置文件：$ENV_FILE"

  detect_docker
  load_image_if_needed

  local runtime_abs outputs_abs h5_abs
  runtime_abs="$(abs_path "$RUNTIME_DIR")"
  outputs_abs="$(abs_path "$OUTPUTS_DIR")"
  h5_abs="$(abs_path "$h5_dir")"

  if container_exists; then
    log "移除旧容器：$CONTAINER_NAME"
    docker_call rm -f "$CONTAINER_NAME" >/dev/null
  fi

  log "启动容器：$CONTAINER_NAME"
  docker_call run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    --env-file "$ENV_FILE" \
    -e LOCAL_BATCH_HOST=0.0.0.0 \
    -e LOCAL_BATCH_PORT=8080 \
    -e LOCAL_BATCH_UI_RUNTIME=/outputs \
    -e LOCAL_BATCH_DATA_ROOT=/data \
    -e LOCAL_BATCH_H5_SEARCH_BASE_DIR=/data \
    -p "${HOST_PORT}:8080" \
    -v "${runtime_abs}:/runtime" \
    -v "${outputs_abs}:/outputs" \
    -v "${h5_abs}:/data:ro" \
    "$IMAGE_NAME" >/dev/null

  log "H5 数据目录：$h5_abs -> /data"
  log "结果输出目录：$outputs_abs"
  wait_health
  print_access_urls
}

wait_health() {
  if ! command -v curl >/dev/null 2>&1; then
    log "未找到 curl，跳过 healthz 检查。可手动访问：http://127.0.0.1:${HOST_PORT}/healthz"
    return
  fi
  log "等待服务启动..."
  local i
  for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${HOST_PORT}/healthz" >/dev/null 2>&1; then
      log "服务已就绪：http://127.0.0.1:${HOST_PORT}"
      return
    fi
    sleep 1
  done
  log "healthz 暂未成功，请查看日志：bash $0 logs"
}

print_access_urls() {
  log "本机访问：http://127.0.0.1:${HOST_PORT}"
  if command -v hostname >/dev/null 2>&1; then
    local ips
    ips="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -Ev '^(127\.|172\.17\.|$)' || true)"
    if [ -n "$ips" ]; then
      log "可尝试的内网访问地址："
      while IFS= read -r ip; do
        [ -n "$ip" ] && printf '  http://%s:%s\n' "$ip" "$HOST_PORT"
      done <<<"$ips"
    fi
  fi
}

start_container() {
  detect_docker
  docker_call start "$CONTAINER_NAME" >/dev/null
  wait_health
  print_access_urls
}

stop_container() {
  detect_docker
  docker_call stop "$CONTAINER_NAME" >/dev/null
  log "已停止：$CONTAINER_NAME"
}

remove_container() {
  detect_docker
  if container_exists; then
    docker_call rm -f "$CONTAINER_NAME" >/dev/null
    log "已移除：$CONTAINER_NAME"
  else
    log "容器不存在：$CONTAINER_NAME"
  fi
}

status_container() {
  detect_docker
  docker_call ps -a --filter "name=^/${CONTAINER_NAME}$"
}

logs_container() {
  detect_docker
  docker_call logs -f "$CONTAINER_NAME"
}

test_api() {
  [ -f "$ENV_FILE" ] || die "未找到配置文件：$ENV_FILE"
  detect_docker
  load_image_if_needed
  log "从容器内部测试本地/内网模型 API，不依赖 Excel 或 H5 数据。"
  docker_call run --rm \
    --env-file "$ENV_FILE" \
    "$IMAGE_NAME" \
    python -c 'from llm_service import call_chat_completion, get_llm_config
c = get_llm_config()
print("Base URL:", c.api_base_url)
print("Model:", c.model)
print("Has key:", bool(c.api_key))
reply = call_chat_completion([
    {"role": "system", "content": "你是接口连通性测试助手。只回答：API调用成功。"},
    {"role": "user", "content": "请返回一句话确认接口可用。"},
], timeout_seconds=30)
print("Reply:", reply[:500])'
}

main() {
  local action="${1:-deploy}"
  case "$action" in
    deploy)
      deploy "${2:-}"
      ;;
    restart)
      deploy "${2:-}"
      ;;
    start)
      start_container
      ;;
    stop)
      stop_container
      ;;
    status)
      status_container
      ;;
    logs)
      logs_container
      ;;
    test-api)
      test_api
      ;;
    remove)
      remove_container
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      usage
      die "未知命令：$action"
      ;;
  esac
}

main "$@"
