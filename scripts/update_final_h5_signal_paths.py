from __future__ import annotations

import re
import os
from copy import copy
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import h5py
from openpyxl import load_workbook


ROOT = Path(__file__).resolve().parents[1]
FINAL_H5_DIR = Path(os.getenv("FINAL_H5_DIR", ""))
FINAL_H5_SCAN_LIMIT = int(os.getenv("FINAL_H5_SCAN_LIMIT", "5"))
J6B_SIGNAL_INDEX = Path(
    os.getenv("J6B_SIGNAL_INDEX", str(ROOT / "config" / "vehicle_response_signal_catalog.xlsx"))
)
VEHICLE_CATALOG = ROOT / "config" / "vehicle_response_signal_catalog.xlsx"
EXIT_RULES = ROOT / "config" / "acc_lcc_exit_reason_rules.xlsx"
OUTPUT_DOC = ROOT / "config" / "h5_signal_path_mapping_20260725.md"


MANUAL_ALIASES = {
    "VehSpdAvgDrvn_1": ("IVehSpdAvgDrvn_1",),
    "VehSpdAvgNDrvn": ("IVehSpdAvgNDrvn",),
    "BrkPedApp": ("IBrkPedApp",),
    "BrkPedTrvlAchvd": ("IBrkPedTrvlAchvd",),
    "ACCBrkngAct": ("IACCBrkngAct",),
    "AutoBrkngAct": ("IAutoBrkngAct",),
    "ActAxleTrq": ("IActAxleTrq",),
    "VehMvngStat": ("IVehMvngStat",),
    "ACCHdwyStg": ("IACCHdwyStg",),
    "Left_Lane_Track_Coeff.LaneModelC2": (
        "Left_Lane_Track_Coeff.LaneModelC2",
        "0xB00_Left_Lane_Track_Coeff.LaneModelC2",
    ),
    "Right_Lane_Track_Coeff.LaneModelC2": (
        "Right_Lane_Track_Coeff.LaneModelC2",
        "0xB01_Right_Lane_Track_Coeff.LaneModelC2",
    ),
}


@dataclass(frozen=True)
class SignalPath:
    path: str
    bus: str
    message: str
    signal: str


def main() -> None:
    if not os.getenv("FINAL_H5_DIR"):
        raise RuntimeError("请通过 FINAL_H5_DIR 配置待扫描的 H5 数据目录。")
    index = build_h5_signal_index(FINAL_H5_DIR)
    vehicle_rows = update_vehicle_catalog(index)
    exit_rows = update_exit_rules(index)
    write_mapping_doc(vehicle_rows, exit_rows)
    print(f"indexed_aliases={len(index)}")
    print(f"updated_vehicle_rows={len(vehicle_rows)}")
    print(f"updated_exit_rows={len(exit_rows)}")
    print(f"doc={OUTPUT_DOC}")
    if J6B_SIGNAL_INDEX.exists():
        print(f"j6b_signal_index={J6B_SIGNAL_INDEX}")


def build_h5_signal_index(h5_dir: Path) -> dict[str, list[SignalPath]]:
    if not h5_dir.exists():
        raise FileNotFoundError(f"Final H5 folder not found: {h5_dir}")

    alias_to_paths: dict[str, list[SignalPath]] = defaultdict(list)
    h5_files = sorted(h5_dir.glob("*.h5"))
    if FINAL_H5_SCAN_LIMIT > 0:
        h5_files = h5_files[:FINAL_H5_SCAN_LIMIT]
    for h5_file in h5_files:
        with h5py.File(h5_file, "r") as handle:
            for dataset_path in iter_signal_dataset_paths(handle):
                item = signal_path_info(dataset_path)
                for alias in aliases_for_path(item):
                    alias_to_paths[normalize_alias(alias)].append(item)

    return {key: unique_sorted_paths(paths) for key, paths in alias_to_paths.items()}


def iter_signal_dataset_paths(group: h5py.Group, prefix: str = "") -> Iterable[str]:
    for name, item in group.items():
        current = f"{prefix}/{name}" if prefix else name
        if isinstance(item, h5py.Dataset):
            if "/signals/" in current:
                yield current
        elif isinstance(item, h5py.Group):
            yield from iter_signal_dataset_paths(item, current)


def signal_path_info(path: str) -> SignalPath:
    clean = path.strip("/")
    before_signal, signal = clean.rsplit("/signals/", 1)
    signal = signal.split("/", 1)[0]
    parts = before_signal.split("/")
    message = parts[-1]
    bus = parts[1] if len(parts) > 1 else ""
    return SignalPath(path=clean, bus=bus, message=message, signal=signal)


def aliases_for_path(item: SignalPath) -> list[str]:
    aliases = [item.path, item.signal]
    aliases.extend(short_variants(item.signal))
    for message_alias in message_aliases(item.message):
        aliases.append(f"{message_alias}.{item.signal}")
        for short in short_variants(item.signal):
            aliases.append(f"{message_alias}.{short}")
    return list(dict.fromkeys(alias for alias in aliases if alias))


def message_aliases(message_name: str) -> list[str]:
    value = str(message_name or "").strip()
    aliases = [value]
    no_prefix = re.sub(r"^0x[0-9A-Fa-f]+_", "", value)
    aliases.append(no_prefix)
    aliases.append(re.sub(r"_0x[0-9A-Fa-f]+$", "", no_prefix))
    return list(dict.fromkeys(alias for alias in aliases if alias))


def short_variants(signal_name: str) -> list[str]:
    value = str(signal_name or "").strip()
    variants = [value]
    if len(value) > 1 and value.startswith("I") and value[1].isupper():
        variants.append(value[1:])
    elif value:
        variants.append(f"I{value}")
    return variants


def normalize_alias(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").strip("/").lower()


def unique_sorted_paths(paths: list[SignalPath]) -> list[SignalPath]:
    unique = {item.path: item for item in paths}
    return sorted(unique.values(), key=path_sort_key)


def path_sort_key(item: SignalPath) -> tuple[int, int, str]:
    bus_rank = 5
    if item.bus == "CAN_CB":
        bus_rank = 0 if item.signal.startswith(("IACCBSCE_", "IFCACBSC_")) else 3
    elif item.bus == "Eth_Debug_CAN5":
        bus_rank = 0 if item.signal.startswith(("FLT1_", "Debug_", "MRR_", "ACC_")) or item.signal == "LaneModelC2" else 2
    elif item.bus == "CAN_PB":
        bus_rank = 1

    channel_rank = 9
    match = re.match(r"ch(\d+)/", item.path)
    if match:
        channel = int(match.group(1))
        preferred = {4: 0, 3: 1, 1: 2, 2: 3, 5: 4, 0: 5, 9: 6}
        channel_rank = preferred.get(channel, 8)
    return (bus_rank, channel_rank, item.path)


def update_vehicle_catalog(index: dict[str, list[SignalPath]]) -> list[dict[str, str]]:
    workbook = load_workbook(VEHICLE_CATALOG)
    sheet = workbook["signal_catalog"]
    header = header_map(sheet)
    path_col = ensure_column(sheet, header, "H5数据集路径")
    rows_for_doc: list[dict[str, str]] = []

    for row_idx in range(2, sheet.max_row + 1):
        semantic = cell_text(sheet.cell(row_idx, header["信号语义"]))
        signal_text = cell_text(sheet.cell(row_idx, header["具体信号名"]))
        status = cell_text(sheet.cell(row_idx, header.get("当前状态", 0)))
        paths = match_paths_for_cell(signal_text, index)
        sheet.cell(row_idx, path_col).value = "\n".join(paths)
        rows_for_doc.append(
            {
                "语义": semantic,
                "信号名": signal_text,
                "状态": status,
                "H5路径": "\n".join(paths),
            }
        )

    workbook.save(VEHICLE_CATALOG)
    return rows_for_doc


def update_exit_rules(index: dict[str, list[SignalPath]]) -> list[dict[str, str]]:
    workbook = load_workbook(EXIT_RULES)
    sheet = workbook["signals"]
    header = header_map(sheet)
    path_col = ensure_column(sheet, header, "H5数据集路径")
    rows_for_doc: list[dict[str, str]] = []

    for row_idx in range(2, sheet.max_row + 1):
        function = cell_text(sheet.cell(row_idx, header.get("功能", 0)))
        label = cell_text(sheet.cell(row_idx, header.get("用途", 0)))
        logical = cell_text(sheet.cell(row_idx, header.get("逻辑字段", 0)))
        signal_text = cell_text(sheet.cell(row_idx, header["真实信号名"]))
        paths = match_paths_for_cell(signal_text, index)
        sheet.cell(row_idx, path_col).value = "\n".join(paths)
        rows_for_doc.append(
            {
                "功能": function,
                "用途": label,
                "逻辑字段": logical,
                "信号名": signal_text,
                "H5路径": "\n".join(paths),
            }
        )

    workbook.save(EXIT_RULES)
    return rows_for_doc


def header_map(sheet) -> dict[str, int]:
    result: dict[str, int] = {}
    for col_idx in range(1, sheet.max_column + 1):
        value = cell_text(sheet.cell(1, col_idx))
        if value:
            result[value] = col_idx
    return result


def ensure_column(sheet, header: dict[str, int], name: str) -> int:
    if name in header:
        return header[name]
    col_idx = sheet.max_column + 1
    sheet.cell(1, col_idx).value = name
    if col_idx > 1:
        source = sheet.cell(1, col_idx - 1)
        target = sheet.cell(1, col_idx)
        target.font = copy(source.font)
        target.fill = copy(source.fill)
        target.border = copy(source.border)
        target.alignment = copy(source.alignment)
        target.number_format = source.number_format
        target.protection = copy(source.protection)
        sheet.column_dimensions[sheet.cell(1, col_idx).column_letter].width = 60
    header[name] = col_idx
    return col_idx


def cell_text(cell) -> str:
    value = cell.value
    if value is None:
        return ""
    return str(value).strip()


def match_paths_for_cell(value: str, index: dict[str, list[SignalPath]]) -> list[str]:
    matches: list[str] = []
    for token in split_signal_cell(value):
        for alias in aliases_for_config_token(token):
            for item in index.get(normalize_alias(alias), []):
                matches.append(item.path)
    return list(dict.fromkeys(matches))


def split_signal_cell(value: str) -> list[str]:
    text = str(value or "")
    for sep in ("；", ";", "，", ",", "\n", "、"):
        text = text.replace(sep, "\n")
    result: list[str] = []
    for raw in text.splitlines():
        token = raw.strip().strip("`")
        if not token:
            continue
        if "计算" in token or "computed" in token.lower() or "默认" in token:
            continue
        result.append(token)
    return result


def aliases_for_config_token(token: str) -> list[str]:
    value = str(token or "").strip().strip("/")
    aliases = [value]
    aliases.extend(short_variants(value))
    aliases.extend(MANUAL_ALIASES.get(value, ()))
    if "." in value:
        message, signal = value.rsplit(".", 1)
        for message_alias in message_aliases(message):
            for signal_alias in short_variants(signal):
                aliases.append(f"{message_alias}.{signal_alias}")
        aliases.extend(MANUAL_ALIASES.get(signal, ()))
    return list(dict.fromkeys(alias for alias in aliases if alias))


def write_mapping_doc(vehicle_rows: list[dict[str, str]], exit_rows: list[dict[str, str]]) -> None:
    OUTPUT_DOC.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 最终 H5 信号路径修正清单",
        "",
        f"- 最终 H5 扫描目录：`{FINAL_H5_DIR}`",
        f"- 本次扫描文件数：`{FINAL_H5_SCAN_LIMIT if FINAL_H5_SCAN_LIMIT > 0 else '全部'}`（同批解析文件结构一致，默认扫描前几份代表文件）",
        f"- J6B 信号名索引参考：`{J6B_SIGNAL_INDEX}`",
        "- 说明：路径为最终 H5 内部 dataset 路径；部分请求信号位于 `CAN_CB`，已经纳入索引。",
        "",
        "## 行车问题原因分析信号",
        "",
        "| 信号语义 | 配置中的信号名 | 状态 | 最终 H5 路径 |",
        "|---|---|---|---|",
    ]
    for row in vehicle_rows:
        lines.append(
            "| "
            + " | ".join(
                escape_cell(row.get(key, "")).replace("\n", "<br>")
                for key in ("语义", "信号名", "状态", "H5路径")
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## ACC/LCC 退出原因信号",
            "",
            "| 功能 | 用途 | 逻辑字段 | 配置中的信号名 | 最终 H5 路径 |",
            "|---|---|---|---|---|",
        ]
    )
    for row in exit_rows:
        lines.append(
            "| "
            + " | ".join(
                escape_cell(row.get(key, "")).replace("\n", "<br>")
                for key in ("功能", "用途", "逻辑字段", "信号名", "H5路径")
            )
            + " |"
        )
    OUTPUT_DOC.write_text("\n".join(lines) + "\n", encoding="utf-8")


def escape_cell(value: str) -> str:
    return str(value or "").replace("|", "\\|")


if __name__ == "__main__":
    main()
