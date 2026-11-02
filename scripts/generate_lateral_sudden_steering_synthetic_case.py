from __future__ import annotations

import json
from pathlib import Path

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "sample_data" / "batch_root_cause"
OUTPUT_H5 = OUTPUT_DIR / "lateral_sudden_steering_request_lane.h5"
OUTPUT_JSON = OUTPUT_DIR / "lateral_sudden_steering_request_lane.json"


def _ramp(timestamps: np.ndarray, start: float, end: float, delta: float, base: float = 0.0) -> np.ndarray:
    ratio = np.clip((timestamps - start) / (end - start), 0.0, 1.0)
    return base + ratio * delta


def _add_split_message(parent, message_name: str, timestamps: np.ndarray, signals: dict[str, np.ndarray]) -> None:
    message = parent.create_group(message_name)
    message.create_dataset("timestamp", data=timestamps)
    signal_group = message.create_group("signals")
    for signal_name, values in signals.items():
        signal_group.create_dataset(signal_name, data=values)


def generate() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamps = np.round(np.arange(0.0, 10.001, 0.02), 3)

    lcc_active = ((timestamps >= 2.0) & (timestamps <= 8.0)).astype(np.float64)
    steering_request = _ramp(timestamps, 3.95, 4.65, 40.0)
    steering_actual = _ramp(timestamps, 4.05, 4.85, 36.0)

    lane_c0_left = np.full_like(timestamps, 0.82, dtype=np.float64)
    lane_c0_right = -0.84 - _ramp(timestamps, 3.72, 4.18, 1.42)
    lane_c1_left = 0.004 + _ramp(timestamps, 3.76, 4.20, 0.037)
    lane_c1_right = -0.003 - _ramp(timestamps, 3.74, 4.24, 0.064)

    with h5py.File(OUTPUT_H5, "w") as h5_file:
        root = h5_file.create_group("synthetic_lateral")
        bus = root.create_group("Channel1")
        _add_split_message(
            bus,
            "ADAS_LCC_State",
            timestamps,
            {
                "ILCCC_Act": lcc_active,
            },
        )
        _add_split_message(
            bus,
            "Debug_Steering",
            timestamps,
            {
                "IStrWhAng_AEBUsd": steering_actual,
                "IStrWhAng": steering_actual,
            },
        )
        _add_split_message(
            bus,
            "ADS_StrReq",
            timestamps,
            {
                "IStrgReqAngVal": steering_request,
            },
        )
        _add_split_message(
            bus,
            "0x502_LKA_Left_Lane_B",
            timestamps,
            {
                "LaneModelC0": lane_c0_left,
                "LaneModelC1": lane_c1_left,
            },
        )
        _add_split_message(
            bus,
            "0x505_LKA_Right_Lane_B",
            timestamps,
            {
                "LaneModelC0": lane_c0_right,
                "LaneModelC1": lane_c1_right,
            },
        )
        h5_file.attrs["scenario"] = "lateral_sudden_steering_request_lane"
        h5_file.attrs["expected_root_cause"] = "lane geometry change triggered sudden steering request"

    summary = {
        "h5_path": OUTPUT_H5.relative_to(ROOT).as_posix(),
        "problem_type": "横向-猛打",
        "problem_description": "LCC激活后方向盘在4s附近突然猛打，怀疑车道线跳变触发。",
        "expected": {
            "event_window_s": [4.05, 4.85],
            "steering_actual_delta_deg": 36.0,
            "steering_request_delta_deg": 40.0,
            "right_lane_c0_delta_m": 1.42,
            "left_lane_c1_delta_rad": 0.037,
            "right_lane_c1_delta_rad": 0.064,
            "root_cause": "车道几何突变触发转向请求猛打",
        },
    }
    OUTPUT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return OUTPUT_H5


if __name__ == "__main__":
    print(generate())
