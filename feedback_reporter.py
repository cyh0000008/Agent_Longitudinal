from __future__ import annotations

import os
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from lark_oapi.api.bitable.v1 import AppTableRecord, CreateAppTableRecordRequest

from runtime_paths import (
    ensure_writable_dir,
    ensure_writable_file_path,
    get_runtime_subdir,
    get_runtime_subprocess_env,
)

ROOT = Path(__file__).resolve().parent
def get_feedback_runtime_dir() -> Path:
    return get_runtime_subdir("feedback")


DEFAULT_RUNTIME_DIR = get_feedback_runtime_dir()


@dataclass(frozen=True)
class FeedbackConfig:
    base_token: str
    table_id: str
    title_field: str = "标题"
    time_field: str = "反馈时间"
    user_field: str = "上传人员"
    chat_field: str = "群聊"
    message_field: str = "触发消息ID"
    content_field: str = "反馈内容"
    status_field: str = "状态"
    skill_field: str = "关联能力"
    context_field: str = "上下文摘要"
    local_image_field: str = "本地快照路径"
    attachment_field: str = "对话快照"
    default_status: str = "待确认"
    lark_cli: str = "lark-cli.cmd"
    cli_identity: str = "bot"
    record_writer: str = "sdk"
    use_cli_attachment: bool = True
    cleanup_local_snapshot: bool = True

    @property
    def is_configured(self) -> bool:
        return bool(self.base_token and self.table_id)


@dataclass
class FeedbackResult:
    configured: bool
    success: bool
    record_id: str = ""
    image_path: str = ""
    attachment_uploaded: bool = False
    local_snapshot_deleted: bool = False
    error: str = ""


def load_feedback_config() -> FeedbackConfig:
    def value(*names: str, default: str = "") -> str:
        for name in names:
            current = os.getenv(name, "").strip()
            if current:
                return current
        return default

    use_cli_attachment = value("FEEDBACK_USE_CLI_ATTACHMENT", default="1").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    cleanup_local_snapshot = value("FEEDBACK_CLEANUP_LOCAL_SNAPSHOT", default="1").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    return FeedbackConfig(
        base_token=value("FEEDBACK_BASE_TOKEN", "FEISHU_FEEDBACK_BASE_TOKEN"),
        table_id=value("FEEDBACK_TABLE_ID", "FEISHU_FEEDBACK_TABLE_ID"),
        title_field=value("FEEDBACK_FIELD_TITLE", default="标题"),
        time_field=value("FEEDBACK_FIELD_TIME", default="反馈时间"),
        user_field=value("FEEDBACK_FIELD_USER", default="上传人员"),
        chat_field=value("FEEDBACK_FIELD_CHAT", default="群聊"),
        message_field=value("FEEDBACK_FIELD_MESSAGE_ID", default="触发消息ID"),
        content_field=value("FEEDBACK_FIELD_CONTENT", default="反馈内容"),
        status_field=value("FEEDBACK_FIELD_STATUS", default="状态"),
        skill_field=value("FEEDBACK_FIELD_SKILL", default="关联能力"),
        context_field=value("FEEDBACK_FIELD_CONTEXT", default="上下文摘要"),
        local_image_field=value("FEEDBACK_FIELD_LOCAL_IMAGE", default="本地快照路径"),
        attachment_field=value("FEEDBACK_ATTACHMENT_FIELD", default="对话快照"),
        default_status=value("FEEDBACK_STATUS_DEFAULT", default="待确认"),
        lark_cli=value("FEEDBACK_LARK_CLI", default="lark-cli.cmd"),
        cli_identity=value("FEEDBACK_LARK_CLI_AS", default="bot"),
        record_writer=value("FEEDBACK_RECORD_WRITER", default="sdk").lower(),
        use_cli_attachment=use_cli_attachment,
        cleanup_local_snapshot=cleanup_local_snapshot,
    )


def render_feedback_snapshot(
    *,
    events: list[dict[str, Any]],
    reporter_open_id: str,
    chat_id: str,
    feedback_text: str,
    source_message_id: str,
    output_dir: Path | None = None,
) -> Path:
    output_dir = ensure_writable_dir(output_dir or get_feedback_runtime_dir())
    now = datetime.now()
    output_path = output_dir / f"feedback_{now.strftime('%Y%m%d_%H%M%S')}_{source_message_id or 'manual'}.png"

    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as exc:  # pragma: no cover - depends on runtime deps
        raise RuntimeError("缺少 Pillow，无法生成反馈快照图片") from exc

    font_regular = _load_font(ImageFont, 22)
    font_small = _load_font(ImageFont, 18)
    font_bold = _load_font(ImageFont, 24, bold=True)
    font_title = _load_font(ImageFont, 32, bold=True)

    width = 1240
    margin = 34
    header_h = 154
    bubble_gap = 18
    bubble_pad = 18
    line_h = 34
    max_bubble_w = 920

    prepared = []
    for event in events[-12:]:
        role = str(event.get("role") or "")
        label = "用户" if role == "user" else "布鲁"
        text = str(event.get("text") or "").strip() or "[空消息]"
        time_text = _format_event_time(event.get("created_at_ms"))
        max_text_w = max_bubble_w - bubble_pad * 2
        lines = _wrap_text(text, font_regular, max_text_w)
        bubble_h = max(62, len(lines) * line_h + bubble_pad * 2 + 22)
        prepared.append((role, label, time_text, lines, bubble_h))

    height = header_h + 24 + sum(item[4] + bubble_gap for item in prepared) + 60
    height = max(height, 520)
    image = Image.new("RGB", (width, height), "#f5f7fb")
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((0, 0, width, 112), radius=0, fill="#eaf1ff")
    draw.text((margin, 26), "布鲁问题反馈快照", fill="#19335f", font=font_title)
    draw.text(
        (margin, 78),
        f"反馈人：{reporter_open_id}    群聊：{chat_id}    时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        fill="#50607a",
        font=font_small,
    )

    feedback_lines = _wrap_text(f"反馈内容：{feedback_text or '未填写'}", font_regular, width - margin * 2)
    y = 126
    for line in feedback_lines[:3]:
        draw.text((margin, y), line, fill="#1f2937", font=font_regular)
        y += line_h
    y += 10

    for role, label, time_text, lines, bubble_h in prepared:
        is_user = role == "user"
        fill = "#dcebff" if is_user else "#ffffff"
        outline = "#a8c9ff" if is_user else "#d8dde8"
        x1 = width - margin - max_bubble_w if is_user else margin
        x2 = width - margin if is_user else margin + max_bubble_w
        y1 = y
        y2 = y + bubble_h
        draw.rounded_rectangle((x1, y1, x2, y2), radius=18, fill=fill, outline=outline, width=1)
        draw.text((x1 + bubble_pad, y1 + 12), f"{label}  {time_text}", fill="#516070", font=font_small)
        text_y = y1 + 42
        for line in lines[:8]:
            draw.text((x1 + bubble_pad, text_y), line, fill="#111827", font=font_regular)
            text_y += line_h
        if len(lines) > 8:
            draw.text((x1 + bubble_pad, text_y), "...", fill="#6b7280", font=font_regular)
        y = y2 + bubble_gap

    image.save(output_path)
    return output_path


def create_feedback_record(
    *,
    rest_client: Any,
    config: FeedbackConfig,
    reporter_open_id: str,
    chat_id: str,
    source_message_id: str,
    feedback_text: str,
    related_skill: str,
    context_summary: str,
    image_path: Path,
) -> FeedbackResult:
    if not config.is_configured:
        return FeedbackResult(
            configured=False,
            success=False,
            image_path=str(image_path),
            error="未配置 FEEDBACK_BASE_TOKEN 和 FEEDBACK_TABLE_ID",
        )

    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = _clip(feedback_text or "布鲁问题反馈", 80)
    fields = {
        config.title_field: title,
        config.time_field: now_text,
        config.user_field: reporter_open_id,
        config.chat_field: chat_id,
        config.message_field: source_message_id,
        config.content_field: feedback_text or "未填写",
        config.status_field: config.default_status,
        config.skill_field: related_skill or "",
        config.context_field: _clip(context_summary, 5000),
        config.local_image_field: _local_snapshot_note(config, image_path),
    }
    fields = {key: value for key, value in fields.items() if key and value is not None}

    if config.record_writer == "cli":
        return _create_record_with_cli(config, fields, image_path)

    response = _create_record(rest_client, config, fields)
    if not response.success() and config.time_field in fields:
        retry_fields = dict(fields)
        retry_fields[config.time_field] = int(time.time() * 1000)
        response = _create_record(rest_client, config, retry_fields)
    if not response.success():
        return FeedbackResult(
            configured=True,
            success=False,
            image_path=str(image_path),
            error=_response_error("写入问题反馈表失败", response),
        )

    response_data = getattr(response, "data", None)
    record = getattr(response_data, "record", None) or response_data
    record_id = str(getattr(record, "record_id", "") or "")
    attachment_uploaded = upload_feedback_attachment(config, record_id, image_path)
    local_snapshot_deleted = cleanup_local_snapshot(config, image_path)
    return FeedbackResult(
        configured=True,
        success=True,
        record_id=record_id,
        image_path=str(image_path),
        attachment_uploaded=attachment_uploaded,
        local_snapshot_deleted=local_snapshot_deleted,
    )


def _create_record(rest_client: Any, config: FeedbackConfig, fields: dict[str, Any]) -> Any:
    request = (
        CreateAppTableRecordRequest.builder()
        .app_token(config.base_token)
        .table_id(config.table_id)
        .request_body(AppTableRecord.builder().fields(fields).build())
        .build()
    )
    return rest_client.bitable.v1.app_table_record.create(request)


def _create_record_with_cli(config: FeedbackConfig, fields: dict[str, Any], image_path: Path) -> FeedbackResult:
    cli_path = _resolve_lark_cli(config)
    if not cli_path:
        return FeedbackResult(
            configured=True,
            success=False,
            image_path=str(image_path),
            error="未找到 lark-cli，无法写入问题反馈表",
        )
    runtime_dir = ensure_writable_dir(get_feedback_runtime_dir())
    payload_path = ensure_writable_file_path(
        runtime_dir / f"feedback_record_{int(time.time() * 1000)}.json",
        "feedback_record.json",
    )
    payload_path.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")
    payload_arg = f"@{payload_path.name}"
    command = [
        cli_path,
        "base",
        "+record-upsert",
        "--as",
        config.cli_identity,
        "--base-token",
        config.base_token,
        "--table-id",
        config.table_id,
        "--json",
        payload_arg,
        "--format",
        "json",
    ]
    try:
            completed = _run_cli_command(command, timeout=45, cwd=payload_path.parent)
    except Exception as exc:
        return FeedbackResult(
            configured=True,
            success=False,
            image_path=str(image_path),
            error=f"写入问题反馈表失败：{exc}",
        )
    finally:
        try:
            payload_path.unlink(missing_ok=True)
        except Exception:
            pass
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit={completed.returncode}"
        return FeedbackResult(
            configured=True,
            success=False,
            image_path=str(image_path),
            error=f"写入问题反馈表失败：{detail}",
        )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return FeedbackResult(
            configured=True,
            success=False,
            image_path=str(image_path),
            error=f"写入问题反馈表失败：无法解析 lark-cli 返回：{exc}",
        )
    if not payload.get("ok", False):
        return FeedbackResult(
            configured=True,
            success=False,
            image_path=str(image_path),
            error=f"写入问题反馈表失败：{payload.get('error') or payload}",
        )
    data = payload.get("data") or {}
    record = data.get("record") if isinstance(data, dict) else {}
    if not isinstance(record, dict):
        record = {}
    record_id = str(record.get("record_id") or record.get("id") or data.get("record_id") or data.get("id") or "")
    if not record_id:
        record_ids = record.get("record_id_list") or data.get("record_id_list") or []
        if isinstance(record_ids, list) and record_ids:
            record_id = str(record_ids[0] or "")
    attachment_uploaded = upload_feedback_attachment(config, record_id, image_path)
    local_snapshot_deleted = cleanup_local_snapshot(config, image_path)
    return FeedbackResult(
        configured=True,
        success=True,
        record_id=record_id,
        image_path=str(image_path),
        attachment_uploaded=attachment_uploaded,
        local_snapshot_deleted=local_snapshot_deleted,
    )


def upload_feedback_attachment(config: FeedbackConfig, record_id: str, image_path: Path) -> bool:
    if not (config.use_cli_attachment and config.attachment_field and record_id and image_path.exists()):
        return False
    cli_path = _resolve_lark_cli(config)
    if not cli_path:
        return False
    staged_path = _stage_feedback_attachment(image_path)
    command = [
        cli_path,
        "base",
        "+record-upload-attachment",
        "--as",
        config.cli_identity,
        "--base-token",
        config.base_token,
        "--table-id",
        config.table_id,
        "--record-id",
        record_id,
        "--field-id",
        config.attachment_field,
        "--file",
        staged_path.name,
        "--format",
        "json",
    ]
    try:
        completed = _run_cli_command(command, timeout=45, cwd=staged_path.parent)
    except Exception as exc:
        print(f"feedback attachment upload failed: {exc}", flush=True)
        return False
    finally:
        try:
            staged_path.unlink(missing_ok=True)
        except Exception:
            pass
    if completed.returncode != 0:
        print(
            "feedback attachment upload failed: "
            f"code={completed.returncode}, stderr={completed.stderr.strip()}, stdout={completed.stdout.strip()}",
            flush=True,
        )
        return False
    return True


def _local_snapshot_note(config: FeedbackConfig, image_path: Path) -> str:
    if config.cleanup_local_snapshot:
        return f"写表后自动清理本地快照：{image_path.name}"
    return str(image_path)


def cleanup_local_snapshot(config: FeedbackConfig, image_path: Path) -> bool:
    if not config.cleanup_local_snapshot or not image_path:
        return False
    try:
        resolved_path = image_path.resolve()
        runtime_dir = ensure_writable_dir(get_feedback_runtime_dir()).resolve()
        try:
            resolved_path.relative_to(runtime_dir)
        except ValueError:
            print(f"skip feedback snapshot cleanup outside runtime dir: {resolved_path}", flush=True)
            return False
        if not resolved_path.exists():
            return False
        resolved_path.unlink()
        return not resolved_path.exists()
    except Exception as exc:
        print(f"feedback snapshot cleanup failed: {image_path}, error={exc}", flush=True)
        return False


def _resolve_lark_cli(config: FeedbackConfig) -> str | None:
    return shutil.which(config.lark_cli) or shutil.which("lark-cli.cmd") or shutil.which("lark-cli")


def _run_cli_command(
    command: list[str], *, timeout: int, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    last_completed: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, 4):
        completed = subprocess.run(
            command,
            cwd=str(cwd or ROOT),
            env=get_runtime_subprocess_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        last_completed = completed
        if completed.returncode == 0 or not _is_retryable_cli_failure(completed):
            return completed
        time.sleep(min(2 * attempt, 5))
    return last_completed  # type: ignore[return-value]


def _stage_feedback_attachment(image_path: Path) -> Path:
    attachment_dir = ensure_writable_dir(get_feedback_runtime_dir() / "attachments")
    safe_stem = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in image_path.stem)[:80]
    suffix = image_path.suffix or ".png"
    target = ensure_writable_file_path(
        attachment_dir / f"{time.time_ns()}_{safe_stem}{suffix}",
        f"{safe_stem or 'feedback'}{suffix}",
    )
    shutil.copy2(image_path, target)
    return target


def _is_retryable_cli_failure(completed: subprocess.CompletedProcess[str]) -> bool:
    text = f"{completed.stderr}\n{completed.stdout}".lower()
    return any(marker in text for marker in ("eof", "transport", "timeout", "temporar", "connection reset"))


def _relative_cli_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def feedback_context_summary(events: list[dict[str, Any]], max_chars: int = 4000) -> str:
    lines = []
    for event in events[-12:]:
        role = "用户" if event.get("role") == "user" else "布鲁"
        text = str(event.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{role}: {text}")
    return _clip("\n".join(lines), max_chars)


def _load_font(ImageFont: Any, size: int, bold: bool = False) -> Any:
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap_text(text: str, font: Any, max_width: int) -> list[str]:
    if not text:
        return [""]
    lines = []
    for paragraph in str(text).replace("\r\n", "\n").split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for char in paragraph:
            trial = current + char
            if _text_width(trial, font) <= max_width:
                current = trial
                continue
            if current:
                lines.append(current)
            current = char
        if current:
            lines.append(current)
    return lines or [""]


def _text_width(text: str, font: Any) -> int:
    try:
        return int(font.getlength(text))
    except Exception:
        return len(text) * 12


def _format_event_time(created_at_ms: Any) -> str:
    try:
        timestamp = int(created_at_ms) / 1000
        return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
    except Exception:
        return ""


def _clip(text: str, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3] + "..."


def _response_error(prefix: str, response: Any) -> str:
    log_id = response.get_log_id() if hasattr(response, "get_log_id") else ""
    troubleshooter = ""
    if hasattr(response, "get_troubleshooter"):
        try:
            troubleshooter = response.get_troubleshooter()
        except Exception:
            troubleshooter = ""
    return (
        f"{prefix}: code={getattr(response, 'code', '')}, "
        f"msg={getattr(response, 'msg', '')}, log_id={log_id}, troubleshooter={troubleshooter}"
    )
