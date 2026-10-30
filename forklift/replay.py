"""``forklift replay <block>-<index>`` (V3 testnet) and ``<block>:<index>`` (Portaldot mainnet, spec 1002).

Re-run a chain extrinsic on a fork of its parent block.

The extrinsic's bytes are read from the chain block, a Chopsticks fork is started at
that block's parent (so it holds exactly the state the chain applied it to), the
same bytes are applied there, and the fork's outcome is compared with the chain's,
field by field, with the same comparison ``rehearse`` uses. Any extrinsic works,
not only Forklift's: the signer's signature is the original one.

A prediction made *before* sending is priced at the height the fork predicts; the
chain may include it a few blocks later, and on V3 the fee multiplier moves every
block. A replay removes that variable: same parent block, same state, same bytes.
Nothing is sent to the chain.

``<block>:<index>`` is a mainnet extrinsic. Mainnet is read-only: the extrinsic's bytes, the
state of every account it names at the parent block and the events it produced are *read*;
the bytes are applied to a Chopsticks fork of the parent block on loopback, never to mainnet
(the fork client is the only path that can submit, invariant I3). Spec 1002's metadata is V13,
so this path decodes with :class:`forklift.tx.Runtime`, not the SDK's V14 interface.
"""

from __future__ import annotations

import time
from pathlib import Path

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
    """``<block>:<index>`` replays a mainnet (1002) extrinsic; ``<block>-<index>`` a V3 testnet one."""
    if ":" in extrinsic_id:
        return replay_mainnet(extrinsic_id, ws_port=ws_port, name=name)
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


# ---------------------------------------------------------------------------------------------
# Mainnet (spec 1002, metadata V13)

# Events whose attributes can name the block author, which differs on a fork (a fork builds its own blocks).
AUTHOR_EVENTS = {"Balances.Deposit"}


def _parse_mainnet_id(extrinsic_id: str) -> tuple[int, int]:
    try:
        number, index = (int(x) for x in extrinsic_id.split(":"))
        if number < 1 or index < 0:
            raise ValueError
    except ValueError:
        raise RehearseError(f"{extrinsic_id!r} is not <block>:<index>") from None
    return number, index


def _walk_addresses(value, found: list[str]) -> None:
    from forklift.tx import as_ss58

    if isinstance(value, dict):
        for v in value.values():
            _walk_addresses(v, found)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _walk_addresses(v, found)
    elif isinstance(value, str):
        value = as_ss58(value)
        if value not in found and 46 <= len(value) <= 50 and accounts_in(value):
            found.append(value)


def _named_accounts(call_args: list[dict], events: list) -> list[str]:
    """Account ids the call arguments and the extrinsic's events name, by declared type, in order."""
    from forklift.tx import _is_account_type

    found: list[str] = []
    for a in call_args:
        if _is_account_type(str(a.get("type", ""))):
            _walk_addresses(a.get("value"), found)
    for e in events:
        for a in e["attributes"] if isinstance(e["attributes"], list) else []:
            if isinstance(a, dict) and _is_account_type(str(a.get("type", ""))):
                _walk_addresses(a.get("value"), found)
    return found


def _account_1002(client: RpcClient, rt, address: str, block_hash: str) -> dict:
    from forklift.tx import ss58_to_public

    raw = client.call("state_getStorage", [rt.storage_key("System", "Account", ss58_to_public(address)), block_hash])
    v = rt.decode_storage("System", "Account", raw)
    return {"nonce": v["nonce"], **{k: v["data"][k] for k in ("free", "reserved", "misc_frozen", "fee_frozen")}}


def _events_json(rt, client: RpcClient, block_hash: str, index: int) -> list[dict]:
    from forklift.tx import events_at, readable_attributes

    return [{"pallet": e.pallet, "event": e.name, "attributes": readable_attributes(e.attributes)}
            for e in events_at(client, rt, block_hash) if e.phase == "ApplyExtrinsic" and e.extrinsic_index == index]


def _outcome_1002(rt, client: RpcClient, *, where: str, block_number: int, block_hash: str, parent_hash: str,
                  index: int, tx_hash: str, accounts: list[str], before: dict, finalized: bool | None) -> dict:
    from forklift.tx import _outcome, events_at

    events = _events_json(rt, client, block_hash, index)
    mine = [e for e in events_at(client, rt, block_hash) if e.phase == "ApplyExtrinsic" and e.extrinsic_index == index]
    success, error = _outcome(rt, mine)
    after = {a: _account_1002(client, rt, a, block_hash) for a in accounts}
    fee = next((e["attributes"][0]["value"] for e in events if (e["pallet"], e["event"]) == ("Treasury", "Deposit")), None)
    return {"where": where, "endpoint": client.endpoint, "extrinsic_id": f"{block_number}:{index}",
            "block_number": block_number, "block_hash": block_hash, "parent_hash": parent_hash,
            "tx_hash": tx_hash, "success": success, "error": error, "events": events, "fee": fee,
            "before": before, "after": after, "finalized": finalized,
            "deltas": {a: {k: after[a][k] - before[a][k] for k in after[a]} for a in accounts}}


def _drop_accounts(attrs):
    from forklift.tx import _is_account_type

    return [a for a in attrs if not (isinstance(a, dict) and _is_account_type(str(a.get("type", ""))))] \
        if isinstance(attrs, list) else attrs


def compare_1002(fork: dict, chain: dict, parent_state_equal: dict[str, bool], code_equal: bool) -> dict:
    """Field by field: parent state, tx hash, outcome, event sequence + attributes, fee, inclusion height, deltas.

    The one explained exception is an event that names the block author (``Balances.Deposit``): its
    account is not compared (a fork builds its own blocks), its amount still is.
    """
    rows: list[dict] = []

    def row(name, f, c, note=None, explained=False):
        rows.append({"field": name, "fork": f, "chain": c, "match": f == c,
                     **({"note": note} if note else {}), **({"explained": True} if explained and f != c else {})})

    row("runtime_code_hash_at_parent", code_equal, True)
    for a, ok in parent_state_equal.items():
        row(f"parent System.Account {a}", ok, True)
    row("tx_hash", fork["tx_hash"], chain["tx_hash"])
    row("success", fork["success"], chain["success"])
    row("error", fork["error"], chain["error"])
    names = lambda o: [f"{e['pallet']}.{e['event']}" for e in o["events"]]  # noqa: E731
    row("event_sequence", names(fork), names(chain))
    for i, (fe, ce) in enumerate(zip(fork["events"], chain["events"])):
        if (fe["pallet"], fe["event"]) == (ce["pallet"], ce["event"]):
            label = f"event[{i}] {fe['pallet']}.{fe['event']}"
            if f"{fe['pallet']}.{fe['event']}" in AUTHOR_EVENTS:
                row(label, _drop_accounts(fe["attributes"]), _drop_accounts(ce["attributes"]),
                    "block-author account not compared: a fork builds its own blocks")
            else:
                row(label, fe["attributes"], ce["attributes"])
    row("inclusion_height", fork["block_number"], chain["block_number"])
    row("fee (Treasury.Deposit)", fork["fee"], chain["fee"])
    for a in chain["deltas"]:
        for k, v in chain["deltas"][a].items():
            row(f"delta {a} {k}", fork["deltas"].get(a, {}).get(k), v)
    differs = [r for r in rows if not r["match"]]
    return {"match": not differs, "fields": len(rows), "differs": [r["field"] for r in differs], "rows": rows}


def _dropped_reason(log_path: str) -> str:
    """Why Chopsticks left the extrinsic out, from the 'Failed to apply extrinsic' entry of its log."""
    for _ in range(10):  # the log line can land a moment after dev_newBlock answers
        try:
            lines = Path(log_path).read_text(errors="replace").splitlines()
        except OSError:
            break
        i = next((k for k in range(len(lines) - 1, -1, -1) if "Failed to apply extrinsic" in lines[k]), None)
        if i is not None:
            msg = next((x.strip() for x in lines[i:i + 8] if x.strip().startswith('"message"')), "")
            return ("Chopsticks 1.5.2 reported 'Failed to apply extrinsic' " + msg.replace('"message": ', "")[:300]
                    + " (it decodes ApplyExtrinsicResult with the current DispatchError type, which is consistent with "
                    "it not reading spec 1002's module-error shape: a call that fails with a module error was not replayable)")
        time.sleep(0.5)
    return "see the Chopsticks log"


def replay_mainnet(extrinsic_id: str, *, ws_port: int, name: str = "replay") -> dict:
    """Read mainnet extrinsic ``<block>:<index>``; re-run its bytes on a fork of the parent block; compare."""
    from forklift.chains import MAINNET
    from forklift.hashing import blake2_256
    from forklift.tx import Runtime, as_ss58

    number, index = _parse_mainnet_id(extrinsic_id)
    chain = MAINNET
    with RpcClient(chain.endpoint) as c:  # read-only client: cannot write (I3)
        if c.call("chain_getBlockHash", [0]) != chain.genesis_hash:
            raise RehearseError(f"{chain.endpoint} is not {chain.label}")
        bh = c.call("chain_getBlockHash", [number])
        if bh is None:
            raise RehearseError(f"no block #{number} on {chain.label}")
        block = c.call("chain_getBlock", [bh])["block"]
        xts = block["extrinsics"]
        if not 0 <= index < len(xts):
            raise RehearseError(f"block #{number} has {len(xts)} extrinsics, no index {index}")
        xt_hex, parent = xts[index], block["header"]["parentHash"]
        rt = Runtime.load(c, parent)  # the runtime the block was executed with
        decoded = rt.decode("Extrinsic", xt_hex)
        if not (decoded.get("address") or decoded.get("account_id")):
            raise RehearseError(f"{extrinsic_id} is unsigned (an inherent): nothing to replay")
        signer = as_ss58(decoded["address"])
        call = decoded["call"]
        events = _events_json(rt, c, bh, index)
        accounts = [signer] + [a for a in _named_accounts(call["call_args"], events) if a != signer]
        before = {a: _account_1002(c, rt, a, parent) for a in accounts}
        tx_hash = "0x" + blake2_256(bytes.fromhex(xt_hex[2:])).hex()
        fin = int(c.call("chain_getHeader", [c.call("chain_getFinalizedHead")])["number"], 16)
        chain_out = _outcome_1002(rt, c, where="mainnet", block_number=number, block_hash=bh, parent_hash=parent,
                                  index=index, tx_hash=tx_hash, accounts=accounts, before=before,
                                  finalized=fin >= number)
        mainnet_code = c.call("state_getStorage", ["0x3a636f6465", parent])
        mainnet_code_hash = "0x" + blake2_256(bytes.fromhex(mainnet_code[2:])).hex()

    rec = chopsticks.start(name, chain, parent, ws_port, default_cache() / "forks" / name)
    try:
        chopsticks.wait_ready(rec)
        with RpcClient(rec["endpoint"], fork=True) as fc:
            ident = chopsticks.fork_identity(fc)
            if ident["head_hash"] != parent:
                raise RehearseError("the replay fork does not start at the extrinsic's parent block")
            fork_before = {a: _account_1002(fc, rt, a, parent) for a in accounts}
            # validate_transaction panics on the spec-1002 runtime under Chopsticks 1.5.2 (cause not isolated)
            # ('Bad input data … Codec error', measured 2026-10-13 and again here), so the bytes go in through
            # dev_newBlock's `transactions`, which applies them in a block built on the parent. A write: a
            # fork client only, and dev_newBlock answers once the block exists (no retry: it would build two).
            with RpcClient(rec["endpoint"], fork=True, timeout=300.0, retries=0) as slow:
                new_hash = slow.call("dev_newBlock", [{"transactions": [xt_hex]}])
            new_block = fc.call("chain_getBlock", [new_hash])["block"]
            if xt_hex not in new_block["extrinsics"]:
                raise RehearseError("the fork built a block without the extrinsic: " + _dropped_reason(rec["log"]))
            fork_out = _outcome_1002(rt, fc, where="fork", block_number=int(new_block["header"]["number"], 16),
                                     block_hash=new_hash, parent_hash=new_block["header"]["parentHash"],
                                     index=new_block["extrinsics"].index(xt_hex), tx_hash=tx_hash,
                                     accounts=accounts, before=fork_before, finalized=None)
    finally:
        chopsticks.stop(rec)
    state_equal = {a: fork_before[a] == before[a] for a in accounts}
    compared = compare_1002(fork_out, chain_out, state_equal, ident["code_hash"] == mainnet_code_hash)
    return {"chain": chain.id, "label": chain.label, "extrinsic_id": extrinsic_id, "signer": signer,
            "call": f"{call['call_module']}.{call['call_function']}", "accounts": accounts,
            "fork_parent": {"number": number - 1, "hash": parent}, "executor": chopsticks.PACKAGE,
            "fork_runtime": {"code_hash": ident["code_hash"], "spec": ident["spec"]},
            "actual": chain_out, "replay": fork_out, "compare": compared}
