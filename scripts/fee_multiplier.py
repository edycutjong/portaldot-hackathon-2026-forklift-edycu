"""Record TransactionPayment.NextFeeMultiplier on the V3 testnet over recent blocks (evidence for rehearse).

    python scripts/fee_multiplier.py [--blocks 20] [--evidence evidence]

Read-only: chain_getBlockHash + state_getStorage at each block, through Forklift's own client.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forklift.chains import V3  # noqa: E402
from forklift.rehearse import fee_multiplier  # noqa: E402
from forklift.rpc import RpcClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=20)
    ap.add_argument("--evidence", type=Path, default=ROOT / "evidence")
    a = ap.parse_args()
    with RpcClient(V3.endpoint) as c:
        if c.call("chain_getBlockHash", [0]) != V3.genesis_hash:
            print("not the V3 testnet", file=sys.stderr)
            return 1
        head = int(c.call("chain_getHeader", [c.call("chain_getFinalizedHead")])["number"], 16)
        rows = []
        for n in range(head - a.blocks + 1, head + 1):
            bh = c.call("chain_getBlockHash", [n])
            xts = len(c.call("chain_getBlock", [bh])["block"]["extrinsics"])
            rows.append({"block": n, "block_hash": bh, "extrinsics": xts, "next_fee_multiplier": str(fee_multiplier(c, n))})
    changed = sum(1 for x, y in zip(rows, rows[1:]) if x["next_fee_multiplier"] != y["next_fee_multiplier"])
    ev = {"recorded_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "profile": "v3",
          "endpoint": V3.endpoint, "item": "TransactionPayment.NextFeeMultiplier (FixedU128, 10^18 = 1.0)",
          "blocks": len(rows), "changed_between_consecutive_blocks": changed, "rows": rows}
    path = a.evidence / f"v3-fee-multiplier-{rows[0]['block']}-{rows[-1]['block']}.json"
    path.write_text(json.dumps(ev, indent=2) + "\n")
    print(f"{changed} of {len(rows) - 1} consecutive pairs differ · {rows[0]['next_fee_multiplier']} → "
          f"{rows[-1]['next_fee_multiplier']} · wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
