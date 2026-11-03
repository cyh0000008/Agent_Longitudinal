#!/usr/bin/env python3
"""Create a private Ubuntu one-click installer from the current .env.

The generated shell script contains local credentials. Keep it private and only
use it inside a copied Agent project directory.
"""

from __future__ import annotations

import argparse
import shlex
import stat
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENV = ROOT_DIR / ".env"
DEFAULT_OUT = ROOT_DIR / "dist" / "install_blu_one_click.sh"
DEFAULT_TEMPLATE_ENV = ROOT_DIR / "deploy" / "ubuntu_run" / "env.example"
DEFAULT_PRIVATE_ENV = ROOT_DIR / "deploy" / "ubuntu_run" / "private" / "ubuntu.env"

REQUIRED_KEYS = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "LLM_API_KEY",
)

ALLOWED_PREFIXES = (
    "FEISHU_",
    "LLM_",
    "KIMI_",
    "AXLE_TORQUE_",
    "BATCH_ROOT_CAUSE_",
    "KPI_REPORT_",
    "FEEDBACK_",
    "BLU_",
)

ALLOWED_EXACT_KEYS = {
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
}

MACHINE_SPECIFIC_KEYS = {
    "BLU_AGENT_HOME",
    "BLU_AGENT_DATA",
    "BLU_AGENT_DATABASE",
    "BLU_AGENT_RUNTIME_DIR",
    "BLU_AGENT_RUNTIME",
}


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key] = value
    return values


def should_include_key(key: str, value: str) -> bool:
    if key in MACHINE_SPECIFIC_KEYS:
        return False
    if not value:
        return False
    if key in ALLOWED_EXACT_KEYS:
        return True
    return key.startswith(ALLOWED_PREFIXES)


def render_script(env_values: dict[str, str]) -> str:
    env_lines = [
        f"{key}={shlex.quote(value)}"
        for key, value in sorted(env_values.items())
        if should_include_key(key, value)
    ]
    env_block = "\n".join(env_lines)

    return f"""#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
PROJECT_DIR="$(cd "${{SCRIPT_DIR}}/.." && pwd)"

if [[ ! -f "${{PROJECT_DIR}}/main.py" || ! -f "${{PROJECT_DIR}}/dist/install_blu_agent.sh" ]]; then
  echo "Agent project not found." >&2
  echo "Please extract Agent_ubuntu_one_click.tar.gz on Ubuntu and keep this script at:" >&2
  echo "  Agent/dist/install_blu_one_click.sh" >&2
  exit 1
fi

ENV_TMP="$(mktemp)"
cleanup() {{
  rm -f "${{ENV_TMP}}"
}}
trap cleanup EXIT
chmod 600 "${{ENV_TMP}}"

cat > "${{ENV_TMP}}" <<'BLU_AGENT_ENV'
{env_block}
BLU_AGENT_ENV

exec bash "${{PROJECT_DIR}}/dist/install_blu_agent.sh" "${{PROJECT_DIR}}" --env-file "${{ENV_TMP}}" --start
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a private one-click Ubuntu installer.",
    )
    parser.add_argument(
        "--env",
        default=str(DEFAULT_ENV),
        help="Source env file. Default: project .env",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="Output shell script. Default: dist/install_blu_one_click.sh",
    )
    parser.add_argument(
        "--overlay-env",
        default=str(DEFAULT_PRIVATE_ENV),
        help="Optional private env overlay for Ubuntu packaging.",
    )
    args = parser.parse_args()

    env_path = Path(args.env)
    out_path = Path(args.out)
    overlay_env_path = Path(args.overlay_env)
    if not env_path.exists():
        raise SystemExit(f"Env file not found: {env_path}")

    env_values = parse_env(env_path)

    merged_env_values: dict[str, str] = {}
    if DEFAULT_TEMPLATE_ENV.exists():
        merged_env_values.update(parse_env(DEFAULT_TEMPLATE_ENV))
    merged_env_values.update(env_values)
    if overlay_env_path.exists():
        merged_env_values.update(parse_env(overlay_env_path))

    missing = [key for key in REQUIRED_KEYS if not merged_env_values.get(key)]
    if missing:
        raise SystemExit("Missing required env key(s): " + ", ".join(missing))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_script(merged_env_values))
    current_mode = out_path.stat().st_mode
    out_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    included_keys = [
        key
        for key, value in sorted(merged_env_values.items())
        if should_include_key(key, value)
    ]
    print(f"Created private one-click script: {out_path}")
    print("Included keys:", ", ".join(included_keys))
    print("Next, generate the clean Ubuntu dist package:")
    print("  python deploy\\ubuntu_run\\prepare_ubuntu_project.py")
    print("Then copy dist\\Agent_ubuntu_one_click.tar.gz to Ubuntu and run:")
    print("  tar -xzf Agent_ubuntu_one_click.tar.gz")
    print("  cd Agent_ubuntu_one_click/Agent")
    print("  chmod +x dist/install_blu_one_click.sh")
    print("  ./dist/install_blu_one_click.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
