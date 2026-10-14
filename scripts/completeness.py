"""I1b completeness: the lifted key SET equals an independent full key listing minus the MMR range.

The listing pages every key at the lift's block hash from 0x with no skip
(~5.4 M keys, ~10+ minutes against the public node). It is streamed: only the
non-MMR keys are kept in memory, the MMR range is counted, and nothing of the
listing is written to disk except the summary.

    python scripts/completeness.py ~/.cache/forklift/lifts/<block>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forklift.hashing import manifest_root  # noqa: E402
from forklift.rpc import DEFAULT_MAINNET, RpcClient  # noqa: E402

MMR = "0x434e2055f3154c54233a8dabeeb2839e"
PAGE = 1000


def full_listing(client: RpcClient, block_hash: str, progress_every: int = 500):
    """Every key at block_hash, no skip. Returns (non-MMR key set, MMR key count, pages)."""
    keys: set[str] = set()
    mmr = pages = 0
    cursor = None
    t0 = time.monotonic()
    while True:
        page = client.call("state_getKeysPaged", ["0x", PAGE, cursor, block_hash])
        pages += 1
        for k in page:
            if k.startswith(MMR):
                mmr += 1
            else:
                keys.add(k)
        if pages % progress_every == 0:
            print(f"  {pages} pages · {len(keys) + mmr:,} keys · {time.monotonic() - t0:.0f} s", flush=True)
        if not page:  # only an empty page ends the listing: a node may return short pages early
            return keys, mmr, pages
        if cursor is not None and page[0] <= cursor:
            raise SystemExit(f"node did not advance past cursor {cursor}: listing aborted")
        cursor = page[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lift_dir", type=Path)
    ap.add_argument("--from", dest="endpoint", default=DEFAULT_MAINNET)
    ap.add_argument("--evidence", type=Path, default=Path(__file__).resolve().parents[1] / "evidence")
    args = ap.parse_args()

    manifest = json.loads((args.lift_dir / "manifest.json").read_text())
    state = json.loads((args.lift_dir / "state.json").read_text())
    lifted = set(state["storage"])
    root = "0x" + manifest_root((bytes.fromhex(k[2:]), bytes.fromhex(v[2:]))
                                for k, v in state["storage"].items()).hex()
    pin_hash = manifest["source"]["block_hash"]
    assert state["block_hash"] == pin_hash

    started = dt.datetime.now(dt.timezone.utc)
    t0 = time.monotonic()
    with RpcClient(args.endpoint, timeout=120) as client:
        listing, mmr_count, pages = full_listing(client, pin_hash)
    seconds = round(time.monotonic() - t0, 1)

    missing = sorted(listing - lifted)  # on chain, not lifted
    extra = sorted(lifted - listing)  # lifted, not on chain
    derived = (manifest["load"]["skipped"] or {}).get("keys_in_range_derived")
    out = {
        "check": "I1b lifted key set == full key listing at the same block hash minus the MMR range",
        "profile": manifest["profile"],
        "endpoint": args.endpoint,
        "pin": {"block_number": manifest["source"]["block_number"], "block_hash": pin_hash},
        "lifted_keys": len(lifted),
        "listing_keys_total": len(listing) + mmr_count,
        "listing_keys_outside_mmr": len(listing),
        "mmr_range_keys": mmr_count,
        "mmr_range_keys_derived_by_lift": derived,
        "mmr_derivation_matches": derived == mmr_count,
        "missing_from_lift": len(missing),
        "extra_in_lift": len(extra),
        "missing_sample": missing[:10],
        "extra_sample": extra[:10],
        "listing_pages": pages,
        "listing_seconds": seconds,
        "manifest_root": manifest["manifest_root"],
        "state_root_matches_manifest": root == manifest["manifest_root"],
        "started_utc": started.isoformat(timespec="seconds"),
        "pass": bool(listing) and not missing and not extra and derived == mmr_count
        and root == manifest["manifest_root"],
    }
    args.evidence.mkdir(parents=True, exist_ok=True)
    path = args.evidence / f"lift-completeness-{manifest['source']['block_number']}.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: v for k, v in out.items() if not k.endswith("_sample")}, indent=2))
    print(f"wrote {path}")
    return 0 if out["pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
