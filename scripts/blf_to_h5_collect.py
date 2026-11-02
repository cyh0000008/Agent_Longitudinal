from __future__ import annotations

import argparse
import struct
import sys
import time
import zlib
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import can.io.blf
import cantools
import h5py
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DBC_ROOT = ROOT / "config" / "UISEE_Analyze_dbc"
DBC_FOLDERS = {
    "CAN_PB": "CAN_PB",
    "CAN_CB": "CAN_CB",
    "Eth_Debug_CAN5": "Eth_Debug_CAN5",
    "Eth_VISION_CAN6": "Eth_VISION_CAN6",
}

BASE = can.io.blf.OBJ_HEADER_BASE_STRUCT
V1 = can.io.blf.OBJ_HEADER_V1_STRUCT
V2 = can.io.blf.OBJ_HEADER_V2_STRUCT
FH = can.io.blf.FILE_HEADER_STRUCT
CAN_FD = can.io.blf.CAN_FD_MSG_STRUCT
CAN_MSG = can.io.blf.CAN_MSG_STRUCT
DLC_MAP = [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64]
TIME_TEN_MICS_FACTOR = Decimal("0.00001")
TIME_ONE_NANS_FACTOR = Decimal("0.000000001")
_ID_TO_MSG: dict[int, list[tuple[str, Any]]] | None = None


def load_dbc_index(dbc_root: str | Path | None = None) -> dict[int, list[tuple[str, Any]]]:
    global _ID_TO_MSG
    root = Path(dbc_root or DEFAULT_DBC_ROOT).expanduser()
    if _ID_TO_MSG is not None and root == DEFAULT_DBC_ROOT:
        return _ID_TO_MSG

    id_to_msg: dict[int, list[tuple[str, Any]]] = defaultdict(list)
    for channel_name, relative_folder in DBC_FOLDERS.items():
        folder = root / relative_folder
        if not folder.exists():
            print(f"  SKIP {channel_name}: DBC folder not found ({folder})")
            continue
        for dbc_path in sorted(folder.glob("*.dbc")):
            dbc_label = f"{channel_name}/{dbc_path.stem}"
            try:
                database = cantools.database.load_file(str(dbc_path), strict=False)
                for message in database.messages:
                    id_to_msg[int(message.frame_id)].append((dbc_label, message))
                print(f"  {dbc_label}: {len(database.messages)} messages")
            except Exception as exc:
                print(f"  {dbc_label}: FAILED - {exc}")
    result = dict(id_to_msg)
    if root == DEFAULT_DBC_ROOT:
        _ID_TO_MSG = result
    return result


def get_output_path(blf_path: str | Path, out_dir: str | Path) -> Path:
    path = Path(blf_path)
    return Path(out_dir) / f"{path.stem}_parsed.h5"


def parse_one_blf(blf_path: str | Path, out_path: str | Path, dbc_root: str | Path | None = None) -> bool:
    blf = Path(blf_path).expanduser().resolve()
    output = Path(out_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    id_to_msg = load_dbc_index(dbc_root)
    print(f"\n{'=' * 70}")
    print(f"Input:  {blf}")
    print(f"Output: {output}")

    started = time.time()
    try:
        raw = blf.read_bytes()
    except Exception as exc:
        print(f"  ERROR reading file: {exc}")
        return False
    print(f"  Read {len(raw) / 1024 / 1024:.0f} MB in {time.time() - started:.1f}s")

    if len(raw) < FH.size:
        print(f"  ERROR: file too small ({len(raw)} bytes), skipping")
        return False

    header_data = FH.unpack_from(raw, 0)
    header_size = header_data[1]
    pos, max_pos = header_size, len(raw)
    records = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {"t": [], "data": [], "signals": defaultdict(list)})))
    can_processed = can_matched = can_unmatched = decode_err = 0
    eth_processed = eth_can_parsed = eth_matched = eth_unmatched = 0

    while pos + BASE.size <= max_pos:
        try:
            next_pos = raw.index(b"LOBJ", pos, min(pos + 16, max_pos))
        except ValueError:
            break
        pos = next_pos
        if pos + BASE.size > max_pos:
            break

        signature, _header_size, header_version, object_size, object_type = BASE.unpack_from(raw, pos)
        if signature != b"LOBJ" or object_type != 10:
            pos += 1
            continue
        if object_size < 48 or pos + object_size > max_pos:
            pos += 1
            continue

        object_header_size = V1.size if header_version == 1 else (V2.size if header_version == 2 else V1.size)
        try:
            decompressed = zlib.decompress(raw[pos + BASE.size + object_header_size : pos + object_size])
        except Exception:
            pos += object_size
            continue

        inner_pos = 0
        while inner_pos + BASE.size <= len(decompressed):
            if decompressed[inner_pos : inner_pos + 4] != b"LOBJ":
                inner_pos += 1
                continue
            _, _, inner_header_version, inner_object_size, inner_object_type = BASE.unpack_from(decompressed, inner_pos)
            if inner_object_size < 32 or inner_pos + inner_object_size > len(decompressed):
                inner_pos += 1
                continue

            inner_header_size = V1.size if inner_header_version == 1 else (V2.size if inner_header_version == 2 else V1.size)
            timestamp_pos = inner_pos + BASE.size
            data_start = timestamp_pos + inner_header_size
            payload = None
            channel = None
            can_id = None
            data_length = 0

            if inner_object_type == 100 and data_start + CAN_FD.size <= len(decompressed):
                channel, flags, dlc_raw, can_id, _valid_bytes, _bitrate1, _bitrate2, _bitrate3, can_data = CAN_FD.unpack_from(decompressed, data_start)
                data_length = DLC_MAP[min(int(dlc_raw), 15)]
                if not (flags & 0x20):
                    payload = can_data[:data_length]
            elif inner_object_type in (1, 86) and data_start + CAN_MSG.size <= len(decompressed):
                channel, flags, data_raw, can_id_raw, can_data = CAN_MSG.unpack_from(decompressed, data_start)
                can_id = can_id_raw & 0x1FFFFFFF
                data_length = min(int(data_raw), 8)
                if not (flags & 0x20):
                    payload = can_data[:data_length]

            if payload is not None and channel is not None and can_id is not None:
                can_processed += 1
                rel_ts = _relative_timestamp(decompressed, timestamp_pos, inner_header_version)
                dbc_messages = id_to_msg.get(int(can_id), [])
                if dbc_messages:
                    can_matched += 1
                    for dbc_name, message in dbc_messages:
                        if data_length < message.length:
                            continue
                        try:
                            decoded = message.decode(bytearray(payload[: message.length]), decode_choices=False)
                            key = f"0x{int(can_id):03X}_{message.name}"
                            rec = records[channel][dbc_name][key]
                            rec["t"].append(rel_ts)
                            rec["data"].append(payload[: message.length])
                            for signal_name, signal_value in decoded.items():
                                rec["signals"][signal_name].append(signal_value)
                        except Exception:
                            decode_err += 1
                else:
                    can_unmatched += 1

            elif inner_object_type == 71:
                eth_processed += 1
                eth_data = decompressed[timestamp_pos + inner_header_size : inner_pos + inner_object_size]
                rel_ts = _relative_timestamp(decompressed, timestamp_pos, inner_header_version)
                for offset in range(len(eth_data) - 10):
                    if eth_data[offset] != 0x55 or eth_data[offset + 1] != 0xAA:
                        continue
                    data_len = struct.unpack_from("<H", eth_data, offset + 2)[0]
                    if data_len < 1 or data_len > 2000:
                        continue
                    frame_end = offset + 4 + data_len
                    if frame_end + 4 > len(eth_data):
                        continue
                    frame_pos = offset + 4
                    while frame_pos + 6 <= frame_end:
                        can_id_raw = struct.unpack_from("<I", eth_data, frame_pos)[0]
                        can_id = can_id_raw & 0x1FFFFFFF
                        dlc_value = eth_data[frame_pos + 4]
                        channel = eth_data[frame_pos + 5]
                        can_length = DLC_MAP[min(int(dlc_value), 15)]
                        if frame_pos + 6 + can_length > frame_end:
                            break
                        can_data = eth_data[frame_pos + 6 : frame_pos + 6 + can_length]
                        frame_pos += 6 + can_length
                        eth_can_parsed += 1

                        dbc_messages = id_to_msg.get(int(can_id), [])
                        if dbc_messages:
                            eth_matched += 1
                            for dbc_name, message in dbc_messages:
                                if can_length < message.length:
                                    continue
                                try:
                                    decoded = message.decode(bytearray(can_data[: message.length]), decode_choices=False)
                                    key = f"0x{int(can_id):03X}_{message.name}"
                                    rec = records[channel][dbc_name][key]
                                    rec["t"].append(rel_ts)
                                    rec["data"].append(can_data[: message.length])
                                    for signal_name, signal_value in decoded.items():
                                        rec["signals"][signal_name].append(signal_value)
                                except Exception:
                                    pass
                        else:
                            eth_unmatched += 1

            inner_pos += inner_object_size
        pos += object_size

        if can_processed % 1000000 == 0 and can_processed > 0:
            print(f"  {can_processed / 1e6:.1f}M CAN frames, {can_matched} matched...")

    elapsed = time.time() - started
    total_processed = can_processed + eth_can_parsed
    total_matched = can_matched + eth_matched
    print(f"  Standard CAN: {can_processed} frames, {can_matched} matched, {can_unmatched} unmatched")
    print(f"  ETH CAN:      {eth_processed} eth frames, {eth_can_parsed} can parsed, {eth_matched} matched")
    print(f"  Total: {total_processed} frames, {total_matched} matched ({elapsed:.0f}s)")

    if total_processed == 0:
        print("  No frames found, skipping H5 output")
        return False

    with h5py.File(str(output), "w") as handle:
        for channel in sorted(records.keys()):
            channel_group = handle.create_group(f"ch{channel}")
            for dbc_name in sorted(records[channel].keys()):
                dbc_group = channel_group.create_group(dbc_name)
                for message_key in sorted(records[channel][dbc_name].keys()):
                    rec = records[channel][dbc_name][message_key]
                    sample_count = len(rec["t"])
                    if sample_count == 0:
                        continue
                    message_group = dbc_group.create_group(message_key)
                    message_group.create_dataset("timestamp", data=np.array(rec["t"], dtype=np.float64), compression="gzip")
                    message_group.create_dataset(
                        "data_bytes",
                        data=np.array([np.frombuffer(item, dtype=np.uint8) for item in rec["data"]], dtype=np.uint8),
                        compression="gzip",
                    )
                    signals_group = message_group.create_group("signals")
                    for signal_name in sorted(rec["signals"].keys()):
                        values = rec["signals"][signal_name]
                        first_value = values[0]
                        if isinstance(first_value, bool):
                            array = np.array(values, dtype=np.bool_)
                        elif isinstance(first_value, float):
                            array = np.array(values, dtype=np.float64)
                        elif isinstance(first_value, (int, np.integer)):
                            try:
                                array = np.array(values, dtype=np.int64)
                            except OverflowError:
                                array = np.array(values, dtype=np.float64)
                        else:
                            array = np.array([str(item) for item in values], dtype=h5py.string_dtype())
                        signals_group.create_dataset(signal_name, data=array, compression="gzip")

        handle.attrs["source_blf"] = blf.name
        handle.attrs["can_frames"] = can_processed
        handle.attrs["can_matched"] = can_matched
        handle.attrs["eth_frames"] = eth_processed
        handle.attrs["eth_can_parsed"] = eth_can_parsed
        handle.attrs["eth_matched"] = eth_matched
        handle.attrs["parse_time"] = elapsed

    print(f"  H5 saved: {output.stat().st_size / 1024 / 1024:.1f} MB")
    return True


def _relative_timestamp(data: bytes, timestamp_pos: int, header_version: int) -> float:
    if header_version == 1:
        flags, _, _, raw_timestamp = V1.unpack_from(data, timestamp_pos)
    elif header_version == 2:
        flags, _, _, raw_timestamp = V2.unpack_from(data, timestamp_pos)
    else:
        flags, _, _, raw_timestamp = V1.unpack_from(data, timestamp_pos)
    factor = TIME_TEN_MICS_FACTOR if (flags & 1) else TIME_ONE_NANS_FACTOR
    return float(Decimal(raw_timestamp) * factor)


def find_blf_files(root: str | Path) -> list[Path]:
    path = Path(root).expanduser()
    if path.is_file():
        return [path] if path.suffix.lower() == ".blf" else []
    return sorted(path.rglob("*.blf")) if path.is_dir() else []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse BLF files to H5 format using bundled DBCs.")
    parser.add_argument("-d", "--directory", required=True, help="A .blf file path or a directory recursively containing .blf files")
    parser.add_argument("-o", "--output-dir", default="", help="Output directory; default is a timestamped *_parsed folder beside this script")
    parser.add_argument("--dbc-root", default=str(DEFAULT_DBC_ROOT), help="DBC root containing CAN_PB/CAN_CB/Eth_Debug_CAN5/Eth_VISION_CAN6")
    args = parser.parse_args(argv)

    target = Path(args.directory).expanduser().resolve()
    if not target.exists():
        print(f"Error: path does not exist: {target}")
        return 1
    if args.output_dir:
        out_dir = Path(args.output_dir).expanduser().resolve()
    else:
        out_dir = ROOT / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_parsed"
    out_dir.mkdir(parents=True, exist_ok=True)

    blf_files = find_blf_files(target)
    if not blf_files:
        print(f"No .blf files found: {target}")
        return 1

    ok = fail = skip = 0
    for blf_file in blf_files:
        out_path = get_output_path(blf_file, out_dir)
        if out_path.exists():
            print(f"\n  SKIP (already parsed): {out_path}")
            skip += 1
            continue
        if parse_one_blf(blf_file, out_path, args.dbc_root):
            ok += 1
        else:
            fail += 1
    print(f"\n{'=' * 70}")
    print(f"Batch done. OK: {ok}, FAIL: {fail}, SKIP: {skip}, Output: {out_dir}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
