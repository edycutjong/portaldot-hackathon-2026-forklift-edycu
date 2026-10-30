"""Measure what Revive on the V3 testnet charges to deploy code and to write storage (evidence for Receipt v2).

    python scripts/revive_deposits.py [--build LABEL=PATH ...] [--evidence evidence]

Read-only: every number is a `ReviveApi_instantiate` dry run (`state_call`) at one pinned finalized block,
from the chain's `Sudo.Key` account as origin (any well-funded account works; a dry run signs nothing and
moves nothing). Measured:

- synthetic init code that returns N zero bytes as the runtime code (N = 1, 100, 200, 400) -> deposit vs code size;
- init code that writes K full storage slots (K = 0..3) and keeps 1 byte of code -> deposit per slot;
- the deployed Receipt v2 (`forklift/contracts/ForkReceipt.bin`; once its code is on chain, a dry run no longer
  includes the code-upload deposit, and the row says so), and any `--build` given, e.g. the plain-Solidity
  comparison `scripts/ForkReceiptSolidity.sol` compiled with resolc (PolkaVM) and with solc (EVM bytecode).

The two comparison builds in `evidence/revive-deposits-v3.json` were made with the npm builds (Node.js 18+),
run inside `scripts/` (solc's metadata hash covers the source path, so the directory matters for its sha256):

    cd scripts
    npx -y @parity/resolc@1.5.0 --bin -o /tmp/out ForkReceiptSolidity.sol                 # .polkavm, 7,851 bytes
    npx -y -p solc@0.8.30 solcjs --bin --optimize -o /tmp/out ForkReceiptSolidity.sol    # .bin (hex), 991 bytes
    cd .. && python scripts/revive_deposits.py --build "plain Solidity, resolc 1.5.0 (PolkaVM)=/tmp/out/<f>.polkavm" \
        --build "plain Solidity, solc 0.8.30 --optimize (EVM)=/tmp/out/<f>.bin"
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forklift import revive_receipt as rr  # noqa: E402
from forklift.chains import V3, units  # noqa: E402
from forklift.hashing import twox128  # noqa: E402
from forklift.rpc import RpcClient  # noqa: E402

SUDO_KEY = "0x" + (twox128("Sudo") + twox128("Key")).hex()


def returns_n_bytes(n: int) -> bytes:
    return bytes([0x61]) + n.to_bytes(2, "big") + bytes([0x60, 0x00, 0xF3])  # PUSH2 n PUSH1 0 RETURN


def writes_k_slots(k: int) -> bytes:
    body = b"".join(bytes([0x7F]) + b"\xff" * 32 + bytes([0x60, i, 0x55]) for i in range(k))  # PUSH32 PUSH1 i SSTORE
    return body + bytes([0x60, 0x01, 0x60, 0x00, 0xF3])  # return 1 byte of code


def fit(points: list[tuple[int, int]]) -> dict:
    """Least-squares line through (x, planck)."""
    n = len(points)
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    return {"fixed_planck": round((sy - slope * sx) / n), "per_unit_planck": round(slope)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="append", default=[], metavar="LABEL=PATH",
                    help="Also dry-run this init code / PolkaVM blob (raw bytes; a .bin file is read as hex text).")
    ap.add_argument("--evidence", type=Path, default=None, help="Write <DIR>/revive-deposits-v3.json.")
    a = ap.parse_args()

    from substrateinterface import SubstrateInterface

    si = SubstrateInterface(url=V3.endpoint, ss58_format=V3.ss58)

    def dry(c: RpcClient, origin: bytes, code: bytes, at: str) -> dict:
        d = rr.dry_instantiate(c, origin, code, at)
        out = {"ok": d.success, "storage_deposit_planck": d.storage_deposit, "error": d.error}
        if d.success:
            # The code hash is keccak256 of the stored code (EVM: the runtime code the init code returned;
            # PolkaVM: the blob). Code already on chain pays no code-upload deposit again.
            stored = d.data if code[:3] != b"PVM" else code
            h = "0x" + rr.keccak256(stored).hex()
            info = si.query("Revive", "CodeInfoOf", [h], block_hash=at).value
            out.update({"code_hash": h, "code_already_stored": info is not None,
                        **({"stored_code_deposit_planck": info["deposit"]} if info else {})})
        return out

    with RpcClient(V3.endpoint, testnet=True) as c:
        if c.call("chain_getBlockHash", [0]) != V3.genesis_hash:
            raise SystemExit(f"{V3.endpoint} is not {V3.label}")
        at = c.call("chain_getFinalizedHead")
        number = int(c.call("chain_getHeader", [at])["number"], 16)
        origin = bytes.fromhex(c.call("state_getStorage", [SUDO_KEY, at])[2:])
        sizes = [{"runtime_bytes": n, **dry(c, origin, returns_n_bytes(n), at)} for n in (1, 100, 200, 400)]
        slots = [{"slots_written": k, **dry(c, origin, writes_k_slots(k), at)} for k in range(4)]
        builds = []
        for label, path in [("receipt-v2 (forklift/contracts/ForkReceipt.bin)", None)] + [
                tuple(b.split("=", 1)) for b in a.build]:
            code = rr.init_code() if path is None else Path(path).read_bytes()
            if path is not None and path.endswith(".bin"):  # hex text (solc / solcjs --bin), with or without 0x
                code = bytes.fromhex(code.decode().strip().removeprefix("0x"))
            builds.append({"label": label, "bytes": len(code), "sha256": hashlib.sha256(code).hexdigest(),
                           **dry(c, origin, code, at)})
    si.close()
    size_fit = fit([(r["runtime_bytes"], r["storage_deposit_planck"]) for r in sizes])
    slot_fit = fit([(r["slots_written"], r["storage_deposit_planck"]) for r in slots])
    out = {"what": "Revive deposits on the Portaldot V3 testnet, measured by ReviveApi_instantiate dry runs",
           "recorded_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "chain": V3.id, "endpoint": V3.endpoint, "at": {"number": number, "hash": at},
           "origin": "Sudo.Key at the pinned block (dry runs only)", "origin_public_key": "0x" + origin.hex(),
           "code_size": sizes, "code_size_fit": size_fit, "slots": slots, "slot_fit": slot_fit, "builds": builds,
           "reproduce": "python scripts/revive_deposits.py --build LABEL=PATH ... (same block: results are fixed "
                        "while the node serves it)"}
    print(f"V3 #{number:,} {at}")
    print(f"code: {units(V3, size_fit['fixed_planck'])} + {units(V3, size_fit['per_unit_planck'])} per runtime byte")
    print(f"storage: {units(V3, slot_fit['per_unit_planck'])} per 32-byte slot written")
    for b in builds:
        print(f"{b['label']}: {b['bytes']:,} bytes -> {units(V3, b['storage_deposit_planck'])}"
              + ("" if b["ok"] else f" (dry run failed: {b['error']})")
              + (f" (code already stored: its {units(V3, b['stored_code_deposit_planck'])} code deposit is not charged again)"
                 if b.get("code_already_stored") else ""))
    if a.evidence:
        a.evidence.mkdir(parents=True, exist_ok=True)
        (a.evidence / "revive-deposits-v3.json").write_text(json.dumps(out, indent=2) + "\n")
        print(f"wrote {a.evidence / 'revive-deposits-v3.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
