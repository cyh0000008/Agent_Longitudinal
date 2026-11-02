from __future__ import annotations

import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
import imageio_ffmpeg


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
OUT = ROOT / "feishu_agent_base_to_result_v2.mp4"
STORYBOARD = ROOT / "storyboard_v2.png"

W, H = 1920, 1080
FPS = 15
FONT_BOLD = r"C:\Windows\Fonts\simhei.ttf"
FONT_REGULAR = r"C:\Windows\Fonts\msyh.ttc"
if not Path(FONT_REGULAR).exists():
    FONT_REGULAR = FONT_BOLD


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR, size)


def cover(img: Image.Image, zoom: float = 1.0, cx: float = 0.5, cy: float = 0.5) -> Image.Image:
    img = img.convert("RGB")
    scale = max(W / img.width, H / img.height) * zoom
    resized = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)
    left = int((resized.width - W) * cx)
    top = int((resized.height - H) * cy)
    left = max(0, min(left, resized.width - W))
    top = max(0, min(top, resized.height - H))
    return resized.crop((left, top, left + W, top + H))


def contain(img: Image.Image, box: tuple[int, int, int, int], fill=(245, 248, 252)) -> Image.Image:
    x0, y0, x1, y1 = box
    canvas = Image.new("RGB", (x1 - x0, y1 - y0), fill)
    src = img.convert("RGB")
    scale = min(canvas.width / src.width, canvas.height / src.height)
    src = src.resize((int(src.width * scale), int(src.height * scale)), Image.Resampling.LANCZOS)
    canvas.paste(src, ((canvas.width - src.width) // 2, (canvas.height - src.height) // 2))
    return canvas


def wrap(text: str, fnt: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines, current = [], ""
    for ch in text:
        trial = current + ch
        if fnt.getlength(trial) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines


def draw_title(draw: ImageDraw.ImageDraw, kicker: str, title: str, subtitle: str = "", dark: bool = True):
    fg = "white" if dark else "#0d1728"
    muted = "#c9d7ea" if dark else "#52637b"
    draw.rounded_rectangle((92, 70, 350, 120), radius=25, fill="#16b8d4")
    draw.text((221, 95), kicker, font=font(25, True), anchor="mm", fill="white")
    draw.text((92, 150), title, font=font(62, True), fill=fg)
    if subtitle:
        draw.text((95, 232), subtitle, font=font(31), fill=muted)


def caption_bar(frame: Image.Image, headline: str, detail: str, accent="#20d39b"):
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    d.rounded_rectangle((70, 872, 1850, 1038), radius=28, fill=(7, 17, 33, 228), outline=(255, 255, 255, 35), width=2)
    d.rounded_rectangle((70, 872, 82, 1038), radius=6, fill=accent)
    d.text((118, 905), headline, font=font(38, True), fill="white")
    y = 960
    for line in wrap(detail, font(26), 1650)[:2]:
        d.text((120, y), line, font=font(26), fill="#c9d7ea")
        y += 34
    frame.alpha_composite(overlay)


def privacy_mask(img: Image.Image, rect: tuple[int, int, int, int]) -> None:
    region = img.crop(rect).filter(ImageFilter.GaussianBlur(14))
    img.paste(region, rect)


def scene_intro(p: float) -> Image.Image:
    bg = cover(IMAGES["hero"], 1.03 + 0.035 * p, 0.53, 0.48).convert("RGBA")
    shade = Image.new("RGBA", bg.size, (2, 10, 28, 100))
    bg.alpha_composite(shade)
    d = ImageDraw.Draw(bg)
    d.rounded_rectangle((88, 80, 430, 134), radius=27, fill="#15b8d4")
    d.text((259, 107), "BLU AGENT · ADAS", font=font(26, True), anchor="mm", fill="white")
    d.text((92, 180), "从飞书多维表格", font=font(70, True), fill="white")
    d.text((92, 275), "到问题分析结果", font=font(70, True), fill="white")
    d.text((98, 380), "真实界面 · 自动取数 · 证据分析 · 结果回写", font=font(33), fill="#d8e8f8")
    for i, (txt, color) in enumerate((("飞书入口", "#2f6fec"), ("Agent 调度", "#7c3aed"), ("数据分析", "#06b6d4"), ("闭环回写", "#10b981"))):
        x = 98 + i * 245
        d.rounded_rectangle((x, 470, x + 205, 530), radius=28, fill=color)
        d.text((x + 102, 500), txt, font=font(26, True), anchor="mm", fill="white")
    return bg


def scene_base(p: float) -> Image.Image:
    frame = cover(IMAGES["base"], 1.0 + 0.05 * p, 0.53, 0.48).convert("RGBA")
    privacy_mask(frame, (930, 165, 1145, 870))
    d = ImageDraw.Draw(frame, "RGBA")
    d.rounded_rectangle((55, 55, 500, 145), radius=22, fill=(7, 17, 33, 225))
    d.text((90, 78), "01  问题进入 Base", font=font(38, True), fill="white")
    caption_bar(frame, "路试问题结构化沉淀", "问题描述、车型、时间、问题类型和处理状态统一进入多维表格；车辆标识已在成片中脱敏。", "#f59e0b")
    return frame


def scene_record_details(p: float) -> Image.Image:
    bg = Image.new("RGBA", (W, H), "#f5f8fc")
    d = ImageDraw.Draw(bg, "RGBA")
    draw_title(d, "01  FILL THE RECORD", "先把问题描述完整", "一行记录就是一条可执行、可追踪的分析任务", dark=False)
    fields = [
        ("Agent_问题描述", "红绿灯路口，前方无车，车辆突然误制动", "#f59e0b"),
        ("Agent_车型 / 车号", "C1UL-2 / 车辆标识已脱敏", "#06b6d4"),
        ("Agent_问题日期 / 时间", "2026/08/26  19:30:00", "#2f6fec"),
        ("Agent_问题类型", "误制动", "#ef4444"),
        ("Agent_问题状态", "待分析", "#7c3aed"),
    ]
    for i, (label, value, color) in enumerate(fields):
        y = 330 + i * 105
        d.rounded_rectangle((110, y, 1810, y + 82), radius=22, fill="white", outline="#d8e2ef", width=2)
        d.rounded_rectangle((110, y, 450, y + 82), radius=22, fill=color)
        d.text((280, y + 41), label, font=font(27, True), anchor="mm", fill="white")
        d.text((500, y + 41), value, font=font(29, i in (0, 3, 4)), anchor="lm", fill="#111b2d")
    caption_bar(bg, "描述越具体，分析窗口越准确", "问题现象、车型、发生时间和问题类型决定数据定位与后续信号查询范围。", "#f59e0b")
    return bg


def scene_trigger(p: float) -> Image.Image:
    frame = cover(IMAGES["chat"], 1.0 + 0.07 * p, 0.58, 0.43).convert("RGBA")
    d = ImageDraw.Draw(frame, "RGBA")
    d.rounded_rectangle((70, 55, 570, 145), radius=22, fill=(7, 17, 33, 225))
    d.text((105, 78), "02  在飞书触发 Agent", font=font(38, True), fill="white")
    caption_bar(frame, "一条固定指令启动批量分析", "@布鲁Mate 分析飞书多维表格中的问题。机器人只处理状态为“待分析”的记录。", "#2f6fec")
    return frame


def scene_pipeline(p: float) -> Image.Image:
    bg = Image.new("RGBA", (W, H), "#071426")
    d = ImageDraw.Draw(bg, "RGBA")
    draw_title(d, "03  AGENT PIPELINE", "自动取数与任务编排", "DeepSeek 负责理解和编排，本地能力负责可信执行")
    nodes = [
        ("读取 Base", "筛选待分析记录", "#2f6fec"),
        ("定位数据", "H5 路径校验 / BLF 兜底", "#06b6d4"),
        ("Agent 调度", "按问题类型选择分析能力", "#7c3aed"),
        ("写回结果", "状态、概要、结论与图示", "#10b981"),
    ]
    xs = [115, 565, 1015, 1465]
    progress = min(1.0, p * 1.25)
    for i, ((title, sub, color), x) in enumerate(zip(nodes, xs)):
        active = progress >= i / 4
        alpha = 255 if active else 90
        d.rounded_rectangle((x, 380, x + 340, 620), radius=30, fill=(15, 31, 54, 240), outline=color if active else "#39465a", width=4)
        d.ellipse((x + 24, 410, x + 88, 474), fill=color if active else "#39465a")
        d.text((x + 56, 442), str(i + 1), font=font(29, True), anchor="mm", fill="white")
        d.text((x + 28, 505), title, font=font(36, True), fill=(255, 255, 255, alpha))
        for j, line in enumerate(wrap(sub, font(24), 285)[:2]):
            d.text((x + 30, 558 + j * 32), line, font=font(24), fill=(190, 207, 229, alpha))
        if i < 3:
            x0, x1, y = x + 345, xs[i + 1] - 18, 500
            d.line((x0, y, x1, y), fill=(32, 211, 155, 190), width=8)
            d.polygon(((x1, y), (x1 - 22, y - 14), (x1 - 22, y + 14)), fill=(32, 211, 155, 220))
    caption_bar(bg, "状态机保持可追踪", "待分析 → 分析中 → 已分析；如果数据未找到，则保留待分析，等待数据准备完成。", "#20d39b")
    return bg


def scene_data_prepare(p: float) -> Image.Image:
    bg = Image.new("RGBA", (W, H), "#071426")
    d = ImageDraw.Draw(bg, "RGBA")
    draw_title(d, "03  DATA PREPARATION", "Agent 先确认数据，再消耗模型", "自动路径校验、时间覆盖检查，以及必要时的 BLF 解析")
    steps = [
        ("读取问题时间", "2026/08/26 19:30:00", "#2f6fec"),
        ("校验 H5 覆盖", "timestamp 是否覆盖目标时刻", "#06b6d4"),
        ("缺失时自动查找", "车型 + 车号 + 日期 + 相邻分段", "#7c3aed"),
        ("仍缺失则定位 BLF", "解析为标准 *_parsed.h5", "#f59e0b"),
        ("状态更新", "待分析 → 分析中", "#10b981"),
    ]
    for i, (title, sub, color) in enumerate(steps):
        x = 105 + i * 360
        active = p >= i / 5 - 0.08
        d.ellipse((x, 410, x + 86, 496), fill=color if active else "#334155")
        d.text((x + 43, 453), str(i + 1), font=font(30, True), anchor="mm", fill="white")
        if i < 4:
            d.line((x + 90, 453, x + 345, 453), fill=(52, 211, 153, 200 if active else 70), width=7)
        d.text((x, 540), title, font=font(31, True), fill="white" if active else "#718096")
        ty = 594
        for line in wrap(sub, font(23), 300)[:2]:
            d.text((x, ty), line, font=font(23), fill="#b9cbe2" if active else "#536175")
            ty += 32
    caption_bar(bg, "找不到数据时不会制造假结论", "H5 不覆盖目标时间且无法重新定位时，记录保持“待分析”，提示先完成数据解析。", "#06b6d4")
    return bg


def scene_evidence(p: float) -> Image.Image:
    bg = Image.new("RGBA", (W, H), "#f5f8fc")
    d = ImageDraw.Draw(bg, "RGBA")
    draw_title(d, "04  ROOT CAUSE", "四层证据链，而不是一句猜测", "同一时间窗口里回答：发生了什么、谁先变化、谁在响应", dark=False)
    cards = [
        ("现象层", "车辆是否真的出现重制动、顿挫、溜车或退出", "#f59e0b"),
        ("算法请求", "ACC / AEB 是否主动发出制动或驱动请求", "#2f6fec"),
        ("场景诱因", "目标、Cut-in、SetSpeed、曲率等是否解释请求", "#7c3aed"),
        ("执行器响应", "制动 / 驱动是否接受请求并完成闭环", "#10b981"),
    ]
    for i, (title, body, color) in enumerate(cards):
        x = 100 + i * 455
        y = 380 + int(22 * math.sin(p * math.pi + i * 0.4))
        d.rounded_rectangle((x, y, x + 385, y + 300), radius=30, fill="white", outline="#d8e2ef", width=3)
        d.rounded_rectangle((x, y, x + 385, y + 18), radius=9, fill=color)
        d.ellipse((x + 28, y + 38, x + 92, y + 102), fill=color)
        d.text((x + 60, y + 70), str(i + 1), font=font(29, True), anchor="mm", fill="white")
        d.text((x + 30, y + 130), title, font=font(37, True), fill="#111b2d")
        ty = y + 190
        for line in wrap(body, font(25), 325):
            d.text((x + 30, ty), line, font=font(25), fill="#52637b")
            ty += 34
    caption_bar(bg, "最终结论必须能追溯到信号", "更像感知/场景输入、算法请求还是执行器响应；证据不够时明确写“证据不足”。", "#7c3aed")
    return bg


def case_scene(kicker: str, title: str, conclusion: str, chain: list[tuple[str, str, str]], note: str, accent: str) -> Image.Image:
    bg = Image.new("RGBA", (W, H), "#f5f8fc")
    d = ImageDraw.Draw(bg, "RGBA")
    draw_title(d, kicker, title, conclusion, dark=False)
    for i, (label, value, color) in enumerate(chain):
        x = 85 + i * 455
        d.rounded_rectangle((x, 350, x + 400, 690), radius=28, fill="white", outline="#d8e2ef", width=3)
        d.rounded_rectangle((x, 350, x + 400, 370), radius=10, fill=color)
        d.ellipse((x + 28, 400, x + 92, 464), fill=color)
        d.text((x + 60, 432), str(i + 1), font=font(28, True), anchor="mm", fill="white")
        d.text((x + 30, 505), label, font=font(33, True), fill="#111b2d")
        ty = 565
        for line in wrap(value, font(25), 335)[:3]:
            d.text((x + 30, ty), line, font=font(25), fill="#52637b")
            ty += 35
        if i < len(chain) - 1:
            d.line((x + 405, 520, x + 445, 520), fill="#7b8ba3", width=6)
            d.polygon(((x + 445, 520), (x + 427, 508), (x + 427, 532)), fill="#7b8ba3")
    caption_bar(bg, "原因样例", note, accent)
    return bg


def scene_case_false_braking(p: float) -> Image.Image:
    return case_scene(
        "05  CASE · 误制动", "样例一：Cut-in 目标误触发最大制动", "结论更偏感知/场景输入，不是执行器自行制动",
        [
            ("现象", "无明显前车场景出现误制动", "#f59e0b"),
            ("诱因先变化", "Cut-in 目标在问题窗口异常出现", "#7c3aed"),
            ("算法请求", "随后发出较大的制动请求", "#2f6fec"),
            ("执行器响应", "实际制动跟随算法请求建立", "#10b981"),
        ],
        "真实回写摘要：根因是 CutIn 目标误触发最大制动；执行器表现为跟随请求。", "#ef4444",
    )


def scene_case_lcc_takeover(p: float) -> Image.Image:
    return case_scene(
        "06  CASE · 功能退出", "样例二：LCC 因驾驶员接管退出", "退出点成立，原因更偏驾驶员手力或方向盘转角抑制",
        [
            ("状态变化", "LCC 从激活态退到非激活态", "#f59e0b"),
            ("接管证据", "驾驶员手力或方向盘转角同步升高", "#7c3aed"),
            ("抑制条件", "接管相关抑制条件在退出点置位", "#2f6fec"),
            ("原因结论", "符合驾驶员主动接管逻辑", "#10b981"),
        ],
        "真实结果样例：LCC 退出主要由驾驶员手力接管或方向盘转角抑制触发。", "#7c3aed",
    )


def scene_case_boundaries(p: float) -> Image.Image:
    bg = Image.new("RGBA", (W, H), "#f5f8fc")
    d = ImageDraw.Draw(bg, "RGBA")
    draw_title(d, "07  MORE CASES", "既给明确原因，也保留证据边界", "不同记录会得到不同层级的结论", dark=False)
    cases = [
        ("车道过窄触发 LCC 退出", "车道几何条件先异常，退出与相应抑制条件同步。", "明确原因", "#10b981"),
        ("退出原因码不清晰", "看到了 LCC 退出，但退出码或抑制信号不足。", "暂不能定因", "#f59e0b"),
        ("算法主动制动请求", "算法请求先建立，执行器随后响应。", "偏算法请求侧", "#2f6fec"),
    ]
    for i, (title, body, tag, color) in enumerate(cases):
        x = 110 + i * 580
        d.rounded_rectangle((x, 360, x + 520, 710), radius=30, fill="white", outline="#d8e2ef", width=3)
        d.rounded_rectangle((x + 28, 395, x + 220, 448), radius=25, fill=color)
        d.text((x + 124, 421), tag, font=font(24, True), anchor="mm", fill="white")
        d.text((x + 28, 495), title, font=font(32, True), fill="#111b2d")
        ty = 565
        for line in wrap(body, font(25), 455):
            d.text((x + 30, ty), line, font=font(25), fill="#52637b")
            ty += 36
    caption_bar(bg, "不确定性也必须可见", "有退出点不等于有根因；关键原因信号缺失时，系统明确回写“证据不足”，避免无关信号凑结论。", "#f59e0b")
    return bg


def scene_signal(p: float) -> Image.Image:
    bg = Image.new("RGBA", (W, H), "#eef3f9")
    panel = contain(IMAGES["signal"], (80, 110, 1840, 900), fill=(238, 243, 249))
    bg.paste(panel, (80, 110))
    d = ImageDraw.Draw(bg, "RGBA")
    d.rounded_rectangle((70, 48, 700, 128), radius=22, fill=(7, 17, 33, 230))
    d.text((105, 68), "05  真实信号窗口与请求响应", font=font(36, True), fill="white")
    x = 1180 + int(260 * p)
    d.line((x, 205, x, 815), fill=(239, 68, 68, 220), width=5)
    d.rounded_rectangle((x - 135, 145, x + 135, 200), radius=18, fill=(239, 68, 68, 235))
    d.text((x, 172), "关键变化点", font=font(25, True), anchor="mm", fill="white")
    caption_bar(bg, "把结论放回同一时间窗验证", "比较加速度、控制请求、轴端扭矩、制动响应和状态信号，识别先后关系与闭环异常。", "#ef4444")
    return bg


def scene_result(p: float) -> Image.Image:
    frame = cover(IMAGES["chat"], 1.12 + 0.05 * p, 0.63, 0.5).convert("RGBA")
    d = ImageDraw.Draw(frame, "RGBA")
    d.rounded_rectangle((70, 55, 560, 145), radius=22, fill=(7, 17, 33, 225))
    d.text((105, 78), "06  群内返回分析摘要", font=font(38, True), fill="white")
    caption_bar(frame, "一张卡片看清执行结果", "读取数量、待分析数量、成功/失败数量、数据文件和结论摘要集中展示；详细结果继续回写 Base。", "#10b981")
    return frame


def scene_base_writeback(p: float) -> Image.Image:
    frame = cover(IMAGES["base"], 1.03 + 0.06 * p, 0.68, 0.48).convert("RGBA")
    privacy_mask(frame, (720, 165, 930, 870))
    d = ImageDraw.Draw(frame, "RGBA")
    d.rounded_rectangle((65, 55, 640, 145), radius=22, fill=(7, 17, 33, 230))
    d.text((100, 78), "09  结论、状态和图示回写 Base", font=font(36, True), fill="white")
    caption_bar(frame, "分析闭环落回原始问题记录", "Agent_问题状态更新为“已分析”，Agent_结论概要便于快速浏览，完整证据和信号图保留用于追溯。", "#10b981")
    return frame


def scene_close(p: float) -> Image.Image:
    bg = cover(IMAGES["hero"], 1.08, 0.52, 0.5).filter(ImageFilter.GaussianBlur(1.5)).convert("RGBA")
    bg.alpha_composite(Image.new("RGBA", bg.size, (3, 12, 31, 145)))
    d = ImageDraw.Draw(bg, "RGBA")
    d.text((W // 2, 190), "从问题到结论，形成可追溯闭环", font=font(58, True), anchor="mm", fill="white")
    stages = [("提问题", "#f59e0b"), ("触发 Agent", "#2f6fec"), ("读取数据", "#06b6d4"), ("证据分析", "#7c3aed"), ("回写结果", "#10b981")]
    for i, (txt, color) in enumerate(stages):
        x = 120 + i * 350
        d.rounded_rectangle((x, 420, x + 285, 570), radius=34, fill=(10, 25, 46, 230), outline=color, width=4)
        d.text((x + 142, 495), txt, font=font(35, True), anchor="mm", fill="white")
        if i < len(stages) - 1:
            d.line((x + 290, 495, x + 340, 495), fill="#8de7d0", width=7)
            d.polygon(((x + 340, 495), (x + 322, 483), (x + 322, 507)), fill="#8de7d0")
    d.text((W // 2, 690), "飞书负责协作入口 · Agent 负责智能编排 · 本地工具负责可信计算", font=font(32), anchor="mm", fill="#d8e8f8")
    d.rounded_rectangle((650, 790, 1270, 875), radius=42, fill="#13b981")
    d.text((960, 832), "布鲁Mate · ADAS 行车分析助手", font=font(33, True), anchor="mm", fill="white")
    return bg


SCENES = [
    (3.5, scene_intro),
    (5.5, scene_base),
    (6.5, scene_record_details),
    (5.0, scene_trigger),
    (6.5, scene_data_prepare),
    (6.5, scene_evidence),
    (9.0, scene_case_false_braking),
    (8.0, scene_case_lcc_takeover),
    (8.0, scene_case_boundaries),
    (7.0, scene_signal),
    (5.5, scene_result),
    (5.5, scene_base_writeback),
    (3.5, scene_close),
]


IMAGES = {
    "hero": Image.open(ASSETS / "hero_ai_workflow.png"),
    "base": Image.open(ASSETS / "feishu_base_anonymized.png"),
    "chat": Image.open(ASSETS / "feishu_chat_anonymized.png"),
    "signal": Image.open(ASSETS / "real_signal_analysis.png"),
}


def render_frame(t: float) -> Image.Image:
    elapsed = 0.0
    for duration, renderer in SCENES:
        if t < elapsed + duration:
            p = max(0.0, min(1.0, (t - elapsed) / duration))
            frame = renderer(p).convert("RGB")
            local = t - elapsed
            fade = min(1.0, local / 0.45, (duration - local) / 0.45)
            if fade < 1:
                frame = ImageEnhance.Brightness(frame).enhance(0.45 + 0.55 * fade)
            return frame
        elapsed += duration
    return SCENES[-1][1](1.0).convert("RGB")


def build_storyboard() -> None:
    thumbs = []
    for _, renderer in SCENES:
        im = renderer(0.5).convert("RGB").resize((480, 270), Image.Resampling.LANCZOS)
        thumbs.append(im)
    cols = 3
    rows = math.ceil(len(thumbs) / cols)
    board = Image.new("RGB", (1440, rows * 270), "white")
    for i, im in enumerate(thumbs):
        board.paste(im, ((i % cols) * 480, (i // cols) * 270))
    board.save(STORYBOARD, quality=94)


def build_video() -> None:
    total = sum(duration for duration, _ in SCENES)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg, "-y", "-f", "rawvideo", "-vcodec", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-an",
        "-vcodec", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(OUT),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for i in range(int(total * FPS)):
            proc.stdin.write(render_frame(i / FPS).tobytes())
    finally:
        proc.stdin.close()
    code = proc.wait()
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    build_storyboard()
    build_video()
    print(OUT)
