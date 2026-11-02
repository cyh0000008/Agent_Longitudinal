from __future__ import annotations

import getpass
import os
import re
import tempfile
from functools import lru_cache
from pathlib import Path
import time


LEGACY_SHARED_RUNTIME = Path("/tmp/blu_agent")
_RUNTIME_ENV_KEYS = ("BLU_AGENT_RUNTIME_DIR", "BLU_AGENT_RUNTIME")
_RUNTIME_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_runtime_name(value: str | Path | None, default: str = "item", max_length: int = 160) -> str:
    fallback = str(default or "item").strip() or "item"
    raw = str(value or "").strip()
    cleaned = _RUNTIME_NAME_RE.sub("_", raw)
    cleaned = re.sub(r"\s+", "_", cleaned).strip(" ._")
    if not cleaned:
        cleaned = _RUNTIME_NAME_RE.sub("_", fallback)
        cleaned = re.sub(r"\s+", "_", cleaned).strip(" ._") or "item"
    if max_length > 0:
        cleaned = cleaned[:max_length].rstrip(" ._") or "item"
    return cleaned


def safe_runtime_user() -> str:
    raw = (
        os.getenv("USER")
        or os.getenv("USERNAME")
        or getpass.getuser()
        or "user"
    ).strip() or "user"
    return safe_runtime_name(raw, "user", max_length=64)


def _legacy_shared_tmp_base(path: Path) -> Path | None:
    raw = str(path.expanduser()).replace("\\", "/")
    if raw == "/tmp/blu_agent" or raw.startswith("/tmp/blu_agent/"):
        return LEGACY_SHARED_RUNTIME
    if raw.startswith("/tmp/blu_agent_"):
        first = raw.split("/", 3)[:3]
        return Path("/".join(first))
    if os.name == "nt":
        return None
    try:
        resolved = path.expanduser().resolve(strict=False)
        tmp_root = Path(tempfile.gettempdir()).resolve(strict=False)
    except OSError:
        return None
    try:
        relative = resolved.relative_to(tmp_root)
    except ValueError:
        return None
    if not relative.parts:
        return None
    first = relative.parts[0]
    if first == "blu_agent" or first.startswith("blu_agent_"):
        return tmp_root / first
    return None


def _is_legacy_shared_tmp(path: Path) -> bool:
    return _legacy_shared_tmp_base(path) is not None


def sanitize_runtime_environment() -> None:
    """Drop stale shared temp runtime paths before child processes inherit them."""
    changed = False
    for name in _RUNTIME_ENV_KEYS:
        value = os.getenv(name, "").strip()
        if not value:
            continue
        if _is_legacy_shared_tmp(Path(value).expanduser()):
            os.environ.pop(name, None)
            changed = True
    if changed:
        try:
            get_runtime_root.cache_clear()
        except NameError:
            pass


def publish_runtime_environment(root: str | Path) -> Path:
    """Publish a verified runtime root so subprocesses use the same safe path."""
    target = Path(root).expanduser()
    if not target.is_absolute():
        target = target.resolve()
    target = _prepare_writable_dir(target)
    target_text = str(target)
    for name in _RUNTIME_ENV_KEYS:
        os.environ[name] = target_text
    return target


def get_runtime_subprocess_env(extra: dict[str, str | None] | None = None) -> dict[str, str]:
    """Return a child-process environment pinned to the current safe runtime root."""
    root = get_runtime_root()
    env = os.environ.copy()
    root_text = str(root)
    for name in _RUNTIME_ENV_KEYS:
        env[name] = root_text
    if extra:
        for key, value in extra.items():
            if value is None:
                env.pop(str(key), None)
            else:
                env[str(key)] = str(value)
    return env


def _candidate_roots() -> list[Path]:
    sanitize_runtime_environment()
    candidates: list[Path] = []
    for name in _RUNTIME_ENV_KEYS:
        value = os.getenv(name, "").strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if not _is_legacy_shared_tmp(path):
            candidates.append(path)

    if os.name == "nt":
        candidates.append(Path(tempfile.gettempdir()) / "blu_agent")
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            candidates.append(Path(local_app_data) / "BluAgent" / "runtime")
    else:
        xdg_runtime = os.getenv("XDG_RUNTIME_DIR", "").strip()
        if xdg_runtime:
            candidates.append(Path(xdg_runtime) / "blu-agent")
        candidates.append(Path.home() / ".cache" / "blu-agent" / "runtime")
    return candidates


def _fallback_runtime_root() -> Path:
    if os.name != "nt":
        return Path.home() / ".cache" / "blu-agent" / f"runtime-{safe_runtime_user()}-{os.getpid()}"
    return Path(tempfile.gettempdir()) / f"blu_agent_{safe_runtime_user()}_{os.getpid()}"


def _redirect_runtime_root() -> Path:
    """Stable user-owned runtime root used when legacy shared /tmp paths appear."""
    sanitize_runtime_environment()
    for name in _RUNTIME_ENV_KEYS:
        value = os.getenv(name, "").strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if not _is_legacy_shared_tmp(path):
            return path

    if os.name == "nt":
        return Path(tempfile.gettempdir()) / "blu_agent"

    xdg_runtime = os.getenv("XDG_RUNTIME_DIR", "").strip()
    if xdg_runtime:
        path = Path(xdg_runtime).expanduser() / "blu-agent"
        if not _is_legacy_shared_tmp(path):
            return path
    return Path.home() / ".cache" / "blu-agent" / "runtime"


def _normalize_runtime_path(path: str | Path) -> Path:
    target = _redirect_legacy_shared_tmp(Path(path).expanduser())
    if not target.is_absolute():
        target = target.resolve()
    return target


def _fallback_for_target(target: Path) -> Path:
    fallback = _redirect_runtime_root()
    try:
        parts = [
            safe_runtime_name(part, "runtime")
            for part in target.parts[-3:]
            if part not in ("", ".", "..", os.sep, target.anchor)
        ]
    except (OSError, ValueError):
        parts = []
    if not parts:
        return fallback / safe_runtime_name(target.name or "runtime", "runtime")
    return fallback.joinpath(*parts)


def _redirect_legacy_shared_tmp(target: Path) -> Path:
    legacy_base = _legacy_shared_tmp_base(target)
    if legacy_base is None:
        return target
    raw_target = str(target.expanduser()).replace("\\", "/")
    raw_base = str(legacy_base).replace("\\", "/").rstrip("/")
    if raw_target == raw_base:
        relative = Path()
    elif raw_target.startswith(f"{raw_base}/"):
        relative = Path(*[part for part in raw_target[len(raw_base) + 1 :].split("/") if part])
    else:
        try:
            relative = target.expanduser().resolve(strict=False).relative_to(legacy_base)
        except (OSError, ValueError):
            relative = Path(target.name or "runtime")
    fallback = _redirect_runtime_root()
    relative_parts = [
        safe_runtime_name(part, "runtime")
        for part in relative.parts
        if part not in ("", ".", "..")
    ]
    return fallback if not relative_parts else fallback.joinpath(*relative_parts)


def _prepare_writable_dir(target: Path) -> Path:
    if target.exists() and not target.is_dir():
        raise NotADirectoryError(str(target))
    target.mkdir(parents=True, exist_ok=True)
    probe = target / f".write_probe_{os.getpid()}_{time.time_ns()}"
    probe.write_text("ok", encoding="utf-8")
    try:
        probe.unlink()
    except (FileNotFoundError, OSError):
        pass
    return target


def _prepare_writable_file_candidate(candidate: Path) -> Path:
    if candidate.exists() and candidate.is_dir():
        raise IsADirectoryError(str(candidate))
    _prepare_writable_dir(candidate.parent)

    probe = candidate.parent / f".write_probe_file_{os.getpid()}_{time.time_ns()}"
    probe.write_bytes(b"ok")
    try:
        probe.unlink()
    except (FileNotFoundError, OSError):
        pass

    if candidate.exists():
        with candidate.open("ab"):
            pass
    return candidate


def _file_candidate_from_target(target: Path, default_name: str) -> Path:
    fallback_name = safe_runtime_name(default_name, "download.bin")
    try:
        if target.exists() and target.is_dir():
            return target / fallback_name
    except OSError:
        pass

    name = safe_runtime_name(target.name or fallback_name, fallback_name)
    if not target.name:
        return target / fallback_name
    return target.with_name(name)


def ensure_writable_dir(path: str | Path) -> Path:
    target = _normalize_runtime_path(path)
    try:
        return _prepare_writable_dir(target)
    except OSError:
        return _prepare_writable_dir(_fallback_for_target(target))


def ensure_writable_file_path(path: str | Path, default_name: str = "download.bin") -> Path:
    target = _normalize_runtime_path(path)
    fallback_name = safe_runtime_name(default_name, "download.bin")
    candidate = _file_candidate_from_target(target, fallback_name)
    try:
        parent = ensure_writable_dir(candidate.parent)
        return _prepare_writable_file_candidate(parent / candidate.name)
    except OSError:
        fallback = _file_candidate_from_target(_fallback_for_target(target), fallback_name)
        parent = _prepare_writable_dir(fallback.parent)
        return _prepare_writable_file_candidate(parent / safe_runtime_name(fallback.name or fallback_name, fallback_name))


def _looks_like_download_dir(target: Path, default_name: str) -> bool:
    fallback_name = safe_runtime_name(default_name, "download.bin")
    if not Path(fallback_name).suffix:
        return False
    if target.name == fallback_name:
        return False
    if target.suffix:
        return False
    return bool(target.name)


def ensure_writable_download_file_path(path: str | Path, default_name: str = "download.bin") -> Path:
    """Return a writable file path for downloads whose target may be a session dir."""
    target = _normalize_runtime_path(path)
    fallback_name = safe_runtime_name(default_name, "download.bin")
    try:
        if target.exists() and target.is_dir():
            target = target / fallback_name
        elif _looks_like_download_dir(target, fallback_name):
            target = target / fallback_name
    except OSError:
        target = target / fallback_name
    return ensure_writable_file_path(target, fallback_name)


@lru_cache(maxsize=1)
def get_runtime_root() -> Path:
    sanitize_runtime_environment()
    for candidate in _candidate_roots():
        try:
            return publish_runtime_environment(ensure_writable_dir(candidate))
        except OSError:
            continue
    return publish_runtime_environment(ensure_writable_dir(_fallback_runtime_root()))


def get_runtime_subdir(name: str) -> Path:
    parts = [
        safe_runtime_name(part, "runtime")
        for part in Path(name).parts
        if part not in ("", ".", "..")
    ]
    try:
        return ensure_writable_dir(get_runtime_root().joinpath(*parts))
    except OSError:
        fallback_root = publish_runtime_environment(ensure_writable_dir(_fallback_runtime_root()))
        return ensure_writable_dir(fallback_root.joinpath(*parts))
