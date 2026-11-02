#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PYTHON="${ROOT_DIR}/.venv/bin/python"
MAIN_FILE="${ROOT_DIR}/main.py"

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1

safe_runtime_user() {
  id -un 2>/dev/null || printf '%s' "${USER:-blu}"
}

is_legacy_runtime() {
  local value="${1:-}"
  case "${value}" in
    /tmp/blu_agent|/tmp/blu_agent/*|/tmp/blu_agent_*|/tmp/blu_agent_*/*) return 0 ;;
    *) return 1 ;;
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
  local runtime_user candidate fallback
  runtime_user="$(safe_runtime_user)"
  for candidate in \
    "${BLU_AGENT_RUNTIME_DIR:-}" \
    "${BLU_AGENT_RUNTIME:-}" \
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

BLU_AGENT_RUNTIME_DIR="$(choose_runtime_dir)"
export BLU_AGENT_RUNTIME_DIR
export BLU_AGENT_RUNTIME="${BLU_AGENT_RUNTIME_DIR}"

mkdir -p "${ROOT_DIR}/logs" \
  "${BLU_AGENT_RUNTIME_DIR}/uploads" \
  "${BLU_AGENT_RUNTIME_DIR}/analysis_results" \
  "${BLU_AGENT_RUNTIME_DIR}/batch_root_cause" \
  "${BLU_AGENT_RUNTIME_DIR}/feedback" \
  "${BLU_AGENT_RUNTIME_DIR}/result_transformer" \
  "${BLU_AGENT_RUNTIME_DIR}/actuator_handshake" \
  "${BLU_AGENT_RUNTIME_DIR}/bad_case_plot" \
  "${BLU_AGENT_RUNTIME_DIR}/root_cause_evidence" \
  "${BLU_AGENT_RUNTIME_DIR}/blf_mdf" \
  "${BLU_AGENT_RUNTIME_DIR}/file_delivery"
chmod -R u+rwX "${BLU_AGENT_RUNTIME_DIR}" 2>/dev/null || true
if [[ ! -w "${BLU_AGENT_RUNTIME_DIR}" ]]; then
  echo "Runtime directory is not writable: ${BLU_AGENT_RUNTIME_DIR}" >&2
  echo "Unset BLU_AGENT_RUNTIME_DIR/BLU_AGENT_RUNTIME or rerun install_blu_agent.sh to reset it." >&2
  exit 1
fi

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "Python virtual environment not found: ${VENV_PYTHON}" >&2
  echo "Create it with:" >&2
  echo "  cd ${ROOT_DIR}" >&2
  echo "  python3 -m venv .venv" >&2
  echo "  source .venv/bin/activate" >&2
  echo "  pip install -U pip" >&2
  echo "  pip install -r requirements.txt" >&2
  exit 1
fi

if [[ ! -f "${MAIN_FILE}" ]]; then
  echo "Bot entry file not found: ${MAIN_FILE}" >&2
  exit 1
fi

cd "${ROOT_DIR}"
echo "Starting Blu bot in foreground mode..."
echo "Project: ${ROOT_DIR}"
echo "Python: ${VENV_PYTHON}"
echo "Main: ${MAIN_FILE}"
echo "Runtime: ${BLU_AGENT_RUNTIME_DIR}"
echo "Keep this terminal open. Closing it will stop the bot."
echo
exec "${VENV_PYTHON}" -u "${MAIN_FILE}"
