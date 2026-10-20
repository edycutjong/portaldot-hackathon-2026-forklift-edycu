"""``forklift rehearse``: predict a transaction on a V3 fork, then send the same bytes to the V3 testnet.

One extrinsic is signed once. Its exact bytes are applied on the Chopsticks fork
first (the prediction: events, fee, balance deltas of every account it names),
and, with ``--send``, the same bytes go to the live chain (same tx hash). The
two outcomes are compared field by field; a difference is reported, never
smoothed over (exit 7).

A prediction is only as good as the fork's state. Before signing, the signer's
and every named account's ``System.Account`` are read on the fork head and the
chain's finalized head; if any differ, the fork is stale and nothing is signed
(re-run ``forklift up --target v3 --at head``). The fee multiplier is not part of
that check because it moves every block: on the idle V3 testnet
``TransactionPayment.NextFeeMultiplier`` changes block by block
(``evidence/v3-fee-multiplier-*.json``, ``scripts/fee_multiplier.py``), so
a fork a few blocks behind predicts a slightly different fee. Instead the fork
is advanced with empty blocks (``dev_newBlock``) to the chain's best block; if
the multiplier those blocks leave differs from the chain's at that height, the
chain's value is set on the fork (both values are recorded). The transaction is applied on top: the prediction is for inclusion in
the next block, and the predicted and actual inclusion heights are both recorded.

Writes go through Forklift's own client and write-guard: the fork through a
fork client (loopback, must answer Chopsticks ``dev_*``), the chain through a
testnet client (``author_submitExtrinsic`` only, pinned writable genesis only).
The SDK composes, signs and decodes; it never sends.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forklift.chains import V3, Chain
from forklift.hashing import blake2_256, twox128
from forklift.rpc import RpcClient, RpcError

EXIT_PREDICTION_DIFFERS = 7
POOL_INVALID = 1010  # author_submitExtrinsic: the transaction pool rejected it as invalid


class RehearseError(Exception):
    pass


def load_signer(path: Path, ss58: int = 42):
    """A keypair from a seed file (mnemonic, 0x seed or //URI). The secret is never echoed."""
    from substrateinterface import Keypair

    try:
        text = Path(path).expanduser().read_text().strip()
    except OSError as exc:
        raise RehearseError(f"cannot read the signer file {path}: {exc.strerror}") from None
    mode = Path(path).expanduser().stat().st_mode & 0o077
    if mode:
        raise RehearseError(f"{path} is readable by group/others (mode {oct(mode)}): chmod 600 it first")
    try:
        if text.startswith("0x"):
            return Keypair.create_from_seed(text, ss58_format=ss58)
        if text.startswith("//") or text.startswith("/"):
            return Keypair.create_from_uri(text, ss58_format=ss58)
        return Keypair.create_from_mnemonic(text, ss58_format=ss58)
    except Exception:  # noqa: BLE001  the SDK's message may quote the secret
        raise RehearseError(f"{path} holds no valid mnemonic, seed or URI") from None


def accounts_in(params: Any) -> list[str]:
    """Every SS58 address named in the call arguments (nested calls included)."""
    from scalecodec.utils.ss58 import is_valid_ss58_address

    found: list[str] = []

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif isinstance(v, str) and 46 <= len(v) <= 50 and is_valid_ss58_address(v):
            if v not in found:
                found.append(v)

    walk(params)
    return found


def _account(si, address: str, block_hash: str) -> dict:
    v = si.query("System", "Account", [address], block_hash=block_hash).value
    return {"nonce": v["nonce"], "free": v["data"]["free"], "reserved": v["data"]["reserved"],
            "frozen": v["data"]["frozen"]}


FEE_MULTIPLIER_KEY = "0x" + (twox128("TransactionPayment") + twox128("NextFeeMultiplier")).hex()


def fee_multiplier(client: RpcClient, number: int) -> int | None:
    """``TransactionPayment.NextFeeMultiplier`` (FixedU128, u128 LE) at block ``number``, by raw key."""
    bh = client.call("chain_getBlockHash", [number])
    raw = client.call("state_getStorage", [FEE_MULTIPLIER_KEY, bh]) if bh else None
    return int.from_bytes(bytes.fromhex(raw[2:]), "little") if raw else None


def _event(e) -> dict:
    v = e.value
    return {"pallet": v["module_id"], "event": v["event_id"], "attributes": v["attributes"]}


@dataclass
class Outcome:
    where: str  # "fork" | "chain"
    endpoint: str
    block_number: int
    block_hash: str
    parent_hash: str
    index: int
    tx_hash: str
    success: bool
    events: list[dict]
    fee: int | None
    before: dict[str, dict]
    after: dict[str, dict]
    finalized: bool = False
    wait_s: float = 0.0
    deltas: dict[str, int] = field(default_factory=dict)

    @property
    def extrinsic_id(self) -> str:
        return f"{self.block_number}-{self.index}"


def apply(client: RpcClient, si, xt_hex: str, accounts: list[str], *, where: str, timeout: float = 180.0,
          wait_finalized: bool = False, sleep=time.sleep) -> Outcome:
    """Submit ``xt_hex`` through Forklift's guarded client, find the block it landed in, read its outcome."""
    head = client.call("chain_getHeader")
    start = int(head["number"], 16)
    tx_hash = client.call("author_submitExtrinsic", [xt_hex])
    want = "0x" + blake2_256(bytes.fromhex(xt_hex[2:])).hex()
    if tx_hash != want:
        raise RehearseError(f"{where}: node returned tx hash {tx_hash}, expected blake2_256(xt) {want}")
    t0 = time.monotonic()
    found = None
    scanned = start
    while found is None:
        if time.monotonic() - t0 > timeout:
            raise RehearseError(f"{where}: {tx_hash} not included within {timeout:.0f} s (scanned to #{scanned})")
        top = int(client.call("chain_getHeader")["number"], 16)
        for n in range(scanned + 1, top + 1):
            bh = client.call("chain_getBlockHash", [n])
            block = client.call("chain_getBlock", [bh])["block"]
            if xt_hex in block["extrinsics"]:
                found = (n, bh, block["extrinsics"].index(xt_hex), block["header"]["parentHash"])
                break
            scanned = n
        if found is None:
            sleep(1.0)
    number, block_hash, index, parent = found
    finalized = False
    if wait_finalized:
        while time.monotonic() - t0 < timeout * 2:
            fin = client.call("chain_getFinalizedHead")
            if int(client.call("chain_getHeader", [fin])["number"], 16) >= number:
                finalized = client.call("chain_getBlockHash", [number]) == block_hash
                break
            sleep(2.0)
    events = [_event(e) for e in si.get_events(block_hash) if e.value.get("extrinsic_idx") == index]
    names = [(e["pallet"], e["event"]) for e in events]
    fee = next((int(e["attributes"]["actual_fee"]) for e in events
                if (e["pallet"], e["event"]) == ("TransactionPayment", "TransactionFeePaid")), None)
    before = {a: _account(si, a, parent) for a in accounts}
    after = {a: _account(si, a, block_hash) for a in accounts}
    out = Outcome(where=where, endpoint=client.endpoint, block_number=number, block_hash=block_hash,
                  parent_hash=parent, index=index, tx_hash=tx_hash, success=("System", "ExtrinsicSuccess") in names,
                  events=events, fee=fee, before=before, after=after, finalized=finalized,
                  wait_s=round(time.monotonic() - t0, 2))
    out.deltas = {a: after[a]["free"] - before[a]["free"] for a in accounts}
    return out


# Events whose attributes name the block author or depend on the block, not on the transaction.
BLOCK_DEPENDENT = {("Balances", "Deposit")}


def compare(pred: Outcome, actual: Outcome) -> dict:
    """Field-by-field: tx hash, success, event sequence + attributes, fee, every named account's delta."""
    def key(e):
        return (e["pallet"], e["event"])

    rows = []

    def row(name, p, a, note=None):
        rows.append({"field": name, "predicted": p, "actual": a, "match": p == a, **({"note": note} if note else {})})

    row("tx_hash", pred.tx_hash, actual.tx_hash)
    row("success", pred.success, actual.success)
    row("event_sequence", [f"{p}.{e}" for p, e in map(key, pred.events)],
        [f"{p}.{e}" for p, e in map(key, actual.events)])
    for i, (pe, ae) in enumerate(zip(pred.events, actual.events)):
        if key(pe) == key(ae):
            note = "names the block author / fee receiver" if key(pe) in BLOCK_DEPENDENT else None
            row(f"event[{i}] {pe['pallet']}.{pe['event']}", pe["attributes"], ae["attributes"], note)
    row("inclusion_height", pred.block_number, actual.block_number,
        None if pred.block_number == actual.block_number else "the fee multiplier moves every block")
    row("fee", pred.fee, actual.fee)
    for a in pred.deltas:
        row(f"delta {a}", pred.deltas[a], actual.deltas.get(a))
    differs = [r for r in rows if not r["match"]]
    return {"match": not differs, "fields": len(rows), "differs": [r["field"] for r in differs], "rows": rows}


def staleness(fork_si, chain_si, accounts: list[str]) -> dict:
    """Do the fork head and the chain head agree on everything the prediction depends on?"""
    fh, ch = fork_si.get_chain_head(), chain_si.get_chain_finalised_head()
    diffs = []
    for a in accounts:
        f, c = _account(fork_si, a, fh), _account(chain_si, a, ch)
        if f != c:
            diffs.append({"account": a, "fork": f, "chain": c})
    return {"fork_head": fh, "chain_finalized_head": ch, "fresh": not diffs, "diffs": diffs}


MAX_ADVANCE = 100  # empty blocks to catch up (~2 min of Chopsticks at ~1.2 s a block); more: re-fork at head
CHUNK = 5


def chain_best(endpoint: str) -> int:
    """The chain's best block number, through Forklift's own read-only client (it retries and reconnects)."""
    with RpcClient(endpoint) as c:
        return int(c.call("chain_getHeader")["number"], 16)


def advance(fc: RpcClient, chain_endpoint: str, *, chunk: int = CHUNK, best_of=None) -> dict:
    """Build empty fork blocks until the fork head number equals the chain's best block number.

    ``dev_newBlock`` answers only once every block is built, so it is sent in chunks on a client with a
    long timeout and no retries (a retried write would build the chunk twice).
    """
    best_of = best_of or (lambda: chain_best(chain_endpoint))
    best = best_of()
    have = int(fc.call("chain_getHeader")["number"], 16)
    if have > best:
        raise RehearseError(f"the fork (#{have}) is ahead of the chain (#{best}): re-run `forklift up --target v3 --at head`")
    if best - have > MAX_ADVANCE:
        raise RehearseError(f"the fork is {best - have} blocks behind the chain: re-run `forklift up --target v3 --at head`")
    built = 0
    with RpcClient(fc.endpoint, fork=True, timeout=300.0, retries=0) as slow:  # the first block after a fork loads state lazily
        for _ in range(10):
            k = best - have
            if k <= 0:
                break
            slow.call("dev_newBlock", [{"count": min(k, chunk)}])
            built += min(k, chunk)
            have = int(slow.call("chain_getHeader")["number"], 16)
            best = best_of()
    head = have
    fork_m = fee_multiplier(fc, head)
    with RpcClient(chain_endpoint) as c:
        chain_m = fee_multiplier(c, head)
    synced = False
    if chain_m is not None and fork_m != chain_m:
        # The fork's empty blocks do not carry what the chain's blocks carried over the same range, so the
        # multiplier can end up different at the same height (seen on 2026-10-20 runs; each run records both
        # values). The one volatile item the fee depends on is re-read from the chain at that height.
        fc.call("dev_setStorage", [[[FEE_MULTIPLIER_KEY, "0x" + chain_m.to_bytes(16, "little").hex()]]])
        synced = fee_multiplier(fc, head) == chain_m
    return {"chain_best": best, "built_empty_blocks": built, "fork_head": head,
            "fee_multiplier": {"fork_after_empty_blocks": str(fork_m), "chain": str(chain_m),
                               "equal": fork_m == chain_m, "synced_from_chain": synced}}


def sign(chain_si, keypair, module: str, function: str, params: dict, nonce: int):
    call = chain_si.compose_call(module, function, params)
    xt = chain_si.create_signed_extrinsic(call, keypair, nonce=nonce)  # immortal era, tip 0
    return "0x" + bytes(xt.data.data).hex()


def explorer(chain: Chain, block_hash: str) -> str:
    return chain.explorer_block(block_hash)


def rehearse(fork_endpoint: str, keypair, module: str, function: str, params: dict, *, send: bool,
             chain: Chain = V3, timeout: float = 180.0) -> dict:
    from substrateinterface import SubstrateInterface

    accounts = [keypair.ss58_address] + [a for a in accounts_in(params) if a != keypair.ss58_address]
    with RpcClient(fork_endpoint, fork=True) as fc:
        if fc.call("chain_getBlockHash", [0]) != chain.genesis_hash:
            raise RehearseError(f"{fork_endpoint} is not a fork of {chain.label}")
        caught_up = advance(fc, chain.endpoint)  # minutes at most; SDK sockets are opened after it, never left idle
    fork_si = SubstrateInterface(url=fork_endpoint, ss58_format=chain.ss58)
    chain_si = SubstrateInterface(url=chain.endpoint, ss58_format=chain.ss58)
    try:
        fresh = staleness(fork_si, chain_si, accounts)
        if not fresh["fresh"]:
            raise RehearseError("the fork is stale for this transaction (" + "; ".join(
                d.get("account", d.get("item", "?")) for d in fresh["diffs"])
                + "): re-run `forklift up --target v3 --at head` first")
        nonce = _account(chain_si, keypair.ss58_address, fresh["chain_finalized_head"])["nonce"]
        xt_hex = sign(chain_si, keypair, module, function, params, nonce)
        with RpcClient(fork_endpoint, fork=True) as fc:
            try:
                pred = apply(fc, fork_si, xt_hex, accounts, where="fork", timeout=60)
            except RpcError as exc:
                if exc.code != POOL_INVALID:
                    raise
                pred = None  # the fork's pool refused it: the prediction is "the chain's pool refuses it too"
                rejected = exc.error
        result = {"chain": chain.id, "label": chain.label, "call": f"{module}.{function}", "params": params,
                  "signer": keypair.ss58_address, "nonce": nonce, "accounts": accounts,
                  "extrinsic_bytes": (len(xt_hex) - 2) // 2, "staleness_check": fresh, "advance": caught_up,
                  "predicted": _plain(pred) if pred else {"success": False, "pool_rejected": rejected},
                  "actual": None, "compare": None}
        if send and pred is not None and pred.success:  # a call the fork predicts to fail is never sent
            with RpcClient(chain.endpoint, testnet=True) as cc:
                actual = apply(cc, chain_si, xt_hex, accounts, where="chain", timeout=timeout, wait_finalized=True)
            result["actual"] = _plain(actual)
            result["actual"]["explorer"] = explorer(chain, actual.block_hash)
            result["compare"] = compare(pred, actual)
        return result
    finally:
        fork_si.close()
        chain_si.close()


def _plain(o: Outcome) -> dict:
    return {"where": o.where, "endpoint": o.endpoint, "extrinsic_id": o.extrinsic_id, "block_number": o.block_number,
            "block_hash": o.block_hash, "parent_hash": o.parent_hash, "tx_hash": o.tx_hash, "success": o.success,
            "events": o.events, "fee": o.fee, "deltas": o.deltas, "before": o.before, "after": o.after,
            "finalized": o.finalized, "wait_s": o.wait_s}
