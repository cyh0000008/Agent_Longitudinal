#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./deploy/ubuntu_run/update_blu_agent.sh [--no-start] [installer options]

Installs the code currently checked out in this repository, runs the health
check, then starts Blu in the foreground. Update the repository separately
before running this script. Use --no-start to only install. Any remaining
options are passed to install_blu_agent.sh.
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
