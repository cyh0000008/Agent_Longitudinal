from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "feishu_agent_signal_flow.gif"
PREVIEW = ROOT / "docs" / "feishu_agent_signal_flow_preview.png"

W, H = 1600, 900
BG = (246, 248, 251)
PANEL = (255, 255, 255)
BORDER = (203, 213, 225)
TEXT = (15, 23, 42)
MUTED = (71, 85, 105)
BLUE = (37, 99, 235)
PURPLE = (124, 58, 237)
GREEN = (5, 150, 105)
ORANGE = (234, 88, 12)
GRAY = (148, 163, 184)


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


F_TITLE = load_font(40)
F_SUB = load_font(20)
F_SECTION = load_font(21)
F_BOX = load_font(24)
F_TEXT = load_font(18)
F_SMALL = load_font(15)
F_CAP = load_font(22)

NODES = [
    ("飞书智能体应用", ["群聊消息 / @机器人", "自然语言需求", "BLF / H5 / 日志上传", "报告、表格、图示回群"], (106, 220, 366, 470), BLUE),
    ("DeepSeek 云端大脑", ["理解用户真实意图", "判断本地能力是否可用", "抽取任务参数", "编排工具并润色回复"], (474, 220, 774, 470), PURPLE),
    ("本地 Agent 服务", ["事件去重 / 会话隔离", "文件下载、解析、清理", "数据库版本选择", "诊断执行与报告渲染"], (882, 220, 1202, 470), GREEN),
    ("Codex 仓库", ["本地代码维护", "规则文档迭代", "调试与启动脚本"], (1300, 220, 1490, 470), ORANGE),
]

CAPABILITIES = [
    ("性能问题分析", ["ACC / APA / 稳定性问题识别", "问题时间点 + 总线信号图示"], (76, 642, 524, 792), (220, 252, 231)),
    ("执行器分析 / 查阅", ["制动 EBCM / ebooster，驱动 PDF / ECP / ECM", "握手、降级、状态未闭环检查"], (576, 642, 1024, 792), (219, 234, 254)),
    ("应用层 / 底软故障诊断", ["FDA7：应用层故障，DID 字节流解析", "FDA3：底软故障，slog / fault_event 解析"], (1076, 642, 1524, 792), (255, 237, 213)),
]

# Orthogonal polylines only.
PATHS = [
    (1, [(366, 345), (474, 345)], BLUE, "用户请求", (410, 318)),
    (2, [(774, 345), (882, 345)], BLUE, "任务分发", (820, 318)),
    (3, [(1042, 470), (1042, 584), (800, 584), (800, 642)], GREEN, "调用执行器分析", (802, 558)),
    (3, [(1042, 470), (1042, 584), (300, 584), (300, 642)], GREEN, "调用性能分析", (420, 558)),
    (3, [(1042, 470), (1042, 584), (1300, 584), (1300, 642)], GREEN, "调用故障查询", (1148, 558)),
    (4, [(882, 420), (774, 420)], PURPLE, "结果润色", (820, 392)),
    (5, [(474, 420), (366, 420)], GREEN, "报告回群", (410, 392)),
    (6, [(1202, 345), (1300, 345)], ORANGE, "代码迭代", (1240, 318)),
]


def rounded(d: ImageDraw.ImageDraw, xy, radius: int, fill, outline=None, width: int = 1) -> None:
    d.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def draw_arrow_head(d: ImageDraw.ImageDraw, end: tuple[int, int], prev: tuple[int, int], color) -> None:
    dx = end[0] - prev[0]
    dy = end[1] - prev[1]
    if abs(dx) >= abs(dy):
        if dx >= 0:
            pts = [(end[0], end[1]), (end[0] - 14, end[1] - 8), (end[0] - 14, end[1] + 8)]
        else:
            pts = [(end[0], end[1]), (end[0] + 14, end[1] - 8), (end[0] + 14, end[1] + 8)]
    else:
        if dy >= 0:
            pts = [(end[0], end[1]), (end[0] - 8, end[1] - 14), (end[0] + 8, end[1] - 14)]
        else:
            pts = [(end[0], end[1]), (end[0] - 8, end[1] + 14), (end[0] + 8, end[1] + 14)]
    d.polygon(pts, fill=color)


def draw_path(d: ImageDraw.ImageDraw, points: list[tuple[int, int]], color, width: int = 4) -> None:
    d.line(points, fill=color, width=width, joint="curve")
    draw_arrow_head(d, points[-1], points[-2], color)


def draw_step_badge(
    d: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    number: int,
    color: tuple[int, int, int],
    active: bool,
) -> None:
    if number <= 0:
        return
    x, y = xy
    radius = 16 if active else 14
    fill = color if active else (255, 255, 255)
    outline = color
    text_fill = (255, 255, 255) if active else color
    d.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill, outline=outline, width=3)
    label = str(number)
    bbox = d.textbbox((0, 0), label, font=F_SMALL)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text((x - tw / 2, y - th / 2 - 1), label, font=F_SMALL, fill=text_fill)


def point_on_path(points: list[tuple[int, int]], t: float) -> tuple[float, float]:
    segments: list[tuple[tuple[int, int], tuple[int, int], float]] = []
    total = 0.0
    for a, b in zip(points, points[1:]):
        length = abs(b[0] - a[0]) + abs(b[1] - a[1])
        segments.append((a, b, length))
        total += length
    distance = total * t
    walked = 0.0
    for a, b, length in segments:
        if walked + length >= distance:
            local = 0 if length == 0 else (distance - walked) / length
            return (a[0] + (b[0] - a[0]) * local, a[1] + (b[1] - a[1]) * local)
        walked += length
    return points[-1]


def draw_base(active_index: int = -1, particle: tuple[float, float, tuple[int, int, int]] | None = None) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    rounded(d, (40, 34, 1560, 866), 26, PANEL, (226, 232, 240), 2)

    d.text((76, 78), "布鲁系统框架", font=F_TITLE, fill=TEXT)
    d.text((78, 126), "飞书入口 · DeepSeek 总控 · 本地工具链 · Codex 维护", font=F_SUB, fill=MUTED)

    rounded(d, (76, 154, 1524, 566), 22, PANEL, (219, 228, 239), 2)
    d.text((104, 188), "主链路", font=F_SECTION, fill=(51, 65, 85))
    d.text((194, 188), "用户需求进入飞书，DeepSeek 编排，本地工具执行，结果回到群里", font=F_SMALL, fill=MUTED)

    rounded(d, (820, 74, 1490, 158), 16, (248, 250, 252), BORDER, 2)
    d.text((848, 103), "本地知识与数据", font=F_SECTION, fill=(51, 65, 85))
    d.text((848, 136), "ARXML 数据库 · 故障表 Excel · ADAS 接口规则 · 历史分析代码 · 图表模板", font=F_SMALL, fill=MUTED)

    draw_path(d, [(1042, 220), (1042, 158)], GRAY, 3)
    d.text((1058, 190), "读取知识", font=F_SMALL, fill=GRAY)

    for idx, (number, points, color, label, label_xy) in enumerate(PATHS):
        active = idx == active_index
        draw_path(d, points, color if active else GRAY, 5 if active else 3)
        badge_xy = (label_xy[0] - 24, label_xy[1] + 8)
        draw_step_badge(d, badge_xy, number, color, active)
        d.text((label_xy[0] + 2, label_xy[1]), label, font=F_SMALL, fill=color if active else GRAY)

    for idx, (title, lines, xy, color) in enumerate(NODES):
        x1, y1, x2, y2 = xy
        active = (
            active_index in (0, 1, 5, 6) and idx in (0, 1)
            or active_index in (2, 3, 4) and idx == 2
            or active_index == 7 and idx in (2, 3)
        )
        rounded(d, xy, 16, PANEL, color if active else BORDER, 4 if active else 1)
        rounded(d, (x1, y1, x2, y1 + 54), 16, color, None, 1)
        d.text((x1 + 26, y1 + 15), title, font=F_BOX, fill=(255, 255, 255))
        for line_idx, line in enumerate(lines):
            d.text((x1 + 26, y1 + 88 + line_idx * 31), line, font=F_TEXT, fill=(51, 65, 85))

    d.text((76, 614), "本地能力工具箱", font=F_SECTION, fill=(51, 65, 85))
    for idx, (title, lines, xy, fill) in enumerate(CAPABILITIES):
        x1, y1, x2, y2 = xy
        active = active_index in (2, 3, 4) and idx == active_index - 2
        rounded(d, xy, 16, PANEL, GREEN if active else BORDER, 4 if active else 1)
        rounded(d, (x1, y1, x2, y1 + 46), 16, fill, None, 1)
        d.text((x1 + 30, y1 + 13), title, font=F_CAP, fill=TEXT)
        for line_idx, line in enumerate(lines):
            d.text((x1 + 30, y1 + 78 + line_idx * 30), line, font=F_TEXT, fill=(51, 65, 85))

    rounded(d, (365, 820, 1235, 858), 19, (239, 246, 255), (191, 219, 254), 1)
    d.text((414, 830), "输出：飞书消息 · Markdown 风格报告 · 可横向滚动表格 · 总线时序图 · 重点结论摘要", font=F_SMALL, fill=MUTED)

    if particle:
        x, y, color = particle
        d.ellipse((x - 12, y - 12, x + 12, y + 12), fill=color, outline=(255, 255, 255), width=4)

    return img


def main() -> None:
    frames: list[Image.Image] = []
    for path_idx, (_number, points, color, _label, _label_xy) in enumerate(PATHS):
        for step in range(12):
            x, y = point_on_path(points, step / 11)
            frames.append(draw_base(path_idx, (x, y, color)))
        for _ in range(2):
            frames.append(draw_base(path_idx))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(OUT, save_all=True, append_images=frames[1:], duration=90, loop=0, optimize=True)
    frames[0].save(PREVIEW)
    print(OUT)
    print(PREVIEW)


if __name__ == "__main__":
    main()

