from __future__ import annotations

import csv
import importlib.util
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import h5py

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font

from signal_lib import FolderSession


CACHED_FUNCTIONS: dict[str, object] = {}
ODOMETER_DATASET_NAME = "Vehicle_Odometer"
ODOMETER_FIELD_NAME = "IVehOdo"
ODOMETER_VALID_FIELD_NAME = "IVehOdoV"
METRIC_CATEGORY_MPI = "MPI"
METRIC_CATEGORY_CPI = "CPI"
APA_CPI_TYPE_SIGNAL = "APA_Brake_Propulsion_Torqu_1F0_M.IAPABT_APAType"
APA_CPI_STATUS_SIGNAL = "Advanced_Park_Assist_Status_190_M.IAPASP_APAStsAuth"


def load_config(config_file_path: str | Path) -> dict[str, Any]:
    config_path = Path(config_file_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as file:
        config = json.load(file)

    required_keys = ["func_folder_path", "source_path", "max_depth", "output_dir", "report_prefix"]
    for key in required_keys:
        if key not in config:
            raise ValueError(f"Missing config key: {key}")

    config.setdefault("file_pattern", "*.h5")
    config.setdefault("query_date", "")
    config.setdefault("query_software", "")
    config.setdefault("suffix", "_done")
    config.setdefault("preprocess_signals", [])

    if config.get("query_date"):
        date_pattern = r'^\d{8}-\d{8}$'
        if not re.match(date_pattern, str(config["query_date"])):
            raise ValueError("query_date格式必须为YYYYMMDD-YYYYMMDD")
        start_date = datetime.strptime(str(config["query_date"]).split('-')[0], '%Y%m%d')
        end_date = datetime.strptime(str(config["query_date"]).split('-')[1], '%Y%m%d')
        if start_date > end_date:
            raise ValueError("query_date起始日期不能晚于终止日期")
    return config


def get_immediate_subfolders(folder_path: str | Path) -> list[str]:
    folder = Path(folder_path)
    try:
        return sorted(str(path) for path in folder.iterdir() if path.is_dir())
    except FileNotFoundError:
        return []


def collect_eligible_folders(
    base_path: str | Path,
    max_depth: int = 4,
    suffix: str = "_done",
    file_pattern: str = "*.h5",
    query_date: str = "",
) -> list[str]:
    base = Path(base_path).resolve()
    if not base.exists():
        print(f"警告：路径不存在 {base}")
        return []

    start_date = None
    end_date = None
    if query_date:
        try:
            date_parts = query_date.split('-')
            start_date = datetime.strptime(date_parts[0], '%Y%m%d')
            end_date = datetime.strptime(date_parts[1], '%Y%m%d')
        except (IndexError, ValueError):
            print(f"警告：日期格式解析失败 {query_date}，将跳过日期筛选")
            query_date = ""

    eligible: list[str] = []
    scanned_folder_count = 0
    base_depth = len(base.parts)
    print(f"开始搜索候选文件夹：{base}")
    for root, dirs, _files in os.walk(base):
        scanned_folder_count += 1
        print(f"[搜索进度] 已扫描 {scanned_folder_count} 个目录，当前目录：{root}，已命中 {len(eligible)} 个候选文件夹")
        current = Path(root)
        current_depth = len(current.parts) - base_depth
        if current_depth >= max_depth:
            dirs.clear()
            continue

        folder_name = current.name
        if suffix and not folder_name.endswith(suffix):
            continue
        if get_immediate_subfolders(current):
            continue

        if query_date and start_date and end_date:
            if len(folder_name) < 8:
                continue
            try:
                folder_date = datetime.strptime(folder_name[:8], '%Y%m%d')
            except ValueError:
                continue
            if not (start_date <= folder_date <= end_date):
                continue

        matched = sorted(str(path) for path in current.glob(file_pattern) if path.is_file())
        if matched:
            eligible.append(str(current))

    print(f"搜索结束，共扫描 {scanned_folder_count} 个目录，命中 {len(eligible)} 个候选文件夹")
    return sorted(set(eligible))


def load_single_function(file_path: str | Path) -> tuple[bool, object | None, str]:
    path = Path(file_path)
    if not path.exists():
        return False, None, f"未找到函数文件：{path}"

    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        return False, None, f"无法创建模块加载器：{path}"

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        return False, None, f"加载函数失败：{exc}"

    target = getattr(module, "__run_haoran_data_run__", None)
    if target is None:
        return False, None, "未找到__run_haoran_data_run__函数"
    return True, module, "函数加载成功"


def load_all_functions(func_folder_path: str | Path) -> dict[str, object]:
    global CACHED_FUNCTIONS
    func_dir = Path(func_folder_path).resolve()
    CACHED_FUNCTIONS = {}

    for path in sorted(func_dir.glob("*.py")):
        if path.name.startswith("__") or path.name.endswith("_scene.py"):
            continue
        success, module_obj, msg = load_single_function(path)
        if success and module_obj is not None:
            CACHED_FUNCTIONS[path.name] = module_obj
            print(f"✅ 加载成功：{path.name}")
        else:
            print(f"❌ 加载失败：{path.name} - {msg}")
    return CACHED_FUNCTIONS


def validate_required_signals(functions: dict[str, object], preprocess_signals: list[str]) -> None:
    configured = set(preprocess_signals)
    missing_by_file: dict[str, list[str]] = {}

    for func_name, module in functions.items():
        required = getattr(module, "REQUIRED_SIGNALS", [])
        missing = [signal for signal in required if signal not in configured]
        if missing:
            missing_by_file[func_name] = missing

    if missing_by_file:
        lines = ["以下指标依赖的信号未写入config.json的preprocess_signals："]
        for func_name, missing in missing_by_file.items():
            lines.append(f"- {func_name}: {', '.join(missing)}")
        raise ValueError("\n".join(lines))


def get_metric_category(module: object, metric_file_name: str) -> str:
    category = str(getattr(module, "METRIC_CATEGORY", "")).strip().upper()
    if category in {METRIC_CATEGORY_MPI, METRIC_CATEGORY_CPI}:
        return category
    if Path(metric_file_name).stem.startswith("apa_"):
        return METRIC_CATEGORY_CPI
    return METRIC_CATEGORY_MPI


def build_metric_category_map(functions: dict[str, object]) -> dict[str, str]:
    category_map: dict[str, str] = {}
    for file_name, module in functions.items():
        category_map[Path(file_name).stem] = get_metric_category(module, file_name)
    return category_map


def validate_function_return(result: Any) -> bool:
    if not isinstance(result, tuple):
        return False
    if len(result) not in (2, 3):
        return False
    if not isinstance(result[0], bool):
        return False
    if not isinstance(result[1], list):
        return False
    if len(result) == 3 and not isinstance(result[2], dict):
        return False
    return True


def _h5_contains_all_signals(h5_path: str | Path, signal_names: list[str]) -> bool:
    if not signal_names:
        return True
    path = Path(h5_path)
    try:
        with h5py.File(path, 'r') as h5_file:
            root_names = list(h5_file.keys())
            if not root_names:
                return False
            channel = h5_file[root_names[0]].get('Channel1')
            if channel is None:
                return False
            available: set[str] = set()
            for message_name in channel.keys():
                dataset = channel[message_name]
                for field_name in dataset.dtype.names or ():
                    if field_name == 'timestamp':
                        continue
                    available.add(f'{message_name}.{field_name}')
            return all(signal_name in available for signal_name in signal_names)
    except Exception:
        return False


def select_primary_h5_file(h5_files: list[str], preprocess_signals: list[str]) -> str | None:
    if not h5_files:
        return None

    preferred = None
    for h5_file in h5_files:
        if Path(h5_file).name == '_msd_can_data.h5':
            preferred = h5_file
            break

    if preferred and _h5_contains_all_signals(preferred, preprocess_signals):
        return preferred

    for h5_file in h5_files:
        if h5_file == preferred:
            continue
        if _h5_contains_all_signals(h5_file, preprocess_signals):
            return h5_file

    if preferred:
        return preferred
    return h5_files[0]


def build_folder_context(folder_path: str | Path, file_pattern: str, preprocess_signals: list[str]) -> dict[str, Any]:
    folder = Path(folder_path)
    h5_files = sorted(str(path) for path in folder.glob(file_pattern) if path.is_file())
    primary_h5_file = select_primary_h5_file(h5_files, preprocess_signals)
    folder_context: dict[str, Any] = {
        "folder_path": str(folder),
        "h5_files": h5_files,
        "primary_h5_file": primary_h5_file,
        "session": None,
    }
    if primary_h5_file:
        folder_context["session"] = FolderSession(primary_h5_file, preprocess_signals=preprocess_signals)
    folder_context["apa_total_count"] = calculate_apa_total_count(folder_context.get("session"))
    return folder_context


def calculate_apa_total_count(session: FolderSession | None) -> int:
    if session is None:
        return 0

    required_signals = [APA_CPI_TYPE_SIGNAL, APA_CPI_STATUS_SIGNAL]
    if session.missing_signals(required_signals):
        return 0

    try:
        start_timestamp, end_timestamp = session.common_time_range(required_signals)
        axis, parking_status_raw = session.get_signal_slice(APA_CPI_STATUS_SIGNAL, start_timestamp, end_timestamp)
        if len(axis) < 2:
            return 0

        apa_type_values = session.sample_signal_to_axis(
            APA_CPI_TYPE_SIGNAL,
            axis,
            method="nearest",
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )

        total_count = 0
        for index in range(1, len(axis)):
            prev_status = _normalize_numeric_value(parking_status_raw[index - 1])
            curr_status = _normalize_numeric_value(parking_status_raw[index])
            prev_type = _normalize_numeric_value(apa_type_values[index - 1])
            if prev_type == 1 and prev_status == 5 and curr_status != 5:
                total_count += 1
        return total_count
    except Exception:
        return 0


def calculate_folder_mileage_km(primary_h5_file: str | Path | None) -> float:
    if not primary_h5_file:
        return 0.0

    try:
        with h5py.File(primary_h5_file, "r") as h5_file:
            root_names = list(h5_file.keys())
            if not root_names:
                return 0.0

            channel = h5_file[root_names[0]].get("Channel1")
            if channel is None or ODOMETER_DATASET_NAME not in channel:
                return 0.0

            dataset = channel[ODOMETER_DATASET_NAME]
            dtype_names = dataset.dtype.names or ()
            if ODOMETER_FIELD_NAME not in dtype_names:
                return 0.0

            odometer_values = dataset[ODOMETER_FIELD_NAME]
            if len(odometer_values) == 0:
                return 0.0

            if ODOMETER_VALID_FIELD_NAME in dtype_names:
                valid_mask = dataset[ODOMETER_VALID_FIELD_NAME] == 0
                if valid_mask.any():
                    odometer_values = odometer_values[valid_mask]

            if len(odometer_values) == 0:
                return 0.0

            mileage_km = float(odometer_values[-1] - odometer_values[0])
            return max(mileage_km, 0.0)
    except Exception:
        return 0.0


def save_results_to_csv(result_info: dict[str, Any], output_file: str | Path) -> None:
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = ["文件夹路径", "检查结果", "符合要求的内容", "总Case", "Bad Case", "严重度得分", "段里程(km)"]
    need_header = not output_path.exists() or output_path.stat().st_size == 0

    statistics = result_info.get("statistics", {}) or {}
    row = {
        "文件夹路径": result_info.get("folder_path", ""),
        "检查结果": "是" if result_info.get("result") else "否",
        "符合要求的内容": "; ".join(str(item) for item in result_info.get("details", [])),
        "总Case": statistics.get("All Case Count", 0),
        "Bad Case": statistics.get("Bad Case", 0),
        "严重度得分": statistics.get("Severity Score", 0.0),
        "段里程(km)": round(_safe_float(result_info.get("mileage_km", 0.0)), 6),
    }

    with output_path.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        if need_header:
            writer.writeheader()
        writer.writerow(row)


def extract_csv_prefix(file_name: str) -> str:
    stem = Path(file_name).stem
    match = re.match(r'^(.*)_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$', stem)
    if match:
        return match.group(1)
    return stem


def read_csv_file(file_path: str | Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with Path(file_path).open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            rows.append({str(key): str(value) for key, value in row.items()})
    return rows


def calculate_case_sums(csv_data: list[dict[str, str]]) -> dict[str, Any]:
    total_case_sum = 0
    bad_case_sum = 0
    total_mileage_km = 0.0
    bad_case_items: list[tuple[float, str, str]] = []

    for row in csv_data:
        total_case_sum += _safe_int(row.get("总Case", "0"))
        bad_case_value = _safe_int(row.get("Bad Case", "0"))
        bad_case_sum += bad_case_value
        total_mileage_km += _safe_float(row.get("段里程(km)", "0"))
        if bad_case_value > 0:
            folder_path = row.get("文件夹路径", "")
            severity_score = _safe_float(row.get("严重度得分", "0"))
            detail_content = row.get("符合要求的内容", "")
            bad_case_items.append((severity_score, folder_path, detail_content))

    bad_case_items.sort(key=lambda item: item[0], reverse=True)
    return {
        "total_case_sum": total_case_sum,
        "bad_case_sum": bad_case_sum,
        "total_mileage_km": total_mileage_km,
        "bad_case_folders": [item[1] for item in bad_case_items],
        "severity_score_folders": [item[0] for item in bad_case_items],
        "detail_content_folders": [item[2] for item in bad_case_items],
    }


def build_mpi_report_name(report_prefix: str, run_timestamp: str) -> str:
    if report_prefix.endswith("_Report"):
        return f"{report_prefix[:-7]}_MPI_Report_{run_timestamp}"
    return f"{report_prefix}_MPI_Report_{run_timestamp}"


def generate_csv_statistics(input_folder_path: str | Path, output_file_name: str, run_timestamp: str) -> None:
    folder_path = Path(input_folder_path)
    folder_path.mkdir(parents=True, exist_ok=True)
    output_file = folder_path / (output_file_name if output_file_name.lower().endswith(".xlsx") else f"{output_file_name}.xlsx")

    statistics_result: list[dict[str, Any]] = []
    bad_case_folders_cache: dict[str, list[str]] = {}
    bad_case_severity_score_cache: dict[str, list[float]] = {}
    bad_case_content_cache: dict[str, list[str]] = {}
    metric_category_map = build_metric_category_map(CACHED_FUNCTIONS)

    current_run_suffix = f"_{run_timestamp}.csv"
    for file_path in sorted(folder_path.glob("*.csv")):
        if not file_path.name.endswith(current_run_suffix):
            continue
        prefix = extract_csv_prefix(file_path.name)
        csv_data = read_csv_file(file_path)
        if not csv_data:
            continue

        case_sums = calculate_case_sums(csv_data)
        total_case_sum = case_sums["total_case_sum"]
        bad_case_sum = case_sums["bad_case_sum"]
        total_mileage_km = case_sums["total_mileage_km"]
        metric_category = metric_category_map.get(prefix, METRIC_CATEGORY_MPI)
        bad_case_folders_cache[prefix] = case_sums["bad_case_folders"]
        bad_case_severity_score_cache[prefix] = case_sums["severity_score_folders"]
        bad_case_content_cache[prefix] = case_sums["detail_content_folders"]

        statistics_result.append(
            {
                "指标名称": prefix,
                "指标类型": metric_category,
                "总里程(km)": round(total_mileage_km, 6),
                "总次数": total_case_sum,
                "触发次数": bad_case_sum,
            }
        )

    if not statistics_result:
        print("未找到可统计的CSV结果文件，跳过统计报告生成")
        return

    workbook = Workbook()
    ws_main = workbook.active
    ws_main.title = "所有指标统计汇总"

    header_font = Font(bold=True, size=11)
    header_alignment = Alignment(horizontal="center", vertical="center")
    green_font = Font(color="008000")
    red_font = Font(color="FF0000")

    headers = ["指标名称", "指标类型", "总里程(km)", "总次数", "触发次数", "MPI", "CPI"]
    for col_idx, header in enumerate(headers, 1):
        cell = ws_main.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.alignment = header_alignment

    for row_idx, row_data in enumerate(statistics_result, 2):
        total_mileage_km = row_data["总里程(km)"]
        total_case = row_data["总次数"]
        trigger_count = row_data["触发次数"]
        metric_category = row_data["指标类型"]
        ws_main.cell(row=row_idx, column=1, value=row_data["指标名称"]).alignment = header_alignment
        ws_main.cell(row=row_idx, column=2, value=metric_category).alignment = header_alignment

        mileage_cell = ws_main.cell(row=row_idx, column=3, value=total_mileage_km)
        mileage_cell.alignment = header_alignment

        total_cell = ws_main.cell(row=row_idx, column=4, value=total_case)
        total_cell.font = green_font
        total_cell.alignment = header_alignment

        trigger_cell = ws_main.cell(row=row_idx, column=5, value=trigger_count)
        trigger_cell.font = red_font
        trigger_cell.alignment = header_alignment

        mpi_text = ""
        cpi_text = ""
        if metric_category == METRIC_CATEGORY_MPI:
            if trigger_count == 0:
                mileage_text = f"{round(total_mileage_km, 3):g}"
                mpi_text = f">{mileage_text}({mileage_text}/0)"
            else:
                mpi_value = round(total_mileage_km / trigger_count, 1)
                mpi_text = f"{mpi_value}({round(total_mileage_km, 3)}/{trigger_count})"
        else:
            if trigger_count == 0:
                cpi_text = f">{total_case}({total_case}/0)"
            else:
                cpi_value = round(total_case / trigger_count, 3)
                cpi_text = f"{cpi_value}({total_case}/{trigger_count})"

        mpi_cell = ws_main.cell(row=row_idx, column=6, value=mpi_text)
        mpi_cell.alignment = header_alignment
        mpi_cell.comment = Comment(
            text="MPI = 总里程(km) / 触发次数\n触发次数沿用各指标CSV中的 Bad Case 统计口径",
            author="统计脚本",
        )
        cpi_cell = ws_main.cell(row=row_idx, column=7, value=cpi_text)
        cpi_cell.alignment = header_alignment
        cpi_cell.comment = Comment(
            text="CPI = 总次数 / 触发次数\n总次数对 APA 指标统一按 APA 退出次数统计",
            author="统计脚本",
        )

    for col in ws_main.columns:
        max_length = max(len(str(cell.value)) if cell.value is not None else 0 for cell in col)
        ws_main.column_dimensions[col[0].column_letter].width = min(max_length + 4, 60)

    for csv_prefix, folders in bad_case_folders_cache.items():
        sheet_name = csv_prefix[:31] if len(csv_prefix) > 31 else csv_prefix
        if sheet_name in workbook.sheetnames:
            sheet_name = f"{sheet_name[:28]}_{len(workbook.sheetnames)}"
        ws = workbook.create_sheet(title=sheet_name)

        headers = ["Bad_Case数据列表", "问题严重程度得分", "问题内容", "问题分析", "分析截图"]
        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = header_font if col_idx <= 3 else red_font
            cell.alignment = header_alignment

        scores = bad_case_severity_score_cache[csv_prefix]
        contents = bad_case_content_cache[csv_prefix]
        if folders and scores and contents:
            for row_idx, (folder_path, score, content) in enumerate(zip(folders, scores, contents), 2):
                ws.cell(row=row_idx, column=1, value=folder_path)
                ws.cell(row=row_idx, column=2, value=score)
                ws.cell(row=row_idx, column=3, value=content)
        else:
            ws.cell(row=2, column=1, value="无Bad_Case")
            ws.cell(row=2, column=2, value="无Bad_Case")
            ws.cell(row=2, column=3, value="无Bad_Case")

        ws.column_dimensions["A"].width = 80
        ws.column_dimensions["B"].width = 18
        ws.column_dimensions["C"].width = 100
        ws.column_dimensions["D"].width = 60
        ws.column_dimensions["E"].width = 60

    workbook.save(output_file)
    print(f"统计报告已生成：{output_file}")


def process_folder_with_all_functions(folder_path: str, folder_index: int, total_folders: int, output_dir: str | Path, file_pattern: str, preprocess_signals: list[str], run_timestamp: str) -> None:
    print(f"[{folder_index}/{total_folders}] 开始处理文件夹：{folder_path}")
    folder_context = build_folder_context(folder_path, file_pattern, preprocess_signals)
    if not folder_context["h5_files"]:
        print(f"  ⚠️ 未找到匹配文件：{folder_path}")
        return
    folder_mileage_km = calculate_folder_mileage_km(folder_context.get("primary_h5_file"))

    session = folder_context.get("session")
    for func_basename, module_obj in CACHED_FUNCTIONS.items():
        print(f"[{folder_index}/{total_folders}]  └── 执行函数检查：{func_basename}")
        output_name = f"{Path(func_basename).stem}_{run_timestamp}.csv"
        required_signals = list(getattr(module_obj, "REQUIRED_SIGNALS", []))
        metric_category = get_metric_category(module_obj, func_basename)
        apa_total_count = _safe_int(folder_context.get("apa_total_count", 0))

        if session is not None:
            missing_signals = session.missing_signals(required_signals)
        else:
            missing_signals = required_signals

        if missing_signals:
            detail = f"缺少信号: {', '.join(missing_signals)}"
            result_info = {
                "folder_path": folder_path,
                "result": False,
                "details": [detail],
                "mileage_km": folder_mileage_km,
                "statistics": {
                    "All Case Count": apa_total_count if metric_category == METRIC_CATEGORY_CPI else 0,
                    "Bad Case": 0,
                    "Severity Score": 0.0,
                },
            }
            save_results_to_csv(result_info, Path(output_dir) / output_name)
            print(f"  ⚠️ {func_basename} 跳过：{detail}")
            continue

        try:
            result = module_obj.__run_haoran_data_run__(folder_context)
            if not validate_function_return(result):
                print(f"  ⚠️ {func_basename} 返回值结构错误")
                continue
            if len(result) == 3:
                check_result, check_details, check_statistics = result
            else:
                check_result, check_details = result
                check_statistics = {}

            if metric_category == METRIC_CATEGORY_CPI:
                check_statistics = dict(check_statistics or {})
                check_statistics["All Case Count"] = apa_total_count

            result_info = {
                "folder_path": folder_path,
                "result": check_result,
                "details": check_details,
                "mileage_km": folder_mileage_km,
                "statistics": check_statistics,
            }
            save_results_to_csv(result_info, Path(output_dir) / output_name)
            print(f"  ✅ {func_basename} 处理完成")
        except Exception as exc:
            print(f"  ❌ {func_basename} 执行失败：{exc}")


def _safe_int(value: str) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


def _safe_float(value: str) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _normalize_numeric_value(value: object) -> int | None:
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def main() -> int:
    base_dir = Path(__file__).resolve().parent
    config_path = base_dir / "config.json"
    try:
        config = load_config(config_path)
    except Exception as exc:
        print(f"配置加载失败：{exc}")
        return 1

    func_dir = (base_dir / config["func_folder_path"]).resolve()
    source_path = Path(config["source_path"]).resolve()
    output_dir = (base_dir / config["output_dir"]).resolve()
    run_timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    report_name = build_mpi_report_name(str(config["report_prefix"]), run_timestamp)

    print("========== 开始加载指标函数 ==========")
    load_all_functions(func_dir)
    print("========== 指标函数加载完成 ==========")
    try:
        validate_required_signals(CACHED_FUNCTIONS, list(config.get("preprocess_signals", [])))
    except Exception as exc:
        print(f"预处理信号配置错误：{exc}")
        return 1

    eligible_folders = collect_eligible_folders(
        base_path=source_path,
        max_depth=int(config["max_depth"]),
        suffix=str(config.get("suffix", "_done")),
        file_pattern=str(config.get("file_pattern", "*.h5")),
        query_date=str(config.get("query_date", "")),
    )
    print(f"共找到 {len(eligible_folders)} 个符合条件的文件夹")

    for index, folder_path in enumerate(eligible_folders, start=1):
        process_folder_with_all_functions(
            folder_path=folder_path,
            folder_index=index,
            total_folders=len(eligible_folders),
            output_dir=output_dir,
            file_pattern=str(config.get("file_pattern", "*.h5")),
            preprocess_signals=list(config.get("preprocess_signals", [])),
            run_timestamp=run_timestamp,
        )

    generate_csv_statistics(output_dir, report_name, run_timestamp)
    print("全部处理完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
