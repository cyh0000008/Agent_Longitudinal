from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib import error, request

try:
    from feishu_config import load_env
except Exception:  # pragma: no cover - intranet image keeps feishu_config.py, this is a guard.
    def load_env() -> None:
        return None


DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
DEFAULT_MODEL = "local-model"


@dataclass(frozen=True)
class LLMConfig:
    api_base_url: str
    api_key: str
    model: str
    timeout_seconds: int


def get_llm_config() -> LLMConfig:
    load_env()
    api_base_url = os.getenv("LLM_API_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    timeout_seconds = _env_int("LLM_TIMEOUT_SECONDS", 300)
    return LLMConfig(
        api_base_url=api_base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
    )


def call_chat_completion(
    messages: list[dict[str, str]],
    *,
    response_format: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
) -> str:
    config = get_llm_config()
    payload: dict[str, Any] = {"model": config.model, "messages": messages, "stream": False}
    if response_format:
        payload["response_format"] = response_format
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    api_request = request.Request(
        url=f"{config.api_base_url}/chat/completions",
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        timeout = timeout_seconds if timeout_seconds is not None else config.timeout_seconds
        with request.urlopen(api_request, timeout=timeout) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return f"聊天模型调用失败：HTTP {exc.code}。{trim_error(detail)}"
    except Exception as exc:
        return f"聊天模型调用失败：{exc}"

    return extract_reply_text(response_data).strip()


def extract_reply_text(response_data: dict[str, Any]) -> str:
    choices = response_data.get("choices") or []
    if choices:
        choice = choices[0] or {}
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text_parts.append(str(item.get("text", "")))
                    elif isinstance(item.get("text"), str):
                        text_parts.append(item["text"])
            return "\n".join(part for part in text_parts if part)
        if isinstance(choice.get("text"), str):
            return choice["text"]
    if isinstance(response_data.get("message"), str):
        return response_data["message"]
    if isinstance(response_data.get("content"), str):
        return response_data["content"]
    return ""


def parse_json_object(text: str) -> dict[str, Any] | None:
    candidate = (text or "").strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.DOTALL).strip()
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def trim_error(detail: str, limit: int = 800) -> str:
    compact = re.sub(r"\s+", " ", detail or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def _env_int(key: str, default: int) -> int:
    value = os.getenv(key, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default
