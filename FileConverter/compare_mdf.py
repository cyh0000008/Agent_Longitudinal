#!/usr/bin/env python
"""Compare two MDF files by acquisition group, channel names and raw samples."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from asammdf import MDF


TIME_CHANNEL_NAMES = {"t", "time", "timestamps"}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--rtol", type=float, default=1e-9)
    parser.add_argument("--atol", type=float, default=1e-9)
    parser.add_argument("--max-diffs", type=int, default=20)
    parser.add_argument("--json", type=Path, help="Optional JSON report path")
    return parser.parse_args(argv)


def group_name(mdf: MDF, index: int) -> str:
    name = getattr(mdf.groups[index].channel_group, "acq_name", "") or ""
    return str(name) or f"group_{index}"


def group_indexes(mdf: MDF) -> Dict[str, List[int]]:
    result: Dict[str, List[int]] = defaultdict(list)
    for index in range(len(mdf.groups)):
        result[group_name(mdf, index)].append(index)
    return result


def channel_names(mdf: MDF, group_index: int) -> List[Tuple[int, str]]:
    names = []
    for index, channel in enumerate(mdf.groups[group_index].channels):
        name = str(channel.name)
        if name not in TIME_CHANNEL_NAMES:
            names.append((index, name))
    return names


def samples_equal(reference: np.ndarray, candidate: np.ndarray, rtol: float, atol: float) -> bool:
    if reference.shape != candidate.shape:
        return False
    if reference.dtype.kind in "fc" or candidate.dtype.kind in "fc":
        return bool(np.allclose(reference, candidate, rtol=rtol, atol=atol, equal_nan=True))
    return bool(np.array_equal(reference, candidate))


def compare(args: argparse.Namespace) -> Dict[str, object]:
    reference = MDF(str(args.reference))
    candidate = MDF(str(args.candidate))
    ref_groups = group_indexes(reference)
    cand_groups = group_indexes(candidate)

    diffs = []
    matched_channels = 0
    equal_channels = 0
    missing_channels = 0
    value_mismatches = 0
    missing_groups = 0

    for name, ref_indexes in ref_groups.items():
        if name not in cand_groups:
            missing_groups += len(ref_indexes)
            if len(diffs) < args.max_diffs:
                diffs.append({"type": "missing_group", "group": name})
            continue

        for occurrence, ref_group in enumerate(ref_indexes):
            cand_group = cand_groups[name][min(occurrence, len(cand_groups[name]) - 1)]
            cand_channels = {channel_name: channel_index for channel_index, channel_name in channel_names(candidate, cand_group)}

            for ref_channel_index, channel_name in channel_names(reference, ref_group):
                if channel_name not in cand_channels:
                    missing_channels += 1
                    if len(diffs) < args.max_diffs:
                        diffs.append({"type": "missing_channel", "group": name, "channel": channel_name})
                    continue

                matched_channels += 1
                ref_signal = reference.get(group=ref_group, index=ref_channel_index, raw=True)
                cand_signal = candidate.get(group=cand_group, index=cand_channels[channel_name], raw=True)

                timestamps_match = samples_equal(ref_signal.timestamps, cand_signal.timestamps, args.rtol, args.atol)
                samples_match = samples_equal(ref_signal.samples, cand_signal.samples, args.rtol, args.atol)
                if timestamps_match and samples_match:
                    equal_channels += 1
                    continue

                value_mismatches += 1
                if len(diffs) < args.max_diffs:
                    detail = {
                        "type": "value_mismatch",
                        "group": name,
                        "channel": channel_name,
                        "reference_samples": int(len(ref_signal.samples)),
                        "candidate_samples": int(len(cand_signal.samples)),
                        "timestamps_match": timestamps_match,
                        "samples_match": samples_match,
                    }
                    if ref_signal.samples.shape == cand_signal.samples.shape:
                        mismatch_index = first_mismatch(ref_signal.samples, cand_signal.samples, args.rtol, args.atol)
                        if mismatch_index is not None:
                            detail["first_sample_mismatch_index"] = int(mismatch_index)
                            detail["reference_value"] = scalar_to_json(ref_signal.samples[mismatch_index])
                            detail["candidate_value"] = scalar_to_json(cand_signal.samples[mismatch_index])
                    diffs.append(detail)

    extra_groups = sorted(set(cand_groups) - set(ref_groups))
    report = {
        "reference": str(args.reference),
        "candidate": str(args.candidate),
        "reference_start_time": reference.start_time.isoformat(),
        "candidate_start_time": candidate.start_time.isoformat(),
        "start_time_match": reference.start_time == candidate.start_time,
        "reference_groups": len(reference.groups),
        "candidate_groups": len(candidate.groups),
        "missing_groups": missing_groups,
        "extra_groups": extra_groups[: args.max_diffs],
        "matched_channels": matched_channels,
        "equal_channels": equal_channels,
        "missing_channels": missing_channels,
        "value_mismatches": value_mismatches,
        "diffs": diffs,
        "passed": reference.start_time == candidate.start_time
        and missing_groups == 0
        and missing_channels == 0
        and value_mismatches == 0
        and not extra_groups,
    }

    reference.close()
    candidate.close()
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def first_mismatch(reference: np.ndarray, candidate: np.ndarray, rtol: float, atol: float) -> Optional[int]:
    if reference.shape != candidate.shape:
        return None
    if reference.dtype.kind in "fc" or candidate.dtype.kind in "fc":
        mask = ~np.isclose(reference, candidate, rtol=rtol, atol=atol, equal_nan=True)
    else:
        mask = reference != candidate
    indexes = np.flatnonzero(mask)
    if len(indexes) == 0:
        return None
    return int(indexes[0])


def scalar_to_json(value):
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return str(value)
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    report = compare(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
