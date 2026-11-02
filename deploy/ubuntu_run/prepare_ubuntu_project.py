#!/usr/bin/env python3
from __future__ import annotations

import fnmatch
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DIST_DIR = ROOT_DIR / "dist"
OUT_ROOT = DIST_DIR / "Agent_ubuntu_one_click"
OUT_AGENT = OUT_ROOT / "Agent"
ONE_CLICK_SCRIPT = DIST_DIR / "install_blu_one_click.sh"
ARCHIVE_PATH = DIST_DIR / "Agent_ubuntu_one_click.tar.gz"

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    "tmp",
    "dist",
    "logs",
    "uploads",
    "runtime",
    "analysis_results",
    "analysis_outputs",
    "reports",
    "output",
    "tmp_blf_mdf_debug",
    "tmp_conversion_tests",
    "tmp_progress_test_plots",
    "tmp_spreadsheet_fix",
    "tmp_window_test_plots",
    ".codex_spreadsheet_work",
    "node_modules",
    "deploy",
}

EXCLUDED_FILES = {
    ".env",
    "AGENT.md",
    "PROJECT_STATUS.md",
    "README.md",
}

EXCLUDED_PATTERNS = (
    "*.bat",
    "*.cmd",
    "*.pyc",
    "*.pyo",
    "*.ps1",
    "*.tmp",
    "tmp_*",
    "*.log",
    "~$*",
)

EXCLUDED_RELATIVE_FILES = {
    "config/h5_signal_path_mapping_20260725.md",
    "docs/batch_root_cause_analysis_user_guide.md",
    "scripts/generate_batch_root_cause_guide_pdf.py",
    "scripts/generate_signal_flow_gif.py",
    "scripts/update_final_h5_signal_paths.py",
}

REQUIRED_RUNTIME_FILES = {
    "main.py",
    "batch_root_cause_analysis.py",
    "deepseek_root_cause_analysis.py",
    "root_cause_analysis.py",
    "axle_torque_accuracy_analysis.py",
    "FileConverter/blf_to_mdf.py",
    "performance_analysis.py",
    "runtime_paths.py",
    "scripts/blf_to_h5_collect.py",
    "scripts/find_j6b_blf_path.py",
    "config/performance_problem_presets.xlsx",
    "config/vehicle_response_signal_catalog.xlsx",
    "config/UISEE_Analyze_dbc/CAN_PB/SGM_C1UL_PB_CAN_0227.dbc",
    "config/UISEE_Analyze_dbc/CAN_CB/SGM_C1UL_CB_CAN_0227.dbc",
    "config/UISEE_Analyze_dbc/Eth_Debug_CAN5/PATAC_Acore_Debug_Msg_v0p1p7_Release_0714.dbc",
    "config/UISEE_Analyze_dbc/Eth_VISION_CAN6/hb_Obstacle_J6B.dbc",
    "analysis_engine/metrics/code/lateral_steering_sudden.py",
}

REQUIRED_UBUNTU_ENV_VALUES = {
    "BATCH_ROOT_CAUSE_ANALYSIS_MODE": "deepseek_investigation",
}


def should_skip_file(path: Path, rel_file: str) -> bool:
    if rel_file.replace("\\", "/") in EXCLUDED_RELATIVE_FILES:
        return True
    if path.name in EXCLUDED_FILES:
        return True
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in EXCLUDED_PATTERNS)


def read_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def validate_source_tree() -> None:
    missing = [rel for rel in sorted(REQUIRED_RUNTIME_FILES) if not (ROOT_DIR / rel).exists()]
    if missing:
        raise RuntimeError("Ubuntu package is missing required source file(s): " + ", ".join(missing))

    env_values = read_env_values(ROOT_DIR / "deploy" / "ubuntu_run" / "env.example")
    mismatches = [
        f"{key}={env_values.get(key, '')!r} (expected {expected!r})"
        for key, expected in REQUIRED_UBUNTU_ENV_VALUES.items()
        if env_values.get(key) != expected
    ]
    if mismatches:
        raise RuntimeError("Ubuntu env.example is not aligned: " + "; ".join(mismatches))


def ensure_one_click_script() -> None:
    # Regenerate every time so Ubuntu packages do not keep stale field names,
    # model settings, or private deployment variables from an older .env.
    subprocess.run(
        [sys.executable, str(ROOT_DIR / "deploy" / "ubuntu_run" / "create_one_click_script.py")],
        cwd=ROOT_DIR,
        check=True,
    )


def reset_output_dir() -> None:
    resolved_out = OUT_ROOT.resolve()
    resolved_dist = DIST_DIR.resolve()
    if resolved_dist not in resolved_out.parents:
        raise RuntimeError(f"Refuse to remove unexpected path: {resolved_out}")
    if OUT_ROOT.exists():
        shutil.rmtree(OUT_ROOT)
    if ARCHIVE_PATH.exists():
        ARCHIVE_PATH.unlink()
    OUT_AGENT.mkdir(parents=True, exist_ok=True)


def copy_project() -> None:
    root = ROOT_DIR.resolve()
    out_root = OUT_ROOT.resolve()
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current).resolve()
        if current_path == out_root or out_root in current_path.parents:
            dirnames[:] = []
            continue

        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in EXCLUDED_DIRS
            and not any(fnmatch.fnmatch(dirname, pattern) for pattern in EXCLUDED_PATTERNS)
        ]

        rel_dir = current_path.relative_to(root)
        target_dir = OUT_AGENT / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        for filename in filenames:
            source = current_path / filename
            rel_file = (rel_dir / filename).as_posix()
            if should_skip_file(source, rel_file):
                continue
            target = target_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def copy_private_script() -> None:
    target = OUT_AGENT / "dist" / "install_blu_one_click.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ONE_CLICK_SCRIPT, target)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def copy_installer_script() -> None:
    source = ROOT_DIR / "deploy" / "ubuntu_run" / "install_blu_agent.sh"
    target = OUT_AGENT / "dist" / "install_blu_agent.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def copy_install_resources() -> None:
    dist_dir = OUT_AGENT / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    env_example = ROOT_DIR / "deploy" / "ubuntu_run" / "env.example"
    if env_example.exists():
        shutil.copy2(env_example, dist_dir / "env.example")

    wheelhouse = ROOT_DIR / "deploy" / "ubuntu_run" / "wheelhouse"
    if wheelhouse.exists():
        shutil.copytree(wheelhouse, dist_dir / "wheelhouse", dirs_exist_ok=True)


def copy_migration_readme() -> None:
    source = ROOT_DIR / "deploy" / "ubuntu_run" / "README.md"
    if source.exists():
        shutil.copy2(source, OUT_AGENT / "README_UBUNTU.md")


def validate_output_package() -> None:
    missing = [rel for rel in sorted(REQUIRED_RUNTIME_FILES) if not (OUT_AGENT / rel).exists()]
    if missing:
        raise RuntimeError("Ubuntu package output lost required file(s): " + ", ".join(missing))

    env_values = read_env_values(OUT_AGENT / "dist" / "env.example")
    mismatches = [
        f"{key}={env_values.get(key, '')!r} (expected {expected!r})"
        for key, expected in REQUIRED_UBUNTU_ENV_VALUES.items()
        if env_values.get(key) != expected
    ]
    if mismatches:
        raise RuntimeError("Ubuntu package env.example is not aligned: " + "; ".join(mismatches))


def write_usage_note() -> None:
    note = OUT_ROOT / "README_UBUNTU_DEPLOY.txt"
    lines = [
        "布鲁 Ubuntu 22.04 一键部署包",
        "",
        "推荐把 dist/Agent_ubuntu_one_click.tar.gz 复制到 Ubuntu 22.04，解压后执行：",
        "",
        "  tar -xzf Agent_ubuntu_one_click.tar.gz",
        "  cd Agent_ubuntu_one_click/Agent",
        "  chmod +x dist/install_blu_one_click.sh",
        "  ./dist/install_blu_one_click.sh",
        "",
        "如果你复制的是本目录下的 Agent 文件夹，也可以执行：",
        "",
        "  cd /path/to/Agent",
        "  chmod +x dist/install_blu_one_click.sh",
        "  ./dist/install_blu_one_click.sh",
        "",
        "注意：Agent/dist/install_blu_one_click.sh 内含当前飞书机器人和模型 API 配置，只给部署人员使用。",
        "更完整的迁移说明见 Agent/README_UBUNTU.md。",
        "Ubuntu 侧只需要使用 Agent/dist/ 里的脚本；deploy 目录只是 Windows 打包源码，不需要手动使用。",
        "不要同时在 Windows 和 Ubuntu 上运行同一个飞书机器人后端，否则会重复回复。",
        "用户态授权可运行 blu-agent-lark-auth；bot 应用权限清单可运行 blu-agent-bot-permissions 查看，并按清单在飞书开放平台启用/发布。",
        "安装脚本会自动清理旧虚拟环境，并把运行期目录重置到当前用户可写目录，避免继续使用共享 /tmp/blu_agent。",
        "单个上传 .h5 的行车问题原因分析、飞书多维表格批量分析和内网 Excel 单条分析共用 run_single_batch_analysis 单条核心。",
        "当前迁移包默认 BATCH_ROOT_CAUSE_ANALYSIS_MODE=deepseek_investigation，确保 Ubuntu 上也走模型调查式原因分析。",
        "",
    ]
    note.write_text("\n".join(lines), encoding="utf-8")
def create_archive() -> None:
    if not OUT_ROOT.exists():
        raise RuntimeError(f"Package directory not found: {OUT_ROOT}")
    with tarfile.open(ARCHIVE_PATH, "w:gz") as tar:
        tar.add(OUT_ROOT, arcname=OUT_ROOT.name)


def main() -> int:
    validate_source_tree()
    ensure_one_click_script()
    reset_output_dir()
    copy_project()
    copy_installer_script()
    copy_install_resources()
    copy_migration_readme()
    copy_private_script()
    validate_output_package()
    write_usage_note()
    create_archive()

    print("Ubuntu clean dist package generated:")
    print(f"  {OUT_AGENT}")
    print(f"  {ARCHIVE_PATH}")
    print("")
    print("Copy this tar.gz file to Ubuntu, then run:")
    print("  tar -xzf Agent_ubuntu_one_click.tar.gz")
    print("  cd Agent_ubuntu_one_click/Agent")
    print("  chmod +x dist/install_blu_one_click.sh")
    print("  ./dist/install_blu_one_click.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

