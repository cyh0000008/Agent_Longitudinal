from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_root_cause_analysis import run_deepseek_root_cause_investigation
from feishu_config import load_env


SAMPLE_ROOT = ROOT / "sample_data" / "batch_root_cause" / "root_cause_synthetic_cases"
SAMPLES = {
    "false_braking": ("false_braking.h5", "误制动", "样例：前方无合理风险目标时出现误制动。"),
    "missed_braking": ("missed_braking.h5", "漏制动", "样例：前车风险窗口出现后自车未及时刹停。"),
    "rollback": ("rollback.h5", "溜车", "样例：跟停或低速保持阶段出现溜车。"),
    "heavy_braking": ("heavy_braking.h5", "重制动", "样例：ACC 窗口内出现重制动。"),
    "set_speed_not_reached": ("set_speed_not_reached.h5", "稳速未达 Setspeed", "样例：ACC 激活后车速未达到设速。"),
}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Run DeepSeek-driven root-cause investigation on synthetic H5 samples.")
    parser.add_argument("--sample", choices=sorted(SAMPLES), default="false_braking")
    parser.add_argument("--max-signals", type=int, default=32)
    parser.add_argument("--output-dir", default=str(ROOT / "analysis_outputs" / "deepseek_investigation"))
    args = parser.parse_args()

    load_env()
    os.environ.setdefault("LLM_TIMEOUT_SECONDS", "300")

    file_name, problem_type, description = SAMPLES[args.sample]
    h5_path = SAMPLE_ROOT / file_name
    result = run_deepseek_root_cause_investigation(
        h5_path=h5_path,
        problem_type=problem_type,
        problem_description=description,
        max_signals=args.max_signals,
        image_root=Path(args.output_dir) / f"{args.sample}_plots",
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = output_dir / f"{args.sample}.md"
    json_path = output_dir / f"{args.sample}.json"
    markdown_path.write_text(result.conclusion, encoding="utf-8")
    json_path.write_text(result.context, encoding="utf-8")

    print(result.conclusion)
    print()
    print(f"Markdown: {markdown_path}")
    print(f"JSON: {json_path}")
    if result.image_paths:
        print("Images:")
        for image_path in result.image_paths:
            print(f"- {image_path}")


if __name__ == "__main__":
    main()
