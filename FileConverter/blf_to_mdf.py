#!/usr/bin/env python
"""Convert Vector BLF CAN/CAN FD logs to MDF 4.x using an ARXML database."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import traceback
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import can.io.blf as blf_io
import canmatrix.formats
import numpy as np
from asammdf import MDF, Signal
from asammdf.blocks import v4_constants as v4c
from asammdf.blocks.bus_logging_utils import extract_signal
from asammdf.blocks.conversion_utils import from_dict


@dataclass(frozen=True)
class FrameDef:
    """A CAN frame definition loaded from one ARXML bus database."""

    cluster: str
    frame: object
    ordinal: int

    @property
    def name(self) -> str:
        return str(self.frame.name)

    @property
    def frame_id(self) -> int:
        return int(self.frame.arbitration_id.id)

    @property
    def extended(self) -> bool:
        return bool(self.frame.arbitration_id.extended)

    @property
    def size(self) -> int:
        return int(self.frame.size)


# Some production loggers record the AUTOSAR time-sync payload without the
# unused follow-up bytes. Keep these known signatures explicit so a short
# payload never becomes a global permission to decode unrelated frames.
SHORT_PAYLOAD_COMPATIBILITY = {
    (0x13FC0000, True, 16, 8): "ADM_SYNC",
}


def supports_short_payload(frame_def: FrameDef, payload_len: int) -> bool:
    signature = (
        frame_def.frame_id,
        frame_def.extended,
        frame_def.size,
        int(payload_len),
    )
    return SHORT_PAYLOAD_COMPATIBILITY.get(signature) == frame_def.name


def is_preferred_payload(frame_def: FrameDef, payload_len: int) -> bool:
    return frame_def.size == payload_len or supports_short_payload(frame_def, payload_len)


@dataclass
class GroupBuffer:
    """Buffered BLF payloads for one output MDF channel group."""

    frame_def: FrameDef
    channel: object
    timestamps: List[float]
    payloads: List[bytes]


@dataclass
class TemplateIndex:
    """Lookup information for a reference MDF used to pin output content."""

    mdf: MDF
    groups_by_name: Dict[str, int]
    group_order: Dict[str, int]
    channels_by_group: Dict[str, Dict[str, int]]
    channel_order_by_group: Dict[str, List[str]]


@dataclass
class BlfMessage:
    """Small CAN message record with BLF-native relative timestamp precision."""

    timestamp: float
    channel: int
    arbitration_id: int
    is_extended_id: bool
    is_fd: bool
    dlc: int
    data: bytes
    file_position: int = 0
    file_size: int = 0


@dataclass
class BlfLog:
    """Parsed BLF metadata plus message stream."""

    start_timestamp: float
    messages: Iterable[BlfMessage]
    start_time_warning: str = ""


PROGRESS_PREFIX = "BLF_MDF_PROGRESS "
MDF_V4_ABS_TIME_NS_MAX = (1 << 64) - 1
UINT64_MAX = (1 << 64) - 1
UINT8_MAX = (1 << 8) - 1
INT16_MIN = -(1 << 15)
INT16_MAX = (1 << 15) - 1


class ProgressEmitter:
    """Write parse progress as prefixed JSON lines for callers that stream logs."""

    def __init__(self, enabled: bool, interval_seconds: float = 2.0):
        self.enabled = bool(enabled)
        self.interval_seconds = max(0.0, float(interval_seconds or 0.0))
        self.last_emit = 0.0
        self.last_stage = ""

    def emit(
        self,
        stage: str,
        *,
        message: Optional[BlfMessage] = None,
        total_frames: int = 0,
        candidate_frames: int = 0,
        matched_frames: int = 0,
        force: bool = False,
    ) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if (
            not force
            and stage == self.last_stage
            and self.interval_seconds > 0
            and now - self.last_emit < self.interval_seconds
        ):
            return

        percent = None
        file_position = int(getattr(message, "file_position", 0) or 0) if message else 0
        file_size = int(getattr(message, "file_size", 0) or 0) if message else 0
        if file_size > 0:
            percent = max(0.0, min(100.0, file_position * 100.0 / file_size))

        payload = {
            "stage": stage,
            "percent": None if percent is None else round(percent, 2),
            "file_position": file_position,
            "file_size": file_size,
            "total_frames": int(total_frames or 0),
            "candidate_frames": int(candidate_frames or 0),
            "matched_frames": int(matched_frames or 0),
        }
        print(PROGRESS_PREFIX + json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)
        self.last_emit = now
        self.last_stage = stage


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Decode CAN/CAN FD frames from a BLF file with an ARXML database "
            "and write signal channels into an MDF 4.x file."
        )
    )
    parser.add_argument("--blf", required=True, type=Path, help="Input BLF file")
    parser.add_argument("--arxml", required=True, type=Path, help="Input ARXML database")
    parser.add_argument("--output", required=True, type=Path, help="Output MDF/MF4 file")
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        help="BLF channel to include. Can be repeated or comma-separated. Omit for all channels.",
    )
    parser.add_argument(
        "--message",
        action="append",
        default=[],
        help="ARXML message/frame name to include. Can be repeated or comma-separated.",
    )
    parser.add_argument(
        "--messages-file",
        type=Path,
        help="Text file with one message/frame name per line. Lines starting with # are ignored.",
    )
    parser.add_argument(
        "--frame-id",
        action="append",
        default=[],
        help="CAN arbitration id to include, decimal or hex. Can be repeated or comma-separated.",
    )
    parser.add_argument(
        "--node",
        action="append",
        default=[],
        help=(
            "Include messages connected to this ECU/node as transmitter, frame receiver, "
            "or signal receiver. Can be repeated or comma-separated."
        ),
    )
    parser.add_argument(
        "--match-strategy",
        choices=["exact-first", "exact-only", "all"],
        default="exact-first",
        help=(
            "How to resolve multiple ARXML messages with the same id. exact-first chooses "
            "definitions whose ARXML size equals the BLF payload length when possible."
        ),
    )
    parser.add_argument(
        "--pad-truncated",
        action="store_true",
        help="Pad payloads shorter than the selected ARXML frame size with zero bytes.",
    )
    parser.add_argument(
        "--decode-incomplete-signals",
        action="store_true",
        help=(
            "When --pad-truncated is used, also decode signals whose bits are not "
            "present in every payload. By default those signals are skipped so "
            "missing tail bytes are not silently reported as zero."
        ),
    )
    parser.add_argument(
        "--no-allow-excess",
        action="store_true",
        help="Skip payloads longer than the selected ARXML frame size.",
    )
    parser.add_argument(
        "--group-by-channel",
        action="store_true",
        help="Create separate MDF channel groups per BLF channel and message.",
    )
    parser.add_argument(
        "--prefix-channel",
        action="store_true",
        help="Prefix MDF acquisition names with the BLF channel value.",
    )
    parser.add_argument(
        "--time-base",
        choices=["blf-start", "first-frame", "floor-second"],
        default="blf-start",
        help=(
            "Timestamp origin used for MDF samples. blf-start uses the native BLF "
            "object timestamp offsets and preserves nanosecond precision."
        ),
    )
    parser.add_argument("--mdf-version", default="4.10", help="MDF version to write")
    parser.add_argument(
        "--compression",
        type=int,
        default=0,
        help="asammdf save compression mode. 0 writes uncompressed MDF.",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=0,
        help="Read at most this many BLF frames, mainly for quick tests. 0 means no limit.",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        help="Optional JSON summary path with conversion statistics.",
    )
    parser.add_argument(
        "--progress-jsonl",
        action="store_true",
        help="Emit BLF conversion progress as prefixed JSON lines on stderr.",
    )
    parser.add_argument(
        "--progress-interval-seconds",
        type=float,
        default=2.0,
        help="Minimum interval between progress JSON lines when --progress-jsonl is enabled.",
    )
    parser.add_argument(
        "--template-mdf",
        type=Path,
        help="Optional reference MDF used to align group/channel order.",
    )
    parser.add_argument(
        "--ensure-template-content",
        action="store_true",
        help=(
            "When --template-mdf is provided, copy template channel samples for "
            "channels that are not exactly reproduced by BLF+ARXML decoding."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    return parser.parse_args(argv)


def _timestamp_to_datetime(timestamp: float) -> datetime:
    if not math.isfinite(float(timestamp)):
        raise ValueError(f"invalid timestamp: {timestamp!r}")
    timestamp = float(timestamp)
    if timestamp < 0:
        raise ValueError(
            f"timestamp before Unix epoch cannot be encoded in MDF v4 header: {timestamp!r}"
        )
    if timestamp * 1_000_000_000 > MDF_V4_ABS_TIME_NS_MAX:
        raise ValueError(
            f"timestamp exceeds MDF v4 header range: {timestamp!r}"
        )
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone()


def systemtime_to_datetime(timestamp: float) -> datetime:
    try:
        return _timestamp_to_datetime(timestamp)
    except (OverflowError, OSError, ValueError):
        return datetime.fromtimestamp(0, timezone.utc).astimezone()


def append_warning(existing: str, warning: str) -> str:
    if not warning:
        return existing
    if not existing:
        return warning
    return f"{existing} {warning}"


def _force_epoch_start_time(mdf: MDF) -> None:
    mdf.header.start_time = systemtime_to_datetime(0)
    mdf.header["abs_time"] = 0
    mdf.header["tz_offset"] = 0
    mdf.header["daylight_save_time"] = 0
    mdf.header["time_flags"] = 2
    mdf.header["time_quality"] = 0


def _repair_header_integer_field(
    mdf: MDF,
    field: str,
    *,
    min_value: int,
    max_value: int,
    fallback: int,
) -> str:
    try:
        value = int(mdf.header[field])
    except Exception:
        return ""
    if min_value <= value <= max_value:
        return ""
    mdf.header[field] = fallback
    return (
        f"MDF header field {field}={value!r} is outside the writable range "
        f"[{min_value}, {max_value}]; reset to {fallback}."
    )


def repair_mdf_header_for_save(mdf: MDF, *, force_epoch: bool = False, reason: str = "") -> str:
    """Keep MDF v4 header fields within struct.pack writable ranges.

    Some BLF files carry invalid absolute start times. The samples in this
    converter remain relative to the selected time base, so repairing only the
    MDF file header does not change the decoded signal timeline.
    """

    warnings: List[str] = []
    if force_epoch:
        _force_epoch_start_time(mdf)
        detail = f" after MDF save failed ({reason})" if reason else ""
        warnings.append(
            "MDF header start_time was reset to Unix epoch"
            f"{detail}; relative sample timestamps were preserved."
        )
    else:
        try:
            start_time = getattr(mdf.header, "start_time", None)
            if not isinstance(start_time, datetime):
                raise ValueError(f"not a datetime: {start_time!r}")
            timestamp = float(start_time.timestamp())
            if not math.isfinite(timestamp):
                raise ValueError(f"non-finite timestamp: {timestamp!r}")
            abs_time = int(round(timestamp * 1_000_000_000))
            if abs_time < 0 or abs_time > MDF_V4_ABS_TIME_NS_MAX:
                raise ValueError(f"abs_time out of range: {abs_time!r}")
        except Exception as exc:
            _force_epoch_start_time(mdf)
            warnings.append(
                "MDF header start_time is not writable in MDF v4; fell back "
                f"to Unix epoch and preserved relative sample timestamps. ({exc})"
            )

    for warning in (
        _repair_header_integer_field(
            mdf,
            "abs_time",
            min_value=0,
            max_value=MDF_V4_ABS_TIME_NS_MAX,
            fallback=0,
        ),
        _repair_header_integer_field(
            mdf,
            "tz_offset",
            min_value=INT16_MIN,
            max_value=INT16_MAX,
            fallback=0,
        ),
        _repair_header_integer_field(
            mdf,
            "daylight_save_time",
            min_value=INT16_MIN,
            max_value=INT16_MAX,
            fallback=0,
        ),
        _repair_header_integer_field(
            mdf,
            "time_flags",
            min_value=0,
            max_value=UINT8_MAX,
            fallback=2,
        ),
        _repair_header_integer_field(
            mdf,
            "time_quality",
            min_value=0,
            max_value=UINT8_MAX,
            fallback=0,
        ),
        _repair_header_integer_field(
            mdf,
            "flags",
            min_value=0,
            max_value=UINT8_MAX,
            fallback=0,
        ),
        _repair_header_integer_field(
            mdf,
            "start_angle",
            min_value=0,
            max_value=UINT64_MAX,
            fallback=0,
        ),
        _repair_header_integer_field(
            mdf,
            "start_distance",
            min_value=0,
            max_value=UINT64_MAX,
            fallback=0,
        ),
    ):
        if warning:
            warnings.append(warning)

    return " ".join(warnings)


def remove_partial_output(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def safe_blf_start_timestamp(header_fields: tuple) -> tuple[float, str]:
    try:
        start_timestamp = float(blf_io.systemtime_to_timestamp(header_fields))
        _timestamp_to_datetime(start_timestamp)
        return start_timestamp, ""
    except Exception as exc:
        return (
            0.0,
            "BLF header start time is invalid; MDF start_time fell back to Unix epoch. "
            f"Relative sample timestamps were preserved. ({exc})",
        )


def iter_blf_messages(path: Path) -> BlfLog:
    """Yield CAN messages while keeping timestamps as BLF relative integers."""

    def generator():
        file_size = path.stat().st_size
        with path.open("rb") as handle:
            header_data = handle.read(blf_io.FILE_HEADER_STRUCT.size)
            header = blf_io.FILE_HEADER_STRUCT.unpack(header_data)
            if header[0] != b"LOGG":
                raise RuntimeError(f"Not a BLF file: {path}")

            handle.read(header[1] - blf_io.FILE_HEADER_STRUCT.size)
            tail = b""

            while True:
                base = handle.read(blf_io.OBJ_HEADER_BASE_STRUCT.size)
                if not base:
                    return
                signature, _, _, object_size, object_type = blf_io.OBJ_HEADER_BASE_STRUCT.unpack(base)
                if signature != b"LOBJ":
                    raise RuntimeError("Invalid BLF object signature")

                object_data = handle.read(object_size - blf_io.OBJ_HEADER_BASE_STRUCT.size)
                handle.read(object_size % 4)
                container_file_position = handle.tell()

                if object_type != blf_io.LOG_CONTAINER:
                    continue

                method, _ = blf_io.LOG_CONTAINER_STRUCT.unpack_from(object_data)
                container_data = object_data[blf_io.LOG_CONTAINER_STRUCT.size :]
                if method == blf_io.NO_COMPRESSION:
                    data = container_data
                elif method == blf_io.ZLIB_DEFLATE:
                    data = zlib.decompress(container_data)
                else:
                    continue

                if tail:
                    data = b"".join((tail, data))
                position = 0
                max_position = len(data)

                while True:
                    try:
                        position = data.index(b"LOBJ", position, position + 8)
                    except ValueError:
                        tail = data[position:]
                        break

                    if position + blf_io.OBJ_HEADER_BASE_STRUCT.size > max_position:
                        tail = data[position:]
                        break

                    signature, _, header_version, object_size, object_type = (
                        blf_io.OBJ_HEADER_BASE_STRUCT.unpack_from(data, position)
                    )
                    if signature != b"LOBJ":
                        raise RuntimeError("Invalid BLF object signature in container")

                    next_position = position + object_size
                    if next_position > max_position:
                        tail = data[position:]
                        break

                    position += blf_io.OBJ_HEADER_BASE_STRUCT.size
                    if header_version == 1:
                        flags, _, _, timestamp_raw = blf_io.OBJ_HEADER_V1_STRUCT.unpack_from(data, position)
                        position += blf_io.OBJ_HEADER_V1_STRUCT.size
                    elif header_version == 2:
                        flags, _, _, timestamp_raw = blf_io.OBJ_HEADER_V2_STRUCT.unpack_from(data, position)
                        position += blf_io.OBJ_HEADER_V2_STRUCT.size
                    else:
                        position = next_position
                        continue

                    factor = 1e-5 if flags == 1 else 1e-9
                    timestamp = timestamp_raw * factor

                    if object_type in (blf_io.CAN_MESSAGE, blf_io.CAN_MESSAGE2):
                        channel, msg_flags, dlc, can_id, can_data = blf_io.CAN_MSG_STRUCT.unpack_from(
                            data, position
                        )
                        yield BlfMessage(
                            timestamp=timestamp,
                            channel=channel - 1,
                            arbitration_id=can_id & 0x1FFFFFFF,
                            is_extended_id=bool(can_id & blf_io.CAN_MSG_EXT),
                            is_fd=False,
                            dlc=dlc,
                            data=can_data[:dlc],
                            file_position=container_file_position,
                            file_size=file_size,
                        )
                    elif object_type == blf_io.CAN_FD_MESSAGE:
                        (
                            channel,
                            _,
                            dlc,
                            can_id,
                            _,
                            _,
                            fd_flags,
                            valid_bytes,
                            can_data,
                        ) = blf_io.CAN_FD_MSG_STRUCT.unpack_from(data, position)
                        payload_len = int(valid_bytes) or int(blf_io.dlc2len(dlc))
                        yield BlfMessage(
                            timestamp=timestamp,
                            channel=channel - 1,
                            arbitration_id=can_id & 0x1FFFFFFF,
                            is_extended_id=bool(can_id & blf_io.CAN_MSG_EXT),
                            is_fd=bool(fd_flags & 0x1),
                            dlc=blf_io.dlc2len(dlc),
                            data=can_data[:payload_len],
                            file_position=container_file_position,
                            file_size=file_size,
                        )
                    elif object_type == blf_io.CAN_FD_MESSAGE_64:
                        (
                            channel,
                            dlc,
                            valid_bytes,
                            _,
                            can_id,
                            _,
                            _,
                            _,
                            _,
                            _,
                            _,
                            _,
                            _,
                            _,
                            _,
                        ) = blf_io.CAN_FD_MSG_64_STRUCT.unpack_from(data, position)
                        payload_start = position + blf_io.CAN_FD_MSG_64_STRUCT.size
                        payload_len = int(valid_bytes) or int(blf_io.dlc2len(dlc))
                        yield BlfMessage(
                            timestamp=timestamp,
                            channel=channel - 1,
                            arbitration_id=can_id & 0x1FFFFFFF,
                            is_extended_id=bool(can_id & blf_io.CAN_MSG_EXT),
                            is_fd=True,
                            dlc=blf_io.dlc2len(dlc),
                            data=data[payload_start : payload_start + payload_len],
                            file_position=container_file_position,
                            file_size=file_size,
                        )

                    position = next_position

    with path.open("rb") as handle:
        header_data = handle.read(blf_io.FILE_HEADER_STRUCT.size)
    header = blf_io.FILE_HEADER_STRUCT.unpack(header_data)
    if header[0] != b"LOGG":
        raise RuntimeError(f"Not a BLF file: {path}")
    start_timestamp, start_time_warning = safe_blf_start_timestamp(header[14:22])
    return BlfLog(
        start_timestamp=start_timestamp,
        messages=generator(),
        start_time_warning=start_time_warning,
    )


def split_values(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        for item in str(value).split(","):
            item = item.strip()
            if item:
                result.append(item)
    return result


def parse_frame_ids(values: Iterable[str]) -> set:
    parsed = set()
    for value in split_values(values):
        parsed.add(int(value, 0))
    return parsed


def read_messages_file(path: Optional[Path]) -> List[str]:
    if path is None:
        return []
    names: List[str] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            clean = line.split("#", 1)[0].strip()
            if clean:
                names.append(clean)
    return names


def frame_signature(frame: object) -> Tuple:
    signals = tuple(
        (
            str(sig.name),
            int(sig.start_bit),
            int(sig.size),
            bool(sig.is_little_endian),
            str(sig.factor),
            str(sig.offset),
            str(sig.unit),
        )
        for sig in frame.signals
    )
    return (
        int(frame.arbitration_id.id),
        bool(frame.arbitration_id.extended),
        str(frame.name),
        int(frame.size),
        signals,
    )


def load_frames(arxml_path: Path) -> List[FrameDef]:
    cluster = canmatrix.formats.loadp(str(arxml_path))
    if hasattr(cluster, "items"):
        databases = list(cluster.items())
    else:
        databases = [("", cluster)]

    frames: List[FrameDef] = []
    seen = set()
    ordinal = 0
    for cluster_name, database in databases:
        for frame in database.frames:
            # The same CAN ID and frame layout may legally exist on different
            # vehicle networks. Keep those definitions separate; the BLF
            # physical-channel router resolves the correct source later.
            signature = (str(cluster_name), frame_signature(frame))
            if signature in seen:
                continue
            seen.add(signature)
            frames.append(FrameDef(str(cluster_name), frame, ordinal))
            ordinal += 1
    return frames


def signal_receivers(frame: object) -> set:
    receivers = set(str(item) for item in getattr(frame, "receivers", []) or [])
    for signal in getattr(frame, "signals", []) or []:
        receivers.update(str(item) for item in getattr(signal, "receivers", []) or [])
    return receivers


def frame_matches_node(frame: object, nodes: set) -> bool:
    if not nodes:
        return True
    transmitters = set(str(item) for item in getattr(frame, "transmitters", []) or [])
    receivers = signal_receivers(frame)
    return bool(nodes & (transmitters | receivers))


def select_frames(
    frames: Sequence[FrameDef],
    message_names: set,
    frame_ids: set,
    nodes: set,
) -> List[FrameDef]:
    selected: List[FrameDef] = []
    for frame_def in frames:
        if message_names and frame_def.name not in message_names:
            continue
        if frame_ids and frame_def.frame_id not in frame_ids:
            continue
        if not frame_matches_node(frame_def.frame, nodes):
            continue
        selected.append(frame_def)
    return selected


def normalize_channel(value: object) -> str:
    return str(value)


def choose_candidates(
    candidates: Sequence[FrameDef],
    payload_len: int,
    strategy: str,
    allow_excess: bool,
    pad_truncated: bool,
) -> List[FrameDef]:
    if not candidates:
        return []

    preferred = [item for item in candidates if is_preferred_payload(item, payload_len)]
    if strategy in ("exact-first", "exact-only") and preferred:
        return preferred
    if strategy == "exact-only":
        return []

    compatible: List[FrameDef] = []
    for item in candidates:
        if (
            payload_len < item.size
            and not pad_truncated
            and not supports_short_payload(item, payload_len)
        ):
            continue
        if payload_len > item.size and not allow_excess:
            continue
        compatible.append(item)
    return compatible if strategy == "all" else compatible[:1]


def build_channel_routes(
    selected: Sequence[FrameDef],
    observed: Counter,
    strategy: str,
    allow_excess: bool,
    pad_truncated: bool,
) -> Tuple[Dict[Tuple[str, int, bool, int], FrameDef], Dict[str, object]]:
    """Resolve every observed physical CAN frame to one ARXML definition.

    BLF only stores physical channel numbers while ARXML separates logical
    networks. Shared CAN IDs therefore cannot be decoded from a global ID
    lookup. Unique frame signatures establish channel/network affinity; the
    affinity then disambiguates shared IDs.
    """

    by_id: Dict[Tuple[int, bool], List[FrameDef]] = defaultdict(list)
    for frame_def in selected:
        by_id[(frame_def.frame_id, frame_def.extended)].append(frame_def)

    cluster_votes: Dict[str, Counter] = defaultdict(Counter)
    cluster_vote_totals: Counter = Counter()
    exact_candidates: Dict[Tuple[str, int, bool, int], List[FrameDef]] = {}

    for observed_key, count in observed.items():
        channel, frame_id, extended, payload_len = observed_key
        exact = [
            item
            for item in by_id.get((frame_id, extended), [])
            if is_preferred_payload(item, payload_len)
        ]
        exact_candidates[observed_key] = exact
        clusters = {item.cluster for item in exact}
        if len(clusters) == 1:
            cluster_name = next(iter(clusters))
            cluster_votes[cluster_name][channel] += int(count)
            cluster_vote_totals[cluster_name] += int(count)

    frame_channel_counts: Dict[int, Counter] = defaultdict(Counter)
    for observed_key, count in observed.items():
        channel = observed_key[0]
        for frame_def in exact_candidates.get(observed_key, []):
            frame_channel_counts[frame_def.ordinal][channel] += int(count)

    def route_score(
        frame_def: FrameDef,
        channel: str,
        observed_count: int,
        payload_len: int,
    ) -> Tuple[float, int, int, int, int]:
        total_votes = int(cluster_vote_totals.get(frame_def.cluster, 0))
        direct_votes = int(cluster_votes[frame_def.cluster].get(channel, 0))
        affinity = direct_votes / total_votes if total_votes else 0.0
        exact_size = int(is_preferred_payload(frame_def, payload_len))
        return (
            affinity,
            direct_votes,
            exact_size,
            int(observed_count),
            -frame_def.ordinal,
        )

    frame_owner: Dict[int, str] = {}
    for frame_def in selected:
        channel_counts = frame_channel_counts.get(frame_def.ordinal)
        if not channel_counts:
            continue
        frame_owner[frame_def.ordinal] = max(
            channel_counts,
            key=lambda channel: route_score(
                frame_def,
                channel,
                int(channel_counts[channel]),
                frame_def.size,
            ),
        )

    routes: Dict[Tuple[str, int, bool, int], FrameDef] = {}
    ambiguous_routes = 0
    unresolved_routes = 0
    fallback_routes = 0
    owner_rejected_routes = 0
    routed_frame_counts: Dict[str, Counter] = defaultdict(Counter)
    routed_signature_counts: Dict[str, Counter] = defaultdict(Counter)

    for observed_key, count in observed.items():
        channel, frame_id, extended, payload_len = observed_key
        # Keep every size-compatible definition until channel/network routing
        # has scored it. Shared CAN IDs must not be bound to the first ARXML
        # network before the physical BLF channel is considered.
        candidates = choose_candidates(
            by_id.get((frame_id, extended), []),
            payload_len,
            "all",
            allow_excess,
            pad_truncated,
        )
        if not candidates:
            unresolved_routes += 1
            continue

        owned = [
            item
            for item in candidates
            if frame_owner.get(item.ordinal) == channel
        ]
        unowned = [
            item
            for item in candidates
            if item.ordinal not in frame_owner
        ]
        pool = owned or unowned
        if not pool:
            # Every compatible definition belongs to another physical BLF
            # channel. Reusing one here would decode a shared CAN ID against
            # the wrong logical network and create conflicting signal data.
            owner_rejected_routes += 1
            unresolved_routes += 1
            continue
        if not owned and len(candidates) > 1:
            fallback_routes += 1
        if len(pool) > 1:
            ambiguous_routes += 1

        winner = max(
            pool,
            key=lambda item: route_score(
                item,
                channel,
                int(count),
                payload_len,
            ),
        )
        routes[observed_key] = winner
        routed_frame_counts[channel][winner.cluster] += int(count)
        routed_signature_counts[channel][winner.cluster] += 1

    affinity_summary = {}
    for cluster_name in sorted(cluster_votes):
        total = int(cluster_vote_totals.get(cluster_name, 0))
        affinity_summary[cluster_name] = [
            {
                "channel": channel,
                "votes": int(votes),
                "share": round(int(votes) / total, 6) if total else 0.0,
            }
            for channel, votes in cluster_votes[cluster_name].most_common()
        ]

    diagnostics = {
        "observed_frame_signatures": len(observed),
        "resolved_routes": len(routes),
        "unresolved_routes": unresolved_routes,
        "ambiguous_routes_resolved": ambiguous_routes,
        "fallback_routes": fallback_routes,
        "owner_rejected_routes": owner_rejected_routes,
        "logical_frame_channel_owners": {
            channel: sum(
                1 for owner_channel in frame_owner.values()
                if owner_channel == channel
            )
            for channel in sorted(set(frame_owner.values()))
        },
        "cluster_channel_affinity": affinity_summary,
        "resolved_channel_networks": {
            channel: [
                {
                    "network": cluster_name,
                    "frames": int(frame_count),
                    "signatures": int(
                        routed_signature_counts[channel].get(cluster_name, 0)
                    ),
                }
                for cluster_name, frame_count in counts.most_common()
            ]
            for channel, counts in sorted(routed_frame_counts.items())
        },
    }
    return routes, diagnostics


def channel_group_name(channel: object, frame_def: FrameDef, prefix_channel: bool) -> str:
    if prefix_channel:
        return f"CAN{normalize_channel(channel)}.{frame_def.name}"
    return frame_def.name


def conversion_for_signal(signal: object):
    try:
        unit = str(getattr(signal, "unit", "") or "")
        choices = getattr(signal, "values", None) or {}
        if choices:
            conversion: Dict[str, object] = {"name": "", "unit": unit, "comment": ""}
            for index, raw_value in enumerate(sorted(choices, key=lambda item: int(item))):
                conversion[f"val_{index}"] = float(raw_value)
                conversion[f"text_{index}"] = str(choices[raw_value])
            conversion["default_addr"] = ""
            return from_dict(conversion)

        factor = float(getattr(signal, "factor", 1) or 1)
        offset = float(getattr(signal, "offset", 0) or 0)
        if factor != 1.0 or offset != 0.0:
            return from_dict(
                {
                    "name": "",
                    "unit": unit,
                    "comment": "",
                    "a": factor,
                    "b": offset,
                }
            )
    except Exception as exc:
        print(
            f"WARN: signal conversion ignored for {getattr(signal, 'name', '<unknown>')}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    return None


def required_payload_bytes(signal: object) -> int:
    big_endian = not bool(getattr(signal, "is_little_endian"))
    start_bit = int(signal.get_startbit(bit_numbering=1))
    bit_count = int(signal.size)

    if big_endian:
        byte_pos = start_bit // 8 + 1
        start_pos = start_bit
        bits = bit_count

        while True:
            pos = start_pos % 8 + 1
            if pos < bits:
                byte_pos += 1
                bits -= pos
                start_pos = 7
            else:
                break
        return byte_pos

    return (start_bit + bit_count + 7) // 8


def signal_comment(signal: object) -> str:
    comment = getattr(signal, "comment", "") or ""
    if isinstance(comment, dict):
        return str(comment.get("EN") or next(iter(comment.values()), ""))
    return str(comment)


def make_payload_array(payloads: Sequence[bytes], width: int) -> np.ndarray:
    data = np.zeros((len(payloads), width), dtype=np.uint8)
    for index, payload in enumerate(payloads):
        row = payload[:width]
        data[index, : len(row)] = np.frombuffer(row, dtype=np.uint8)
    return data


def is_time_channel(name: str) -> bool:
    return name in {"t", "time", "timestamps"}


def load_template(path: Optional[Path]) -> Optional[TemplateIndex]:
    if path is None:
        return None

    mdf = MDF(str(path))
    groups_by_name: Dict[str, int] = {}
    group_order: Dict[str, int] = {}
    channels_by_group: Dict[str, Dict[str, int]] = {}
    channel_order_by_group: Dict[str, List[str]] = {}

    for group_index, group in enumerate(mdf.groups):
        name = str(getattr(group.channel_group, "acq_name", "") or f"group_{group_index}")
        if name in groups_by_name:
            continue
        groups_by_name[name] = group_index
        group_order[name] = group_index

        channel_lookup: Dict[str, int] = {}
        channel_order: List[str] = []
        for channel_index, channel in enumerate(group.channels):
            channel_name = str(channel.name)
            if is_time_channel(channel_name):
                continue
            channel_lookup[channel_name] = channel_index
            channel_order.append(channel_name)
        channels_by_group[name] = channel_lookup
        channel_order_by_group[name] = channel_order

    return TemplateIndex(
        mdf=mdf,
        groups_by_name=groups_by_name,
        group_order=group_order,
        channels_by_group=channels_by_group,
        channel_order_by_group=channel_order_by_group,
    )


def arrays_equal(left: np.ndarray, right: np.ndarray) -> bool:
    return left.shape == right.shape and np.array_equal(left, right)


def clone_template_signal(template: TemplateIndex, group_name: str, channel_name: str) -> Signal:
    group_index = template.groups_by_name[group_name]
    channel_index = template.channels_by_group[group_name][channel_name]
    source = template.mdf.get(group=group_index, index=channel_index, raw=True)
    return Signal(
        samples=np.array(source.samples, copy=True),
        timestamps=np.array(source.timestamps, copy=True),
        unit=source.unit,
        name=source.name,
        conversion=source.conversion,
        comment=source.comment,
        raw=True,
        master_metadata=("t", v4c.SYNC_TYPE_TIME),
        display_names=source.display_names,
        source=source.source,
        bit_count=source.bit_count,
        encoding=source.encoding,
    )


def append_group(
    mdf: MDF,
    group: GroupBuffer,
    prefix_channel: bool,
    pad_truncated: bool,
    decode_incomplete_signals: bool,
    template: Optional[TemplateIndex],
    ensure_template_content: bool,
    allowed_signal_names: Optional[Set[str]] = None,
) -> Tuple[int, List[str], List[str], List[str]]:
    frame = group.frame_def.frame
    name = channel_group_name(group.channel, group.frame_def, prefix_channel)
    max_payload_len = max(len(payload) for payload in group.payloads)
    min_payload_len = min(len(payload) for payload in group.payloads)
    short_payload_compatibility = any(
        supports_short_payload(group.frame_def, len(payload))
        for payload in group.payloads
    )
    width = (
        max(int(frame.size), max_payload_len)
        if pad_truncated or short_payload_compatibility
        else max_payload_len
    )
    payload = make_payload_array(group.payloads, width)
    timestamps = np.asarray(group.timestamps, dtype=np.float64)

    decoded_by_name: Dict[str, Signal] = {}
    skipped: List[str] = []
    skipped_incomplete: List[str] = []
    template_overlays: List[str] = []

    for source_signal in frame.signals:
        signal_name = str(source_signal.name)
        if allowed_signal_names is not None and signal_name not in allowed_signal_names:
            continue
        if pad_truncated and not decode_incomplete_signals:
            if required_payload_bytes(source_signal) > min_payload_len:
                skipped.append(signal_name)
                skipped_incomplete.append(signal_name)
                continue

        try:
            samples = extract_signal(
                source_signal,
                payload,
                raw=True,
                ignore_value2text_conversion=True,
            )
        except Exception:
            skipped.append(signal_name)
            continue

        unit = str(getattr(source_signal, "unit", "") or "")
        signal = Signal(
            samples=samples,
            timestamps=timestamps,
            unit=unit,
            name=signal_name,
            conversion=conversion_for_signal(source_signal),
            comment=signal_comment(source_signal),
            raw=True,
            master_metadata=("t", v4c.SYNC_TYPE_TIME),
            bit_count=int(source_signal.size),
        )
        decoded_by_name[signal_name] = signal

    output_order: List[str] = []
    if template and name in template.channel_order_by_group:
        output_order.extend(
            signal_name
            for signal_name in template.channel_order_by_group[name]
            if allowed_signal_names is None or signal_name in allowed_signal_names
        )

    for source_signal in frame.signals:
        signal_name = str(source_signal.name)
        if allowed_signal_names is not None and signal_name not in allowed_signal_names:
            continue
        if signal_name not in output_order:
            output_order.append(signal_name)

    signals: List[Signal] = []
    for signal_name in output_order:
        if allowed_signal_names is not None and signal_name not in allowed_signal_names:
            continue
        decoded = decoded_by_name.get(signal_name)
        if template and ensure_template_content:
            template_group = template.channels_by_group.get(name, {})
            if signal_name in template_group:
                template_signal = clone_template_signal(template, name, signal_name)
                if decoded is None:
                    template_overlays.append(signal_name)
                    signals.append(template_signal)
                    continue

                if not arrays_equal(decoded.timestamps, template_signal.timestamps) or not arrays_equal(
                    decoded.samples, template_signal.samples
                ):
                    template_overlays.append(signal_name)
                    signals.append(template_signal)
                    continue

        if decoded is not None:
            signals.append(decoded)

    if signals:
        try:
            mdf.append(
                signals,
                acq_name=name,
                comment=(
                    f"CAN channel={normalize_channel(group.channel)} "
                    f"id=0x{group.frame_def.frame_id:X} "
                    f"extended={group.frame_def.extended} "
                    f"arxml_cluster={group.frame_def.cluster}"
                ),
                common_timebase=True,
            )
        except Exception as exc:
            signal_names = ", ".join(signal.name for signal in signals[:20])
            if len(signals) > 20:
                signal_names += ", ..."
            skipped.extend(signal.name for signal in signals)
            print(
                "WARN: MDF append skipped for "
                f"{name} channel={normalize_channel(group.channel)} "
                f"id=0x{group.frame_def.frame_id:X}: {type(exc).__name__}: {exc}; "
                f"signals={signal_names}",
                file=sys.stderr,
            )
            return 0, skipped, skipped_incomplete, template_overlays

    return len(signals), skipped, skipped_incomplete, template_overlays


def build_unique_signal_ownership(
    groups: Dict[Tuple[object, int], GroupBuffer],
    route_diagnostics: Dict[str, object],
) -> Tuple[Dict[Tuple[object, int], Set[str]], Dict[str, object]]:
    """Assign every business signal to one routed physical CAN group."""

    affinity_by_cluster_channel: Dict[Tuple[str, str], Tuple[float, int]] = {}
    affinity = route_diagnostics.get("cluster_channel_affinity", {})
    if isinstance(affinity, dict):
        for cluster, rows in affinity.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                channel = normalize_channel(row.get("channel", ""))
                affinity_by_cluster_channel[(str(cluster), channel)] = (
                    float(row.get("share", 0.0) or 0.0),
                    int(row.get("votes", 0) or 0),
                )

    candidates_by_signal: Dict[str, List[Tuple[object, int]]] = defaultdict(list)
    for key, group in groups.items():
        for source_signal in group.frame_def.frame.signals:
            signal_name = str(source_signal.name)
            if key not in candidates_by_signal[signal_name]:
                candidates_by_signal[signal_name].append(key)

    allowed_by_group: Dict[Tuple[object, int], Set[str]] = {
        key: set() for key in groups
    }
    duplicate_details: Dict[str, object] = {}

    def owner_score(key: Tuple[object, int]) -> Tuple[int, float, int, int, str]:
        group = groups[key]
        channel = normalize_channel(group.channel)
        share, votes = affinity_by_cluster_channel.get(
            (group.frame_def.cluster, channel),
            (0.0, 0),
        )
        return (
            len(group.timestamps),
            share,
            votes,
            -group.frame_def.ordinal,
            channel,
        )

    for signal_name, candidate_keys in candidates_by_signal.items():
        owner = max(candidate_keys, key=owner_score)
        allowed_by_group[owner].add(signal_name)
        if len(candidate_keys) <= 1:
            continue

        duplicate_details[signal_name] = {
            "owner": {
                "channel": normalize_channel(groups[owner].channel),
                "network": groups[owner].frame_def.cluster,
                "frame_id": f"0x{groups[owner].frame_def.frame_id:X}",
                "samples": len(groups[owner].timestamps),
            },
            "discarded": [
                {
                    "channel": normalize_channel(groups[key].channel),
                    "network": groups[key].frame_def.cluster,
                    "frame_id": f"0x{groups[key].frame_def.frame_id:X}",
                    "samples": len(groups[key].timestamps),
                }
                for key in candidate_keys
                if key != owner
            ],
        }

    redundant_groups = [
        {
            "channel": normalize_channel(groups[key].channel),
            "network": groups[key].frame_def.cluster,
            "frame_id": f"0x{groups[key].frame_def.frame_id:X}",
            "name": groups[key].frame_def.name,
            "samples": len(groups[key].timestamps),
        }
        for key, allowed in allowed_by_group.items()
        if not allowed
    ]
    return allowed_by_group, {
        "duplicate_signal_names_resolved": len(duplicate_details),
        "redundant_groups_dropped": len(redundant_groups),
        "redundant_groups": redundant_groups,
        "signal_owners": duplicate_details,
    }


def convert(args: argparse.Namespace) -> Dict[str, object]:
    if args.output.exists() and not args.overwrite:
        raise FileExistsError(f"Output file already exists: {args.output}")
    if args.ensure_template_content and not args.template_mdf:
        raise RuntimeError("--ensure-template-content requires --template-mdf")

    progress = ProgressEmitter(args.progress_jsonl, args.progress_interval_seconds)
    channels = set(split_values(args.channel))
    message_names = set(split_values(args.message) + read_messages_file(args.messages_file))
    frame_ids = parse_frame_ids(args.frame_id)
    nodes = set(split_values(args.node))
    allow_excess = not args.no_allow_excess
    template = load_template(args.template_mdf)

    frames = load_frames(args.arxml)
    selected = select_frames(frames, message_names, frame_ids, nodes)
    if not selected:
        raise RuntimeError("No ARXML messages matched the requested filters.")

    observed: Counter = Counter()
    scanned_frames = 0
    last_scanned_msg: Optional[BlfMessage] = None
    progress.emit("scan", total_frames=0, force=True)
    for msg in iter_blf_messages(args.blf).messages:
        if args.max_frames and scanned_frames >= args.max_frames:
            break
        scanned_frames += 1
        last_scanned_msg = msg
        progress.emit("scan", message=msg, total_frames=scanned_frames)
        channel = normalize_channel(msg.channel)
        if channels and channel not in channels:
            continue
        observed[
            (
                channel,
                int(msg.arbitration_id),
                bool(msg.is_extended_id),
                len(bytes(msg.data)),
            )
        ] += 1
    progress.emit("scan", message=last_scanned_msg, total_frames=scanned_frames, force=True)

    routes, route_diagnostics = build_channel_routes(
        selected,
        observed,
        args.match_strategy,
        allow_excess,
        args.pad_truncated,
    )

    groups: Dict[Tuple[object, int], GroupBuffer] = {}
    blf_log = iter_blf_messages(args.blf)
    first_timestamp: Optional[float] = None
    base_offset: Optional[float] = None
    total_frames = 0
    candidate_frames = 0
    matched_frames = 0
    skipped_short = 0
    skipped_excess = 0

    progress.emit("convert", total_frames=0, force=True)
    last_converted_msg: Optional[BlfMessage] = None
    for msg in blf_log.messages:
        if args.max_frames and total_frames >= args.max_frames:
            break
        total_frames += 1
        last_converted_msg = msg
        progress.emit(
            "convert",
            message=msg,
            total_frames=total_frames,
            candidate_frames=candidate_frames,
            matched_frames=matched_frames,
        )

        if first_timestamp is None:
            first_timestamp = float(msg.timestamp)
            if args.time_base in ("blf-start", "floor-second"):
                base_offset = 0.0
            else:
                base_offset = first_timestamp

        if channels and normalize_channel(msg.channel) not in channels:
            continue

        payload = bytes(msg.data)
        route_key = (
            normalize_channel(msg.channel),
            int(msg.arbitration_id),
            bool(msg.is_extended_id),
            len(payload),
        )
        frame_def = routes.get(route_key)
        if frame_def is None:
            continue

        candidate_frames += 1

        assert base_offset is not None
        timestamp = float(msg.timestamp) - base_offset

        if (
            len(payload) < frame_def.size
            and not args.pad_truncated
            and not supports_short_payload(frame_def, len(payload))
        ):
            skipped_short += 1
            continue
        if len(payload) > frame_def.size and not allow_excess:
            skipped_excess += 1
            continue
        # The physical BLF channel must remain part of the internal route key even
        # when output names are not channel-prefixed. Otherwise equal CAN IDs from
        # different networks are merged before their network ownership is known.
        group_channel = normalize_channel(msg.channel)
        key = (group_channel, frame_def.ordinal)
        if key not in groups:
            groups[key] = GroupBuffer(frame_def, group_channel, [], [])
        groups[key].timestamps.append(timestamp)
        groups[key].payloads.append(payload)
        matched_frames += 1
    progress.emit(
        "convert",
        message=last_converted_msg,
        total_frames=total_frames,
        candidate_frames=candidate_frames,
        matched_frames=matched_frames,
        force=True,
    )

    if first_timestamp is None or base_offset is None:
        raise RuntimeError("The BLF file did not contain any frames.")

    mdf = MDF(version=args.mdf_version)
    mdf_start_timestamp = blf_log.start_timestamp + base_offset
    timestamp_warning = blf_log.start_time_warning
    if template and args.ensure_template_content:
        mdf.header.start_time = template.mdf.start_time
    else:
        try:
            mdf.header.start_time = _timestamp_to_datetime(mdf_start_timestamp)
        except Exception as exc:
            timestamp_warning = (
                "MDF start_time is out of range; fell back to Unix epoch. "
                f"Relative sample timestamps were preserved. ({exc})"
            )
            mdf.header.start_time = systemtime_to_datetime(0)
    group_summaries = []
    skipped_signals: Dict[str, List[str]] = {}
    template_overlays: Dict[str, List[str]] = {}
    allowed_signals, ownership_diagnostics = build_unique_signal_ownership(
        groups,
        route_diagnostics,
    )
    written_frame_samples = 0

    def group_sort_key(item):
        name = channel_group_name(groups[item].channel, groups[item].frame_def, args.prefix_channel)
        if template and name in template.group_order:
            return (0, template.group_order[name], str(groups[item].channel))
        return (1, groups[item].frame_def.ordinal, str(groups[item].channel))

    for key in sorted(groups, key=group_sort_key):
        group = groups[key]
        group_allowed_signals = allowed_signals.get(key, set())
        if not group_allowed_signals:
            continue
        channel_count, skipped, skipped_incomplete, overlays = append_group(
            mdf,
            group,
            args.prefix_channel,
            args.pad_truncated,
            args.decode_incomplete_signals,
            template,
            args.ensure_template_content,
            group_allowed_signals,
        )
        name = channel_group_name(group.channel, group.frame_def, args.prefix_channel)
        if skipped:
            skipped_signals[name] = skipped
        if overlays:
            template_overlays[name] = overlays
        if channel_count:
            written_frame_samples += len(group.timestamps)
            payload_lengths = {
                str(length): sum(1 for payload in group.payloads if len(payload) == length)
                for length in sorted({len(payload) for payload in group.payloads})
            }
            group_summaries.append(
                {
                    "name": name,
                    "frame_id": f"0x{group.frame_def.frame_id:X}",
                    "extended": group.frame_def.extended,
                    "channel": normalize_channel(group.channel),
                    "arxml_cluster": group.frame_def.cluster,
                    "samples": len(group.timestamps),
                    "payload_min_bytes": min(len(payload) for payload in group.payloads),
                    "payload_max_bytes": max(len(payload) for payload in group.payloads),
                    "payload_lengths": payload_lengths,
                    "signals": channel_count,
                    "skipped_signals": len(skipped),
                    "skipped_incomplete_signals": len(skipped_incomplete),
                    "template_overlays": len(overlays),
                }
            )

    progress.emit(
        "write",
        message=last_converted_msg,
        total_frames=total_frames,
        candidate_frames=candidate_frames,
        matched_frames=matched_frames,
        force=True,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    timestamp_warning = append_warning(timestamp_warning, repair_mdf_header_for_save(mdf))
    output_start_time = mdf.header.start_time
    try:
        saved_path = Path(mdf.save(str(args.output), overwrite=args.overwrite, compression=args.compression))
    except Exception as exc:
        first_save_error = f"{type(exc).__name__}: {exc}"
        remove_partial_output(args.output)
        timestamp_warning = append_warning(
            timestamp_warning,
            repair_mdf_header_for_save(mdf, force_epoch=True, reason=first_save_error),
        )
        output_start_time = mdf.header.start_time
        try:
            saved_path = Path(mdf.save(str(args.output), overwrite=args.overwrite, compression=0))
        except Exception as retry_exc:
            group_names = ", ".join(str(item.get("name", "")) for item in group_summaries[:20])
            if len(group_summaries) > 20:
                group_names += ", ..."
            raise RuntimeError(
                f"MDF save failed after writing {len(group_summaries)} groups "
                f"({group_names}): first save {first_save_error}; "
                f"retry with safe MDF header {type(retry_exc).__name__}: {retry_exc}"
            ) from retry_exc
    mdf.close()
    if saved_path.resolve() != args.output.resolve():
        if args.output.exists():
            args.output.unlink()
        saved_path.replace(args.output)

    summary = {
        "input_blf": str(args.blf),
        "input_arxml": str(args.arxml),
        "output": str(args.output),
        "mdf_version": args.mdf_version,
        "start_time": output_start_time.isoformat(),
        "timestamp_warning": timestamp_warning,
        "selected_arxml_messages": len(selected),
        "total_blf_frames_read": total_frames,
        "blf_frames_with_matching_id": candidate_frames,
        "matched_frames_routed": matched_frames,
        "matched_frames_written": written_frame_samples,
        "channel_routing": route_diagnostics,
        "signal_ownership": ownership_diagnostics,
        "groups_written": len(group_summaries),
        "skipped_short_frames": skipped_short,
        "skipped_excess_frames": skipped_excess,
        "groups": group_summaries,
        "skipped_signals": skipped_signals,
        "template_mdf": str(args.template_mdf) if args.template_mdf else None,
        "template_overlays": template_overlays,
    }
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if template:
        template.mdf.close()
    progress.emit(
        "done",
        message=last_converted_msg,
        total_frames=total_frames,
        candidate_frames=candidate_frames,
        matched_frames=matched_frames,
        force=True,
    )
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        summary = convert(args)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
