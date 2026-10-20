"""``forklift replay <block>-<index>``: re-run a V3 testnet extrinsic on a fork of its parent block.

The extrinsic's bytes are read from the chain block, a Chopsticks fork is started at
that block's parent (so it holds exactly the state the chain applied it to), the
same bytes are applied there, and the fork's outcome is compared with the chain's,
field by field, with the same comparison ``rehearse`` uses. Any extrinsic works,
not only Forklift's: the signer's signature is the original one.

A prediction made *before* sending is priced at the height the fork predicts; the
chain may include it a few blocks later, and on V3 the fee multiplier moves every
block. A replay removes that variable: same parent block, same state, same bytes.
Nothing is sent to the chain.
"""

from __future__ import annotations

from forklift import chopsticks
from forklift.chains import V3, Chain
from forklift.lift import default_cache
from forklift.rehearse import Outcome, RehearseError, accounts_in, apply, compare
from forklift.rpc import RpcClient


def _signer_and_args(si, xt_hex: str) -> tuple[str | None, dict]:
    from scalecodec.base import ScaleBytes

    xt = si.create_scale_object("Extrinsic", data=ScaleBytes(xt_hex))
    xt.decode()
    v = xt.value
    signer = v.get("address")
    args = {a["name"]: a["value"] for a in v.get("call", {}).get("call_args", [])}
    return signer, args


def chain_outcome(si, client: RpcClient, block_number: int, index: int,
                  accounts: list[str]) -> tuple[Outcome, str]:
    """What the chain recorded for extrinsic ``block_number-index``, and its bytes."""
    from forklift.hashing import blake2_256
    from forklift.rehearse import _account, _event

    bh = client.call("chain_getBlockHash", [block_number])
    block = client.call("chain_getBlock", [bh])["block"]
    xt_hex = block["extrinsics"][index]
    parent = block["header"]["parentHash"]
    events = [_event(e) for e in si.get_events(bh) if e.value.get("extrinsic_idx") == index]
    names = [(e["pallet"], e["event"]) for e in events]
    fee = next((int(e["attributes"]["actual_fee"]) for e in events
                if (e["pallet"], e["event"]) == ("TransactionPayment", "TransactionFeePaid")), None)
    before = {a: _account(si, a, parent) for a in accounts}
    after = {a: _account(si, a, bh) for a in accounts}
    fin = client.call("chain_getFinalizedHead")
    finalized = int(client.call("chain_getHeader", [fin])["number"], 16) >= block_number
    out = Outcome(where="chain", endpoint=client.endpoint, block_number=block_number, block_hash=bh,
                  parent_hash=parent, index=index, tx_hash="0x" + blake2_256(bytes.fromhex(xt_hex[2:])).hex(),
                  success=("System", "ExtrinsicSuccess") in names, events=events, fee=fee, before=before,
                  after=after, finalized=finalized)
    out.deltas = {a: after[a]["free"] - before[a]["free"] for a in accounts}
    return out, xt_hex


def replay(extrinsic_id: str, *, ws_port: int, name: str = "replay", chain: Chain = V3) -> dict:
    from substrateinterface import SubstrateInterface

    try:
        number, index = (int(x) for x in extrinsic_id.split("-"))
    except ValueError:
        raise RehearseError(f"{extrinsic_id!r} is not <block>-<index>") from None
    si = SubstrateInterface(url=chain.endpoint, ss58_format=chain.ss58)
    try:
        with RpcClient(chain.endpoint) as c:
            if c.call("chain_getBlockHash", [0]) != chain.genesis_hash:
                raise RehearseError(f"{chain.endpoint} is not {chain.label}")
            bh = c.call("chain_getBlockHash", [number])
            if bh is None:
                raise RehearseError(f"no block #{number} on {chain.label}")
            xts = c.call("chain_getBlock", [bh])["block"]["extrinsics"]
            if not 0 <= index < len(xts):
                raise RehearseError(f"block #{number} has {len(xts)} extrinsics, no index {index}")
            si.init_runtime(block_hash=bh)
            signer, args = _signer_and_args(si, xts[index])
            if signer is None:
                raise RehearseError(f"{extrinsic_id} is unsigned (an inherent): nothing to replay")
            accounts = [signer] + [a for a in accounts_in(args) if a != signer]
            actual, xt_hex = chain_outcome(si, c, number, index, accounts)
    finally:
        si.close()

    rec = chopsticks.start(name, chain, actual.parent_hash, ws_port, default_cache() / "forks" / name)
    try:
        chopsticks.wait_ready(rec)
        fork_si = SubstrateInterface(url=rec["endpoint"], ss58_format=chain.ss58)
        try:
            with RpcClient(rec["endpoint"], fork=True) as fc:
                head = fc.call("chain_getHeader")
                if fc.call("chain_getBlockHash", [int(head["number"], 16)]) != actual.parent_hash:
                    raise RehearseError("the replay fork does not start at the extrinsic's parent block")
                got = apply(fc, fork_si, xt_hex, accounts, where="replay", timeout=120)
        finally:
            fork_si.close()
    finally:
        chopsticks.stop(rec)
    from forklift.rehearse import _plain

    result = {"chain": chain.id, "label": chain.label, "extrinsic_id": extrinsic_id, "signer": signer,
              "accounts": accounts, "fork_parent": {"number": number - 1, "hash": actual.parent_hash},
              "executor": chopsticks.PACKAGE, "actual": _plain(actual), "replay": _plain(got),
              "compare": compare(got, actual)}
    result["actual"]["explorer"] = chain.explorer_block(actual.block_hash)
    return result
