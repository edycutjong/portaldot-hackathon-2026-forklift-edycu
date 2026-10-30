"""Fork Receipt v2: the Receipt as a Revive contract on the Portaldot V3 testnet (``forklift receipt --target v3``).

v1 (``receipt.py``, ``contracts/receipt.wat``) lives on the fork's own rent-era Contracts pallet and
is gone with the fork. v2 is public: ``contracts/ForkReceipt.yul`` (interface ``IForkReceipt.sol``),
EVM bytecode run by V3's Revive pallet (``Revive.AllowEVMBytecode`` is true there). ``attest``
stores the manifest root and the key count under the pinned block hash and emits all three
(plus the attester) as an anonymous event, which arrives as ``Revive.ContractEmitted``.

Every write is dry-run first through the chain's own runtime API (``ReviveApi_instantiate`` /
``ReviveApi_call``): the weight and storage deposit come from that dry run, and the funder's free
balance is checked against the deposits before anything is signed. Revive holds a storage deposit
per code byte and per storage slot, which is why the contract is hand-written Yul (84 bytes of
init code): the resolc (PolkaVM) build of a plain-Solidity version needs 625.488 tPOT of deposit on V3
(``scripts/revive_deposits.py``).

Writes go through Forklift's testnet client (``author_submitExtrinsic`` only, pinned writable
genesis only); runtime-API reads through ``state_call``. The SDK composes, signs and decodes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from forklift.chains import V3, Chain
from forklift.rpc import RpcClient
from forklift.tx import encode_compact

KEYS_MAX = 2**64 - 1
REVERT_FLAG = 0x1  # pallet-revive ReturnFlags::REVERT
WEIGHT_FLOOR = {"ref_time": 10**10, "proof_size": 2_000_000}  # dry-run weights x 20, at least this
DEPOSIT_MARGIN_PERMILLE = 100  # storage_deposit_limit = dry-run deposit + 10 %
FEE_RESERVE = 10**13  # 0.1 tPOT kept back for the two transaction fees
EXIT_MISMATCH = 2  # the emitted or stored receipt is not the manifest's
EXIT_DISPATCH_FAILED = 5


class ReviveReceiptError(Exception):
    def __init__(self, message: str, partial: dict | None = None):
        super().__init__(message)
        self.partial = partial  # the run so far, when a transaction had already landed


def keccak256(data: bytes) -> bytes:
    from Crypto.Hash import keccak  # pycryptodome, a substrate-interface dependency

    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def selector(signature: str) -> bytes:
    return keccak256(signature.encode())[:4]


ATTEST = "attest(bytes32,bytes32,uint64)"
RECEIPT = "receipt(bytes32)"


def init_code() -> bytes:
    """The deployed init code: ``contracts/ForkReceipt.bin``, solc's output for ``ForkReceipt.yul``."""
    text = resources.files("forklift.contracts").joinpath("ForkReceipt.bin").read_text().strip()
    return bytes.fromhex(text.removeprefix("0x"))


def _b32(hex_str: str, what: str) -> bytes:
    b = bytes.fromhex(hex_str.removeprefix("0x"))
    if len(b) != 32:
        raise ValueError(f"{what} must be 32 bytes")
    return b


def attest_data(block_hash: str, root: str, keys_lifted: int) -> bytes:
    if not 0 <= keys_lifted <= KEYS_MAX:
        raise ValueError("keys_lifted must fit in a uint64")
    return (selector(ATTEST) + _b32(block_hash, "block hash") + _b32(root, "manifest root")
            + keys_lifted.to_bytes(32, "big"))


def receipt_query(block_hash: str) -> bytes:
    return selector(RECEIPT) + _b32(block_hash, "block hash")


def decode_receipt(data: bytes) -> dict:
    """``receipt(bytes32)`` return data: (bytes32 manifestRoot, uint64 keysLifted)."""
    if len(data) != 64:
        raise ValueError(f"receipt() returns 64 bytes, got {len(data)}")
    return {"manifest_root": "0x" + data[:32].hex(), "keys_lifted": int.from_bytes(data[32:], "big")}


def decode_attested(topics: list[str], data: bytes) -> dict:
    """The anonymous ``Attested`` event: topic 0 = block hash; data = root ‖ keysLifted ‖ attester."""
    if len(topics) != 1 or len(data) != 96:
        raise ValueError(f"an Attested event has 1 topic and 96 data bytes, got {len(topics)} and {len(data)}")
    return {"block_hash": "0x" + _b32(topics[0], "topic").hex(), "manifest_root": "0x" + data[:32].hex(),
            "keys_lifted": int.from_bytes(data[32:64], "big"), "attester": "0x" + data[76:96].hex()}


def expected(manifest: dict) -> dict:
    return {"block_hash": manifest["source"]["block_hash"].lower(), "manifest_root": manifest["manifest_root"].lower(),
            "keys_lifted": int(manifest["load"]["keys_lifted"])}


def differs(got: dict, manifest: dict) -> list[str]:
    """Fields of ``got`` that differ from the manifest (only the fields ``got`` carries)."""
    want = expected(manifest)
    return [k for k in want if k in got and str(got[k]).lower() != str(want[k]).lower()]


# --- ReviveApi over state_call -------------------------------------------------------------------

def _read_compact(raw: bytes, pos: int) -> tuple[int, int]:
    mode = raw[pos] & 3
    if mode == 0:
        return raw[pos] >> 2, pos + 1
    if mode == 1:
        return int.from_bytes(raw[pos:pos + 2], "little") >> 2, pos + 2
    if mode == 2:
        return int.from_bytes(raw[pos:pos + 4], "little") >> 2, pos + 4
    n = (raw[pos] >> 2) + 4
    return int.from_bytes(raw[pos + 1:pos + 1 + n], "little"), pos + 1 + n


def _bytes(data: bytes) -> bytes:
    return encode_compact(len(data)) + data


@dataclass
class DryRun:
    ok: bool  # the runtime call returned Ok (the contract ran)
    reverted: bool
    data: bytes
    weight_required: dict
    storage_deposit: int  # planck; negative = refund
    address: str | None = None  # instantiate only
    error: str | None = None  # raw SCALE DispatchError (hex) when not ok

    @property
    def success(self) -> bool:
        return self.ok and not self.reverted


def decode_contract_result(raw: bytes, *, instantiate: bool = False) -> DryRun:
    """``ContractResult`` as V3's ReviveApi returns it (layout checked against V3 on 2026-10-31):
    weight_consumed, weight_required (Compact u64 pairs), storage_deposit, max_storage_deposit
    (Refund|Charge u128), gas_consumed u128, Result<ExecReturnValue{flags u32, data}[, H160], DispatchError>."""
    pos, w = 0, []
    for _ in range(4):
        v, pos = _read_compact(raw, pos)
        w.append(v)
    deposit = (1 if raw[pos] else -1) * int.from_bytes(raw[pos + 1:pos + 17], "little")
    pos += 34 + 16  # both deposits, then gas_consumed
    common = {"weight_required": {"ref_time": w[2], "proof_size": w[3]}, "storage_deposit": deposit}
    if raw[pos] != 0:
        return DryRun(ok=False, reverted=False, data=b"", error="0x" + raw[pos + 1:].hex(), **common)
    flags = int.from_bytes(raw[pos + 1:pos + 5], "little")
    n, pos = _read_compact(raw, pos + 5)
    data = raw[pos:pos + n]
    address = "0x" + raw[pos + n:pos + n + 20].hex() if instantiate else None
    if instantiate and len(address) != 42:
        raise ReviveReceiptError("ReviveApi_instantiate returned no contract address")
    return DryRun(ok=True, reverted=bool(flags & REVERT_FLAG), data=data, address=address, **common)


def state_call(client: RpcClient, api: str, args: bytes, at: str | None = None) -> bytes:
    raw = client.call("state_call", [api, "0x" + args.hex()] + ([at] if at else []))
    return bytes.fromhex(raw.removeprefix("0x"))


def h160_of(client: RpcClient, public_key: bytes) -> str:
    """The address an account acts as inside Revive (``msg.sender``), as the runtime computes it."""
    return "0x" + state_call(client, "ReviveApi_address", public_key).hex()


def dry_instantiate(client: RpcClient, origin: bytes, code: bytes, at: str | None = None) -> DryRun:
    args = (origin + (0).to_bytes(16, "little") + b"\x00" + b"\x00" + b"\x00" + _bytes(code) + _bytes(b"")
            + b"\x00")  # value 0, no weight / deposit limit, Code::Upload(code), no data, no salt
    return decode_contract_result(state_call(client, "ReviveApi_instantiate", args, at), instantiate=True)


def dry_call(client: RpcClient, origin: bytes, dest: str, data: bytes, at: str | None = None) -> DryRun:
    dest_b = bytes.fromhex(dest.removeprefix("0x"))
    if len(dest_b) != 20:
        raise ValueError("dest must be a 20-byte H160")
    args = origin + dest_b + (0).to_bytes(16, "little") + b"\x00" + b"\x00" + _bytes(data)
    return decode_contract_result(state_call(client, "ReviveApi_call", args, at))


def weight_limit(required: dict) -> dict:
    return {k: max(20 * required[k], WEIGHT_FLOOR[k]) for k in WEIGHT_FLOOR}


def deposit_limit(deposit: int) -> int:
    return max(deposit, 0) * (1000 + DEPOSIT_MARGIN_PERMILLE) // 1000


def read_receipt(client: RpcClient, origin: bytes, address: str, block_hash: str, at: str | None = None) -> dict:
    d = dry_call(client, origin, address, receipt_query(block_hash), at)
    if not d.success:
        raise ReviveReceiptError(f"receipt() on {address} failed (ok={d.ok}, reverted={d.reverted}, error={d.error})")
    return decode_receipt(d.data)


# --- events ----------------------------------------------------------------------------------------

def _hexstr(v: Any) -> str:
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    if isinstance(v, (list, tuple)):  # [u8; N] as a list of ints
        return "0x" + bytes(v).hex()
    return str(v).lower()


def emitted(events: list[dict], contract: str) -> list[dict]:
    """``Revive.ContractEmitted`` events of ``contract`` (rehearse's event dicts), decoded as Receipts."""
    out = []
    for e in events:
        if (e["pallet"], e["event"]) != ("Revive", "ContractEmitted"):
            continue
        a = e["attributes"]
        if _hexstr(a.get("contract")) != contract.lower():
            continue
        topics = [_hexstr(t) for t in a.get("topics", [])]
        data = bytes.fromhex(_hexstr(a.get("data")).removeprefix("0x"))
        try:
            out.append({"decoded": decode_attested(topics, data), "raw": {"topics": topics, "data": "0x" + data.hex()}})
        except ValueError as exc:
            out.append({"decoded": None, "error": str(exc), "raw": {"topics": topics, "data": "0x" + data.hex()}})
    return out


def instantiated(events: list[dict]) -> str | None:
    for e in events:
        if (e["pallet"], e["event"]) == ("Revive", "Instantiated"):
            return _hexstr(e["attributes"].get("contract"))
    return None


# --- the run ---------------------------------------------------------------------------------------

def state_path() -> Path:
    from forklift.lift import default_cache

    return default_cache() / "receipt-v3.json"


def load_state(chain: Chain = V3) -> dict | None:
    try:
        st = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return None
    ok = (isinstance(st, dict) and st.get("genesis_hash") == chain.genesis_hash and st.get("address")
          and st.get("init_code") == "0x" + init_code().hex())
    return st if ok else None


def save_state(st: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=2) + "\n")


def has_code(chain_si, address: str) -> bool:
    return chain_si.query("Revive", "AccountInfoOf", [address]).value is not None


def run(manifest: dict, keypair, *, address: str | None = None, chain: Chain = V3, timeout: float = 180.0) -> dict:
    """Deploy (unless ``address`` / a saved deploy exists) and attest ``manifest`` on ``chain``; read it back."""
    from substrateinterface import SubstrateInterface

    want = expected(manifest)
    result: dict[str, Any] = {"chain": chain.id, "label": chain.label, "signer": keypair.ss58_address,
                              "manifest": {**want, "block_number": manifest["source"]["block_number"],
                                           "profile": manifest.get("profile")},
                              "deploy": None, "attest": None}
    chain_si = SubstrateInterface(url=chain.endpoint, ss58_format=chain.ss58)
    try:
        return _run(chain_si, manifest, keypair, address, chain, timeout, result)
    except Exception as exc:
        if result["deploy"] or result["attest"]:  # a transaction landed: never lose it from the evidence
            result.update({"exit_code": 1, "error": f"{type(exc).__name__}: {exc}"})
            raise ReviveReceiptError(f"{exc} (after {'the attest' if result['attest'] else 'the deploy'} landed)",
                                     partial=result) from exc
        raise
    finally:
        chain_si.close()


def _run(chain_si, manifest: dict, keypair, address: str | None, chain: Chain, timeout: float, result: dict) -> dict:
    from forklift.rehearse import _plain, apply, sign

    want = expected(manifest)
    origin = keypair.public_key
    with RpcClient(chain.endpoint, testnet=True, timeout=timeout) as cc:
        if cc.call("chain_getBlockHash", [0]) != chain.genesis_hash:
            raise ReviveReceiptError(f"{chain.endpoint} is not {chain.label}")
        result["attester_h160"] = h160_of(cc, origin)
        saved = load_state(chain)
        if address is None and saved:
            address = saved["address"]
        if address is not None and not has_code(chain_si, address):
            raise ReviveReceiptError(f"{address} holds no contract on {chain.label}")
        data = attest_data(want["block_hash"], want["manifest_root"], want["keys_lifted"])
        dry_deploy = None if address else dry_instantiate(cc, origin, init_code())
        if dry_deploy is not None and not dry_deploy.success:
            raise ReviveReceiptError(f"the deploy dry-run failed (ok={dry_deploy.ok}, reverted={dry_deploy.reverted}, "
                                     f"error={dry_deploy.error})")
        # The attest dry-run needs the contract: before a deploy its deposit is the measured slot cost bound.
        free = chain_si.query("System", "Account", [keypair.ss58_address]).value["data"]["free"]
        need = (dry_deploy.storage_deposit if dry_deploy else 0) + FEE_RESERVE
        result["preflight"] = {"free": free, "deploy_deposit": dry_deploy.storage_deposit if dry_deploy else None,
                               "deploy_weight_required": dry_deploy.weight_required if dry_deploy else None}
        if free < need:
            raise ReviveReceiptError(f"the funder holds {free} planck, the deploy needs {need} (deposit + fees)")

        def send(module: str, function: str, params: dict) -> dict:
            nonce = chain_si.query("System", "Account", [keypair.ss58_address]).value["nonce"]
            xt_hex = sign(chain_si, keypair, module, function, params, nonce)
            out = apply(cc, chain_si, xt_hex, [keypair.ss58_address], where="chain", timeout=timeout,
                        wait_finalized=True)
            row = _plain(out)
            row["call"] = f"{module}.{function}"
            row["explorer"] = chain.explorer_block(out.block_hash)
            return row

        if dry_deploy is not None:
            dep = send("Revive", "instantiate_with_code", {
                "value": 0, "weight_limit": weight_limit(dry_deploy.weight_required),
                "storage_deposit_limit": deposit_limit(dry_deploy.storage_deposit),
                "code": "0x" + init_code().hex(), "data": "0x", "salt": None})
            dep["dry_run"] = {"storage_deposit": dry_deploy.storage_deposit, "address": dry_deploy.address,
                              "weight_required": dry_deploy.weight_required}
            result["deploy"] = dep
            if not dep["success"]:
                result["exit_code"] = EXIT_DISPATCH_FAILED
                return result
            address = instantiated(dep["events"]) or dry_deploy.address
            save_state({"genesis_hash": chain.genesis_hash, "address": address,
                        "init_code": "0x" + init_code().hex(), "deploy": dep["extrinsic_id"]})
        result["address"] = address
        dry_attest = dry_call(cc, origin, address, data)
        if not dry_attest.success:
            raise ReviveReceiptError(f"the attest dry-run failed (ok={dry_attest.ok}, reverted={dry_attest.reverted}, "
                                     f"error={dry_attest.error})")
        free = chain_si.query("System", "Account", [keypair.ss58_address]).value["data"]["free"]
        if free < dry_attest.storage_deposit + FEE_RESERVE:
            raise ReviveReceiptError(f"the funder holds {free} planck, the attest needs "
                                     f"{dry_attest.storage_deposit + FEE_RESERVE} (deposit + fees)")
        att = send("Revive", "call", {
            "dest": address, "value": 0, "weight_limit": weight_limit(dry_attest.weight_required),
            "storage_deposit_limit": deposit_limit(dry_attest.storage_deposit), "data": "0x" + data.hex()})
        att["dry_run"] = {"storage_deposit": dry_attest.storage_deposit, "weight_required": dry_attest.weight_required}
        result["attest"] = att
        if not att["success"]:
            result["exit_code"] = EXIT_DISPATCH_FAILED
            return result
        got = emitted(att["events"], address)
        att["emitted"] = got
        stored = read_receipt(cc, origin, address, want["block_hash"], at=att["block_hash"])
        result["stored"] = {**stored, "at": att["block_hash"]}
        mismatch = []
        if len(got) != 1 or got[0]["decoded"] is None:
            mismatch.append(f"emitted {len(got)} Receipt events")
        else:
            mismatch += [f"emitted.{k}" for k in differs(got[0]["decoded"], manifest)]
            if got[0]["decoded"]["attester"] != result["attester_h160"].lower():
                mismatch.append("emitted.attester")
        mismatch += [f"stored.{k}" for k in differs(stored, manifest)]
        result["mismatch"] = mismatch
        result["exit_code"] = EXIT_MISMATCH if mismatch else 0
        return result


def read_back(extrinsic_id: str, *, chain: Chain = V3, address: str | None = None) -> dict:
    """Re-read a Receipt attest (or deploy) on ``chain`` by its extrinsic id: the call, its events, the
    decoded Receipt event, and the receipt the contract stores for that block hash now (read-only)."""
    from substrateinterface import SubstrateInterface

    from forklift.hashing import blake2_256
    from forklift.rehearse import _event

    number, index = (int(x) for x in extrinsic_id.split("-"))
    si = SubstrateInterface(url=chain.endpoint, ss58_format=chain.ss58)
    try:
        with RpcClient(chain.endpoint) as c:  # read-only client
            bh = c.call("chain_getBlockHash", [number])
            if bh is None:
                raise ReviveReceiptError(f"no block #{number} on {chain.label}")
            xts = c.call("chain_getBlock", [bh])["block"]["extrinsics"]
            if index >= len(xts):
                raise ReviveReceiptError(f"block #{number} has {len(xts)} extrinsics, no index {index}")
            events = [_event(e) for e in si.get_events(bh) if e.value.get("extrinsic_idx") == index]
            block = si.get_block(bh)
            ext = block["extrinsics"][index].value
            call = ext.get("call", {})
            out = {"chain": chain.id, "extrinsic_id": extrinsic_id, "block_number": number, "block_hash": bh,
                   "tx_hash": "0x" + blake2_256(bytes.fromhex(xts[index][2:])).hex(),
                   "signer": ext.get("address"), "call": f"{call.get('call_module')}.{call.get('call_function')}",
                   "success": any((e["pallet"], e["event"]) == ("System", "ExtrinsicSuccess") for e in events),
                   "events": events, "explorer": chain.explorer_block(bh)}
            args = {a["name"]: a["value"] for a in call.get("call_args", [])}
            dest = address or (args.get("dest") if out["call"] == "Revive.call" else instantiated(events))
            out["contract"] = dest
            if dest:
                got = emitted(events, dest)
                out["emitted"] = got
                if got and got[0]["decoded"]:
                    origin = bytes.fromhex(si.ss58_decode(out["signer"]))
                    head = c.call("chain_getFinalizedHead")
                    out["stored_now"] = {**read_receipt(c, origin, dest, got[0]["decoded"]["block_hash"], at=head),
                                         "at": head}
            return out
    finally:
        si.close()
