from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np

try:
    from signal_lib import FolderSession

    _HAS_SIGNAL_LIB = True
except Exception:
    FolderSession = None  # type: ignore[assignment]
    _HAS_SIGNAL_LIB = False


METRIC_CATEGORY = "MPI"

SIGNAL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "lcc_act": (
        "ILCCC_Act",
        "ILCCC_Act1_1AX",
        "LCCC_Act",
        "ILCCCActStat",
        "ILCCC_Act_1AX",
    ),
    "steering_angle": (
        "IStrWhAng_AEBUsd",
        "Host_Str_Whl_Angle",
        "IStrWhAng",
    ),
    "steering_request": (
        "IStrgReqAngVal",
    ),
    "lane_c0_left": (
        "F_V_Left_Lane_ModelC0",
        "0x502_LKA_Left_Lane_B/LaneModelC0",
        "0xB00_Left_Lane_Track_Coeff/LaneModelC0",
        "LKA_Left_Lane_B/LaneModelC0",
        "Left_Lane_ModelC0",
        "Left_Lane_A0",
    ),
    "lane_c0_right": (
        "F_V_Right_Lane_ModelC0",
        "0x505_LKA_Right_Lane_B/LaneModelC0",
        "0xB01_Right_Lane_Track_Coeff/LaneModelC0",
        "LKA_Right_Lane_B/LaneModelC0",
        "Right_Lane_ModelC0",
        "Right_Lane_A0",
    ),
    "lane_c1_left": (
        "F_V_Left_Lane_ModelC1",
        "0x502_LKA_Left_Lane_B/LaneModelC1",
        "0xB00_Left_Lane_Track_Coeff/LaneModelC1",
        "LKA_Left_Lane_B/LaneModelC1",
        "Left_Lane_ModelC1",
        "Left_Lane_A1",
    ),
    "lane_c1_right": (
        "F_V_Right_Lane_ModelC1",
        "0x505_LKA_Right_Lane_B/LaneModelC1",
        "0xB01_Right_Lane_Track_Coeff/LaneModelC1",
        "LKA_Right_Lane_B/LaneModelC1",
        "Right_Lane_ModelC1",
        "Right_Lane_A1",
    ),
}

REQUIRED_SIGNAL_KEYS = ("lcc_act", "steering_angle")
REQUEST_SIGNAL_KEY = "steering_request"
REQUIRED_SIGNALS = [SIGNAL_CANDIDATES[key][0] for key in REQUIRED_SIGNAL_KEYS]
OPTIONAL_SIGNALS = sorted({name for names in SIGNAL_CANDIDATES.values() for name in names})
PLOT_SIGNAL_KEYS = (
    "lcc_act",
    "steering_angle",
    "steering_request",
    "lane_c0_left",
    "lane_c1_left",
    "lane_c0_right",
    "lane_c1_right",
)
PLOT_SIGNALS = [SIGNAL_CANDIDATES[key][0] for key in PLOT_SIGNAL_KEYS]

DEFAULT_SUDDEN_DEG_PER_S = 30.0
DEFAULT_WINDOW_S = 1.0
DEFAULT_MIN_EVENT_GAP_S = 0.5
DEFAULT_REQUEST_CHANGE_RATIO = 0.3
DEFAULT_LANE_C0_CHANGE_THRESH = 0.3
DEFAULT_LANE_C1_CHANGE_THRESH = 0.02


def __run_haoran_data_run__(folder_context):
    start_time = time.perf_counter()
    try:
        result = analyze_lcc_sudden_steering(folder_context)
    except Exception as exc:
        import traceback

        return False, [
            f"LCC sudden steering metric failed: {exc}",
            traceback.format_exc(),
        ], {
            "issue_type": "lcc_sudden_steering",
            "event_count": 0,
            "events": [],
            "missing_signals": [],
            "Bad Case": 0,
            "All Case Count": 0,
            "Severity Score": 0.0,
            "error": str(exc),
            "runtime_s": round(time.perf_counter() - start_time, 3),
        }

    event_count = int(result.get("event_count") or 0)
    max_severity = max(
        [float(event.get("severity") or 0.0) for event in result.get("events", [])] or [0.0]
    )
    result["Bad Case"] = 1 if event_count > 0 else 0
    result["All Case Count"] = event_count
    result["Severity Score"] = round(max_severity, 3)
    result["runtime_s"] = round(time.perf_counter() - start_time, 3)
    return event_count > 0, _format_messages(result), result


def analyze_lcc_sudden_steering(
    session_or_context,
    sudden_deg_per_s: float = DEFAULT_SUDDEN_DEG_PER_S,
    window_s: float = DEFAULT_WINDOW_S,
    min_event_gap_s: float = DEFAULT_MIN_EVENT_GAP_S,
    request_change_ratio: float = DEFAULT_REQUEST_CHANGE_RATIO,
    lane_c0_change_thresh: float = DEFAULT_LANE_C0_CHANGE_THRESH,
    lane_c1_change_thresh: float = DEFAULT_LANE_C1_CHANGE_THRESH,
) -> dict[str, Any]:
    session = _as_session(session_or_context)
    resolved = _resolve_signals(session)
    missing_core = [key for key in REQUIRED_SIGNAL_KEYS if key not in resolved]
    missing_request = REQUEST_SIGNAL_KEY not in resolved

    if missing_core:
        return _empty_result(
            "insufficient evidence - missing LCC active or steering angle signal",
            _missing_descriptions(missing_core),
            resolved,
            note="Cannot determine whether sudden steering happened without the core phenomenon signals.",
        )

    t_lcc, v_lcc = _get_signal(session, resolved["lcc_act"])
    t_swa, v_swa = _get_signal(session, resolved["steering_angle"])
    if len(t_lcc) == 0 or len(t_swa) == 0:
        return _empty_result(
            "insufficient evidence - core signal data is empty",
            [],
            resolved,
            note="LCC active or steering angle signal has zero samples.",
        )

    t_req = np.asarray([], dtype=np.float64)
    v_req = np.asarray([], dtype=np.float64)
    if not missing_request:
        t_req, v_req = _get_signal(session, resolved["steering_request"])

    lane_signals: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for key in ("lane_c0_left", "lane_c0_right", "lane_c1_left", "lane_c1_right"):
        if key not in resolved:
            continue
        t_lane, v_lane = _get_signal(session, resolved[key])
        if len(t_lane) > 0:
            lane_signals[key] = (t_lane, v_lane)

    lcc_active = _active_mask(v_lcc)
    if not np.any(lcc_active):
        return {
            "issue_type": "lcc_sudden_steering",
            "conclusion": "no_lcc_active",
            "event_count": 0,
            "events": [],
            "missing_signals": [],
            "resolved_signals": resolved,
            "plot_signals": _resolved_plot_signals(resolved),
            "evidence_layers": _evidence_layers(resolved, lane_signals),
            "lcc_active_duration_s": 0.0,
        }

    segments = _bool_to_segments(t_lcc, lcc_active)
    raw_events = _detect_sudden_events(
        t_swa=t_swa,
        v_swa=v_swa,
        segments=segments,
        sudden_deg_per_s=sudden_deg_per_s,
        window_s=window_s,
        min_event_gap_s=min_event_gap_s,
    )

    events: list[dict[str, Any]] = []
    for index, event in enumerate(raw_events, start=1):
        evidence = _classify_event(
            t_req=t_req,
            v_req=v_req,
            t_swa=t_swa,
            v_swa=v_swa,
            event_start=event["start"],
            event_end=event["end"],
            window_s=window_s,
            request_change_ratio=request_change_ratio,
            request_available=not missing_request and len(t_req) >= 2,
            lane_signals=lane_signals,
            lane_c0_change_thresh=lane_c0_change_thresh,
            lane_c1_change_thresh=lane_c1_change_thresh,
            resolved=resolved,
        )
        direction = _event_direction(evidence)
        events.append(
            {
                "index": index,
                "start": event["start"],
                "end": event["end"],
                "duration": event["duration"],
                "severity": event["severity"],
                "delta_deg": event["delta_deg"],
                "problem": "LCC sudden steering",
                "root_cause_direction": direction,
                "evidence": evidence,
            }
        )

    if not events:
        conclusion = "no_lcc_sudden_steering_event"
    elif all(event["root_cause_direction"] == "request_side_lane_geometry" for event in events):
        conclusion = "request_side_lane_geometry_trigger"
    elif all(event["root_cause_direction"].startswith("request_side") for event in events):
        conclusion = "request_side_other_trigger"
    elif all(event["root_cause_direction"] == "actuator_response" for event in events):
        conclusion = "actuator_response_issue"
    elif all(event["root_cause_direction"] == "insufficient_evidence" for event in events):
        conclusion = "insufficient_evidence"
    else:
        conclusion = "mixed_evidence"

    missing_signals = []
    if missing_request:
        missing_signals.append(_missing_descriptions([REQUEST_SIGNAL_KEY])[0])

    return {
        "issue_type": "lcc_sudden_steering",
        "conclusion": conclusion,
        "event_count": len(events),
        "events": events,
        "missing_signals": missing_signals,
        "resolved_signals": resolved,
        "plot_signals": _resolved_plot_signals(resolved),
        "evidence_layers": _evidence_layers(resolved, lane_signals),
        "lcc_active_duration_s": round(sum(item["end"] - item["start"] for item in segments), 1),
    }


def _detect_sudden_events(
    *,
    t_swa: np.ndarray,
    v_swa: np.ndarray,
    segments: list[dict[str, float]],
    sudden_deg_per_s: float,
    window_s: float,
    min_event_gap_s: float,
) -> list[dict[str, Any]]:
    all_events: list[dict[str, Any]] = []
    sample_period = _median_dt(t_swa)
    window_samples = max(2, int(round(window_s / sample_period))) if sample_period > 0 else 25

    for segment in segments:
        in_segment = (t_swa >= segment["start"]) & (t_swa <= segment["end"])
        indices = np.where(in_segment)[0]
        if len(indices) < window_samples:
            continue
        seg_t = t_swa[indices]
        seg_v = v_swa[indices]

        for start_idx in range(0, len(seg_v) - window_samples + 1):
            end_idx = start_idx + window_samples - 1
            values = seg_v[start_idx : end_idx + 1]
            valid = ~np.isnan(values)
            if np.sum(valid) < 2:
                continue
            delta = float(np.nanmax(values[valid]) - np.nanmin(values[valid]))
            if delta < sudden_deg_per_s:
                continue

            event_start = float(seg_t[start_idx])
            event_end = float(seg_t[end_idx])
            if all_events and (event_start - all_events[-1]["end"]) < min_event_gap_s:
                merged = all_events[-1]
                merged["end"] = event_end
                merged["duration"] = round(merged["end"] - merged["start"], 3)
                merged["severity"] = round(max(float(merged["severity"]), delta), 1)
                merged["delta_deg"] = merged["severity"]
            else:
                all_events.append(
                    {
                        "start": event_start,
                        "end": event_end,
                        "duration": round(event_end - event_start, 3),
                        "severity": round(delta, 1),
                        "delta_deg": round(delta, 1),
                    }
                )
    return all_events


def _classify_event(
    *,
    t_req: np.ndarray,
    v_req: np.ndarray,
    t_swa: np.ndarray,
    v_swa: np.ndarray,
    event_start: float,
    event_end: float,
    window_s: float,
    request_change_ratio: float,
    request_available: bool,
    lane_signals: dict[str, tuple[np.ndarray, np.ndarray]],
    lane_c0_change_thresh: float,
    lane_c1_change_thresh: float,
    resolved: dict[str, str],
) -> dict[str, Any]:
    actual_delta = _signal_delta(t_swa, v_swa, event_start, event_end)
    request_delta = 0.0
    request_sudden = False
    if request_available:
        request_delta = _signal_delta(t_req, v_req, event_start - window_s * 0.5, event_end)
        if actual_delta > 0:
            request_sudden = (request_delta / actual_delta) >= request_change_ratio
        else:
            request_sudden = request_delta > 5.0

    lane_triggered = False
    lane_evidence: dict[str, dict[str, Any]] = {}
    if request_sudden:
        for lane_key, (t_lane, v_lane) in lane_signals.items():
            lane_delta = _signal_delta(t_lane, v_lane, event_start - window_s, event_end)
            threshold = lane_c0_change_thresh if "c0" in lane_key else lane_c1_change_thresh
            changed = lane_delta > threshold
            lane_evidence[lane_key] = {
                "signal": resolved.get(lane_key, lane_key),
                "delta": round(lane_delta, 4),
                "threshold": threshold,
                "changed": changed,
            }
            lane_triggered = lane_triggered or changed

    ratio = round(request_delta / actual_delta, 3) if actual_delta else 0.0
    return {
        "actual_delta": round(actual_delta, 3),
        "request_delta": round(request_delta, 3),
        "request_ratio": ratio,
        "request_data_available": bool(request_available),
        "request_sudden": bool(request_sudden),
        "request_change_ratio_threshold": request_change_ratio,
        "lane_triggered": bool(lane_triggered),
        "lane_evidence": lane_evidence,
    }


def _event_direction(evidence: dict[str, Any]) -> str:
    if not evidence.get("request_data_available"):
        return "insufficient_evidence"
    if evidence.get("request_sudden"):
        if evidence.get("lane_triggered"):
            return "request_side_lane_geometry"
        return "request_side_other"
    return "actuator_response"


def _format_messages(result: dict[str, Any]) -> list[str]:
    messages = [
        f"LCC sudden steering conclusion: {result.get('conclusion', 'unknown')}",
        f"LCC active duration: {float(result.get('lcc_active_duration_s') or 0):.1f} s",
        f"Detected {int(result.get('event_count') or 0)} sudden steering event(s).",
    ]
    if result.get("missing_signals"):
        messages.append("Missing signals: " + ", ".join(map(str, result["missing_signals"])))
    for event in result.get("events", []):
        messages.append(
            "[Event {idx}] {problem} start={start:.3f} end={end:.3f} "
            "duration={duration:.3f} delta={delta}deg severity={severity} "
            "root_cause={root_cause}".format(
                idx=event.get("index"),
                problem=event.get("problem", "LCC sudden steering"),
                start=float(event.get("start") or 0.0),
                end=float(event.get("end") or 0.0),
                duration=float(event.get("duration") or 0.0),
                delta=event.get("delta_deg", "?"),
                severity=event.get("severity", "?"),
                root_cause=event.get("root_cause_direction", "unknown"),
            )
        )
    return messages


def _as_session(session_or_context):
    if isinstance(session_or_context, dict):
        session = session_or_context.get("session")
        if session is not None and (
            hasattr(session, "get_signal_slice") or hasattr(session, "get_signal")
        ):
            return session
        path = (
            session_or_context.get("primary_h5_file")
            or session_or_context.get("h5_file")
            or session_or_context.get("folder")
            or session_or_context.get("path")
        )
        if path:
            return _path_session(Path(path))

    if isinstance(session_or_context, H5FileAdapter):
        return session_or_context
    if _HAS_SIGNAL_LIB and FolderSession is not None and isinstance(session_or_context, FolderSession):
        return session_or_context
    if isinstance(session_or_context, (str, Path)):
        return _path_session(Path(session_or_context))
    if hasattr(session_or_context, "folder"):
        return _path_session(Path(session_or_context.folder))
    if hasattr(session_or_context, "get_signal") or hasattr(session_or_context, "get_signal_slice"):
        return session_or_context
    raise TypeError("Unsupported session or context for lateral sudden steering analysis.")


def _path_session(path: Path):
    if _HAS_SIGNAL_LIB and FolderSession is not None:
        try:
            return FolderSession(path, preprocess_signals=OPTIONAL_SIGNALS)
        except Exception:
            pass
    return H5FileAdapter(path)


def _resolve_signals(session) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for key, candidates in SIGNAL_CANDIDATES.items():
        for candidate in candidates:
            resolved_name = _resolve_signal_name(session, candidate)
            if resolved_name:
                resolved[key] = resolved_name
                break
    return resolved


def _resolve_signal_name(session, name: str) -> str | None:
    for candidate in _signal_name_variants(name):
        if _signal_exists_exact(session, candidate):
            return candidate

    names = _list_session_signals(session)
    if not names:
        return None

    lowered = {signal.lower(): signal for signal in names}
    for candidate in _signal_name_variants(name):
        match = lowered.get(candidate.lower())
        if match:
            return match

    tail = _signal_tail(name).lower()
    for signal in names:
        value = signal.lower()
        if value.endswith("." + tail) or value.endswith("/" + tail) or value == tail:
            return signal
    return None


def _signal_name_variants(name: str) -> list[str]:
    value = str(name or "").strip()
    variants = [value]
    if "/" in value:
        variants.append(value.replace("/", "."))
        parts = [part for part in value.split("/") if part]
        if len(parts) >= 2:
            variants.append(f"{parts[-2]}.{parts[-1]}")
            variants.append(parts[-1])
    if "." in value:
        parts = [part for part in value.split(".") if part]
        if len(parts) >= 2:
            variants.append(f"{parts[-2]}/{parts[-1]}")
            variants.append(parts[-1])
    return list(dict.fromkeys(item for item in variants if item))


def _signal_tail(name: str) -> str:
    value = str(name or "").strip().replace("\\", "/")
    if "/signals/" in value:
        value = value.rsplit("/signals/", 1)[-1]
    if "/" in value:
        value = value.rsplit("/", 1)[-1]
    if "." in value:
        value = value.rsplit(".", 1)[-1]
    return value


def _signal_exists_exact(session, name: str) -> bool:
    try:
        if hasattr(session, "has_signal"):
            return bool(session.has_signal(name))
    except Exception:
        return False
    return False


def _list_session_signals(session) -> list[str]:
    try:
        if hasattr(session, "list_signals"):
            return list(session.list_signals())
    except Exception:
        pass
    try:
        store = session.get_store()
        if hasattr(store, "list_signals"):
            return list(store.list_signals())
    except Exception:
        pass
    return []


def _get_signal(session, name: str) -> tuple[np.ndarray, np.ndarray]:
    resolved = _resolve_signal_name(session, name) or name
    if _HAS_SIGNAL_LIB and FolderSession is not None and isinstance(session, FolderSession):
        signal = session.get_store().get_signal(resolved)
        t = np.asarray(signal.get("timestamps", []), dtype=np.float64)
        v = np.asarray([_to_float(item) for item in signal.get("values", [])], dtype=np.float64)
        return t, v
    if isinstance(session, H5FileAdapter):
        return session.get_signal(resolved)
    if hasattr(session, "get_signal"):
        t, v = session.get_signal(resolved)
        return np.asarray(t, dtype=np.float64), np.asarray(v, dtype=np.float64)
    if hasattr(session, "get_signal_slice"):
        t, v = session.get_signal_slice(resolved, float("-inf"), float("inf"))
        return np.asarray(t, dtype=np.float64), np.asarray([_to_float(item) for item in v], dtype=np.float64)
    raise TypeError(f"Cannot read signal {name!r} from {type(session)!r}")


class H5FileAdapter:
    def __init__(self, path: str | Path):
        import h5py

        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"H5 file not found: {self.path}")
        self._h5 = h5py.File(str(self.path), "r")
        self._signal_index: dict[str, dict[str, str | None]] = {}
        self._build_index()

    def _build_index(self) -> None:
        raw: list[tuple[str, str, str | None, str]] = []

        def walk(group):
            import h5py

            for child in group.values():
                if isinstance(child, h5py.Group):
                    if isinstance(child.get("timestamp"), h5py.Dataset) and isinstance(child.get("signals"), h5py.Group):
                        self._register_split_group(child, raw)
                    walk(child)
                elif isinstance(child, h5py.Dataset):
                    names = child.dtype.names or ()
                    if "timestamp" in names:
                        self._register_compound_dataset(child, raw)

        walk(self._h5)
        for key, ds_path, field, ts_path in raw:
            self._signal_index.setdefault(key, {"ds_path": ds_path, "field": field, "timestamp_path": ts_path})

    def _register_split_group(self, group, raw: list[tuple[str, str, str | None, str]]) -> None:
        message_path = group.name.strip("/")
        message_name = message_path.rsplit("/", 1)[-1]
        ts_path = group["timestamp"].name
        for signal_name, dataset in group["signals"].items():
            ds_path = dataset.name
            keys = [
                signal_name,
                f"{message_name}/{signal_name}",
                f"{message_name}.{signal_name}",
                f"{message_path}/signals/{signal_name}",
            ]
            for key in keys:
                raw.append((key, ds_path, None, ts_path))

    def _register_compound_dataset(self, dataset, raw: list[tuple[str, str, str | None, str]]) -> None:
        message_name = dataset.name.strip("/").rsplit("/", 1)[-1]
        for field_name in dataset.dtype.names or ():
            if field_name == "timestamp":
                continue
            for key in (field_name, f"{message_name}/{field_name}", f"{message_name}.{field_name}"):
                raw.append((key, dataset.name, field_name, dataset.name))

    def has_signal(self, name: str) -> bool:
        return name in self._signal_index

    def list_signals(self) -> list[str]:
        return sorted(self._signal_index)

    def get_signal(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        entry = self._signal_index[name]
        if entry["field"]:
            dataset = self._h5[str(entry["ds_path"])]
            data = dataset[:]
            timestamps = data["timestamp"].astype(np.float64)
            values = data[str(entry["field"])].astype(np.float64)
        else:
            timestamps = self._h5[str(entry["timestamp_path"])][:].astype(np.float64)
            values = self._h5[str(entry["ds_path"])][:].astype(np.float64)
        order = np.argsort(timestamps)
        return timestamps[order], values[order]

    def common_time_range(self, signal_names: list[str]) -> tuple[float, float]:
        ranges = [self.get_signal(name)[0] for name in signal_names]
        return max(float(item[0]) for item in ranges), min(float(item[-1]) for item in ranges)

    def close(self) -> None:
        self._h5.close()

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()


def _active_mask(values: np.ndarray) -> np.ndarray:
    mask = []
    for value in values:
        if np.isnan(value):
            mask.append(False)
        else:
            mask.append(bool(value) and float(value) > 0.5)
    return np.asarray(mask, dtype=bool)


def _bool_to_segments(timestamps: np.ndarray, mask: np.ndarray) -> list[dict[str, float]]:
    segments: list[dict[str, float]] = []
    start_index: int | None = None
    for index, active in enumerate(mask):
        if active and start_index is None:
            start_index = index
        elif not active and start_index is not None:
            segments.append({"start": float(timestamps[start_index]), "end": float(timestamps[index - 1])})
            start_index = None
    if start_index is not None:
        segments.append({"start": float(timestamps[start_index]), "end": float(timestamps[-1])})
    return segments


def _signal_delta(timestamps: np.ndarray, values: np.ndarray, start: float, end: float) -> float:
    mask = (timestamps >= start) & (timestamps <= end)
    window = values[mask]
    if len(window) < 2:
        return 0.0
    valid = ~np.isnan(window)
    if np.sum(valid) < 2:
        return 0.0
    return float(np.nanmax(window[valid]) - np.nanmin(window[valid]))


def _median_dt(timestamps: np.ndarray) -> float:
    if len(timestamps) < 2:
        return 0.0
    return float(np.median(np.diff(timestamps)))


def _to_float(value: Any) -> float:
    try:
        if value is None:
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")


def _evidence_layers(resolved: dict[str, str], lane_signals: dict[str, Any]) -> dict[str, list[str]]:
    return {
        "phenomenon": [resolved["steering_angle"]] if "steering_angle" in resolved else [],
        "request": [resolved["steering_request"]] if "steering_request" in resolved else [],
        "response": [resolved["steering_angle"]] if "steering_angle" in resolved else [],
        "trigger": [resolved[key] for key in lane_signals if key in resolved],
    }


def _resolved_plot_signals(resolved: dict[str, str]) -> list[str]:
    return [resolved[key] for key in PLOT_SIGNAL_KEYS if key in resolved]


def _missing_descriptions(keys: list[str]) -> list[str]:
    descriptions = []
    for key in keys:
        candidates = "/".join(SIGNAL_CANDIDATES.get(key, (key,))[:3])
        descriptions.append(f"{key}({candidates})")
    return descriptions


def _empty_result(
    conclusion: str,
    missing_signals: list[str],
    resolved: dict[str, str] | None = None,
    note: str = "",
) -> dict[str, Any]:
    return {
        "issue_type": "lcc_sudden_steering",
        "conclusion": conclusion,
        "event_count": 0,
        "events": [],
        "missing_signals": missing_signals,
        "resolved_signals": resolved or {},
        "plot_signals": _resolved_plot_signals(resolved or {}),
        "note": note,
        "evidence_layers": {
            "phenomenon": [],
            "request": [],
            "response": [],
            "trigger": [],
        },
        "lcc_active_duration_s": 0.0,
    }
