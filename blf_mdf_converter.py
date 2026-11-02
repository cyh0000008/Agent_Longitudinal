from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from runtime_paths import (
    ensure_writable_dir,
    ensure_writable_file_path,
    get_runtime_subdir,
    get_runtime_subprocess_env,
    safe_runtime_name,
)


DEFAULT_BLF_MDF_DATABASE_NAME = "Clea_Mode_Year23_ADM_HC_V27.80.0.arxml"
PROJECT_ROOT = Path(__file__).resolve().parent
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_FALSE_ENV_VALUES = {"0", "false", "no", "off"}
DEFAULT_MDF_COMPRESSION = int(os.getenv("BLF_MDF_COMPRESSION", "0"))
DEFAULT_PRESERVE_CHANNEL = os.getenv("BLF_MDF_PRESERVE_CHANNEL", "0").strip().lower() not in _FALSE_ENV_VALUES
DEFAULT_DECODE_INCOMPLETE_SIGNALS = (
    os.getenv("BLF_MDF_DECODE_INCOMPLETE_SIGNALS", "1").strip().lower() not in _FALSE_ENV_VALUES
)


@dataclass(frozen=True)
class BlfMdfConversionResult:
    input_path: Path
    output_path: Path
    database_path: Path
    summary_path: Path | None = None
    frame_count: int | None = None
    decoded_message_count: int | None = None
    compression: int = DEFAULT_MDF_COMPRESSION
    preserve_channel: bool = DEFAULT_PRESERVE_CHANNEL
    decode_incomplete_signals: bool = DEFAULT_DECODE_INCOMPLETE_SIGNALS
    stdout_tail: str = ""

    @property
    def output_size(self) -> int:
        return self.output_path.stat().st_size if self.output_path.exists() else 0


def _tail(text: str, limit: int = 4000) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[-limit:]


def _is_current_platform_path(path_text: str) -> bool:
    value = (path_text or "").strip()
    if not value:
        return False
    if os.name != "nt" and _WINDOWS_ABSOLUTE_RE.match(value):
        return False
    return True


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _converter_script_candidates() -> list[Path]:
    # The bundled converter is the single source of truth on every platform.
    # Explicit environment overrides are accepted only when they are valid for
    # the current platform, so Windows paths can never leak into Ubuntu.
    candidates: list[Path] = [
        PROJECT_ROOT / "FileConverter" / "blf_to_mdf.py",
    ]

    script_env = os.getenv("BLF_MDF_CONVERTER_SCRIPT")
    if script_env and _is_current_platform_path(script_env):
        candidates.append(Path(script_env).expanduser())

    for env_name in ("BLF_MDF_CONVERTER_DIR", "FILECONVERTER_DIR"):
        converter_dir = os.getenv(env_name)
        if converter_dir and _is_current_platform_path(converter_dir):
            candidates.append(Path(converter_dir).expanduser() / "blf_to_mdf.py")

    return _dedupe_paths(candidates)


def resolve_blf_mdf_converter_script() -> Path:
    candidates = _converter_script_candidates()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved.exists() and resolved.is_file():
            return resolved

    checked = "\n".join(f"- {path}" for path in candidates)
    raise FileNotFoundError(
        "未找到随工程分发的 BLF 转 MDF 工具。已检查：\n"
        f"{checked}\n"
        "当前安装必须包含 FileConverter/blf_to_mdf.py。"
    )

def _find_count(summary: object, key_tokens: tuple[str, ...]) -> int | None:
    if isinstance(summary, dict):
        for key, value in summary.items():
            normalized = str(key).lower()
            if all(token in normalized for token in key_tokens):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
            nested = _find_count(value, key_tokens)
            if nested is not None:
                return nested
    if isinstance(summary, list):
        for item in summary:
            nested = _find_count(item, key_tokens)
            if nested is not None:
                return nested
    return None


def _read_summary(summary_path: Path) -> tuple[int | None, int | None]:
    if not summary_path.exists() or summary_path.stat().st_size <= 0:
        return None, None
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    frame_count = _find_count(summary, ("frame",)) or _find_count(summary, ("raw", "count"))
    decoded_count = _find_count(summary, ("decoded",)) or _find_count(summary, ("message", "count"))
    return frame_count, decoded_count


def run_blf_to_mdf_conversion(
    blf_path: str | Path,
    arxml_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    output_name: str | None = None,
    compression: int | None = None,
    preserve_channel: bool | None = None,
    decode_incomplete_signals: bool | None = None,
    timeout_seconds: int = 3600,
) -> BlfMdfConversionResult:
    blf_path = Path(blf_path).expanduser()
    arxml_path = Path(arxml_path).expanduser()
    if not blf_path.is_absolute():
        blf_path = blf_path.resolve()
    if not arxml_path.is_absolute():
        arxml_path = arxml_path.resolve()
    if blf_path.suffix.lower() != ".blf":
        raise ValueError("BLF 转 MDF 只支持 .blf 输入文件。")
    if arxml_path.suffix.lower() != ".arxml":
        raise ValueError("BLF 转 MDF 的解析数据库必须是 .arxml 文件。")
    if not blf_path.exists() or blf_path.stat().st_size <= 0:
        raise FileNotFoundError(f"BLF 文件不存在或为空：{blf_path}")
    if not arxml_path.exists() or arxml_path.stat().st_size <= 0:
        raise FileNotFoundError(f"ARXML 数据库不存在或为空：{arxml_path}")

    script_path = resolve_blf_mdf_converter_script()
    converter_dir = script_path.parent

    output_dir = Path(output_dir).expanduser() if output_dir else get_runtime_subdir("blf_mdf")
    if not output_dir.is_absolute():
        output_dir = output_dir.resolve()
    output_dir = ensure_writable_dir(output_dir)
    safe_stem = safe_runtime_name(blf_path.stem, "converted")
    output_name = safe_runtime_name(output_name or f"{safe_stem}.mdf", f"{safe_stem}.mdf")
    if not output_name.lower().endswith(".mdf"):
        output_name = f"{safe_runtime_name(Path(output_name).stem, safe_stem)}.mdf"
    summary_stem = safe_runtime_name(Path(output_name).stem, safe_stem)
    output_path = ensure_writable_file_path(output_dir / output_name, f"{safe_stem}.mdf")
    summary_path = ensure_writable_file_path(
        output_dir / f"{summary_stem}_summary.json",
        f"{safe_stem}_summary.json",
    )
    compression = DEFAULT_MDF_COMPRESSION if compression is None else int(compression)
    compression = max(0, compression)
    preserve_channel = DEFAULT_PRESERVE_CHANNEL if preserve_channel is None else bool(preserve_channel)
    decode_incomplete_signals = (
        DEFAULT_DECODE_INCOMPLETE_SIGNALS
        if decode_incomplete_signals is None
        else bool(decode_incomplete_signals)
    )

    cmd = [
        sys.executable,
        str(script_path),
        "--blf",
        str(blf_path),
        "--arxml",
        str(arxml_path),
        "--output",
        str(output_path),
        "--summary",
        str(summary_path),
        "--pad-truncated",
        "--compression",
        str(compression),
        "--overwrite",
    ]
    if decode_incomplete_signals:
        cmd.append("--decode-incomplete-signals")
    if preserve_channel:
        cmd.extend(["--group-by-channel", "--prefix-channel"])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(converter_dir),
            env=get_runtime_subprocess_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"BLF 转 MDF 超时，已等待 {timeout_seconds} 秒。") from exc

    stdout_tail = _tail("\n".join(part for part in [proc.stdout, proc.stderr] if part))
    if proc.returncode != 0:
        raise RuntimeError(f"BLF 转 MDF 失败：\n{stdout_tail or f'exit code {proc.returncode}'}")
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError(f"BLF 转 MDF 没有生成有效文件：{output_path}")

    frame_count, decoded_count = _read_summary(summary_path)
    return BlfMdfConversionResult(
        input_path=blf_path,
        output_path=output_path,
        database_path=arxml_path,
        summary_path=summary_path if summary_path.exists() else None,
        frame_count=frame_count,
        decoded_message_count=decoded_count,
        compression=compression,
        preserve_channel=preserve_channel,
        decode_incomplete_signals=decode_incomplete_signals,
        stdout_tail=stdout_tail,
    )
