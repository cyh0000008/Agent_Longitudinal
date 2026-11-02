from __future__ import annotations

import csv
import importlib.util
import json
import math
import re
import shutil
import sys
import tempfile
from copy import copy
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor, XDRPositiveSize2D
from openpyxl.styles import Alignment
from PIL import Image, ImageDraw, ImageFont

from DataMining import build_folder_context, extract_csv_prefix, load_config
from runtime_paths import ensure_writable_dir, get_runtime_subdir

EVENT_PATTERN = re.compile(
    r"\[(?:Event|Scene)\s+(?P<body>[^\]]*)\]"
)
START_PATTERN = re.compile(r"(?:^|[,\s])start=(?P<value>[-+]?\d+(?:\.\d+)?)")
END_PATTERN = re.compile(r"(?:^|[,\s])end=(?P<value>[-+]?\d+(?:\.\d+)?)")
DURATION_PATTERN = re.compile(r"(?:^|[,\s])duration=(?P<value>[-+]?\d+(?:\.\d+)?)")
NUMBER_PATTERN = re.compile(r"([a-zA-Z_]+)=([-+]?\d+(?:\.\d+)?)")

BACKGROUND = (255, 255, 255)
GRID = (232, 232, 232)
AXIS = (120, 120, 120)
TEXT = (40, 40, 40)
SUBTEXT = (90, 90, 90)
EVENT_FILL = (244, 196, 203)
EVENT_LINE = (196, 57, 57)
SERIES_COLORS = [
    (31, 119, 180),
    (255, 127, 14),
    (44, 160, 44),
    (214, 39, 40),
    (148, 103, 189),
    (140, 86, 75),
    (227, 119, 194),
    (127, 127, 127),
    (188, 189, 34),
    (23, 190, 207),
    (66, 133, 244),
    (251, 188, 5),
]
SCREENSHOT_COLUMN = 5
SCREENSHOT_COLUMN_LETTER = 'E'
EMU_PER_PIXEL = 9525
DEFAULT_ROW_HEIGHT_PT = 15
DEFAULT_COL_WIDTH = 8.43
SCREENSHOT_MARGIN_PX = 8


def load_tool_config(config_path: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding='utf-8'))
    required_path_keys = ['report_path', 'metrics_dir', 'copy_output_dir']
    for key in required_path_keys:
        if not str(config.get(key, '')).strip():
            raise ValueError(f'bad_case_tool_config.json 缺少必填绝对路径: {key}')

    config.setdefault('output_report_suffix', '_with_badcase_images')
    config.setdefault('selection', {})
    config['selection'].setdefault('default_min_severity_score', 0.0)
    config['selection'].setdefault('default_max_cases_per_metric', 0)
    config['selection'].setdefault('metric_overrides', {})
    config.setdefault('copy', {})
    config['copy'].setdefault('enabled', True)
    config['copy'].setdefault('done_folder_suffix', '_done')
    config['copy'].setdefault('video_folder_suffix', '_video')
    config['copy'].setdefault('done_file_names', [])
    return config


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def ensure_absolute_path(path_text: str, field_name: str, allow_empty: bool = False) -> Path | None:
    text = str(path_text).strip()
    if not text:
        if allow_empty:
            return None
        raise ValueError(f'{field_name} 不能为空')
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError(f'{field_name} 必须是绝对路径: {path_text}')
    return path


def resolve_report_path(tool_config: dict[str, Any], argv: list[str]) -> Path:
    print('[STEP] 解析报告路径')
    candidate_text = ''
    if len(argv) >= 2 and argv[1].strip():
        candidate_text = argv[1].strip()
    else:
        candidate_text = str(tool_config.get('report_path', '')).strip()
    candidate = ensure_absolute_path(candidate_text, 'report_path')
    if candidate is None or not candidate.exists():
        raise FileNotFoundError(f'指定报告不存在: {candidate_text}')
    return candidate


def load_metric_csvs_for_report(results_dir: Path, report_path: Path) -> dict[str, Path]:
    match = re.search(r'(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})', report_path.stem)
    csvs: dict[str, Path] = {}
    if match:
        timestamp = match.group(1)
        for path in sorted(results_dir.glob(f'*_{timestamp}.csv')):
            csvs[extract_csv_prefix(path.name)] = path
    if csvs:
        return csvs
    for path in sorted(results_dir.glob('*.csv')):
        csvs[extract_csv_prefix(path.name)] = path
    return csvs


def load_metric_modules(metrics_dir: Path) -> dict[str, object]:
    modules: dict[str, object] = {}
    for path in sorted(metrics_dir.glob('*.py')):
        if path.name.startswith('__'):
            continue
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules[path.stem] = module
    return modules


def parse_events(details: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for match in EVENT_PATTERN.finditer(details or ''):
        body = match.group('body')
        start_match = START_PATTERN.search(body)
        end_match = END_PATTERN.search(body)
        duration_match = DURATION_PATTERN.search(body)
        if not start_match or not end_match:
            continue
        event: dict[str, Any] = {
            'start': float(start_match.group('value')),
            'end': float(end_match.group('value')),
            'duration': float(duration_match.group('value')) if duration_match else max(0.0, float(end_match.group('value')) - float(start_match.group('value'))),
        }
        for key, value in NUMBER_PATTERN.findall(body):
            try:
                event[key] = float(value)
            except Exception:
                continue
        events.append(event)
    return events


def parse_bad_case_rows(csv_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with csv_path.open('r', encoding='utf-8-sig', newline='') as file:
        for row in csv.DictReader(file):
            bad_case = str(row.get('Bad Case', '0')).strip()
            if bad_case in {'', '0', '0.0'}:
                continue
            details = str(row.get('符合要求的内容', ''))
            rows.append(
                {
                    'folder_path': str(row.get('文件夹路径', '')).strip(),
                    'details': details,
                    'events': parse_events(details),
                    'severity_score': safe_float(row.get('严重度得分', '0'), 0.0),
                }
            )
    return rows


def filter_bad_rows(metric_name: str, rows: list[dict[str, Any]], tool_config: dict[str, Any]) -> list[dict[str, Any]]:
    selection = tool_config.get('selection', {})
    overrides = selection.get('metric_overrides', {}) or {}
    metric_rule = overrides.get(metric_name, {}) or {}
    min_score = safe_float(metric_rule.get('min_severity_score', selection.get('default_min_severity_score', 0.0)), 0.0)
    max_cases = int(safe_float(metric_rule.get('max_cases_per_metric', selection.get('default_max_cases_per_metric', 0)), 0.0))
    filtered = [row for row in rows if float(row.get('severity_score', 0.0)) >= min_score]
    filtered.sort(key=lambda row: float(row.get('severity_score', 0.0)), reverse=True)
    if max_cases <= 0:
        return filtered
    return filtered[:max_cases]


def sanitize_signal_label(signal_name: str) -> str:
    return signal_name.split('.')[-1]


def extract_signal_series(session, signal_name: str, axis: list[float], start_ts: float, end_ts: float) -> list[float]:
    if signal_name == getattr(session, '_axis_signal_name', None):
        return [float(value) for value in session._axis_values]
    return [float(value) for value in session.sample_signal_to_axis(signal_name, axis, method='nearest', start_timestamp=start_ts, end_timestamp=end_ts)]


def nice_range(values: list[float]) -> tuple[float, float]:
    min_y = min(values)
    max_y = max(values)
    if math.isclose(min_y, max_y, rel_tol=1e-9, abs_tol=1e-9):
        pad = max(abs(min_y) * 0.1, 1.0)
        return min_y - pad, max_y + pad
    pad = max((max_y - min_y) * 0.1, 0.2)
    return min_y - pad, max_y + pad


def draw_dashed_line(draw: ImageDraw.ImageDraw, x: float, y1: float, y2: float, color: tuple[int, int, int], dash: int = 6, gap: int = 4) -> None:
    y = y1
    while y < y2:
        draw.line((x, y, x, min(y + dash, y2)), fill=color, width=1)
        y += dash + gap


def nearest_value(axis: list[float], values: list[float], target: float) -> float:
    best_idx = min(range(len(axis)), key=lambda idx: abs(axis[idx] - target))
    return values[best_idx]


def trim_text(value: Any, limit: int = 180) -> str:
    text = str(value or '').strip().replace('\n', ' ')
    if len(text) <= limit:
        return text
    return text[: limit - 3] + '...'


def build_plot_image(metric_name: str, folder_name: str, primary_h5: str, axis: list[float], signal_series: dict[str, list[float]], events: list[dict[str, Any]], severity_score: float, detail_text: str) -> Image.Image:
    font = ImageFont.load_default()
    title_font = ImageFont.load_default()
    left = 110
    right = 30
    top = 70
    row_height = 180
    bottom = 50
    width = 1700
    signal_names = list(signal_series.keys())
    total_height = top + bottom + row_height * len(signal_names)
    image = Image.new('RGB', (width, total_height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    plot_left = left
    plot_right = width - right
    plot_width = plot_right - plot_left
    time_min = axis[0]
    time_max = axis[-1] if axis[-1] > axis[0] else axis[0] + 1e-6

    def map_x(value: float) -> float:
        return plot_left + (value - time_min) / (time_max - time_min) * plot_width

    draw.text((left, 15), f'{metric_name} bad case: {folder_name}', fill=TEXT, font=title_font)
    draw.text((left, 32), f'primary_h5={primary_h5} | severity_score={severity_score:.3f}', fill=SUBTEXT, font=font)
    draw.text((left, 48), trim_text(detail_text), fill=SUBTEXT, font=font)

    for panel_index, signal_name in enumerate(signal_names):
        panel_top = top + panel_index * row_height
        plot_top = panel_top + 25
        plot_height = 110
        plot_bottom = plot_top + plot_height
        values = signal_series[signal_name]
        y_min, y_max = nice_range(values)

        def map_y(value: float) -> float:
            return plot_top + (y_max - value) / (y_max - y_min) * plot_height

        draw.text((plot_left, panel_top + 2), sanitize_signal_label(signal_name), fill=TEXT, font=font)
        for tick in range(6):
            y_value = y_min + (y_max - y_min) * tick / 5.0
            y = map_y(y_value)
            draw.line((plot_left, y, plot_right, y), fill=GRID, width=1)
            draw.text((15, y - 6), f'{y_value:.3f}', fill=SUBTEXT, font=font)
        for tick in range(7):
            t = time_min + (time_max - time_min) * tick / 6.0
            x = map_x(t)
            draw.line((x, plot_top, x, plot_bottom), fill=(245, 245, 245), width=1)
            draw.text((x - 15, plot_bottom + 5), f'{t:.1f}s', fill=SUBTEXT, font=font)
        draw.line((plot_left, plot_bottom, plot_right, plot_bottom), fill=AXIS, width=1)
        draw.line((plot_left, plot_top, plot_left, plot_bottom), fill=AXIS, width=1)

        for event_index, event in enumerate(events, start=1):
            start_value = float(event['start'])
            end_value = float(event['end'])
            if end_value < time_min or start_value > time_max:
                continue
            x1 = map_x(max(start_value, time_min))
            x2 = map_x(min(end_value, time_max))
            draw.rectangle((x1, plot_top, max(x2, x1 + 2), plot_bottom), fill=EVENT_FILL, outline=None)
            draw_dashed_line(draw, x1, plot_top, plot_bottom, EVENT_LINE)
            draw_dashed_line(draw, x2, plot_top, plot_bottom, EVENT_LINE)
            if panel_index == 0:
                extras = []
                for key in ('severity', 'amplitude', 'sign_flips', 'min_acceleration', 'min_filtered_jerk', 'event_count'):
                    if key in event:
                        value = event[key]
                        if abs(value - int(value)) < 1e-9:
                            extras.append(f'{key}={int(value)}')
                        else:
                            extras.append(f'{key}={value:.3f}')
                label = f"E{event_index}: {', '.join(extras[:3])}" if extras else f"E{event_index}: duration={event['duration']:.3f}"
                draw.text((plot_left + 10, plot_top + 5 + 14 * (event_index - 1)), label, fill=EVENT_LINE, font=font)

        color = SERIES_COLORS[panel_index % len(SERIES_COLORS)]
        points = [(map_x(x_value), map_y(y_value)) for x_value, y_value in zip(axis, values)]
        if len(points) >= 2:
            draw.line(points, fill=color, width=2)
        elif points:
            x, y = points[0]
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=color)

        for event in events:
            for key in ('start', 'end'):
                event_time = float(event[key])
                if event_time < time_min or event_time > time_max:
                    continue
                event_value = nearest_value(axis, values, event_time)
                cx = map_x(event_time)
                cy = map_y(event_value)
                draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), outline=EVENT_LINE, width=2)

    return image


def ensure_output_report(report_path: Path, suffix: str) -> Path:
    output_path = report_path.with_name(f'{report_path.stem}{suffix}.xlsx')
    if output_path.exists():
        output_path.unlink()
    shutil.copy2(report_path, output_path)
    return output_path


def col_width_to_pixels(width: float | None) -> int:
    width = DEFAULT_COL_WIDTH if width is None else width
    return int(width * 7 + 5)


def row_height_to_pixels(height: float | None) -> int:
    height = DEFAULT_ROW_HEIGHT_PT if height is None else height
    return int(height * 96 / 72)


def pixels_to_row_height(pixels: float) -> float:
    return float(pixels) * 72 / 96


def image_anchor_marker(col: int, row: int, col_off_px: float = 0, row_off_px: float = 0) -> AnchorMarker:
    return AnchorMarker(
        col=col,
        row=row,
        colOff=int(col_off_px * EMU_PER_PIXEL),
        rowOff=int(row_off_px * EMU_PER_PIXEL),
    )


def copy_alignment_with(alignment: Alignment, *, wrap_text: bool | None = None, vertical: str | None = None) -> Alignment:
    return Alignment(
        horizontal=alignment.horizontal,
        vertical=vertical if vertical is not None else alignment.vertical,
        text_rotation=alignment.text_rotation,
        wrap_text=wrap_text if wrap_text is not None else alignment.wrap_text,
        shrink_to_fit=alignment.shrink_to_fit,
        indent=alignment.indent,
        relativeIndent=alignment.relativeIndent,
        justifyLastLine=alignment.justifyLastLine,
        readingOrder=alignment.readingOrder,
    )


def ensure_detail_report_headers(ws) -> None:
    if ws.cell(1, 3).value != '详情':
        return
    source = ws.cell(1, 3)
    for col, title in ((4, '问题分析'), (SCREENSHOT_COLUMN, '分析截图')):
        cell = ws.cell(1, col)
        if not cell.value:
            cell.value = title
        if source.has_style:
            cell._style = copy(source._style)
        cell.font = copy(source.font)
        cell.fill = copy(source.fill)
        cell.border = copy(source.border)
        cell.alignment = copy_alignment_with(source.alignment, wrap_text=True, vertical='center')


def apply_report_cell_formatting(workbook) -> None:
    wrap_headers = {'问题内容', '问题分析', '详情'}
    for ws in workbook.worksheets:
        ensure_detail_report_headers(ws)
        wrap_columns = {
            col_index
            for col_index in range(1, ws.max_column + 1)
            if ws.cell(1, col_index).value in wrap_headers
        }
        for row in ws.iter_rows(min_row=1, max_row=ws.max_row, min_col=1, max_col=ws.max_column):
            for cell in row:
                cell.alignment = copy_alignment_with(
                    cell.alignment,
                    wrap_text=True if cell.column in wrap_columns else cell.alignment.wrap_text,
                    vertical='center',
                )


def fit_image_to_cell(ws, excel_image: XLImage, row_index: int, col_index: int = SCREENSHOT_COLUMN, margin_px: int = 4) -> None:
    column_letter = SCREENSHOT_COLUMN_LETTER if col_index == SCREENSHOT_COLUMN else ws.cell(1, col_index).column_letter
    cell_width_px = col_width_to_pixels(ws.column_dimensions[column_letter].width)
    cell_height_px = row_height_to_pixels(ws.row_dimensions[row_index].height)
    fit_width = max(1, cell_width_px - 2 * margin_px)
    fit_height = max(1, cell_height_px - 2 * margin_px)
    scale = min(fit_width / float(excel_image.width), fit_height / float(excel_image.height), 1.0)
    image_width = max(1, int(excel_image.width * scale))
    image_height = max(1, int(excel_image.height * scale))
    xoff = max(0, (cell_width_px - image_width) / 2)
    yoff = max(0, (cell_height_px - image_height) / 2)
    zero_based_col = col_index - 1
    zero_based_row = row_index - 1
    excel_image.width = image_width
    excel_image.height = image_height
    excel_image.anchor = OneCellAnchor(
        _from=image_anchor_marker(zero_based_col, zero_based_row, xoff, yoff),
        ext=XDRPositiveSize2D(
            cx=int(image_width * EMU_PER_PIXEL),
            cy=int(image_height * EMU_PER_PIXEL),
        ),
    )


def prepare_screenshot_image_layout(
    ws,
    row_index: int,
    image_width_px: int,
    image_height_px: int,
    col_index: int = SCREENSHOT_COLUMN,
    margin_px: int = SCREENSHOT_MARGIN_PX,
) -> tuple[int, int]:
    column_letter = SCREENSHOT_COLUMN_LETTER if col_index == SCREENSHOT_COLUMN else ws.cell(1, col_index).column_letter
    cell_width_px = col_width_to_pixels(ws.column_dimensions[column_letter].width)
    fit_width = max(1, cell_width_px - 2 * margin_px)
    scale = min(fit_width / float(image_width_px), 1.0)
    target_width = max(1, int(image_width_px * scale))
    target_height = max(1, int(image_height_px * scale))
    required_row_height_px = target_height + 2 * margin_px
    ws.row_dimensions[row_index].height = pixels_to_row_height(required_row_height_px)
    return target_width, target_height


def load_done_file_names(copy_config: dict[str, Any]) -> list[str]:
    names = copy_config.get('done_file_names', [])
    if not isinstance(names, list):
        return []
    result = []
    for item in names:
        text = str(item).strip()
        if text:
            result.append(text)
    return sorted(set(result))


def copy_matched_files_from_done(src_done_dir: Path, dst_root_dir: Path, allowed_filenames: list[str]) -> None:
    dst_done_dir = dst_root_dir / src_done_dir.name
    dst_done_dir.mkdir(parents=True, exist_ok=True)
    try:
        all_files = [f for f in src_done_dir.iterdir() if f.is_file()]
    except Exception:
        return
    for src_file in all_files:
        if allowed_filenames and src_file.name not in allowed_filenames:
            continue
        dst_file = dst_done_dir / src_file.name
        if dst_file.exists():
            continue
        try:
            os_link = getattr(__import__('os'), 'link')
            os_link(src_file, dst_file)
        except Exception:
            try:
                shutil.copy2(src_file, dst_file)
            except Exception:
                continue


def link_or_copy_file(src_file: Path, dst_file: Path) -> None:
    try:
        os_link = getattr(__import__('os'), 'link')
        os_link(src_file, dst_file)
    except Exception:
        try:
            shutil.copy2(src_file, dst_file)
        except Exception:
            return


def copy_mp4_files_from_video(src_video_dir: Path, dst_root_dir: Path) -> None:
    if not src_video_dir.is_dir():
        return
    dst_video_dir = dst_root_dir / src_video_dir.name
    dst_video_dir.mkdir(parents=True, exist_ok=True)
    try:
        mp4_files = [f for f in src_video_dir.iterdir() if f.is_file() and f.suffix.lower() == '.mp4']
    except Exception:
        return
    for src_file in mp4_files:
        dst_file = dst_video_dir / src_file.name
        if dst_file.exists():
            continue
        link_or_copy_file(src_file, dst_file)


def copy_bad_case_assets(selected_rows_by_metric: dict[str, list[dict[str, Any]]], tool_config: dict[str, Any]) -> None:
    copy_cfg = tool_config.get('copy', {})
    if not copy_cfg.get('enabled', True):
        print('[STEP] 拷贝已禁用，跳过 bad case 资源复制')
        return
    target_root = ensure_absolute_path(str(tool_config.get('copy_output_dir', '')).strip(), 'copy_output_dir')
    if target_root is None:
        raise ValueError('copy_output_dir 配置无效')
    target_root.mkdir(parents=True, exist_ok=True)
    allowed_filenames = load_done_file_names(copy_cfg)
    done_suffix = str(copy_cfg.get('done_folder_suffix', '_done'))
    video_suffix = str(copy_cfg.get('video_folder_suffix', '_video'))

    metric_items = list(selected_rows_by_metric.items())
    total_metrics = len(metric_items)
    total_rows = sum(len(rows) for _, rows in metric_items)
    print(f'[STEP] 开始拷贝 bad case 资源: 指标数={total_metrics}, bad case 数={total_rows}, 输出目录={target_root}')

    copied_rows = 0
    for metric_index, (metric_name, rows) in enumerate(metric_items, start=1):
        metric_target = target_root / metric_name
        metric_target.mkdir(parents=True, exist_ok=True)
        for row_index, row in enumerate(rows, start=1):
            copied_rows += 1
            done_path = Path(row['folder_path'])
            print(f'[INFO] 拷贝 {metric_index}/{total_metrics} | {row_index}/{len(rows)} | {metric_name} | {done_path}')
            if not (done_path.is_dir() and done_path.name.endswith(done_suffix)):
                continue
            copy_matched_files_from_done(done_path, metric_target, allowed_filenames)
            video_path = done_path.with_name(done_path.name[:-len(done_suffix)] + video_suffix)
            copy_mp4_files_from_video(video_path, metric_target)

    print('[STEP] bad case 资源拷贝完成')


def normalize_folder_key(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    text = text.replace('\\', '/')
    try:
        return str(Path(text))
    except Exception:
        return text


def find_sheet_for_metric(metric_name: str, workbook) -> str | None:
    if metric_name in workbook.sheetnames:
        return metric_name
    truncated = metric_name[:31]
    if truncated in workbook.sheetnames:
        return truncated
    candidates: list[tuple[int, str]] = []
    for sheet_name in workbook.sheetnames:
        if sheet_name == '所有指标统计汇总':
            continue
        if metric_name.startswith(sheet_name) or sheet_name.startswith(metric_name) or metric_name[: len(sheet_name)] == sheet_name:
            candidates.append((len(sheet_name), sheet_name))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def build_row_lookup(ws) -> tuple[dict[str, int], dict[str, int], dict[str, int | None]]:
    exact: dict[str, int] = {}
    normalized: dict[str, int] = {}
    by_name_multi: dict[str, list[int]] = {}
    for row_index in range(2, ws.max_row + 1):
        folder_value = ws.cell(row=row_index, column=1).value
        if not folder_value:
            continue
        text = str(folder_value).strip()
        if text and text not in exact:
            exact[text] = row_index
        normalized_text = normalize_folder_key(text)
        if normalized_text and normalized_text not in normalized:
            normalized[normalized_text] = row_index
        folder_name = Path(normalized_text or text).name
        if folder_name:
            by_name_multi.setdefault(folder_name, []).append(row_index)
    by_name: dict[str, int | None] = {}
    for name, rows in by_name_multi.items():
        by_name[name] = rows[0] if len(rows) == 1 else None
    return exact, normalized, by_name


def find_row_index(folder_path: str, row_lookup: tuple[dict[str, int], dict[str, int], dict[str, int | None]]) -> int | None:
    exact, normalized, by_name = row_lookup
    if folder_path in exact:
        return exact[folder_path]
    normalized_folder = normalize_folder_key(folder_path)
    if normalized_folder in normalized:
        return normalized[normalized_folder]
    folder_name = Path(normalized_folder or folder_path).name
    row_index = by_name.get(folder_name)
    if isinstance(row_index, int):
        return row_index
    return None


def generate_plots_and_embed(argv: list[str] | None = None) -> Path:
    argv = argv or sys.argv
    script_dir = Path(__file__).resolve().parent
    config_path = script_dir / 'bad_case_tool_config.json'
    print(f'[STEP] 读取工具配置: {config_path}')
    tool_config = load_tool_config(config_path)
    project_config = load_config(script_dir / 'config.json')
    metrics_dir = ensure_absolute_path(tool_config['metrics_dir'], 'metrics_dir')
    report_path = resolve_report_path(tool_config, argv)
    results_dir = report_path.parent
    copy_output_dir = ensure_absolute_path(tool_config['copy_output_dir'], 'copy_output_dir')
    if metrics_dir is None or copy_output_dir is None:
        raise RuntimeError('路径配置不完整')

    print(f'[STEP] 目标MPI报告: {report_path}')
    print(f'[STEP] 指标脚本目录: {metrics_dir}')
    print(f'[STEP] bad case 资源输出目录: {copy_output_dir}')
    metric_csvs = load_metric_csvs_for_report(results_dir, report_path)
    modules = load_metric_modules(metrics_dir)
    print(f'[STEP] 已加载指标脚本 {len(modules)} 个，匹配到指标CSV {len(metric_csvs)} 个')
    output_report = ensure_output_report(report_path, str(tool_config.get('output_report_suffix', '_with_badcase_images')))
    print(f'[STEP] 已复制输出报告: {output_report}')
    workbook = load_workbook(output_report)

    preprocess_signals = list(project_config.get('preprocess_signals', []))
    file_pattern = str(project_config.get('file_pattern', '*.h5'))
    selected_rows_by_metric: dict[str, list[dict[str, Any]]] = {}
    module_items = list(modules.items())
    total_metrics = len(module_items)
    print(f'[STEP] 开始处理指标，共 {total_metrics} 个')

    temp_root = ensure_writable_dir(get_runtime_subdir('bad_case_plot'))
    with tempfile.TemporaryDirectory(prefix='bad_case_plot_', dir=str(temp_root)) as temp_dir_text:
        temp_dir = Path(temp_dir_text)
        for metric_index, (metric_name, module) in enumerate(module_items, start=1):
            print(f'[STEP] 处理指标 {metric_index}/{total_metrics}: {metric_name}')
            csv_path = metric_csvs.get(metric_name)
            if csv_path is None:
                print('[INFO] 跳过，未找到对应CSV')
                continue
            sheet_name = find_sheet_for_metric(metric_name, workbook)
            if sheet_name is None:
                print('[INFO] 跳过，报告中缺少对应sheet')
                continue

            bad_rows = filter_bad_rows(metric_name, parse_bad_case_rows(csv_path), tool_config)
            selected_rows_by_metric[metric_name] = bad_rows
            print(f'[INFO] 入选 bad case 数: {len(bad_rows)} | sheet={sheet_name}')
            if not bad_rows:
                continue

            required_signals = list(getattr(module, 'REQUIRED_SIGNALS', []))
            if not required_signals:
                print('[INFO] 跳过，指标未声明 REQUIRED_SIGNALS')
                continue

            ws = workbook[sheet_name]
            ensure_detail_report_headers(ws)
            ws.column_dimensions[SCREENSHOT_COLUMN_LETTER].width = 85
            row_lookup = build_row_lookup(ws)
            total_cases = len(bad_rows)
            for case_index, bad_row in enumerate(bad_rows, start=1):
                folder_path = bad_row['folder_path']
                print(f'[INFO] 绘图 {metric_name} {case_index}/{total_cases}: {folder_path}')
                row_index = find_row_index(folder_path, row_lookup)
                if row_index is None:
                    print('[INFO] 跳过，sheet中未找到该文件夹行')
                    continue
                folder_context = build_folder_context(folder_path, file_pattern, preprocess_signals)
                session = folder_context.get('session')
                if session is None:
                    print('[INFO] 跳过，无法创建 FolderSession')
                    continue
                try:
                    start_ts, end_ts = session.common_time_range(required_signals)
                except Exception as exc:
                    print(f'[INFO] 跳过，公共时间区间获取失败: {exc}')
                    continue
                axis_signal = required_signals[0]
                axis, axis_values_raw = session.get_signal_slice(axis_signal, start_ts, end_ts)
                if len(axis) < 2:
                    print('[INFO] 跳过，时间轴点数不足')
                    continue
                session._axis_signal_name = axis_signal
                session._axis_values = axis_values_raw
                signal_series: dict[str, list[float]] = {}
                for signal_name in required_signals:
                    try:
                        signal_series[signal_name] = extract_signal_series(session, signal_name, axis, start_ts, end_ts)
                    except Exception as exc:
                        print(f'[INFO] 信号跳过: {signal_name} | {exc}')
                if not signal_series:
                    print('[INFO] 跳过，未取到任何可绘制信号')
                    continue
                image = build_plot_image(
                    metric_name,
                    Path(folder_path).name,
                    Path(session.primary_h5_file).name,
                    axis,
                    signal_series,
                    bad_row['events'],
                    float(bad_row['severity_score']),
                    bad_row['details'],
                )
                image_path = temp_dir / f'{metric_name}_{row_index}.png'
                image.save(image_path)
                excel_image = XLImage(str(image_path))
                target_width, target_height = prepare_screenshot_image_layout(
                    ws,
                    row_index,
                    image.width,
                    image.height,
                )
                excel_image.width = target_width
                excel_image.height = target_height
                fit_image_to_cell(ws, excel_image, row_index, margin_px=SCREENSHOT_MARGIN_PX)
                ws.add_image(excel_image)
                print(f'[INFO] 已写入报告: sheet={sheet_name} row={row_index}')

        apply_report_cell_formatting(workbook)
        print(f'[STEP] 保存带截图报告: {output_report}')
        workbook.save(output_report)

    copy_bad_case_assets(selected_rows_by_metric, tool_config)
    print(f'[STEP] 处理完成: {output_report}')
    return output_report


def main() -> None:
    output_report = generate_plots_and_embed(sys.argv)
    print(output_report)


if __name__ == '__main__':
    main()
