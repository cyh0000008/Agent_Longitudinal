#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  install_blu_agent.sh <payload_dir> [options]

Common:
  install_blu_agent.sh <payload_dir> --start
  install_blu_agent.sh <payload_dir> --env-file ./blu-agent.env --start

Options:
  --install-dir DIR       Install project files here. Default: $HOME/.local/opt/blu-agent
  --bin-dir DIR           Write launcher commands here. Default: $HOME/.local/bin
  --env-file FILE         Copy an existing env file into ~/.config/blu-agent/blu-agent.env
  --start                 Start Blu in foreground after installation
  --non-interactive       Do not prompt for missing credentials
  --skip-system-deps      Do not install Ubuntu apt packages
  --skip-python-deps      Do not create/install Python venv dependencies
  --skip-lark-cli         Do not attempt to install lark-cli
  --no-path-update        Do not append the bin dir to ~/.bashrc
  -h, --help              Show this help

Environment:
  BLU_AGENT_REFRESH_LARK_CLI=1  Refresh lark-cli even when an existing one is available

This installer does not register systemd services and does not package secrets.
EOF
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

PAYLOAD_DIR="$1"
shift

INSTALL_DIR="${BLU_AGENT_INSTALL_DIR:-${HOME}/.local/opt/blu-agent}"
BIN_DIR="${BLU_AGENT_BIN_DIR:-${HOME}/.local/bin}"
CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/blu-agent"
DATA_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/blu-agent"
if [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
  DEFAULT_RUNTIME_DIR="${XDG_RUNTIME_DIR}/blu-agent"
else
  DEFAULT_RUNTIME_DIR="${HOME}/.cache/blu-agent/runtime"
fi
RUNTIME_DIR="${BLU_AGENT_RUNTIME_DIR:-${BLU_AGENT_RUNTIME:-${DEFAULT_RUNTIME_DIR}}}"
ENV_FILE="${CONFIG_DIR}/blu-agent.env"
ENV_EXAMPLE="${PAYLOAD_DIR}/dist/env.example"
ENV_FILE_SOURCE=""
INSTALL_SYSTEM_DEPS=1
INSTALL_PYTHON_DEPS=1
INSTALL_LARK_CLI=1
UPDATE_PATH=1
START_AFTER_INSTALL=0
NON_INTERACTIVE=0
PYTHON_BIN="${PYTHON_BIN:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir)
      INSTALL_DIR="$2"
      shift 2
      ;;
    --bin-dir)
      BIN_DIR="$2"
      shift 2
      ;;
    --env-file)
      ENV_FILE_SOURCE="$2"
      shift 2
      ;;
    --start)
      START_AFTER_INSTALL=1
      shift
      ;;
    --non-interactive)
      NON_INTERACTIVE=1
      shift
      ;;
    --skip-system-deps)
      INSTALL_SYSTEM_DEPS=0
      shift
      ;;
    --skip-python-deps|--skip-deps)
      INSTALL_PYTHON_DEPS=0
      shift
      ;;
    --skip-lark-cli)
      INSTALL_LARK_CLI=0
      shift
      ;;
    --no-path-update)
      UPDATE_PATH=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

SYSTEM_PACKAGES=(
  python3.10
  python3.10-venv
  python3.10-dev
  python3-pip
  build-essential
  libhdf5-dev
  graphviz
  fonts-noto-cjk
  acl
  curl
  rsync
  dos2unix
)

log() {
  printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

warn() {
  printf '\n[WARN] %s\n' "$*" >&2
}

is_legacy_runtime() {
  local value="${1:-}"
  case "${value}" in
    /tmp/blu_agent|/tmp/blu_agent/*|/tmp/blu_agent_*|/tmp/blu_agent_*/*) return 0 ;;
    *) return 1 ;;
  esac
}

safe_runtime_user() {
  id -un 2>/dev/null || printf '%s' "${USER:-blu}"
}

clear_blu_proxy_env() {
  unset http_proxy HTTP_PROXY https_proxy HTTPS_PROXY all_proxy ALL_PROXY
  unset PIP_PROXY pip_proxy PIP_HTTP_PROXY PIP_HTTPS_PROXY
  unset npm_config_proxy npm_config_https_proxy NPM_CONFIG_PROXY NPM_CONFIG_HTTPS_PROXY
  export NO_PROXY="${NO_PROXY:-localhost,127.0.0.1,::1}"
  export no_proxy="${no_proxy:-localhost,127.0.0.1,::1}"
}

prepare_python_package_env() {
  export PIP_DISABLE_PIP_VERSION_CHECK="${PIP_DISABLE_PIP_VERSION_CHECK:-1}"
  if [[ "${BLU_AGENT_KEEP_PROXY:-0}" == "1" ]]; then
    return
  fi

  export PIP_CONFIG_FILE="/dev/null"
  unset PIP_PROXY pip_proxy PIP_HTTP_PROXY PIP_HTTPS_PROXY
}

run_lark_cli_npx_install() {
  if [[ "${BLU_AGENT_KEEP_PROXY:-0}" == "1" ]]; then
    npx -y @larksuite/cli@latest install
    return
  fi

  env \
    -u http_proxy -u HTTP_PROXY -u https_proxy -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
    -u npm_config_proxy -u npm_config_https_proxy -u NPM_CONFIG_PROXY -u NPM_CONFIG_HTTPS_PROXY \
    npx -y @larksuite/cli@latest install
}

sanitize_proxy_env() {
  if [[ "${BLU_AGENT_KEEP_PROXY:-0}" == "1" ]]; then
    log "BLU_AGENT_KEEP_PROXY=1; keeping proxy environment for install."
    return
  fi

  if [[ "${BLU_AGENT_NO_PROXY:-0}" == "1" ]]; then
    log "BLU_AGENT_NO_PROXY=1; clearing proxy environment for install."
    clear_blu_proxy_env
    return
  fi

  local proxy="${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-${ALL_PROXY:-${all_proxy:-}}}}}}"
  [[ -n "${proxy}" ]] || return 0

  local checker="${PYTHON_BIN:-$(command -v python3 || true)}"
  if [[ -z "${checker}" ]]; then
    warn "Proxy env detected but python3 is unavailable; keeping it. Set BLU_AGENT_NO_PROXY=1 to disable proxy for install."
    return
  fi

  local verdict
  verdict="$("${checker}" - "${proxy}" <<'PY'
import sys
from urllib.parse import urlparse

raw = sys.argv[1]
url = raw if "://" in raw else "http://" + raw
parsed = urlparse(url)
host = (parsed.hostname or "").lower()
try:
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
except ValueError:
    print("invalid")
    raise SystemExit

if host not in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
    print("remote")
    raise SystemExit

print("local_loopback")
PY
  )" || verdict="unknown"

  case "${verdict}" in
    local_loopback|invalid)
      log "Detected local loopback proxy (${proxy}); clearing it for install. Set BLU_AGENT_KEEP_PROXY=1 to keep it."
      clear_blu_proxy_env
      ;;
    remote)
      log "Detected non-local proxy (${proxy}); keeping proxy environment."
      ;;
    *)
      warn "Proxy env detected but could not be checked; keeping it. Set BLU_AGENT_NO_PROXY=1 to disable proxy for install."
      ;;
  esac
}

runtime_is_writable() {
  local dir="${1:-}"
  local probe
  [[ -n "${dir}" ]] || return 1
  mkdir -p "${dir}" 2>/dev/null || return 1
  probe="${dir}/.write_probe_$$"
  : > "${probe}" 2>/dev/null || return 1
  rm -f "${probe}" 2>/dev/null || true
  return 0
}

choose_runtime_dir() {
  local requested="${1:-}"
  local runtime_user candidate fallback
  runtime_user="$(safe_runtime_user)"
  for candidate in \
    "${requested}" \
    "${XDG_RUNTIME_DIR:+${XDG_RUNTIME_DIR}/blu-agent}" \
    "${HOME}/.cache/blu-agent/runtime"
  do
    [[ -n "${candidate}" ]] || continue
    is_legacy_runtime "${candidate}" && continue
    if runtime_is_writable "${candidate}"; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done

  fallback="${HOME}/.cache/blu-agent/runtime-${runtime_user}-$$"
  if runtime_is_writable "${fallback}"; then
    printf '%s\n' "${fallback}"
    return 0
  fi

  echo "Unable to create a writable Blu runtime directory." >&2
  exit 1
}

run_as_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "sudo is required to install system packages. Re-run with sudo or install packages manually." >&2
    return 1
  fi
}

apt_install() {
  if [[ "$(id -u)" -eq 0 ]]; then
    env DEBIAN_FRONTEND=noninteractive apt-get "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo env DEBIAN_FRONTEND=noninteractive apt-get "$@"
  else
    echo "sudo is required to install system packages. Re-run with sudo or install packages manually." >&2
    return 1
  fi
}

ensure_system_dependencies() {
  if [[ "${INSTALL_SYSTEM_DEPS}" -eq 0 ]]; then
    log "Skipping Ubuntu system dependency installation."
    return
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    warn "apt-get not found. Skipping system dependency installation."
    return
  fi

  local missing=()
  local pkg
  for pkg in "${SYSTEM_PACKAGES[@]}"; do
    if ! dpkg-query -W -f='${Status}' "${pkg}" 2>/dev/null | grep -q "install ok installed"; then
      missing+=("${pkg}")
    fi
  done

  if [[ "${#missing[@]}" -eq 0 ]]; then
    log "Ubuntu system dependencies are already installed."
    return
  fi

  log "Installing Ubuntu system packages: ${missing[*]}"
  apt_install update
  apt_install install -y "${missing[@]}"
}

ensure_node_runtime() {
  if [[ "${INSTALL_SYSTEM_DEPS}" -eq 0 ]]; then
    return
  fi

  if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 && command -v npx >/dev/null 2>&1; then
    log "Node.js/npm/npx are already available: node $(node --version), npm $(npm --version)."
    return
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    warn "apt-get not found. Skipping Node.js installation."
    return
  fi
  if ! command -v curl >/dev/null 2>&1; then
    warn "curl not found. Skipping Node.js installation."
    return
  fi

  log "Installing Node.js 20 from NodeSource. This avoids Ubuntu apt npm dependency conflicts."
  local setup_script="/tmp/blu_nodesource_setup_20.x.sh"
  if ! curl -fsSL "https://deb.nodesource.com/setup_20.x" -o "${setup_script}"; then
    warn "Failed to download NodeSource setup script. lark-cli installation may need manual Node.js/npm setup."
    return
  fi

  if ! run_as_root bash "${setup_script}"; then
    rm -f "${setup_script}" || true
    warn "NodeSource setup failed. lark-cli installation may need manual Node.js/npm setup."
    return
  fi
  rm -f "${setup_script}" || true

  if ! apt_install install -y nodejs; then
    warn "Node.js installation failed. lark-cli installation may need manual Node.js/npm setup."
    return
  fi

  if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 && command -v npx >/dev/null 2>&1; then
    log "Node.js/npm/npx installed: node $(node --version), npm $(npm --version)."
  else
    warn "Node.js was installed but npm/npx is still missing. lark-cli was not installed automatically."
  fi
}

ensure_python() {
  local candidates=()
  if [[ -n "${PYTHON_BIN}" ]]; then
    candidates+=("${PYTHON_BIN}")
  fi
  candidates+=(
    /usr/bin/python3.10
    /usr/bin/python3.11
    python3.10
    python3.11
    /usr/bin/python3
    python3
  )

  local candidate resolved
  for candidate in "${candidates[@]}"; do
    if [[ "${candidate}" == /* ]]; then
      resolved="${candidate}"
    else
      resolved="$(command -v "${candidate}" 2>/dev/null || true)"
    fi
    if [[ -z "${resolved}" || ! -x "${resolved}" ]]; then
      continue
    fi
    if "${resolved}" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 12) else 1)
PY
    then
      PYTHON_BIN="${resolved}"
      log "Using Python: ${PYTHON_BIN} ($("${PYTHON_BIN}" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))"
      return
    fi
  done

  cat >&2 <<'EOF'
Python 3.10 or 3.11 is required for Blu Agent.

The installer intentionally avoids Python 3.12/3.13 because this project pins
numpy==1.24.4 and several CAN/HDF5 dependencies that do not support Python 3.13.

On Ubuntu 22.04, install the system runtime and re-run:
  sudo apt-get update
  sudo apt-get install -y python3.10 python3.10-venv python3.10-dev

If Anaconda/conda is active, deactivate it first. The installer will prefer
/usr/bin/python3.10 when it exists.
EOF
  exit 1
}

copy_payload() {
  RUNTIME_DIR="$(choose_runtime_dir "${RUNTIME_DIR}")"
  mkdir -p "${INSTALL_DIR}" "${BIN_DIR}" "${CONFIG_DIR}" "${DATA_DIR}" "${RUNTIME_DIR}"

  if [[ ! -d "${PAYLOAD_DIR}" ]]; then
    echo "Payload directory not found: ${PAYLOAD_DIR}" >&2
    exit 1
  fi

  log "Deploying project files to ${INSTALL_DIR}"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a \
      --exclude ".env" \
      --exclude ".venv/" \
      --exclude "__pycache__/" \
      --exclude "*.pyc" \
      --exclude ".git/" \
      --exclude "logs/" \
      --exclude "uploads/" \
      --exclude "analysis_results/" \
      --exclude "analysis_outputs/" \
      --exclude "runtime/" \
      --exclude ".tmp/" \
      --exclude "tmp/" \
      --exclude "dist/" \
      "${PAYLOAD_DIR}/" "${INSTALL_DIR}/"
  else
    cp -a "${PAYLOAD_DIR}/." "${INSTALL_DIR}/"
    rm -f "${INSTALL_DIR}/.env"
    rm -rf "${INSTALL_DIR}/.venv" "${INSTALL_DIR}/logs" "${INSTALL_DIR}/uploads" "${INSTALL_DIR}/analysis_results" "${INSTALL_DIR}/analysis_outputs" "${INSTALL_DIR}/runtime" "${INSTALL_DIR}/.tmp" "${INSTALL_DIR}/tmp" "${INSTALL_DIR}/dist"
  fi
}

install_python_dependencies() {
  if [[ "${INSTALL_PYTHON_DEPS}" -eq 0 ]]; then
    log "Skipping Python dependency installation."
    return
  fi

  local venv_dir="${INSTALL_DIR}/.venv"
  if [[ -d "${venv_dir}" ]]; then
    log "Removing existing Python virtual environment: ${venv_dir}"
    rm -rf -- "${venv_dir}"
  fi

  log "Creating Python virtual environment."
  "${PYTHON_BIN}" -m venv "${venv_dir}"
  prepare_python_package_env
  local pip_args=(--retries "${PIP_RETRIES:-2}" --timeout "${PIP_TIMEOUT:-60}" --prefer-binary)
  "${venv_dir}/bin/python" -m pip install "${pip_args[@]}" --upgrade pip setuptools wheel

  log "Installing Python requirements."
  local wheelhouse="${INSTALL_DIR}/dist/wheelhouse"
  if [[ -d "${wheelhouse}" ]] && [[ -n "$(find "${wheelhouse}" -maxdepth 1 -type f -name '*.whl' -print -quit)" ]]; then
    "${venv_dir}/bin/pip" install "${pip_args[@]}" --no-index --find-links "${wheelhouse}" -r "${INSTALL_DIR}/requirements.txt"
  else
    "${venv_dir}/bin/pip" install "${pip_args[@]}" -r "${INSTALL_DIR}/requirements.txt"
  fi
}

install_lark_cli() {
  if [[ "${INSTALL_LARK_CLI}" -eq 0 ]]; then
    log "Skipping lark-cli installation."
    return
  fi

  if command -v lark-cli >/dev/null 2>&1; then
    log "lark-cli is already available: $(lark-cli --version 2>/dev/null || true)"
    if [[ "${BLU_AGENT_REFRESH_LARK_CLI:-0}" != "1" ]]; then
      log "Keeping existing lark-cli. Set BLU_AGENT_REFRESH_LARK_CLI=1 to refresh it."
      return
    fi
    if command -v npx >/dev/null 2>&1; then
      log "Refreshing official lark-cli with npx @larksuite/cli@latest install."
      if ! run_lark_cli_npx_install; then
        warn "lark-cli refresh failed. Continuing with the existing lark-cli; auth helpers may need a manual lark-cli update."
      else
        log "lark-cli after refresh: $(lark-cli --version 2>/dev/null || true)"
      fi
    else
      warn "npx not found. Continuing with the existing lark-cli; auth helpers may need a manual lark-cli update."
    fi
    return
  fi

  if ! command -v npx >/dev/null 2>&1; then
    warn "npx not found. lark-cli was not installed."
    return
  fi

  log "Installing official lark-cli with npx @larksuite/cli@latest install."
  if ! run_lark_cli_npx_install; then
    warn "lark-cli installation failed. Blu can still start, but Base/feedback helper flows need lark-cli."
  fi
}

run_lark_user_auth() {
  if [[ "${INSTALL_LARK_CLI}" -eq 0 ]]; then
    warn "Skipping Feishu Base user authorization because lark-cli installation was skipped."
    return
  fi
  if [[ "${BLU_AGENT_AUTO_LARK_AUTH:-0}" != "1" ]]; then
    log "Skipping Feishu Base user authorization. Set BLU_AGENT_AUTO_LARK_AUTH=1 to run it during install."
    return
  fi
  if [[ "${NON_INTERACTIVE}" -eq 1 || ! -t 0 ]]; then
    warn "Skipping Feishu Base user authorization because this shell is non-interactive. Run blu-agent-lark-auth later."
    return
  fi
  if ! command -v lark-cli >/dev/null 2>&1; then
    warn "lark-cli not found. Run blu-agent-lark-auth after lark-cli is available."
    return
  fi

  log "Running Feishu Base user authorization before starting Blu."
  echo "A browser link or QR code may appear. Finish the authorization, then the installer will continue."
  if ! "${BIN_DIR}/blu-agent-lark-auth"; then
    warn "Feishu Base user authorization did not complete. Run blu-agent-lark-auth, then restart Blu with blu-agent-start."
  fi
}

write_env_value() {
  local key="$1"
  local value="$2"
  "${PYTHON_BIN}" - "${ENV_FILE}" "${key}" "${value}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()
prefix = f"{key}="
for index, line in enumerate(lines):
    if line.startswith(prefix):
        lines[index] = f"{key}={value}"
        break
else:
    lines.append(f"{key}={value}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

read_env_value() {
  local key="$1"
  "${PYTHON_BIN}" - "${ENV_FILE}" "${key}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
prefix = f"{key}="
if not path.exists():
    raise SystemExit(0)
for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith(prefix):
        print(line[len(prefix):])
        break
PY
}

delete_env_value() {
  local key="$1"
  "${PYTHON_BIN}" - "${ENV_FILE}" "${key}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
if not path.exists():
    raise SystemExit(0)
prefix = f"{key}="
lines = [line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith(prefix)]
path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
PY
}

ensure_directory_write_permission() {
  local dir="$1"
  local label="$2"
  local current_uid current_gid current_user
  [[ -n "${dir}" ]] || return 0

  if runtime_is_writable "${dir}"; then
    log "${label} is writable: ${dir}"
    return 0
  fi

  current_uid="$(id -u)"
  current_gid="$(id -g)"
  current_user="$(id -un 2>/dev/null || printf '%s' "${USER:-blu}")"
  log "${label} is not writable: ${dir}"
  log "Requesting permission with sudo so ${current_user} can create parsed H5 files."

  if ! run_as_root mkdir -p "${dir}"; then
    cat >&2 <<EOF
Unable to create ${label}: ${dir}

Ask an administrator to run:
  sudo mkdir -p '${dir}'
  sudo setfacl -m u:${current_user}:rwx -m d:u:${current_user}:rwx '${dir}'

If setfacl is unavailable, grant this user write permission on the parsed H5 root directory.
EOF
    exit 1
  fi

  if command -v setfacl >/dev/null 2>&1; then
    if ! run_as_root setfacl -m "u:${current_uid}:rwx" -m "d:u:${current_uid}:rwx" "${dir}"; then
      warn "setfacl failed for ${dir}; trying a non-recursive owner grant on the directory itself."
    fi
  else
    warn "setfacl is not installed; trying a non-recursive owner grant on the directory itself."
  fi

  if ! runtime_is_writable "${dir}"; then
    run_as_root chown "${current_uid}:${current_gid}" "${dir}" 2>/dev/null || true
  fi

  if runtime_is_writable "${dir}"; then
    log "${label} permission granted: ${dir}"
    return 0
  fi

  cat >&2 <<EOF
Unable to write ${label}: ${dir}

The installer tried to request permission automatically, but the directory is still not writable.
Ask an administrator to grant this Ubuntu user create/write permission on the parsed H5 root:
  sudo mkdir -p '${dir}'
  sudo setfacl -m u:${current_user}:rwx -m d:u:${current_user}:rwx '${dir}'

Fallback if ACL is not available and this directory can be owned by the Blu runtime user:
  sudo chown ${current_uid}:${current_gid} '${dir}'
EOF
  exit 1
}

ensure_batch_data_permissions() {
  local h5_base blf_base
  h5_base="$(read_env_value "BATCH_ROOT_CAUSE_H5_SEARCH_BASE_DIR")"
  blf_base="$(read_env_value "BATCH_ROOT_CAUSE_BLF_SEARCH_BASE_DIR")"

  if [[ -n "${h5_base}" ]]; then
    ensure_directory_write_permission "${h5_base}" "Batch parsed H5 output root"
  fi

  if [[ -n "${blf_base}" && ! -r "${blf_base}" ]]; then
    warn "BLF search root is not readable now: ${blf_base}. Batch BLF fallback will need read access to this mounted directory."
  fi
}

seed_env_from_process() {
  local key
  for key in \
    FEISHU_APP_ID \
    FEISHU_APP_SECRET \
    KIMI_API_KEY \
    KIMI_MODEL \
    KIMI_TIMEOUT_SECONDS \
    BATCH_ROOT_CAUSE_BASE_TOKEN \
    BATCH_ROOT_CAUSE_TABLE_ID \
    FEEDBACK_BASE_TOKEN \
    FEEDBACK_TABLE_ID \
    BLU_LARK_AUTH_DOMAINS \
    BLU_LARK_AUTH_SCOPES \
    BLU_BOT_PERMISSION_SCOPES; do
    local current="${!key:-}"
    if [[ -n "${current}" ]] && [[ -z "$(read_env_value "${key}")" ]]; then
      write_env_value "${key}" "${current}"
    fi
  done
}

seed_kpi_report_defaults() {
  local key default_value
  for key in \
    KPI_REPORT_BASE_TOKEN \
    KPI_REPORT_TABLE_ID \
    KPI_REPORT_FIELD_REPORT \
    KPI_REPORT_FIELD_TRIGGER \
    KPI_REPORT_FIELD_RESULT \
    KPI_REPORT_TRIGGER_VALUE \
    KPI_REPORT_RESET_VALUE; do
    if [[ -n "$(read_env_value "${key}")" ]]; then
      continue
    fi
    default_value="$(awk -F= -v key="${key}" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "${ENV_EXAMPLE}")"
    if [[ -n "${default_value}" ]]; then
      write_env_value "${key}" "${default_value}"
    fi
  done
}

prompt_env_value() {
  local key="$1"
  local label="$2"
  local secret="${3:-0}"
  local optional="${4:-0}"
  local current
  current="$(read_env_value "${key}")"
  if [[ -n "${current}" ]]; then
    return
  fi
  if [[ "${NON_INTERACTIVE}" -eq 1 ]]; then
    return
  fi

  local value=""
  if [[ "${secret}" -eq 1 ]]; then
    read -r -s -p "${label}: " value || true
    printf '\n'
  else
    read -r -p "${label}: " value || true
  fi

  if [[ -n "${value}" ]]; then
    write_env_value "${key}" "${value}"
  elif [[ "${optional}" -eq 0 ]]; then
    warn "${key} is still empty."
  fi
}

configure_env() {
  if [[ -f "${ENV_EXAMPLE}" ]]; then
    cp "${ENV_EXAMPLE}" "${CONFIG_DIR}/blu-agent.env.example"
  else
    echo "Config template not found: ${ENV_EXAMPLE}" >&2
    exit 1
  fi

  if [[ -n "${ENV_FILE_SOURCE}" ]]; then
    log "Using env file: ${ENV_FILE_SOURCE}"
    cp "${ENV_FILE_SOURCE}" "${ENV_FILE}"
  elif [[ ! -f "${ENV_FILE}" ]]; then
    cp "${CONFIG_DIR}/blu-agent.env.example" "${ENV_FILE}"
  fi

  seed_env_from_process
  # Git updates keep the user's existing env file. Backfill only the non-secret
  # coordinates and real field names for the production KPI Base.
  seed_kpi_report_defaults
  delete_env_value "BLU_AGENT_RUNTIME_DIR"
  delete_env_value "BLU_AGENT_RUNTIME"
  RUNTIME_DIR="$(choose_runtime_dir "${RUNTIME_DIR}")"
  write_env_value "BLU_AGENT_RUNTIME_DIR" "${RUNTIME_DIR}"
  write_env_value "BLU_AGENT_RUNTIME" "${RUNTIME_DIR}"

  if [[ -z "$(read_env_value "BLU_BOT_PERMISSION_SCOPES")" ]]; then
    write_env_value "BLU_BOT_PERMISSION_SCOPES" "im_message_send,im_group_at_receive,im_resource_download,im_file_image_upload,contact_user_read,base_record_read,base_record_write,base_field_read,base_field_write,base_attachment_upload"
  fi

  log "Configuring credentials. Press Enter to skip optional fields."
  prompt_env_value "FEISHU_APP_ID" "FEISHU_APP_ID (same cli_xxx as Windows)"
  prompt_env_value "FEISHU_APP_SECRET" "FEISHU_APP_SECRET" 1
  prompt_env_value "KIMI_API_KEY" "KIMI_API_KEY / Kimi API Key" 1
  prompt_env_value "BATCH_ROOT_CAUSE_BASE_TOKEN" "BATCH_ROOT_CAUSE_BASE_TOKEN (optional)" 0 1
  prompt_env_value "BATCH_ROOT_CAUSE_TABLE_ID" "BATCH_ROOT_CAUSE_TABLE_ID (optional)" 0 1
  prompt_env_value "FEEDBACK_BASE_TOKEN" "FEEDBACK_BASE_TOKEN (optional)" 0 1
  prompt_env_value "FEEDBACK_TABLE_ID" "FEEDBACK_TABLE_ID (optional)" 0 1
}

write_launcher_scripts() {
  cat > "${BIN_DIR}/blu-agent-start" <<EOF
#!/usr/bin/env bash
set -euo pipefail

DEFAULT_APP_HOME="${INSTALL_DIR}"
USER_CONFIG="\${XDG_CONFIG_HOME:-\${HOME}/.config}/blu-agent/blu-agent.env"
DATA_DIR="\${XDG_DATA_HOME:-\${HOME}/.local/share}/blu-agent"
if [[ -n "\${XDG_RUNTIME_DIR:-}" ]]; then
  DEFAULT_RUNTIME_DIR="\${XDG_RUNTIME_DIR}/blu-agent"
else
  DEFAULT_RUNTIME_DIR="\${HOME}/.cache/blu-agent/runtime"
fi

if [[ ! -f "\${USER_CONFIG}" ]]; then
  echo "Config file not found: \${USER_CONFIG}" >&2
  echo "Run blu-agent-configure first." >&2
  exit 1
fi

set -a
source "\${USER_CONFIG}"
set +a

safe_runtime_user() {
  id -un 2>/dev/null || printf '%s' "\${USER:-blu}"
}

is_legacy_runtime() {
  local value="\${1:-}"
  case "\${value}" in
    /tmp/blu_agent|/tmp/blu_agent/*|/tmp/blu_agent_*|/tmp/blu_agent_*/*) return 0 ;;
    *) return 1 ;;
  esac
}

runtime_is_writable() {
  local dir="\${1:-}"
  local probe
  [[ -n "\${dir}" ]] || return 1
  mkdir -p "\${dir}" 2>/dev/null || return 1
  probe="\${dir}/.write_probe_\$\$"
  : > "\${probe}" 2>/dev/null || return 1
  rm -f "\${probe}" 2>/dev/null || true
  return 0
}

choose_runtime_dir() {
  local requested="\${1:-}"
  local runtime_user candidate fallback
  runtime_user="\$(safe_runtime_user)"
  for candidate in \
    "\${requested}" \
    "\${XDG_RUNTIME_DIR:+\${XDG_RUNTIME_DIR}/blu-agent}" \
    "\${HOME}/.cache/blu-agent/runtime"
  do
    [[ -n "\${candidate}" ]] || continue
    is_legacy_runtime "\${candidate}" && continue
    if runtime_is_writable "\${candidate}"; then
      printf '%s\n' "\${candidate}"
      return 0
    fi
  done

  fallback="\${HOME}/.cache/blu-agent/runtime-\${runtime_user}-\$\$"
  if runtime_is_writable "\${fallback}"; then
    printf '%s\n' "\${fallback}"
    return 0
  fi

  echo "Unable to create a writable Blu runtime directory." >&2
  exit 1
}

APP_HOME="\${BLU_AGENT_HOME:-\${DEFAULT_APP_HOME}}"
export BLU_AGENT_HOME="\${BLU_AGENT_HOME:-\${APP_HOME}}"
export BLU_AGENT_DATA="\${BLU_AGENT_DATA:-\${DATA_DIR}}"
export BLU_AGENT_DATABASE="\${BLU_AGENT_DATABASE:-\${DATA_DIR}/database}"
BLU_AGENT_RUNTIME_DIR="\$(choose_runtime_dir "\${BLU_AGENT_RUNTIME_DIR:-\${BLU_AGENT_RUNTIME:-\${DEFAULT_RUNTIME_DIR}}}")"
export BLU_AGENT_RUNTIME_DIR
export BLU_AGENT_RUNTIME="\${BLU_AGENT_RUNTIME_DIR}"
export PYTHONUTF8="\${PYTHONUTF8:-1}"
export PYTHONIOENCODING="\${PYTHONIOENCODING:-utf-8}"
export PYTHONUNBUFFERED="\${PYTHONUNBUFFERED:-1}"
export PYTHONDONTWRITEBYTECODE="\${PYTHONDONTWRITEBYTECODE:-1}"

mkdir -p "\${BLU_AGENT_DATA}" "\${BLU_AGENT_DATABASE}" \
  "\${BLU_AGENT_RUNTIME_DIR}/uploads" \
  "\${BLU_AGENT_RUNTIME_DIR}/analysis_results" \
  "\${BLU_AGENT_RUNTIME_DIR}/batch_root_cause" \
  "\${BLU_AGENT_RUNTIME_DIR}/feedback" \
  "\${BLU_AGENT_RUNTIME_DIR}/result_transformer" \
  "\${BLU_AGENT_RUNTIME_DIR}/actuator_handshake" \
  "\${BLU_AGENT_RUNTIME_DIR}/bad_case_plot" \
  "\${BLU_AGENT_RUNTIME_DIR}/root_cause_evidence" \
  "\${BLU_AGENT_RUNTIME_DIR}/blf_mdf" \
  "\${BLU_AGENT_RUNTIME_DIR}/file_delivery"
chmod -R u+rwX "\${BLU_AGENT_RUNTIME_DIR}" 2>/dev/null || true
if [[ ! -w "\${BLU_AGENT_RUNTIME_DIR}" ]]; then
  echo "Runtime directory is not writable: \${BLU_AGENT_RUNTIME_DIR}" >&2
  echo "Unset BLU_AGENT_RUNTIME_DIR/BLU_AGENT_RUNTIME or rerun install_blu_agent.sh to reset it." >&2
  exit 1
fi
cd "\${APP_HOME}"

echo "Starting Blu bot in foreground mode..."
echo "Project: \${APP_HOME}"
echo "Python: \${APP_HOME}/.venv/bin/python"
echo "Config: \${USER_CONFIG}"
echo "Runtime: \${BLU_AGENT_RUNTIME_DIR}"
echo "Keep this terminal open. Ctrl+C stops the bot."
echo
exec "\${APP_HOME}/.venv/bin/python" -u "\${APP_HOME}/main.py"
EOF

  cat > "${BIN_DIR}/blu-agent-configure" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/blu-agent"
ENV_FILE="${CONFIG_DIR}/blu-agent.env"
EXAMPLE_FILE="${CONFIG_DIR}/blu-agent.env.example"
PY_HELPER="/usr/bin/python3.10"
if [[ ! -x "${PY_HELPER}" ]]; then
  PY_HELPER="$(command -v python3.10 2>/dev/null || command -v python3 2>/dev/null || true)"
fi
if [[ -z "${PY_HELPER}" ]]; then
  echo "Python helper not found. Re-run dist/install_blu_one_click.sh first." >&2
  exit 1
fi
mkdir -p "${CONFIG_DIR}"
if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${EXAMPLE_FILE}" "${ENV_FILE}"
fi

write_env_value() {
  local key="$1"
  local value="$2"
  "${PY_HELPER}" - "${ENV_FILE}" "${key}" "${value}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()
prefix = f"{key}="
for index, line in enumerate(lines):
    if line.startswith(prefix):
        lines[index] = f"{key}={value}"
        break
else:
    lines.append(f"{key}={value}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

read_env_value() {
  local key="$1"
  "${PY_HELPER}" - "${ENV_FILE}" "${key}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
prefix = f"{key}="
for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith(prefix):
        print(line[len(prefix):])
        break
PY
}

prompt_env_value() {
  local key="$1"
  local label="$2"
  local secret="${3:-0}"
  local current
  current="$(read_env_value "${key}")"
  if [[ -n "${current}" ]]; then
    echo "${key}: already configured"
    return
  fi
  local value=""
  if [[ "${secret}" -eq 1 ]]; then
    read -r -s -p "${label}: " value || true
    printf '\n'
  else
    read -r -p "${label}: " value || true
  fi
  if [[ -n "${value}" ]]; then
    write_env_value "${key}" "${value}"
  fi
}

echo "Config file: ${ENV_FILE}"
prompt_env_value "FEISHU_APP_ID" "FEISHU_APP_ID (same cli_xxx as Windows)"
prompt_env_value "FEISHU_APP_SECRET" "FEISHU_APP_SECRET" 1
prompt_env_value "KIMI_API_KEY" "KIMI_API_KEY / Kimi API Key" 1
echo "Optional values can be edited directly in ${ENV_FILE}."
EOF

  cat > "${BIN_DIR}/blu-agent-doctor" <<EOF
#!/usr/bin/env bash
set -euo pipefail
DEFAULT_APP_HOME="${INSTALL_DIR}"
USER_CONFIG="\${XDG_CONFIG_HOME:-\${HOME}/.config}/blu-agent/blu-agent.env"
if [[ ! -f "\${USER_CONFIG}" ]]; then
  echo "[FAIL] Config file missing. Run blu-agent-configure."
  exit 1
fi

set -a
source "\${USER_CONFIG}"
set +a

APP_HOME="\${BLU_AGENT_HOME:-\${DEFAULT_APP_HOME}}"
echo "Blu Agent doctor"
echo "App home: \${APP_HOME}"
echo "Config:   \${USER_CONFIG}"
echo

missing=0
for key in FEISHU_APP_ID FEISHU_APP_SECRET KIMI_API_KEY; do
  if [[ -z "\${!key:-}" ]]; then
    echo "[MISS] \${key}"
    missing=1
  else
    echo "[ OK ] \${key}"
  fi
done

if [[ -x "\${APP_HOME}/.venv/bin/python" ]]; then
  echo "[ OK ] Python venv"
else
  echo "[FAIL] Python venv missing"
  missing=1
fi

"\${APP_HOME}/.venv/bin/python" - <<'PY'
import importlib
mods = ["lark_oapi", "h5py", "openpyxl", "PIL", "numpy", "can", "cantools", "asammdf", "canmatrix", "requests"]
missing = []
for mod in mods:
    try:
        importlib.import_module(mod)
    except Exception as exc:
        missing.append(f"{mod}: {exc}")
if missing:
    print("[FAIL] Python modules")
    for item in missing:
        print("  -", item)
    raise SystemExit(1)
print("[ OK ] Python modules")
PY

(
  cd "\${APP_HOME}"
  "\${APP_HOME}/.venv/bin/python" - <<'PY'
import importlib
import os
from pathlib import Path

required_files = [
    "main.py",
    "batch_root_cause_analysis.py",
    "deepseek_root_cause_analysis.py",
    "root_cause_analysis.py",
    "axle_torque_accuracy_analysis.py",
    "FileConverter/blf_to_mdf.py",
  "scripts/blf_to_h5_collect.py",
  "scripts/find_j6b_blf_path.py",
    "config/performance_problem_presets.xlsx",
    "config/vehicle_response_signal_catalog.xlsx",
  "config/UISEE_Analyze_dbc/CAN_PB/SGM_C1UL_PB_CAN_0227.dbc",
  "config/UISEE_Analyze_dbc/CAN_CB/SGM_C1UL_CB_CAN_0227.dbc",
  "config/UISEE_Analyze_dbc/Eth_Debug_CAN5/PATAC_Acore_Debug_Msg_v0p1p7_Release_0714.dbc",
  "config/UISEE_Analyze_dbc/Eth_VISION_CAN6/hb_Obstacle_J6B.dbc",
]
missing_files = [item for item in required_files if not Path(item).exists()]
if missing_files:
    print("[FAIL] Root-cause analysis package files")
    for item in missing_files:
        print("  - missing", item)
    raise SystemExit(1)

modules = ["batch_root_cause_analysis", "kpi_report_analysis", "deepseek_root_cause_analysis", "root_cause_analysis", "axle_torque_accuracy_analysis"]
missing_modules = []
for module in modules:
    try:
        importlib.import_module(module)
    except Exception as exc:
        missing_modules.append(f"{module}: {exc}")
if missing_modules:
    print("[FAIL] Root-cause analysis imports")
    for item in missing_modules:
        print("  -", item)
    raise SystemExit(1)

from batch_root_cause_analysis import allowed_problem_types, run_single_batch_analysis

labels = set(allowed_problem_types())
expected = {"误制动", "重制动", "横向-猛打", "功能退出"}
missing_labels = sorted(expected - labels)
if missing_labels:
    print("[FAIL] Root-cause problem types")
    for item in missing_labels:
        print("  - missing", item)
    raise SystemExit(1)

main_text = Path("main.py").read_text(encoding="utf-8")
required_snippets = [
    "run_single_batch_analysis(",
    "infer_upload_root_cause_problem_type",
    "single_root_cause_analysis_result_card",
]
missing_snippets = [item for item in required_snippets if item not in main_text]
if missing_snippets:
    print("[FAIL] Single-upload root-cause entry")
    for item in missing_snippets:
        print("  - missing", item)
    raise SystemExit(1)

mode = os.getenv("BATCH_ROOT_CAUSE_ANALYSIS_MODE", "")
if mode != "deepseek_investigation":
    print(f"[WARN] BATCH_ROOT_CAUSE_ANALYSIS_MODE={mode!r}; expected 'deepseek_investigation' for DeepSeek investigation mode.")
else:
    print("[ OK ] DeepSeek root-cause mode")
for key, label, should_write in (
  ("BATCH_ROOT_CAUSE_H5_SEARCH_BASE_DIR", "Batch parsed H5 output root", True),
  ("BATCH_ROOT_CAUSE_BLF_SEARCH_BASE_DIR", "Batch BLF search root", False),
):
  value = os.getenv(key, "").strip()
  if not value:
    print(f"[WARN] {key} is empty")
    continue
  path = Path(value).expanduser()
  if should_write:
    try:
      path.mkdir(parents=True, exist_ok=True)
      probe = path / f".blu_doctor_write_probe_{os.getpid()}"
      probe.write_text("ok", encoding="utf-8")
      probe.unlink(missing_ok=True)
      print(f"[ OK ] {label}: {path}")
    except Exception as exc:
      print(f"[FAIL] {label} is not writable: {path} ({exc})")
      raise SystemExit(1)
  elif path.exists() and os.access(path, os.R_OK):
    print(f"[ OK ] {label}: {path}")
  else:
    print(f"[WARN] {label} is not readable: {path}")
print("[ OK ] Root-cause single/batch shared chain")
_ = run_single_batch_analysis

from axle_torque_accuracy_analysis import DEFAULT_OUTPUT_TORQUE_SCALE, find_default_ecm_arxml, run_axle_torque_accuracy_analysis

axle_arxml = find_default_ecm_arxml()
if axle_arxml:
    print(f"[ OK ] Axle torque ECM ARXML: {Path(axle_arxml).name}")
else:
    print("[WARN] Axle torque ECM ARXML not found. Upload an ECM .arxml in chat or set AXLE_TORQUE_ARXML before using axle torque accuracy analysis.")
print(f"[ OK ] Axle torque output scale: {DEFAULT_OUTPUT_TORQUE_SCALE:g}")
_ = run_axle_torque_accuracy_analysis
PY
) || missing=1

if command -v lark-cli >/dev/null 2>&1; then
  echo "[ OK ] lark-cli"
else
  echo "[WARN] lark-cli not found. Feishu Base/feedback helper flows may need it."
fi
echo "[INFO] User authorization helper: blu-agent-lark-auth"
echo "[INFO] Bot permission checklist: blu-agent-bot-permissions"

exit "\${missing}"
EOF

  cat > "${BIN_DIR}/blu-agent-lark-auth" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/blu-agent"
ENV_FILE="${CONFIG_DIR}/blu-agent.env"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi

if ! command -v lark-cli >/dev/null 2>&1; then
  echo "lark-cli not found. Run the installer again, or install lark-cli first." >&2
  exit 1
fi

domains="${BLU_LARK_AUTH_DOMAINS:-}"
scopes="${BLU_LARK_AUTH_SCOPES:-}"

cat <<MSG
This command requests Blu's default Feishu user authorization.

Use it when Base/feedback commands report:
  need_user_authorization / token_missing / requires scope

It does not fix bot permission errors. If the error points to Feishu Developer
Console or says the bot lacks a scope, enable that scope in the bot app instead.

Domains:      ${domains:-<none>}
Scopes:       ${scopes:-<none>}
MSG

if [[ -z "${domains}" && -z "${scopes}" ]]; then
  cat <<MSG

No user authorization is requested by default.
Batch root-cause Base access uses bot/app OpenAPI permissions. To request user
OAuth for another feature, first set BLU_LARK_AUTH_SCOPES to the exact missing
scope reported by lark-cli, or set BLU_LARK_AUTH_DOMAINS explicitly.
MSG
  exit 0
fi

args=(auth login)
if [[ -n "${scopes}" ]]; then
  args+=(--scope "${scopes}")
fi
if [[ -n "${domains}" ]]; then
  args+=(--domain "${domains}")
fi

exec lark-cli "${args[@]}"
EOF

  cat > "${BIN_DIR}/blu-agent-bot-permissions" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/blu-agent"
ENV_FILE="${CONFIG_DIR}/blu-agent.env"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi

scopes="${BLU_BOT_PERMISSION_SCOPES:-im_message_send,im_group_at_receive,im_resource_download,im_file_image_upload,contact_user_read,base_record_read,base_record_write,base_field_read,base_field_write,base_attachment_upload}"

describe_scope() {
  case "$1" in
    im_message_send)
      echo "消息发送：回复用户、发送卡片、回传分析结果。开放平台里搜索“发送消息 / im.message”。"
      ;;
    im_group_at_receive)
      echo "群消息接收：群里 @ 布鲁后能收到消息事件。开放平台里搜索“接收消息 / im.message.receive_v1 / group_at_msg”。"
      ;;
    im_resource_download)
      echo "消息资源读取：下载用户上传的 h5、blf、arxml 等附件。开放平台里搜索“im:resource / 获取消息中的资源”。"
      ;;
    im_file_image_upload)
      echo "文件和图片上传：回传 mdf、信号图、报告附件。开放平台里搜索“上传文件 / 上传图片 / im:file / im:image”。"
      ;;
    contact_user_read)
      echo "用户基础信息读取：把 open_id 显示为用户姓名，用于处理对象和反馈记录。开放平台里搜索“获取用户基本信息 / contact:user.base:readonly”。"
      ;;
    base_record_read)
      echo "多维表格记录读取：读取批量行车问题原因分析、问题反馈表记录。开放平台里搜索“多维表格记录读取 / base:record:read”。"
      ;;
    base_record_write)
      echo "多维表格记录写入：回填分析结论、状态、概要和反馈记录。开放平台里搜索“多维表格记录写入 / base:record:write”。"
      ;;
    base_field_read)
      echo "多维表格字段读取：读取字段结构，识别 Agent_关键列。开放平台里搜索“多维表格字段读取 / base:field:read”。"
      ;;
    base_field_write)
      echo "多维表格字段写入：创建或更新批量分析/反馈表字段。开放平台里搜索“多维表格字段写入 / base:field:write”。"
      ;;
    base_attachment_upload)
      echo "多维表格附件上传：把分析信号图写入 Agent_分析信号图示。开放平台里搜索“多维表格附件 / 附件上传”。"
      ;;
    *)
      echo "$1"
      ;;
  esac
}

# Keep the user-facing permission checklist readable even if an older generated
# script left mojibake text in the first definition above.
describe_scope() {
  case "$1" in
    im_message_send)
      echo "消息发送：回复用户、发送卡片、回传分析结果。开放平台里搜索“发送消息 / im.message”。"
      ;;
    im_group_at_receive)
      echo "群聊 @ 接收：群里 @ 布鲁后能收到消息事件。开放平台里搜索“接收消息 / im.message.receive_v1 / group_at_msg”。"
      ;;
    im_resource_download)
      echo "消息资源读取：下载用户上传的 h5、blf、arxml 等附件。开放平台里搜索“im:resource / 获取消息中的资源”。"
      ;;
    im_file_image_upload)
      echo "文件和图片上传：回传 mdf、信号图、报告附件。开放平台里搜索“上传文件 / 上传图片 / im:file / im:image”。"
      ;;
    contact_user_read)
      echo "用户基础信息读取：把 open_id 显示为用户姓名，用于处理对象和反馈记录。开放平台里搜索“获取用户基本信息 / contact:user.base:readonly”。"
      ;;
    base_record_read)
      echo "多维表格记录读取：读取批量行车问题原因分析、问题反馈表记录。开放平台里搜索“多维表格记录读取 / base:record:read”。"
      ;;
    base_record_write)
      echo "多维表格记录写入：回填分析结论、状态、概要和反馈记录。开放平台里搜索“多维表格记录写入 / base:record:write”。"
      ;;
    base_field_read)
      echo "多维表格字段读取：读取字段结构，识别 Agent_关键列。开放平台里搜索“多维表格字段读取 / base:field:read”。"
      ;;
    base_field_write)
      echo "多维表格字段写入：创建或更新批量分析/反馈表字段。开放平台里搜索“多维表格字段写入 / base:field:write”。"
      ;;
    base_attachment_upload)
      echo "多维表格附件上传：把分析信号图写入 Agent_分析信号图示。开放平台里搜索“多维表格附件 / 附件上传”。"
      ;;
    *)
      echo "$1"
      ;;
  esac
}

cat <<MSG
Blu bot permissions are app-level permissions.

The local installer can keep this checklist in sync, but Feishu bot scopes still
must be enabled and approved in Feishu Developer Console for the same app:
  App ID: ${FEISHU_APP_ID:-<not configured>}

Open:
  Feishu Developer Console -> the Blu app -> Permissions & Scopes

Enable/search the following permissions:
MSG

IFS=',' read -ra items <<< "${scopes}"
index=1
for item in "${items[@]}"; do
  item="${item#"${item%%[![:space:]]*}"}"
  item="${item%"${item##*[![:space:]]}"}"
  [[ -z "${item}" ]] && continue
  printf '  %s. %s\n' "${index}" "$(describe_scope "${item}")"
  index=$((index + 1))
done

cat <<MSG

Notes:
  - User authorization errors such as need_user_authorization/token_missing/base:*:
      run blu-agent-lark-auth
  - Bot/app permission errors:
      enable the corresponding permission above, publish the app if required,
      and wait for tenant/admin approval when the console asks for it.
MSG
EOF

  cat > "${BIN_DIR}/blu-agent-uninstall" <<EOF
#!/usr/bin/env bash
set -euo pipefail
APP_HOME="${INSTALL_DIR}"
BIN_DIR="${BIN_DIR}"
echo "Removing Blu Agent from \${APP_HOME}"
rm -rf "\${APP_HOME}"
rm -f "\${BIN_DIR}/blu-agent-start" "\${BIN_DIR}/blu-agent-doctor" "\${BIN_DIR}/blu-agent-configure" "\${BIN_DIR}/blu-agent-lark-auth" "\${BIN_DIR}/blu-agent-bot-permissions" "\${BIN_DIR}/blu-agent-uninstall"
echo "Done. Config remains under ~/.config/blu-agent."
EOF

  chmod +x "${BIN_DIR}/blu-agent-start" "${BIN_DIR}/blu-agent-doctor" "${BIN_DIR}/blu-agent-configure" "${BIN_DIR}/blu-agent-lark-auth" "${BIN_DIR}/blu-agent-bot-permissions" "${BIN_DIR}/blu-agent-uninstall"
}

update_shell_path() {
  if [[ "${UPDATE_PATH}" -eq 0 ]]; then
    return
  fi
  if [[ ":${PATH}:" == *":${BIN_DIR}:"* ]]; then
    return
  fi

  local profile="${HOME}/.bashrc"
  local marker="# Blu Agent launcher path"
  if [[ -f "${profile}" ]] && grep -Fq "${marker}" "${profile}"; then
    return
  fi

  {
    echo ""
    echo "${marker}"
    echo "export PATH=\"${BIN_DIR}:\$PATH\""
  } >> "${profile}"
  export PATH="${BIN_DIR}:${PATH}"
}

print_next_steps() {
  echo
  echo "Blu Agent installed."
  echo "Install dir: ${INSTALL_DIR}"
  echo "Commands:    ${BIN_DIR}/blu-agent-start, ${BIN_DIR}/blu-agent-doctor, ${BIN_DIR}/blu-agent-configure, ${BIN_DIR}/blu-agent-lark-auth, ${BIN_DIR}/blu-agent-bot-permissions"
  echo "Config:      ${ENV_FILE}"
  echo
  echo "Useful commands:"
  echo "  blu-agent-doctor"
  echo "  blu-agent-lark-auth"
  echo "  blu-agent-bot-permissions"
  echo "  blu-agent-start"
  echo
  if [[ "${START_AFTER_INSTALL}" -eq 0 ]]; then
    echo "Run 'blu-agent-start' whenever you want to start Blu in the foreground."
  fi
}

ensure_system_dependencies
ensure_python
sanitize_proxy_env
ensure_node_runtime
copy_payload
install_python_dependencies
install_lark_cli
configure_env
ensure_batch_data_permissions
write_launcher_scripts
update_shell_path
run_lark_user_auth

if [[ "${START_AFTER_INSTALL}" -eq 1 ]]; then
  log "Running doctor before start."
  "${BIN_DIR}/blu-agent-doctor"
  log "Starting Blu."
  exec "${BIN_DIR}/blu-agent-start"
fi

print_next_steps
