"""Check a long-running fork: has it passed N epochs, and is it still authoring and finalizing?

    python scripts/soak.py [--name soak] [--min-epochs 2] [--evidence evidence]

Reads over RPC only (read-only client): ``Babe.EpochIndex`` now, and the first block
of every epoch since genesis (binary search on the fork's archive state), with the
block's ``Timestamp.Now``. Then waits two slots and checks that the best and the
finalized head both moved, and that the node still has 0 peers. The measured
duration is from the fork's recorded start to this check. With ``--evidence`` the
result is written to ``<DIR>/soak-<YYYY-MM-DD>.json``. Exit 0 = pass, 2 = not (yet), 1 = unreachable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forklift import tx  # noqa: E402
from forklift.hashing import twox128  # noqa: E402
from forklift.node import read_pid  # noqa: E402
from forklift.rpc import RpcClient, RpcError, RpcUnavailable  # noqa: E402

EPOCH_INDEX = "0x" + (twox128("Babe") + twox128("EpochIndex")).hex()
NOW = "0x" + (twox128("Timestamp") + twox128("Now")).hex()


def _u64(raw: str | None) -> int:
    return 0 if raw is None else int.from_bytes(bytes.fromhex(raw[2:]), "little")


def _utc(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="seconds")


def epoch_at(c: RpcClient, number: int) -> int:
    return _u64(c.call("state_getStorage", [EPOCH_INDEX, c.call("chain_getBlockHash", [number])]))


def first_block_of_epoch(c: RpcClient, epoch: int, best: int) -> int:
    """Smallest block number whose state has EpochIndex >= epoch (EpochIndex never decreases)."""
    lo, hi = 1, best
    while lo < hi:
        mid = (lo + hi) // 2
        if epoch_at(c, mid) >= epoch:
            hi = mid
        else:
            lo = mid + 1
    return lo


def heads(c: RpcClient) -> tuple[int, int]:
    best = int(c.call("chain_getHeader", [])["number"], 16)
    fin = int(c.call("chain_getHeader", [c.call("chain_getFinalizedHead", [])])["number"], 16)
    return best, fin


def alive(pid: int) -> bool:
    return subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", default="soak")
    ap.add_argument("--min-epochs", type=int, default=2)
    ap.add_argument("--evidence", type=Path, default=None)
    ap.add_argument("--wait", type=float, default=None, help="seconds between the two head reads [2 slots + 1]")
    a = ap.parse_args()
    rec = read_pid(a.name)
    if not rec:
        print(f"no PID record for fork {a.name!r}", file=sys.stderr)
        return 1
    manifest = json.loads(Path(rec["manifest"]).read_text())
    try:
        with RpcClient(rec["endpoint"]) as c:
            rt = tx.Runtime.load(c)
            slot_ms = rt.constant_int("Babe", "ExpectedBlockTime")
            epoch_slots = rt.constant_int("Babe", "EpochDuration")
            genesis = c.call("chain_getBlockHash", [0])
            best0, fin0 = heads(c)
            index = _u64(c.call("state_getStorage", [EPOCH_INDEX]))
            transitions = []
            for e in range(1, index + 1):
                n = first_block_of_epoch(c, e, best0)
                h = c.call("chain_getBlockHash", [n])
                transitions.append({"epoch": e, "block": n, "block_hash": h,
                                    "timestamp_utc": _utc(_u64(c.call("state_getStorage", [NOW, h])))})
            block1 = c.call("chain_getBlockHash", [1])
            first_ts = _u64(c.call("state_getStorage", [NOW, block1])) if block1 else None
            time.sleep(a.wait if a.wait is not None else 2 * slot_ms / 1000 + 1)
            best1, fin1 = heads(c)
            peers = c.call("system_health", [])["peers"]
    except (RpcError, RpcUnavailable, OSError) as exc:
        print(f"fork {a.name!r} unreachable: {exc}", file=sys.stderr)
        return 1
    now = time.time()
    started = float(rec.get("started_unix") or 0)
    ok = {"process_alive": alive(int(rec["pid"])), "same_genesis": genesis == rec.get("genesis_hash"),
          "epochs": index >= a.min_epochs, "authoring": best1 > best0, "finalizing": fin1 > fin0, "no_peers": peers == 0}
    out = {
        "fork": {"name": a.name, "label": manifest["fork"]["label"], "endpoint": rec["endpoint"],
                 "genesis_hash": genesis, "pin": manifest["source"]["block_number"],
                 "base_path": rec.get("base_path"), "pid": rec["pid"]},
        "started_utc": dt.datetime.fromtimestamp(started, dt.timezone.utc).isoformat(timespec="seconds"),
        "block_1_utc": _utc(first_ts) if first_ts else None,
        "checked_utc": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds"),
        "measured_hours": round((now - started) / 3600, 2),
        "epochs": {"epoch_index": index, "required": a.min_epochs, "epoch_duration_slots": epoch_slots,
                   "slot_ms": slot_ms, "transitions": transitions},
        "authoring": {"best_before": best0, "best_after": best1},
        "finalizing": {"finalized_before": fin0, "finalized_after": fin1},
        "peers": peers, "checks": ok, "pass": all(ok.values()),
    }
    print(json.dumps(out, indent=2))
    if a.evidence is not None:
        a.evidence.mkdir(parents=True, exist_ok=True)
        dest = a.evidence / f"soak-{dt.datetime.fromtimestamp(now).date().isoformat()}.json"
        dest.write_text(json.dumps(out, indent=2) + "\n")
        print(f"wrote {dest}", file=sys.stderr)
    return 0 if out["pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
