"""Time one lift from a fresh process and record it (stub: one run; p50/p95 over 5 runs comes later).

    python scripts/bench.py [--at <block|0xhash|head>]

Writes evidence/bench-lift-<YYYY-MM-DD>.json. The lift goes to a temporary
directory, so a checkpoint or an earlier lift is never reused.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forklift.cli import _parse_at, run_lift  # noqa: E402
from forklift.rpc import DEFAULT_MAINNET  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="head")
    ap.add_argument("--from", dest="endpoint", default=DEFAULT_MAINNET)
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="forklift-bench-") as tmp:
        t0 = time.monotonic()
        m = run_lift(args.endpoint, _parse_at(args.at), Path(tmp), resume=False)
        wall = round(time.monotonic() - t0, 2)
    now = dt.datetime.now(dt.timezone.utc)
    out = {
        "bench": "lift (stub: 1 run)",
        "profile": m["profile"],
        "endpoint": args.endpoint,
        "pin": {"block_number": m["source"]["block_number"], "block_hash": m["source"]["block_hash"]},
        "keys_lifted": m["load"]["keys_lifted"],
        "key_pages": m["load"]["key_pages"],
        "keys_s": m["load"]["seconds"]["keys_s"],
        "values_s": m["load"]["seconds"]["values_s"],
        "total_s": m["load"]["seconds"]["total_s"],
        "wall_s_incl_metadata_and_mmr_reads": wall,
        "manifest_root": m["manifest_root"],
        "host": {"machine": platform.machine(), "system": platform.system(), "python": platform.python_version()},
        "recorded_utc": now.isoformat(timespec="seconds"),
    }
    path = ROOT / "evidence" / f"bench-lift-{now.date().isoformat()}.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
