from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
import re

import h5py


SignalRecord = dict
SignalDict = dict


@dataclass
class SceneSegment:
    start_index: int
    end_index: int
    start_timestamp: float
    end_timestamp: float
    duration: float
    sample_count: int


class FolderSession:
    def __init__(self, primary_h5_file: str | Path, preprocess_signals: list[str] | None = None) -> None:
        self.primary_h5_file = str(primary_h5_file)
        self.store = H5SignalStore.from_hdf5(primary_h5_file)
        self.preprocess_signals = list(preprocess_signals or [])
        self._slice_cache: dict[tuple[str, float, float], tuple[list[float], list[object]]] = {}
        self._sample_cache: dict[tuple[str, tuple[float, ...], str, float | None, float | None], list[object]] = {}
        self._preload_configured_signals()

    def get_store(self) -> "H5SignalStore":
        return self.store

    def has_signal(self, signal_name: str) -> bool:
        return self.store.has_signal(signal_name)

    def missing_signals(self, signal_names: list[str]) -> list[str]:
        return [signal_name for signal_name in signal_names if not self.has_signal(signal_name)]

    def common_time_range(self, signal_names: list[str]) -> tuple[float, float]:
        start_timestamp = max(_timestamps(self.store.get_signal(name))[0] for name in signal_names)
        end_timestamp = min(_timestamps(self.store.get_signal(name))[-1] for name in signal_names)
        if end_timestamp < start_timestamp:
            raise ValueError("No overlapping time range across the requested signals")
        return float(start_timestamp), float(end_timestamp)

    def get_signal_slice(
        self,
        signal_name: str,
        start_timestamp: float,
        end_timestamp: float,
    ) -> tuple[list[float], list[object]]:
        cache_key = (signal_name, float(start_timestamp), float(end_timestamp))
        if cache_key in self._slice_cache:
            return self._slice_cache[cache_key]

        signal = self.store.get_signal(signal_name)
        timestamps: list[float] = []
        values: list[object] = []
        for timestamp, value in zip(_timestamps(signal), _values(signal)):
            timestamp = float(timestamp)
            if start_timestamp <= timestamp <= end_timestamp:
                timestamps.append(timestamp)
                values.append(value)
        self._slice_cache[cache_key] = (timestamps, values)
        return self._slice_cache[cache_key]

    def sample_signal_to_axis(
        self,
        signal_name: str,
        target_timestamps: list[float],
        method: str = "nearest",
        start_timestamp: float | None = None,
        end_timestamp: float | None = None,
    ) -> list[object]:
        cache_key = (
            signal_name,
            tuple(target_timestamps),
            method,
            None if start_timestamp is None else float(start_timestamp),
            None if end_timestamp is None else float(end_timestamp),
        )
        if cache_key in self._sample_cache:
            return self._sample_cache[cache_key]

        if start_timestamp is None or end_timestamp is None:
            signal = self.store.get_signal(signal_name)
            source_timestamps = _timestamps(signal)
            source_values = _values(signal)
        else:
            source_timestamps, source_values = self.get_signal_slice(signal_name, start_timestamp, end_timestamp)

        sampled = sample_signal(source_timestamps, source_values, target_timestamps, method=method)
        self._sample_cache[cache_key] = sampled
        return sampled


    def _preload_configured_signals(self) -> None:
        for signal_name in self.preprocess_signals:
            try:
                if not self.store.has_signal(signal_name):
                    continue
                signal = self.store.get_signal(signal_name)
            except KeyError:
                continue

            timestamps = _timestamps(signal)
            values = _values(signal)
            if not timestamps:
                continue
            self._slice_cache[(signal_name, float(timestamps[0]), float(timestamps[-1]))] = (
                timestamps[:],
                values[:],
            )


class H5SignalStore:
    def __init__(
        self,
        signals: SignalDict,
        source_path: str,
        base_group: str,
        channel_name: str = "Channel1",
    ) -> None:
        self.signals = signals
        self.source_path = source_path
        self.base_group = base_group
        self.channel_name = channel_name

    @classmethod
    def from_hdf5(
        cls,
        file_path: str | Path,
        channel_name: str = "Channel1",
        use_message_prefix: bool = True,
    ) -> "H5SignalStore":
        signals, base_group = load_signal_dict(
            file_path=file_path,
            channel_name=channel_name,
            use_message_prefix=use_message_prefix,
        )
        return cls(
            signals=signals,
            source_path=str(file_path),
            base_group=base_group,
            channel_name=channel_name,
        )

    def list_signals(self) -> list[str]:
        return sorted(self.signals.keys())

    def has_signal(self, signal_name: str) -> bool:
        return self._resolve_signal_key(signal_name) is not None

    def get_signal(self, signal_name: str) -> SignalRecord:
        resolved = self._resolve_signal_key(signal_name)
        if resolved is None:
            raise KeyError(f"Signal not found: {signal_name}")
        return self.signals[resolved]

    def _resolve_signal_key(self, signal_name: str) -> str | None:
        for candidate in _signal_lookup_variants(signal_name):
            if candidate in self.signals:
                return candidate
        return None

    def get_value_at(
        self,
        signal_name: str,
        timestamp: float,
        method: str = "nearest",
    ) -> dict[str, object]:
        signal = self.get_signal(signal_name)
        timestamps = _timestamps(signal)
        values = _values(signal)

        if not timestamps:
            raise ValueError(f"Signal has no samples: {signal_name}")

        index = _locate_index(timestamps, float(timestamp), method)
        return {
            "signal_name": signal_name,
            "query_timestamp": float(timestamp),
            "matched_timestamp": timestamps[index],
            "value": values[index],
            "index": index,
        }

    def get_range(
        self,
        signal_name: str,
        start_timestamp: float,
        end_timestamp: float,
    ) -> dict[str, object]:
        signal = self.get_signal(signal_name)
        timestamps = _timestamps(signal)
        values = _values(signal)

        left = bisect_left(timestamps, float(start_timestamp))
        right = bisect_right(timestamps, float(end_timestamp))
        return {
            "signal_name": signal_name,
            "start_timestamp": float(start_timestamp),
            "end_timestamp": float(end_timestamp),
            "timestamps": timestamps[left:right],
            "values": values[left:right],
            "size": right - left,
        }

    def align_signals(
        self,
        signal_names: list[str],
        start_timestamp: float | None = None,
        end_timestamp: float | None = None,
        step: float = 0.1,
        method: str = "nearest",
    ) -> dict[str, object]:
        if not signal_names:
            raise ValueError("signal_names must not be empty")
        if step <= 0:
            raise ValueError("step must be positive")

        if start_timestamp is None:
            start_timestamp = max(_timestamps(self.get_signal(name))[0] for name in signal_names)
        if end_timestamp is None:
            end_timestamp = min(_timestamps(self.get_signal(name))[-1] for name in signal_names)
        if end_timestamp < start_timestamp:
            raise ValueError("No overlapping time range across the requested signals")

        axis = build_time_axis(float(start_timestamp), float(end_timestamp), step)
        aligned = {"timestamp": axis}
        for name in signal_names:
            signal = self.get_signal(name)
            aligned[name] = sample_signal(
                source_timestamps=_timestamps(signal),
                source_values=_values(signal),
                target_timestamps=axis,
                method=method,
            )
        return aligned

    def find_segments(
        self,
        aligned_data: dict[str, object],
        condition: Callable[[dict[str, object]], Iterable[bool]],
        min_duration: float = 0.0,
    ) -> list[SceneSegment]:
        timestamps = aligned_data.get("timestamp", [])
        mask = [bool(item) for item in condition(aligned_data)]
        if len(mask) != len(timestamps):
            raise ValueError("condition must return a boolean list with the same length as timestamps")

        segments = mask_to_segments(timestamps, mask)
        if min_duration <= 0:
            return segments
        return [segment for segment in segments if segment.duration >= min_duration]


def load_signal_dict(
    file_path: str | Path,
    channel_name: str = "Channel1",
    use_message_prefix: bool = True,
) -> tuple[SignalDict, str]:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"HDF5 file not found: {path}")

    signal_dict: SignalDict = {}
    with h5py.File(path, "r") as h5_file:
        base_group = _first_group_name(h5_file)
        found_dataset = _load_split_signal_groups(
            h5_file,
            signal_dict,
            use_message_prefix=use_message_prefix,
        )

        # Backward compatible path for older H5 files whose messages are
        # compound datasets with a timestamp field.
        channel_path = f"{base_group}/{channel_name}"
        if channel_path in h5_file:
            dataset_root = h5_file[channel_path]
        elif base_group in h5_file:
            dataset_root = h5_file[base_group]
        else:
            dataset_root = h5_file

        for message_name, dataset in _iter_signal_datasets(dataset_root):
            found_dataset = True
            field_names = dataset.dtype.names or ()
            if "timestamp" not in field_names:
                continue

            timestamps = [float(item) for item in dataset["timestamp"][:]]
            order = sorted(range(len(timestamps)), key=timestamps.__getitem__)
            ordered_timestamps = [timestamps[index] for index in order]

            for field_name in field_names:
                if field_name == "timestamp":
                    continue

                raw_values = dataset[field_name][:]
                ordered_values = [_to_python(raw_values[index]) for index in order]
                signal_key = make_signal_key(message_name, field_name, use_message_prefix)
                signal_dict.setdefault(signal_key, {
                    "message": message_name,
                    "signal": field_name,
                    "timestamps": ordered_timestamps,
                    "values": ordered_values,
                    "dtype": str(getattr(raw_values, "dtype", type(raw_values).__name__)),
                    "size": len(ordered_values),
                })

        if not found_dataset:
            raise KeyError(f"No timestamped signal datasets found under {base_group}")

    return signal_dict, base_group


def _load_split_signal_groups(
    h5_file: h5py.File,
    signal_dict: SignalDict,
    *,
    use_message_prefix: bool,
) -> bool:
    found = False
    for message_group in _iter_split_signal_groups(h5_file):
        timestamp_dataset = message_group.get("timestamp")
        signals_group = message_group.get("signals")
        if not isinstance(timestamp_dataset, h5py.Dataset) or not isinstance(signals_group, h5py.Group):
            continue

        timestamps = [float(item) for item in timestamp_dataset[:]]
        order = sorted(range(len(timestamps)), key=timestamps.__getitem__)
        ordered_timestamps = [timestamps[index] for index in order]

        message_path = message_group.name.strip("/")
        message_name = message_path.rsplit("/", 1)[-1]
        bus_name = _bus_name_from_message_path(message_path)
        for signal_name, dataset in signals_group.items():
            if not isinstance(dataset, h5py.Dataset):
                continue
            raw_values = dataset[:]
            ordered_values = [_to_python(raw_values[index]) for index in order]
            dataset_path = dataset.name.strip("/")
            record = {
                "message": message_name,
                "message_path": message_path,
                "bus": bus_name,
                "signal": signal_name,
                "dataset_path": dataset_path,
                "timestamps": ordered_timestamps,
                "values": ordered_values,
                "dtype": str(getattr(raw_values, "dtype", type(raw_values).__name__)),
                "size": len(ordered_values),
            }
            for signal_key in _split_signal_keys(message_path, message_name, signal_name, use_message_prefix):
                signal_dict.setdefault(signal_key, record)
            found = True
    return found


def _iter_split_signal_groups(group: h5py.Group) -> Iterable[h5py.Group]:
    for item in group.values():
        if isinstance(item, h5py.Group):
            if isinstance(item.get("timestamp"), h5py.Dataset) and isinstance(item.get("signals"), h5py.Group):
                yield item
            yield from _iter_split_signal_groups(item)


def _split_signal_keys(
    message_path: str,
    message_name: str,
    signal_name: str,
    use_message_prefix: bool,
) -> list[str]:
    keys: list[str] = [f"{message_path}/signals/{signal_name}"]
    if not use_message_prefix:
        keys.append(signal_name)
    for alias in _message_aliases(message_name):
        keys.append(make_signal_key(alias, signal_name, use_message_prefix=True))
    # New parsed H5 files often shorten message names compared with the older
    # metric configuration. Keep a short-name fallback so
    # Old_Message_Name.SignalName still resolves when SignalName is unique enough
    # for the configured diagnostic logic.
    keys.append(signal_name)
    return list(dict.fromkeys(key for key in keys if key))


def _signal_lookup_variants(signal_name: str) -> list[str]:
    value = str(signal_name or "").strip()
    variants = [value]
    if "/signals/" in value:
        variants.append(value.rsplit("/signals/", 1)[-1])
    if "." in value:
        variants.append(value.rsplit(".", 1)[-1])
    if "/" in value:
        variants.append(value.rsplit("/", 1)[-1])
    expanded: list[str] = []
    for item in variants:
        if not item:
            continue
        expanded.append(item)
        if item.startswith("I") and len(item) > 1 and item[1].isupper():
            expanded.append(item[1:])
        elif item[:1].isupper():
            expanded.append(f"I{item}")
    return list(dict.fromkeys(item for item in expanded if item))


def _message_aliases(message_name: str) -> list[str]:
    value = str(message_name or "").strip()
    aliases = [value]
    no_prefix = re.sub(r"^0x[0-9A-Fa-f]+_", "", value)
    aliases.append(no_prefix)
    aliases.append(re.sub(r"_0x[0-9A-Fa-f]+$", "", no_prefix))
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _bus_name_from_message_path(message_path: str) -> str:
    parts = [part for part in message_path.split("/") if part]
    if len(parts) >= 2:
        return parts[1]
    return ""


def _iter_signal_datasets(group: h5py.Group) -> Iterable[tuple[str, h5py.Dataset]]:
    for name, item in group.items():
        if isinstance(item, h5py.Dataset):
            field_names = item.dtype.names or ()
            if "timestamp" in field_names:
                yield name, item
        elif isinstance(item, h5py.Group):
            yield from _iter_signal_datasets(item)


def build_time_axis(start_timestamp: float, end_timestamp: float, step: float) -> list[float]:
    axis: list[float] = []
    current = float(start_timestamp)
    end_value = float(end_timestamp)
    while current <= end_value + step * 1e-9:
        axis.append(round(current, 9))
        current += step
    return axis


def sample_signal(
    source_timestamps: list[float],
    source_values: list[object],
    target_timestamps: list[float],
    method: str = "nearest",
) -> list[object]:
    if method not in {"nearest", "pad", "backfill"}:
        raise ValueError("method must be one of: nearest, pad, backfill")
    if not source_timestamps:
        raise ValueError("source signal is empty")

    sampled: list[object] = []
    for timestamp in target_timestamps:
        index = _locate_index(source_timestamps, timestamp, method)
        sampled.append(source_values[index])
    return sampled


def mask_to_segments(timestamps: list[float], mask: list[bool]) -> list[SceneSegment]:
    segments: list[SceneSegment] = []
    start_index: int | None = None

    for index, active in enumerate(mask):
        if active and start_index is None:
            start_index = index
        if not active and start_index is not None:
            end_index = index - 1
            segments.append(_build_segment(timestamps, start_index, end_index))
            start_index = None

    if start_index is not None:
        segments.append(_build_segment(timestamps, start_index, len(mask) - 1))
    return segments


def make_signal_key(message_name: str, signal_name: str, use_message_prefix: bool = True) -> str:
    if use_message_prefix:
        return f"{message_name}.{signal_name}"
    return signal_name


def _build_segment(timestamps: list[float], start_index: int, end_index: int) -> SceneSegment:
    start_timestamp = timestamps[start_index]
    end_timestamp = timestamps[end_index]
    return SceneSegment(
        start_index=start_index,
        end_index=end_index,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        duration=max(0.0, end_timestamp - start_timestamp),
        sample_count=end_index - start_index + 1,
    )


def _first_group_name(h5_file: h5py.File) -> str:
    keys = list(h5_file.keys())
    if not keys:
        raise ValueError("HDF5 file is empty")
    return keys[0]


def _locate_index(timestamps: list[float], timestamp: float, method: str) -> int:
    insert_pos = bisect_left(timestamps, timestamp)

    if method == "pad":
        if insert_pos < len(timestamps) and timestamps[insert_pos] == timestamp:
            return insert_pos
        if insert_pos == 0:
            raise ValueError("No earlier sample found for pad lookup")
        return insert_pos - 1

    if method == "backfill":
        if insert_pos >= len(timestamps):
            raise ValueError("No later sample found for backfill lookup")
        return insert_pos

    if insert_pos == 0:
        return 0
    if insert_pos >= len(timestamps):
        return len(timestamps) - 1

    prev_index = insert_pos - 1
    next_index = insert_pos
    prev_gap = abs(timestamp - timestamps[prev_index])
    next_gap = abs(timestamps[next_index] - timestamp)
    if prev_gap <= next_gap:
        return prev_index
    return next_index


def _timestamps(signal: SignalRecord) -> list[float]:
    return signal["timestamps"]  # type: ignore[return-value]


def _values(signal: SignalRecord) -> list[object]:
    return signal["values"]  # type: ignore[return-value]


def _to_python(value: object) -> object:
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value
