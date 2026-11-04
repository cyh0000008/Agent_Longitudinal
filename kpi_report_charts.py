from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from openpyxl import load_workbook
from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class Metric:
    name: str
    kind: str
    previous: str
    current: str
    criterion: str
    previous_status: str
    current_status: str
    source: str
    scene: str = ""

    @property
    def summary_performance(self) -> str:
        context = self.name.lower()
        performances = (
            ("舒适性", ("舒适", "顿挫", "颠簸", "冲击", "jerk", "平顺", "抖动", "振荡", "震荡", "点刹")),
            ("响应及时性", ("响应", "反应", "时延", "延迟", "迟滞", "建立时间", "起步慢")),
            ("制动安全性", ("刹停距离", "刹停风险", "安全距离", "碰撞", "跟车过近", "车距过近")),
            ("坡道保持能力", ("溜车", "坡道保持")),
            ("功能稳定性", ("断握手", "退出", "降级", "失效", "接管")),
            ("制动强度", ("重制动", "最大减速度", "制动强度", "急刹")),
            ("加速能力", ("最大加速度", "加速能力", "提速乏力")),
            ("速度跟踪与稳定性", ("控速", "稳速", "setspeed", "不合理低速", "车速误差", "速度误差", "超速", "欠速")),
            ("控制精度", ("精度", "误差", "偏差", "超调")),
            ("目标识别连续性", ("识别", "感知", "丢失", "丢目标")),
        )
        for label, words in performances:
            if any(word in context for word in words):
                return label
        return re.sub(r"^行车[\s:：/_-]*", "", self.name.strip())

    @property
    def summary_scene(self) -> str:
        return re.sub(r"^行车[\s:：/_-]*", "", self.scene.strip())

    @property
    def direction(self) -> str:
        before, after = number(self.previous), number(self.current)
        if before is None or after is None:
            return "不可比"
        return "提升" if after > before else "退化" if after < before else "持平"

    @property
    def transition(self) -> str:
        if not self.previous_status:
            return "无基线"
        before, after = status(self.previous_status), status(self.current_status)
        if before == "可准出" and after == "未准出":
            return "新增未准出"
        if before == "未准出" and after == "可准出":
            return "恢复准出"
        return "状态未变" if before == after else "状态待确认"


@dataclass(frozen=True)
class ReportChart:
    name: str
    previous_version: str
    current_version: str
    metrics: list[Metric]
    vehicle: str = "未提供"
    powertrain: str = "未提供"
    previous_dates: str = ""
    current_dates: str = "未提供"

    @property
    def comparisons(self) -> list[Metric]:
        if not self.previous_version:
            return [m for m in self.metrics if status(m.current_status) == "未准出"]
        return [m for m in self.metrics if significant(m)]

    @property
    def comparison_groups(self) -> list[tuple[str, list[Metric]]]:
        if not self.previous_version:
            metrics = self.comparisons
            return [("当前未准出（无基线）", metrics)] if metrics else []
        labels = (
            "新增未准出", "恢复准出", "未准出但指标提升",
            "未准出且指标下降", "可准出但指标下降",
        )
        groups: dict[str, list[Metric]] = {label: [] for label in labels}
        for metric in self.comparisons:
            if metric.transition in labels[:2]:
                label = metric.transition
            elif status(metric.current_status) == "未准出" and metric.direction == "提升":
                label = labels[2]
            elif status(metric.current_status) == "未准出" and metric.direction == "退化":
                label = labels[3]
            elif status(metric.current_status) == "可准出" and metric.direction == "退化":
                label = labels[4]
            else:
                label = "其他显著变化"
            groups.setdefault(label, []).append(metric)
        return [(label, metrics) for label, metrics in groups.items() if metrics]

    @property
    def attention_summary(self) -> str:
        def topics(metrics: list[Metric]) -> str:
            performances: dict[str, list[str]] = {}
            for metric in metrics:
                scenes = performances.setdefault(metric.summary_performance, [])
                if metric.summary_scene and metric.summary_scene not in scenes:
                    scenes.append(metric.summary_scene)
            labels = [
                performance + (f"（{'、'.join(scenes)}）" if scenes else "")
                for performance, scenes in performances.items()
            ]
            return "、".join(labels[:3]) + ("等方面（完整指标见下方明细）" if len(labels) > 3 else "")

        failing = [m for m in self.metrics if status(m.current_status) == "未准出"]
        unknown = [m for m in self.metrics if status(m.current_status) == "待确认"]
        if not self.previous_version:
            sentences = ["缺少前序版本，不能判断版本提升或退化。"]
            sentences.append(f"当前未准出：{topics(failing)}，需优先跟进。" if failing else "当前未发现明确未准出项。")
        else:
            groups = dict(self.comparison_groups)
            messages = (
                ("新增未准出", "需优先排查"),
                ("未准出且指标下降", "未准出项明显下降，需重点跟进"),
                ("可准出但指标下降", "可准出项明显下降，需关注后续准出风险"),
                ("未准出但指标提升", "虽明显提升但仍未达到准出要求"),
                ("恢复准出", "已恢复准出，建议后续验证稳定性"),
                ("其他显著变化", "需结合明细确认"),
            )
            sentences = [
                f"{label}：{topics(groups[label])}，{message}。"
                for label, message in messages if label in groups
            ]
            if not groups:
                sentences.append("未发现达到筛选门槛的明显变化或准出状态改变。")
            remaining = [m for m in failing if m not in self.comparisons]
            if remaining:
                sentences.append(f"持续关注：{topics(remaining)}仍存在未准出项，需持续跟进。")
        if unknown:
            sentences.append(f"结论待确认：{topics(unknown)}准出结论待确认，暂不判断是否达标。")
        return "\n".join(f"{index}. {sentence}" for index, sentence in enumerate(sentences, 1))


def significant(metric: Metric) -> bool:
    if metric.previous_status and metric.current_status and metric.previous_status.strip() != metric.current_status.strip():
        return True
    before, after = number(metric.previous), number(metric.current)
    if before is None or after is None or before == 0:
        return False
    change = abs(after - before)
    return change > 6 and change / abs(before) >= 0.5


def excluded_metric(name: str) -> bool:
    normalized = name.upper()
    return "ACC" in normalized and "退出" in name and any(
        re.search(rf"(?<![A-Z]){system}(?![A-Z])", normalized) for system in ("ABS", "TCS", "VSE")
    )


def number(raw: str) -> float | None:
    if re.search(r"/\s*0(?:\.0*)?\s*\)", raw):
        return None
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(?:\([^()]*\))?\s*", raw)
    return float(match[1]) if match else None


def status(raw: str) -> str:
    if "不满足" in raw or "不可" in raw:
        return "未准出"
    if raw.strip() == "可准出":
        return "可准出"
    return "待确认"


def extract_chart_reports(file_name: str, content: bytes) -> list[ReportChart]:
    if Path(file_name).suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("定量图表需要带版本表头的 Excel 报告，文本附件不能可靠识别版本。")
    workbook = load_workbook(io.BytesIO(content), data_only=True)
    theme: list[str] = []
    if workbook.loaded_theme:
        root = ElementTree.fromstring(workbook.loaded_theme)
        scheme = root.find("{*}themeElements/{*}clrScheme")
        if scheme is not None:
            theme = [list(item)[0].get("val", "") for item in scheme]

    def green(cell) -> bool:
        color = cell.fill.fgColor
        rgb = color.rgb if color.type == "rgb" else (
            theme[color.theme] if color.type == "theme" and color.theme < len(theme) else ""
        )
        if not isinstance(rgb, str) or not re.fullmatch(r"[a-fA-F0-9]{6,8}", rgb):
            return False
        r, g, b = (int(rgb[-6:][i:i + 2], 16) for i in (0, 2, 4))
        return g > r * 1.15 and g > b * 1.15

    reports: list[ReportChart] = []
    try:
        for sheet in workbook:
            version_row = next((
                row for row in sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 20))
                if any(str(c.value or "").strip() == "软件版本" for c in row)
            ), None)
            if version_row is None:
                raise ValueError(f"{sheet.title} 缺少软件版本表头，不能生成可信对比图。")
            versions = [c for c in version_row if c.value and str(c.value).strip() != "软件版本"]
            candidates = [c for c in versions if green(c)]
            if not candidates:
                raise ValueError(f"{sheet.title} 未识别到绿色当前版本，不能猜测版本。")
            current = max(candidates, key=lambda c: c.column)
            previous = next((c for c in reversed(versions) if c.column < current.column), None)
            if previous is not None and ("无基线" in str(previous.value) or "无基线" in file_name):
                previous = None
            type_row = next((
                row for row in sheet.iter_rows(min_row=current.row + 1, max_row=min(sheet.max_row, current.row + 5))
                if any(str(c.value or "").strip() == "MPI" for c in row)
                and any(str(c.value or "").strip() == "CPI" for c in row)
            ), None)
            if type_row is None:
                raise ValueError(f"{sheet.title} 缺少 MPI/CPI 表头。")
            header_row = next((
                row for row in sheet.iter_rows(min_row=type_row[0].row + 1, max_row=min(sheet.max_row, type_row[0].row + 3))
                if any(str(c.value or "").strip() == "指标名称" for c in row)
            ), None)
            if header_row is None:
                raise ValueError(f"{sheet.title} 缺少指标名称表头。")
            headers = {str(c.value).strip(): c.column for c in header_row if c.value}

            def text(row: int, column: int | None) -> str:
                value = sheet.cell(row, column).value if column else None
                return "" if value is None else str(value)

            def metadata(label: str, column: int) -> str:
                row = next((
                    cells[0].row for cells in sheet.iter_rows(
                        min_row=1, max_row=current.row,
                    ) if any(str(c.value or "").strip() == label for c in cells)
                ), None)
                return text(row, column) if row is not None else ""

            def scene_text(row: int) -> str:
                column = headers.get("场景")
                if column is None:
                    return ""
                for merged in sheet.merged_cells.ranges:
                    if merged.min_row <= row <= merged.max_row and merged.min_col <= column <= merged.max_col:
                        return text(merged.min_row, merged.min_col)
                return text(row, column)

            metrics: list[Metric] = []
            for row in range(header_row[0].row + 1, sheet.max_row + 1):
                name = text(row, headers["指标名称"])
                if not name or excluded_metric(name):
                    continue
                for kind in ("MPI", "CPI"):
                    col = next((c.column for c in type_row if current.column <= c.column <= current.column + 1 and c.value == kind), None)
                    old_col = next((c.column for c in type_row if previous and previous.column <= c.column <= previous.column + 1 and c.value == kind), None)
                    raw = text(row, col)
                    if raw.strip() in {"", "\\", "/", "-", "N/A"}:
                        continue
                    metrics.append(Metric(
                        name, kind, text(row, old_col), raw,
                        text(row, headers.get("Target准出标准")),
                        text(row, headers.get("Base准出结论")) if previous else "",
                        text(row, headers.get("Target准出结论")),
                        f"{sheet.title}!{sheet.cell(row, col).coordinate}",
                        scene_text(row),
                    ))
            if not metrics:
                raise ValueError(f"{sheet.title} 没有可绘制的指标。")
            reports.append(ReportChart(
                f"{file_name} / {sheet.title}", str(previous.value) if previous else "",
                str(current.value), metrics,
                metadata("车型", current.column) or "未提供",
                metadata("动力形式", current.column) or "未提供",
                metadata("日期", previous.column) if previous else "",
                metadata("日期", current.column) or "未提供",
            ))
    finally:
        workbook.close()
    return reports


def render_dashboard(reports: list[ReportChart], output_path: Path) -> None:
    if not reports:
        raise ValueError("没有报告原始数据，不能生成定量图。")
    font_path = next((p for p in (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    ) if p.is_file()), None)
    if font_path is None:
        raise RuntimeError("缺少中文字体，请安装 Noto Sans CJK 或微软雅黑。")
    font = ImageFont.truetype(str(font_path), 22)
    small = ImageFont.truetype(str(font_path), 18)
    metadata_font = ImageFont.truetype(str(font_path), 28)
    title = ImageFont.truetype(str(font_path), 32)
    colors = {"可准出": "#25856A", "未准出": "#C34D48", "待确认": "#8894A6"}

    def summary_lines(text: str) -> list[str]:
        lines: list[str] = []
        for paragraph in text.splitlines():
            line = ""
            for character in paragraph:
                if line and small.getlength(line + character) > 1500:
                    lines.append(line)
                    line = ""
                line += character
            if line:
                lines.append(line)
        return lines

    summaries = [summary_lines(report.attention_summary) for report in reports]
    summary_heights = [max(180, 80 + len(lines) * 25) for lines in summaries]
    height = 130 + sum(
        570 + summary_height + len(r.comparisons) * 186
        + 60 * len(r.comparison_groups)
        for r, summary_height in zip(reports, summary_heights)
    )
    if height > 60000:
        raise ValueError("报告图示超过画布安全限制，请拆分报告。")
    image = Image.new("RGB", (1600, height), "#F3F6FB")
    draw = ImageDraw.Draw(image)

    def write(
        x: int, y: int, text: str, width: int, color: str = "#27364B",
        lines: int = 2, text_font: ImageFont.FreeTypeFont | None = None,
    ):
        selected_font = text_font or small
        remaining = str(text)
        for index in range(lines):
            line = ""
            while remaining and draw.textlength(line + remaining[0], font=selected_font) <= width:
                line += remaining[0]
                remaining = remaining[1:]
            if index == lines - 1 and remaining:
                line = line[:-1] + "…"
            draw.text((x, y + index * (selected_font.size + 7)), line, font=selected_font, fill=color)
            if not remaining:
                break

    draw.rectangle((0, 0, 1600, 100), fill="#17345C")
    draw.text((36, 24), "行车纵向KPI当前版本问题与提升 | 数据仪表盘", font=title, fill="white")
    y = 130
    for report, lines, summary_height in zip(reports, summaries, summary_heights):
        write(36, y, f"车型：{report.vehicle}    动力形式：{report.powertrain}", 1500,
              lines=1, text_font=metadata_font)
        y += 55
        draw.text((36, y), f"{report.previous_version or '无基线'}  →  {report.current_version}", font=title, fill="#17345C")
        y += 60
        write(36, y - 16, "数据日期：" + (
            f"{report.previous_dates or '未提供'} → {report.current_dates}"
            if report.previous_version else report.current_dates
        ), 1500, lines=1)
        y += 22
        draw.text((36, y), "当前准出状态（按有效指标项）", font=font, fill="#17345C")
        counts = {key: sum(status(m.current_status) == key for m in report.metrics) for key in colors}
        start = -90.0
        for key, count in counts.items():
            end = start + count / len(report.metrics) * 360
            if count:
                draw.pieslice((40, y + 40, 250, y + 250), start, end, fill=colors[key])
            start = end
        for index, (key, count) in enumerate(counts.items()):
            draw.text((285, y + 60 + index * 55), f"{key}  {count}", font=font, fill=colors[key])
        draw.text((650, y), "显著变化分布（仅筛选项）" if report.previous_version else "无基线：仅展示当前问题，不判断版本提升", font=font, fill="#17345C")
        for index, (kind, color) in enumerate((("MPI", "#3668B1"), ("CPI", "#C07A2A"))) if report.previous_version else ():
            group = [m for m in report.comparisons if m.kind == kind]
            for offset, label in enumerate(("提升", "退化", "持平")):
                count = sum(m.direction == label for m in group)
                yy = y + 48 + index * 100 + offset * 22
                draw.text((650, yy), f"{kind} {label}", font=small, fill="#27364B")
                if count:
                    draw.rectangle((790, yy + 3, 790 + 500 * count / max(len(group), 1), yy + 18), fill=color)
                draw.text((1320, yy), str(count), font=small, fill="#27364B")
        y += 258
        draw.rounded_rectangle((24, y + 8, 1576, y + summary_height - 12), radius=12, fill="white")
        draw.text((40, y + 18), "版本关注摘要", font=font, fill="#17345C")
        for index, line in enumerate(lines):
            draw.text((40, y + 54 + index * 25), line, font=small, fill="#27364B")
        y += summary_height
        draw.text((36, y), "重点问题与提升对比 | 蓝：上一版本  绿：当前版本", font=font, fill="#17345C")
        y += 46
        draw.text((36, y), "筛选：相对变化≥50%且绝对差值>6，或准出状态改变；下界/零事件不判升降，各行独立刻度。", font=small, fill="#69758B")
        y += 32
        group_headers: dict[int, list[tuple[str, list[Metric]]]] = {}
        ordered_metrics: list[Metric] = []
        for label, metrics in report.comparison_groups:
            group_headers.setdefault(len(ordered_metrics), []).append((label, metrics))
            ordered_metrics.extend(sorted(metrics, key=lambda m: m.kind))
        for index, metric in enumerate(ordered_metrics):
            if index in group_headers:
                for label, metrics in group_headers[index]:
                    draw.text((36, y + 10), f"{label}（{len(metrics)}项）", font=font, fill="#17345C")
                    y += 60
            draw.rounded_rectangle((24, y, 1576, y + 168), radius=12, fill="white")
            write(40, y + 8, f"{metric.kind} | {metric.name}", 480)
            changes = [metric.transition]
            if metric.direction != "不可比":
                changes.insert(0, metric.direction)
            write(40, y + 62, f"当前：{metric.current_status}" + (f" | {' / '.join(changes)}" if report.previous_version else ""), 480, colors[status(metric.current_status)])
            write(40, y + 108, f"标准：{metric.criterion}", 480)
            before, after = number(metric.previous), number(metric.current)
            values = [v for v in (before, after) if v is not None]
            scale = max([abs(v) for v in values] + [1])
            for index, (raw, value, color, label) in enumerate((
                (metric.previous, before, "#3668B1", "旧"),
                (metric.current, after, "#25856A", "新"),
            )):
                if label == "旧" and not report.previous_version:
                    continue
                yy = y + 15 + index * 48
                draw.text((550, yy), label, font=small, fill=color)
                if value is not None and value >= 0:
                    draw.rectangle((590, yy + 3, 590 + max(1, value / scale * 500), yy + 24), fill=color)
                write(1110, yy, raw or "无基线/缺失", 430, color, lines=1)
            write(550, y + 117, f"上一版：{metric.previous_status or '无基线'}", 980)
            y += 186
        y += 97
    image.save(output_path, "PNG")
