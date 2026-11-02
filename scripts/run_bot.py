from __future__ import annotations

import runpy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

out = (LOG_DIR / "feishuagent.out.log").open("a", encoding="utf-8", buffering=1)
err = (LOG_DIR / "feishuagent.err.log").open("a", encoding="utf-8", buffering=1)
sys.stdout = out
sys.stderr = err

print("\n=== FeishuAgent launcher started ===", flush=True)
runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
