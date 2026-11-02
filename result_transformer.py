from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime_paths import ensure_writable_dir, ensure_writable_file_path


IMAGE_ARTIFACT_TYPES = {"flowchart_image", "timeline_image", "diagram_image"}
FILE_ARTIFACT_TYPES = {"markdown_file", "html_file"}
SUPPORTED_ARTIFACT_TYPES = IMAGE_ARTIFACT_TYPES | FILE_ARTIFACT_TYPES | {"gif", "table", "rich_card"}


@dataclass
class GeneratedArtifact:
    artifact_type: str
    title: str
    path: Path | None = None
    description: str = ""
    card_text: str = ""

    @property
    def is_uploadable_image(self) -> bool:
        return bool(self.path and self.path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif"})


def render_transform_artifacts(plan: dict[str, Any], output_root: str | Path) -> list[GeneratedArtifact]:
    if not plan or not plan.get("should_transform"):
        return []
    output_dir = ensure_writable_dir(Path(output_root) / f"transform_{uuid.uuid4().hex[:10]}")

    generated: list[GeneratedArtifact] = []
    for index, artifact in enumerate(plan.get("artifacts") or [], start=1):
        if not isinstance(artifact, dict):
            continue
        artifact_type = str(artifact.get("type") or "").strip()
        if artifact_type not in SUPPORTED_ARTIFACT_TYPES:
            continue
        title = _clean_title(artifact.get("title") or f"产物 {index}")
        description = str(artifact.get("description") or "").strip()
        try:
            if artifact_type in {"flowchart_image", "diagram_image"}:
                path = output_dir / f"{index:02d}_{_slug(title)}.png"
                render_flowchart_image(artifact, path)
                generated.append(GeneratedArtifact(artifact_type, title, path, description))
            elif artifact_type == "timeline_image":
                path = output_dir / f"{index:02d}_{_slug(title)}.png"
                render_timeline_image(artifact, path)
                generated.append(GeneratedArtifact(artifact_type, title, path, description))
            elif artifact_type == "gif":
                path = output_dir / f"{index:02d}_{_slug(title)}.gif"
                render_flowchart_gif(artifact, path)
                generated.append(GeneratedArtifact(artifact_type, title, path, description))
            elif artifact_type == "table":
                generated.append(GeneratedArtifact(artifact_type, title, None, description, _format_table_text(artifact)))
            elif artifact_type == "rich_card":
                generated.append(GeneratedArtifact(artifact_type, title, None, description, str(artifact.get("content") or "").strip()))
            elif artifact_type in FILE_ARTIFACT_TYPES:
                suffix = ".html" if artifact_type == "html_file" else ".md"
                path = ensure_writable_file_path(
                    output_dir / f"{index:02d}_{_slug(title)}{suffix}",
                    f"{index:02d}_{_slug(title)}{suffix}",
                )
                content = str(artifact.get("content") or "").strip()
                if artifact_type == "html_file":
                    content = _wrap_html(title, content)
                path.write_text(content, encoding="utf-8")
                generated.append(
                    GeneratedArtifact(
                        artifact_type,
                        title,
                        path,
                        description,
                        f"已生成 `{path.name}`。当前飞书文件上传通道未接入时，会优先把关键内容放进卡片。",
                    )
                )
        except Exception as exc:
            generated.append(
                GeneratedArtifact(
                    artifact_type,
                    title,
                    None,
                    description,
                    f"{title} 生成失败：{exc}",
                )
            )
    return generated


def render_flowchart_image(artifact: dict[str, Any], output_path: str | Path, highlight: set[str] | None = None) -> Path:
    image = _render_flowchart_pil(artifact, highlight=highlight or set())
    output = ensure_writable_file_path(Path(output_path), Path(output_path).name or "flowchart.png")
    image.save(output)
    return output


def render_flowchart_gif(artifact: dict[str, Any], output_path: str | Path) -> Path:
    from PIL import Image

    nodes = _normalize_nodes(artifact.get("nodes") or [])
    node_ids = [node["id"] for node in nodes]
    frames = artifact.get("frames") or []
    if not frames:
        frames = [
            {"title": f"步骤 {index}", "highlight": [node_id], "caption": ""}
            for index, node_id in enumerate(node_ids, start=1)
        ]
    pil_frames: list[Image.Image] = []
    for frame_index, frame in enumerate(frames[:12], start=1):
        highlight = {str(item) for item in (frame.get("highlight") or [])}
        caption = str(frame.get("caption") or frame.get("title") or f"步骤 {frame_index}").strip()
        frame_artifact = dict(artifact)
        if caption:
            frame_artifact["description"] = f"{frame_index}. {caption}"
        pil_frames.append(_render_flowchart_pil(frame_artifact, highlight=highlight))
    if not pil_frames:
        pil_frames = [_render_flowchart_pil(artifact, highlight=set())]

    output = ensure_writable_file_path(Path(output_path), Path(output_path).name or "flowchart.gif")
    pil_frames[0].save(
        output,
        save_all=True,
        append_images=pil_frames[1:],
        duration=900,
        loop=0,
        optimize=False,
    )
    return output


def render_timeline_image(artifact: dict[str, Any], output_path: str | Path) -> Path:
    from PIL import Image, ImageDraw

    font = _font(28)
    small = _font(22)
    title_font = _font(38)
    events = _normalize_events(artifact.get("events") or [])
    if not events:
        events = [{"time": "", "title": "资料不足", "detail": "本地结果没有提供可绘制的时序事件。", "level": "warning"}]

    width = 1280
    row_heights = []
    for event in events:
        detail_lines = _wrap_text(event["detail"], 46)
        row_heights.append(88 + max(1, len(detail_lines)) * 30)
    height = 170 + sum(row_heights) + 50
    image = Image.new("RGB", (width, height), "#f7f9fc")
    draw = ImageDraw.Draw(image)

    title = _clean_title(artifact.get("title") or "状态时序图")
    description = str(artifact.get("description") or "").strip()
    draw.text((56, 38), title, fill="#17324d", font=title_font)
    if description:
        draw.text((58, 92), _truncate(description, 70), fill="#50657a", font=small)

    x_line = 120
    y = 155
    draw.line((x_line, y, x_line, height - 55), fill="#c6d3df", width=4)
    for event, row_height in zip(events, row_heights):
        color = {"error": "#e5484d", "warning": "#f59f00", "normal": "#2f9e44"}.get(event["level"], "#467fcf")
        draw.ellipse((x_line - 13, y + 10, x_line + 13, y + 36), fill=color, outline="#ffffff", width=3)
        draw.text((155, y + 4), event["time"] or "-", fill="#49606f", font=small)
        draw.text((255, y), event["title"], fill="#162331", font=font)
        detail_y = y + 42
        for line in _wrap_text(event["detail"], 48):
            draw.text((255, detail_y), line, fill="#4a5965", font=small)
            detail_y += 30
        y += row_height

    output = ensure_writable_file_path(Path(output_path), Path(output_path).name or "timeline.png")
    image.save(output)
    return output


def _render_flowchart_pil(artifact: dict[str, Any], highlight: set[str]) -> Any:
    from PIL import Image, ImageDraw

    nodes = _normalize_nodes(artifact.get("nodes") or [])
    edges = _normalize_edges(artifact.get("edges") or [])
    if not nodes:
        nodes = [{"id": "start", "label": "资料不足，无法生成完整流程", "kind": "warning"}]
        edges = []

    font = _font(24)
    small = _font(20)
    title_font = _font(36)
    width = 1500
    box_width = 1050
    box_x = 150
    box_gap = 58
    title = _clean_title(artifact.get("title") or "流程图")
    description = str(artifact.get("description") or "").strip()

    node_heights = []
    wrapped_labels = []
    for node in nodes:
        lines = _wrap_text(node["label"], 42)
        wrapped_labels.append(lines)
        node_heights.append(max(92, 46 + len(lines) * 34))
    non_seq_edge_count = sum(
        1
        for edge in edges
        if edge.get("label")
        and node_order_index_preview(edge, nodes).get("to", 0) != node_order_index_preview(edge, nodes).get("from", -2) + 1
    )
    bottom_note_height = 70 + non_seq_edge_count * 36 if non_seq_edge_count else 0
    height = 180 + sum(node_heights) + box_gap * max(0, len(nodes) - 1) + 90 + bottom_note_height

    image = Image.new("RGB", (width, height), "#f7f9fc")
    draw = ImageDraw.Draw(image)
    draw.text((56, 36), title, fill="#17324d", font=title_font)
    if description:
        draw.text((58, 90), _truncate(description, 76), fill="#50657a", font=small)

    positions: dict[str, tuple[int, int, int, int]] = {}
    y = 150
    for index, (node, lines, box_height) in enumerate(zip(nodes, wrapped_labels, node_heights), start=1):
        x1, y1, x2, y2 = box_x, y, box_x + box_width, y + box_height
        positions[node["id"]] = (x1, y1, x2, y2)
        style = _node_style(node.get("kind", "state"), node["id"] in highlight)
        draw.rounded_rectangle((x1, y1, x2, y2), radius=18, fill=style["fill"], outline=style["outline"], width=4)
        draw.rounded_rectangle((x1 + 20, y1 + 18, x1 + 52, y1 + 50), radius=10, fill="#eef4ff", outline=None)
        draw.text((x1 + 30, y1 + 22), f"{index}", fill=style["index_fill"], font=small)
        text_y = y1 + 20
        for line in lines:
            draw.text((x1 + 76, text_y), line, fill=style["text"], font=font)
            text_y += 34
        y = y2 + box_gap

    node_order = {node["id"]: index for index, node in enumerate(nodes)}
    if not edges and len(nodes) > 1:
        edges = [
            {"from": nodes[index]["id"], "to": nodes[index + 1]["id"], "label": ""}
            for index in range(len(nodes) - 1)
        ]
    non_seq_notes: list[str] = []
    for edge in edges:
        start = positions.get(edge["from"])
        end = positions.get(edge["to"])
        if not start or not end:
            continue
        label = edge.get("label", "")
        if node_order.get(edge["to"], 0) == node_order.get(edge["from"], -2) + 1:
            x = (start[0] + start[2]) // 2
            y1 = start[3]
            y2 = end[1]
            _draw_arrow(draw, (x, y1 + 4), (x, y2 - 6), "#7b8fa3")
            if label:
                _draw_edge_label(draw, _truncate(label, 34), x + 18, (y1 + y2) // 2 - 15, small)
        else:
            gutter = min(width - 95, start[2] + 92)
            y_start = (start[1] + start[3]) // 2
            y_end = (end[1] + end[3]) // 2
            draw.line((start[2], y_start, gutter, y_start), fill="#9aabba", width=3)
            draw.line((gutter, y_start, gutter, y_end), fill="#9aabba", width=3)
            _draw_arrow(draw, (gutter, y_end), (end[0] - 8, y_end), "#9aabba")
            if label:
                from_idx = node_order.get(edge["from"], 0) + 1
                to_idx = node_order.get(edge["to"], 0) + 1
                non_seq_notes.append(f"{from_idx} -> {to_idx}：{label}")

    if non_seq_notes:
        note_y = max(position[3] for position in positions.values()) + 52
        draw.text((box_x, note_y), "补充跳转关系", fill="#17324d", font=font)
        note_y += 42
        for note in non_seq_notes[:8]:
            for line in _wrap_text(note, 54):
                draw.text((box_x + 24, note_y), f"- {line}", fill="#4a5965", font=small)
                note_y += 30

    return image


def node_order_index_preview(edge: dict[str, str], nodes: list[dict[str, str]]) -> dict[str, int]:
    order = {node["id"]: index for index, node in enumerate(nodes)}
    return {"from": order.get(edge.get("from", ""), -99), "to": order.get(edge.get("to", ""), -99)}


def _draw_edge_label(draw: Any, text: str, x: int, y: int, font: Any) -> None:
    bbox = draw.textbbox((x, y), text, font=font)
    pad_x, pad_y = 8, 4
    draw.rounded_rectangle(
        (bbox[0] - pad_x, bbox[1] - pad_y, bbox[2] + pad_x, bbox[3] + pad_y),
        radius=8,
        fill="#f7f9fc",
        outline="#d7e1ea",
    )
    draw.text((x, y), text, fill="#5f6f7f", font=font)


def _draw_arrow(draw: Any, start: tuple[int, int], end: tuple[int, int], color: str) -> None:
    x1, y1 = start
    x2, y2 = end
    draw.line((x1, y1, x2, y2), fill=color, width=4)
    if abs(x2 - x1) > abs(y2 - y1):
        direction = 1 if x2 >= x1 else -1
        points = [(x2, y2), (x2 - direction * 14, y2 - 8), (x2 - direction * 14, y2 + 8)]
    else:
        direction = 1 if y2 >= y1 else -1
        points = [(x2, y2), (x2 - 8, y2 - direction * 14), (x2 + 8, y2 - direction * 14)]
    draw.polygon(points, fill=color)


def _node_style(kind: str, active: bool) -> dict[str, str]:
    if active:
        return {"fill": "#fff3bf", "outline": "#f08c00", "text": "#1f2933", "index_fill": "#e67700"}
    styles = {
        "start": {"fill": "#e6fcf5", "outline": "#20c997", "text": "#0b3b2e", "index_fill": "#087f5b"},
        "end": {"fill": "#edf2ff", "outline": "#5c7cfa", "text": "#172554", "index_fill": "#364fc7"},
        "decision": {"fill": "#fff9db", "outline": "#fcc419", "text": "#3b2f00", "index_fill": "#e67700"},
        "action": {"fill": "#e7f5ff", "outline": "#339af0", "text": "#123047", "index_fill": "#1c7ed6"},
        "warning": {"fill": "#fff5f5", "outline": "#ff6b6b", "text": "#58151c", "index_fill": "#c92a2a"},
    }
    return styles.get(kind, {"fill": "#ffffff", "outline": "#b7c4d0", "text": "#1f2933", "index_fill": "#4c6ef5"})


def _normalize_nodes(items: list[Any]) -> list[dict[str, str]]:
    nodes = []
    seen = set()
    for index, item in enumerate(items[:14], start=1):
        if not isinstance(item, dict):
            continue
        node_id = _sanitize_id(item.get("id") or f"n{index}", f"n{index}")
        if node_id in seen:
            node_id = f"{node_id}_{index}"
        seen.add(node_id)
        label = _clean_text(item.get("label") or item.get("title") or node_id)
        kind = str(item.get("kind") or item.get("type") or "state").strip().lower()
        if kind not in {"start", "state", "decision", "action", "end", "warning"}:
            kind = "state"
        nodes.append({"id": node_id, "label": label, "kind": kind})
    return nodes


def _normalize_edges(items: list[Any]) -> list[dict[str, str]]:
    edges = []
    for index, item in enumerate(items[:18], start=1):
        if not isinstance(item, dict):
            continue
        source = _sanitize_id(item.get("from") or item.get("source") or "", f"from_{index}")
        target = _sanitize_id(item.get("to") or item.get("target") or "", f"to_{index}")
        if not source or not target:
            continue
        edges.append({"from": source, "to": target, "label": _clean_text(item.get("label") or "")})
    return edges


def _sanitize_id(value: Any, fallback: str) -> str:
    raw = str(value or "").strip()
    sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw)[:40].strip("_")
    return sanitized or fallback


def _normalize_events(items: list[Any]) -> list[dict[str, str]]:
    events = []
    for item in items[:14]:
        if not isinstance(item, dict):
            continue
        level = str(item.get("level") or "normal").strip().lower()
        if level not in {"normal", "warning", "error"}:
            level = "normal"
        events.append(
            {
                "time": _clean_text(item.get("time") or ""),
                "title": _clean_text(item.get("title") or "状态变化"),
                "detail": _clean_text(item.get("detail") or item.get("description") or ""),
                "level": level,
            }
        )
    return events


def _format_table_text(artifact: dict[str, Any]) -> str:
    columns = artifact.get("columns") or []
    rows = artifact.get("rows") or []
    if not columns or not rows:
        return str(artifact.get("content") or "").strip()
    headers = [str(item.get("title") or item.get("key") or "") for item in columns if isinstance(item, dict)]
    keys = [str(item.get("key") or item.get("title") or "") for item in columns if isinstance(item, dict)]
    lines = [" | ".join(headers), " | ".join(["---"] * len(headers))]
    for row in rows[:16]:
        if not isinstance(row, dict):
            continue
        lines.append(" | ".join(str(row.get(key, "")).replace("|", "｜") for key in keys))
    return "\n".join(lines)


def _wrap_html(title: str, content: str) -> str:
    if "<html" in content.lower():
        return content
    escaped = html.escape(content).replace("\n", "<br>\n")
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:Microsoft YaHei,Arial,sans-serif;line-height:1.7;padding:32px;color:#1f2933}"
        "table{border-collapse:collapse}td,th{border:1px solid #d0d7de;padding:8px 10px}</style>"
        "</head><body>"
        f"<h1>{html.escape(title)}</h1><p>{escaped}</p>"
        "</body></html>"
    )


def _font(size: int) -> Any:
    from PIL import ImageFont

    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    for path in candidates:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_text(text: str, width: int) -> list[str]:
    value = _clean_text(text)
    if not value:
        return [""]
    lines = []
    for paragraph in re.split(r"\n+", value):
        current = ""
        current_width = 0
        for char in paragraph:
            char_width = 2 if ord(char) > 127 else 1
            if current and current_width + char_width > width:
                lines.append(current)
                current = char
                current_width = char_width
            else:
                current += char
                current_width += char_width
        if current:
            lines.append(current)
    return lines[:6] or [value]


def _clean_title(value: Any) -> str:
    return _truncate(_clean_text(value), 42) or "结果整理"


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _truncate(text: str, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _slug(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]+", "_", text or "").strip("_")
    return value[:40] or "artifact"
