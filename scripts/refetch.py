"""Re-read every recorded fork transaction by its extrinsic id, from this (separate) process.

    python scripts/refetch.py [--evidence evidence] [--all]

For each row of ``evidence/txs-<block>.json`` (by default only rows on a persistent
fork, the ones PROOF.md cites) it connects to the row's fork endpoint with a
read-only client, checks the chain is the same fork (genesis hash), fetches the
extrinsic at ``<block>-<index>`` and compares block hash, tx hash and outcome with
what was recorded. Receipt rows also have their ``ContractEmitted`` payload decoded
again and compared with the pin's manifest root. Writes ``evidence/refetch-<block>.json``.
Exit 0 when every row re-reads identically, 2 otherwise, 1 if a fork is unreachable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forklift import receipt, tx  # noqa: E402
from forklift.rpc import RpcClient, RpcError, RpcUnavailable  # noqa: E402


def refetch_row(client: RpcClient, rt: tx.Runtime, row: dict, manifest_like: dict) -> dict:
    got = tx.fetch_extrinsic(client, rt, row["extrinsic_id"])
    checks = {
        "block_hash": got["block_hash"] == row["block_hash"],
        "tx_hash": got["tx_hash"] == row["tx_hash"],
        "success": got["success"] == row["success"],
    }
    out = {"extrinsic_id": row["extrinsic_id"], "kind": row["kind"], "call": f"{got['call']['pallet']}.{got['call']['function']}",
           "signer": got["signer"], "block_hash": got["block_hash"], "tx_hash": got["tx_hash"],
           "success": got["success"], "error": got["error"], "events": [e["event"] for e in got["events"]]}
    if row["kind"] in ("instantiate", "attest"):
        emitted = [e for e in got["events"] if e["event"] == "Contracts.ContractEmitted"]
        payloads = []
        for e in emitted:
            contract, data = (a["value"] for a in e["attributes"])
            raw = bytes.fromhex(data[2:])
            payloads.append({"contract": contract, "decoded": receipt.decode_payload(raw)})
        checks["receipt_payload_matches_manifest"] = bool(payloads) and all(
            p["contract"] == row["contract"] and not receipt.compare_to_manifest(p["decoded"], manifest_like)
            for p in payloads)
        out["emitted"] = payloads
    out["checks"] = checks
    out["ok"] = all(checks.values())
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--evidence", type=Path, default=ROOT / "evidence")
    ap.add_argument("--all", action="store_true", help="also rows on throwaway (--tmp) forks")
    a = ap.parse_args()
    code = 0
    for path in sorted(a.evidence.glob("txs-*.json")):
        ev = json.loads(path.read_text())
        manifest_like = {"source": {"block_hash": ev["pin"]["block_hash"]}, "manifest_root": ev["manifest_root"],
                         "load": {"keys_lifted": ev["keys_lifted"]}}
        rows = [r for r in ev["rows"] if a.all or r.get("persistent_base_path")]
        results, clients = [], {}
        try:
            for row in rows:
                ep = row["fork_endpoint"]
                if ep not in clients:
                    c = RpcClient(ep)  # read-only: this script never writes
                    genesis = c.call("chain_getBlockHash", [0])
                    clients[ep] = (c, tx.Runtime.load(c), genesis)
                c, rt, genesis = clients[ep]
                if genesis != row["fork_genesis_hash"]:
                    results.append({"extrinsic_id": row["extrinsic_id"], "kind": row["kind"], "ok": False,
                                    "checks": {"same_fork": False}, "genesis": genesis})
                    continue
                results.append(refetch_row(c, rt, row, manifest_like))
        except (RpcError, RpcUnavailable, OSError) as exc:
            print(f"{path.name}: fork unreachable: {exc}", file=sys.stderr)
            return 1
        finally:
            for c, _, _ in clients.values():
                c.close()
        ok = all(r["ok"] for r in results)
        code = max(code, 0 if ok else 2)
        out = {"refetched_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "process": {"pid": os.getpid(), "note": "a separate process from the one that submitted"},
               "source": f"evidence/{path.name}", "pin": ev["pin"], "rows_checked": len(results),
               "all_identical": ok, "rows": results}
        dest = a.evidence / path.name.replace("txs-", "refetch-")
        dest.write_text(json.dumps(out, indent=2) + "\n")
        for r in results:
            print(f"{r['extrinsic_id']:>8}  {r['kind']:<12} {'ok' if r['ok'] else 'DIFFERS ' + json.dumps(r['checks'])}")
        print(f"wrote {dest.relative_to(ROOT) if dest.is_relative_to(ROOT) else dest}: "
              f"{len(results)} rows, {'all identical' if ok else 'DIFFERENCES'}")
    return code


if __name__ == "__main__":
    sys.exit(main())
