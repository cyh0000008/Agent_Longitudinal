#!/usr/bin/env python
"""Verify that a routed MDF is complete and free of duplicate business signals."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from asammdf import MDF


TIME_CHANNEL_NAMES = {"t", "time", "timestamps"}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--json", type=Path, help="Optional JSON report path")
    parser.add_argument(
        "--exact-group",
        action="append",
        default=[],
        help="Acquisition group whose timestamps and samples must match exactly",
    )
    return parser.parse_args(argv)


def group_name(mdf: MDF, index: int) -> str:
    name = getattr(mdf.groups[index].channel_group, "acq_name", "") or ""
    return str(name) or f"group_{index}"


def group_inventory(mdf: MDF) -> Dict[str, Tuple[int, List[Tuple[int, str]]]]:
    result: Dict[str, Tuple[int, List[Tuple[int, str]]]] = {}
    for group_index, group in enumerate(mdf.groups):
        channels = []
        for channel_index, channel in enumerate(group.channels):
            name = str(channel.name)
            if name not in TIME_CHANNEL_NAMES:
                channels.append((channel_index, name))
        result[group_name(mdf, group_index)] = (group_index, channels)
    return result


def duplicate_names(inventory: Dict[str, Tuple[int, List[Tuple[int, str]]]]) -> List[str]:
    counts = Counter(
        channel_name
        for _, channels in inventory.values()
        for _, channel_name in channels
    )
    return sorted(name for name, count in counts.items() if count > 1)


def exact_group_differences(
    reference: MDF,
    candidate: MDF,
    reference_inventory: Dict[str, Tuple[int, List[Tuple[int, str]]]],
    candidate_inventory: Dict[str, Tuple[int, List[Tuple[int, str]]]],
    requested_groups: Sequence[str],
) -> List[Dict[str, object]]:
    differences: List[Dict[str, object]] = []
    for group in requested_groups:
        if group not in reference_inventory or group not in candidate_inventory:
            differences.append({"group": group, "reason": "missing_group"})
            continue

        reference_group, reference_channels = reference_inventory[group]
        candidate_group, candidate_channels = candidate_inventory[group]
        candidate_indexes = {name: index for index, name in candidate_channels}
        for reference_index, name in reference_channels:
            candidate_index = candidate_indexes.get(name)
            if candidate_index is None:
                differences.append(
                    {"group": group, "channel": name, "reason": "missing_channel"}
                )
                continue
            reference_signal = reference.get(
                group=reference_group, index=reference_index, raw=True
            )
            candidate_signal = candidate.get(
                group=candidate_group, index=candidate_index, raw=True
            )
            if not np.array_equal(reference_signal.timestamps, candidate_signal.timestamps):
                differences.append(
                    {"group": group, "channel": name, "reason": "timestamps_differ"}
                )
            elif not np.array_equal(reference_signal.samples, candidate_signal.samples):
                differences.append(
                    {"group": group, "channel": name, "reason": "samples_differ"}
                )
    return differences


def verify(args: argparse.Namespace) -> Dict[str, object]:
    reference = MDF(str(args.reference), use_display_names=False)
    candidate = MDF(str(args.candidate), use_display_names=False)
    try:
        reference_inventory = group_inventory(reference)
        candidate_inventory = group_inventory(candidate)
        reference_names = {
            name
            for _, channels in reference_inventory.values()
            for _, name in channels
        }
        candidate_names = {
            name
            for _, channels in candidate_inventory.values()
            for _, name in channels
        }
        duplicates = duplicate_names(candidate_inventory)
        missing_groups = sorted(set(reference_inventory) - set(candidate_inventory))
        extra_groups = sorted(set(candidate_inventory) - set(reference_inventory))
        missing_channels = sorted(reference_names - candidate_names)
        extra_channels = sorted(candidate_names - reference_names)
        exact_differences = exact_group_differences(
            reference,
            candidate,
            reference_inventory,
            candidate_inventory,
            args.exact_group,
        )

        passed = not any(
            (
                duplicates,
                missing_groups,
                extra_groups,
                missing_channels,
                extra_channels,
                exact_differences,
            )
        )
        return {
            "reference": str(args.reference),
            "candidate": str(args.candidate),
            "reference_groups": len(reference_inventory),
            "candidate_groups": len(candidate_inventory),
            "reference_business_signals": len(reference_names),
            "candidate_business_signals": len(candidate_names),
            "duplicate_business_signals": len(duplicates),
            "missing_groups": missing_groups,
            "extra_groups": extra_groups,
            "missing_business_signals": missing_channels,
            "extra_business_signals": extra_channels,
            "exact_groups_checked": list(args.exact_group),
            "exact_group_differences": exact_differences,
            "passed": passed,
        }
    finally:
        reference.close()
        candidate.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    report = verify(args)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(rendered, encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
