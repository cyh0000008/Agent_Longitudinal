#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./deploy/ubuntu_run/update_blu_agent.sh [--no-start] [--no-pull] [installer options]

Fast-forwards the current Git branch from its configured upstream, installs
that checkout, runs the health check, then starts Blu in the foreground.
Use --no-pull for an already-synchronized checkout and --no-start to only install.
Any remaining options are passed to install_blu_agent.sh.
EOF
}

START_AFTER_INSTALL=1
PULL_BEFORE_INSTALL=1
INSTALLER_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-start)
      START_AFTER_INSTALL=0
      shift
      ;;
    --no-pull)
      PULL_BEFORE_INSTALL=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      INSTALLER_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This updater must be run on the Ubuntu/Linux machine." >&2
  exit 1
fi

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

if [[ "${PULL_BEFORE_INSTALL}" -eq 1 ]]; then
  command -v git >/dev/null 2>&1 || { echo "git is required for code synchronization." >&2; exit 1; }
  git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "Not a Git worktree: ${REPO_ROOT}" >&2; exit 1; }
  if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain)" ]]; then
    echo "Git worktree is not clean. Commit or stash changes before updating." >&2
    exit 1
  fi
  git -C "${REPO_ROOT}" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' >/dev/null 2>&1 || {
    echo "Current branch has no upstream. Configure one before updating." >&2
    exit 1
  }
  git -C "${REPO_ROOT}" pull --ff-only
fi

INSTALLER="${REPO_ROOT}/deploy/ubuntu_run/install_blu_agent.sh"
if [[ ! -f "${INSTALLER}" ]]; then
  echo "Installer not found: ${INSTALLER}" >&2
  exit 1
fi

mkdir -p "${REPO_ROOT}/dist"
cp "${REPO_ROOT}/deploy/ubuntu_run/env.example" "${REPO_ROOT}/dist/env.example"

if [[ "${START_AFTER_INSTALL}" -eq 1 ]]; then
  echo "The installer will start the checked-out Blu version in the foreground."
  echo "Stop any existing Blu process first to avoid running two bot instances."
  INSTALLER_ARGS+=(--start)
fi

exec bash "${INSTALLER}" "${REPO_ROOT}" "${INSTALLER_ARGS[@]}"
