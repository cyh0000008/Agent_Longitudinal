from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "sample_data" / "batch_root_cause" / "function_exit_synthetic_cases"
SUMMARY_PATH = OUT_DIR / "function_exit_synthetic_summary.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from function_exit_analysis import run_function_exit_analysis


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cases = [
        {
            "key": "acc_exit_brake_pedal",
            "description": "样例：ACC 激活后因制动踏板介入退出",
            "mutate": _case_acc_exit_brake_pedal,
        },
        {
            "key": "acc_exit_esp_off",
            "description": "样例：ACC 激活后因 ESP Off 抑制退出",
            "mutate": _case_acc_exit_esp_off,
        },
        {
            "key": "lcc_exit_curve_radius",
            "description": "样例：LCC 激活后因弯道半径过小临时退出",
            "mutate": _case_lcc_exit_curve_radius,
        },
    ]

    summaries = []
    for case in cases:
        path = OUT_DIR / f"{case['key']}.h5"
        signals = _base_signals()
        mutate: Callable[[dict[str, dict[str, np.ndarray]]], None] = case["mutate"]
        mutate(signals)
        _write_h5(path, signals)
        result = run_function_exit_analysis(path, request_text=case["description"])
        summaries.append(
            {
                "key": case["key"],
                "problem_type": "功能退出",
                "description": case["description"],
                "h5_path": path.relative_to(ROOT).as_posix(),
                "status": result.status,
                "summary": result.summary,
                "event_count": len(result.events),
                "events": [
                    {
                        "function": event.function,
                        "time": event.time_text,
                        "from_state": event.from_state,
                        "to_state": event.to_state,
                        "reason": event.reason_summary,
                        "details": event.reason_details[:5],
                    }
                    for event in result.events
                ],
            }
        )

    SUMMARY_PATH.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {SUMMARY_PATH}")
    for item in summaries:
        print(f"{item['key']}: {item['summary']}")


def _time_axis() -> np.ndarray:
    return np.round(np.arange(0.0, 20.01, 0.1), 3)


def _base_array(value: float) -> np.ndarray:
    return np.full_like(_time_axis(), float(value), dtype=float)


def _mask(start: float, end: float) -> np.ndarray:
    t = _time_axis()
    return (t >= start) & (t <= end)


def _base_signals() -> dict[str, dict[str, np.ndarray]]:
    return {
        "MPC_DebugMsg_Reserved_14": {
            "Debug_uint_Reserved_47": _base_array(2.0),
            "ACC_Inhibit_Reasion": _base_array(0.0),
        },
        "MPC_DebugMsg_Reserved_10": {
            "Debug_uint_Reserved_11": _base_array(0.0),
            "LCC_Inhibit_Reasion": _base_array(0.0),
        },
        "PPEI_Vehicle_Speed_and_Distance_209_M1": {
            "IVehSpdAvgDrvn_1": _base_array(22.0),
        },
    }


def _case_acc_exit_brake_pedal(signals: dict[str, dict[str, np.ndarray]]) -> None:
    state = signals["MPC_DebugMsg_Reserved_14"]["Debug_uint_Reserved_47"]
    state[_mask(3.0, 9.9)] = 5.0
    state[_mask(10.0, 20.0)] = 2.0
    signals["MPC_DebugMsg_Reserved_14"]["ACC_Inhibit_Reasion"][_mask(10.0, 12.0)] = 26.0


def _case_acc_exit_esp_off(signals: dict[str, dict[str, np.ndarray]]) -> None:
    state = signals["MPC_DebugMsg_Reserved_14"]["Debug_uint_Reserved_47"]
    state[_mask(3.0, 8.9)] = 5.0
    state[_mask(9.0, 20.0)] = 1.0
    signals["MPC_DebugMsg_Reserved_14"]["ACC_Inhibit_Reasion"][_mask(9.0, 11.0)] = 7.0


def _case_lcc_exit_curve_radius(signals: dict[str, dict[str, np.ndarray]]) -> None:
    state = signals["MPC_DebugMsg_Reserved_10"]["Debug_uint_Reserved_11"]
    state[_mask(3.0, 11.4)] = 4.0
    state[_mask(11.5, 20.0)] = 3.0
    signals["MPC_DebugMsg_Reserved_10"]["LCC_Inhibit_Reasion"][_mask(11.5, 13.5)] = 12.0


def _write_h5(path: Path, signals: dict[str, dict[str, np.ndarray]]) -> None:
    timestamps = _time_axis()
    if path.exists():
        path.unlink()
    with h5py.File(path, "w") as h5_file:
        channel = h5_file.create_group("synthetic").create_group("Channel1")
        for message_name, fields in signals.items():
            dtype = [("timestamp", "f8")]
            for field_name in fields:
                dtype.append((field_name, "f8"))
            data = np.zeros(len(timestamps), dtype=np.dtype(dtype))
            data["timestamp"] = timestamps
            for field_name, values in fields.items():
                data[field_name] = values
            channel.create_dataset(message_name, data=data, compression="gzip")


if __name__ == "__main__":
    main()
