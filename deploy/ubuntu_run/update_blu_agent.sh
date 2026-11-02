#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./deploy/ubuntu_run/update_blu_agent.sh [--no-start] [installer options]

Updates the main branch, installs the latest code and dependencies, runs the
health check, then starts Blu in the foreground. Use --no-start to only update
and install. Any remaining options are passed to install_blu_agent.sh.
EOF
}

START_AFTER_INSTALL=1
INSTALLER_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-start)
      START_AFTER_INSTALL=0
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

if ! command -v git >/dev/null 2>&1; then
  echo "git is required. Install it, then rerun this script." >&2
  exit 1
fi

if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "This updater only updates the main branch. Check out main and rerun." >&2
  exit 1
fi

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  cat >&2 <<'EOF'
The repository has uncommitted or untracked files. Nothing was updated.
Commit or move those files out of the repository, then rerun this script.
Ignored local files such as .env and machine-specific data are not affected.
EOF
  git status --short >&2
  exit 1
fi

echo "Updating ${REPO_ROOT} from origin/main..."
git pull --ff-only origin main

INSTALLER="${REPO_ROOT}/deploy/ubuntu_run/install_blu_agent.sh"
if [[ ! -f "${INSTALLER}" ]]; then
  echo "Installer not found: ${INSTALLER}" >&2
  exit 1
fi

mkdir -p "${REPO_ROOT}/dist"
cp "${REPO_ROOT}/deploy/ubuntu_run/env.example" "${REPO_ROOT}/dist/env.example"

if [[ "${START_AFTER_INSTALL}" -eq 1 ]]; then
  echo "The installer will start Blu in the foreground after updating."
  echo "Stop any existing Blu process first to avoid running two bot instances."
  INSTALLER_ARGS+=(--start)
fi

exec bash "${INSTALLER}" "${REPO_ROOT}" "${INSTALLER_ARGS[@]}"
