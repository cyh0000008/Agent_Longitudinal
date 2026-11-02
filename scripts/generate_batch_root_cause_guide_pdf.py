from __future__ import annotations

import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "batch_root_cause_analysis_user_guide.md"
OUTPUT = ROOT / "output" / "pdf" / "行车问题分析使用说明.pdf"
ACTIVE_FONT = "STSong-Light"


def _register_fonts() -> str:
    global ACTIVE_FONT
    for candidate in (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\simsun.ttc"),
    ):
        if candidate.exists():
            font_name = "BluGuideFont"
            pdfmetrics.registerFont(TTFont(font_name, str(candidate)))
            ACTIVE_FONT = font_name
            return font_name
    font_name = "STSong-Light"
    pdfmetrics.registerFont(UnicodeCIDFont(font_name))
    ACTIVE_FONT = font_name
    return font_name


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("`", "")
    )


def _inline(text: str) -> str:
    text = _escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return text


def _split_table_row(line: str) -> list[str]:
    raw = line.strip().strip("|")
    return [cell.strip() for cell in raw.split("|")]


def _is_table_separator(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|") or "---" not in stripped:
        return False
    return all(set(part.strip()) <= {"-", ":"} for part in stripped.strip("|").split("|"))


def _build_styles(font_name: str) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "GuideTitle",
            parent=base["Title"],
            fontName=font_name,
            fontSize=20,
            leading=28,
            textColor=colors.HexColor("#1f4e8c"),
            alignment=TA_CENTER,
            spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "GuideH2",
            parent=base["Heading2"],
            fontName=font_name,
            fontSize=14,
            leading=20,
            textColor=colors.HexColor("#1f4e8c"),
            spaceBefore=10,
            spaceAfter=6,
            keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "GuideBody",
            parent=base["BodyText"],
            fontName=font_name,
            fontSize=10.5,
            leading=17,
            textColor=colors.HexColor("#222222"),
            alignment=TA_LEFT,
            spaceAfter=5,
        ),
        "bullet": ParagraphStyle(
            "GuideBullet",
            parent=base["BodyText"],
            fontName=font_name,
            fontSize=10.3,
            leading=16,
            leftIndent=12,
            firstLineIndent=0,
            textColor=colors.HexColor("#222222"),
        ),
        "code": ParagraphStyle(
            "GuideCode",
            parent=base["Code"],
            fontName=font_name,
            fontSize=9.5,
            leading=14,
            backColor=colors.HexColor("#f3f5f7"),
            borderColor=colors.HexColor("#d9dee7"),
            borderWidth=0.5,
            borderPadding=5,
            textColor=colors.HexColor("#1c2430"),
            spaceBefore=4,
            spaceAfter=8,
        ),
        "cell": ParagraphStyle(
            "GuideCell",
            parent=base["BodyText"],
            fontName=font_name,
            fontSize=8.8,
            leading=12.5,
            textColor=colors.HexColor("#222222"),
        ),
        "cell_header": ParagraphStyle(
            "GuideCellHeader",
            parent=base["BodyText"],
            fontName=font_name,
            fontSize=9.2,
            leading=13,
            textColor=colors.white,
        ),
        "footer": ParagraphStyle(
            "GuideFooter",
            parent=base["BodyText"],
            fontName=font_name,
            fontSize=8,
            leading=10,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#6b7280"),
        ),
    }


def _add_table(
    story: list,
    rows: list[list[str]],
    styles: dict[str, ParagraphStyle],
    doc_width: float,
    font_name: str,
) -> None:
    if not rows:
        return
    col_count = max(len(row) for row in rows)
    normalized = [row + [""] * (col_count - len(row)) for row in rows]
    widths = [doc_width / col_count] * col_count
    data = []
    for row_index, row in enumerate(normalized):
        style = styles["cell_header"] if row_index == 0 else styles["cell"]
        data.append([Paragraph(_inline(cell), style) for cell in row])
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4e8c")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, -1), font_name),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cfd6e2")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fc")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 7))


def _on_page(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#d9dee7"))
    canvas.line(18 * mm, 285 * mm, 192 * mm, 285 * mm)
    canvas.setFillColor(colors.HexColor("#6b7280"))
    canvas.setFont(ACTIVE_FONT, 8)
    canvas.drawCentredString(105 * mm, 11 * mm, f"行车问题分析使用说明  ·  第 {doc.page} 页")
    canvas.restoreState()


def build_pdf() -> None:
    font_name = _register_fonts()
    styles = _build_styles(font_name)
    text = SOURCE.read_text(encoding="utf-8")
    lines = text.splitlines()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="行车问题分析使用说明",
        author="Blu Agent",
    )
    story: list = []
    i = 0
    in_code = False
    code_lines: list[str] = []
    bullet_items: list[ListItem] = []

    def flush_bullets() -> None:
        if bullet_items:
            story.append(
                ListFlowable(
                    bullet_items.copy(),
                    bulletType="bullet",
                    start="circle",
                    bulletFontName=font_name,
                    leftIndent=14,
                )
            )
            story.append(Spacer(1, 5))
            bullet_items.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                story.append(Paragraph("<br/>".join(_escape(x) for x in code_lines), styles["code"]))
                code_lines.clear()
                in_code = False
            else:
                flush_bullets()
                in_code = True
            i += 1
            continue

        if in_code:
            code_lines.append(line)
            i += 1
            continue

        if not stripped:
            flush_bullets()
            story.append(Spacer(1, 3))
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and _is_table_separator(lines[i + 1]):
            flush_bullets()
            table_rows = [_split_table_row(stripped)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_rows.append(_split_table_row(lines[i]))
                i += 1
            _add_table(story, table_rows, styles, doc.width, font_name)
            continue

        if stripped.startswith("# "):
            flush_bullets()
            story.append(Paragraph(_inline(stripped[2:]), styles["title"]))
            story.append(Spacer(1, 6))
            i += 1
            continue

        if stripped.startswith("## "):
            flush_bullets()
            story.append(Paragraph(_inline(stripped[3:]), styles["h2"]))
            i += 1
            continue

        if stripped.startswith("- "):
            bullet_items.append(ListItem(Paragraph(_inline(stripped[2:]), styles["bullet"])))
            i += 1
            continue

        ordered_match = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if ordered_match:
            flush_bullets()
            story.append(Paragraph(f"{ordered_match.group(1)}. {_inline(ordered_match.group(2))}", styles["body"]))
            i += 1
            continue

        flush_bullets()
        story.append(Paragraph(_inline(stripped), styles["body"]))
        i += 1

    flush_bullets()
    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)


if __name__ == "__main__":
    build_pdf()
    print(OUTPUT)
