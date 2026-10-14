"""The Fork Receipt: an on-chain record, on the fork, of which mainnet state it carries.

``attest`` emits ``mainnet_block_hash ‖ manifest_root ‖ keys_lifted`` (72 bytes) as
a ``Contracts.ContractEmitted`` event. The contract (``contracts/receipt.wat``) is
instantiated on the first ``forklift receipt`` of a fork and called on every run.

Rent. Spec 1002 runs the rent-era Contracts pallet: a contract whose free balance
does not cover its deposit (``DepositPerContract`` + ``DepositPerStorageByte`` x
bytes + ``DepositPerStorageItem`` x items) pays ``RentFraction`` of the uncovered
part every block and is evicted once its balance falls below the subsistence
threshold (``ExistentialDeposit`` + ``TombstoneDeposit``). The bytes include a share
of the contract's stored code: on a fork of #2,725,639 the pallet charged rent
against 481 bytes for the 169-byte Receipt (back-solved from two measured charges,
2026-10-14). That count is not derivable from what the chain exposes, so the rent
check covers an upper bound, both stored encodings of the code (``CodeStorage`` +
``PristineCode`` values, 502 bytes for the Receipt), tops the contract up past it, and then
*measures* that the next call charged no rent (``rent_paid`` unchanged).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from forklift import tx
from forklift.hashing import blake2_256
from forklift.rpc import RpcClient

PAYLOAD_LEN = 72
SALT = b"forklift-receipt-v1"  # fixed: the same deployer and code give the same address on every fork
GAS_LIMIT = 20_000_000_000  # weight; unused gas is refunded
TOP_UP_MARGIN_PERMILLE = 100  # top up to the deposit + 10 %
PERBILL = 1_000_000_000
BLOCK_SECONDS = 6  # fast runtime (dev wasm): 6,000 ms slots, read from BabeApi_configuration at boot

EXIT_PAYLOAD_MISMATCH = 2  # the emitted payload is not the manifest's (same meaning as verify's 2)


class ReceiptError(Exception):
    pass


def wasm() -> bytes:
    return resources.files("forklift.contracts").joinpath("receipt.wasm").read_bytes()


def code_hash() -> str:
    return "0x" + blake2_256(wasm()).hex()


def payload(block_hash: str, root: str, keys_lifted: int) -> bytes:
    bh, mr = bytes.fromhex(block_hash.removeprefix("0x")), bytes.fromhex(root.removeprefix("0x"))
    if len(bh) != 32 or len(mr) != 32:
        raise ValueError("block hash and manifest root must be 32 bytes each")
    if not 0 <= keys_lifted < 2**64:
        raise ValueError("keys_lifted must fit in a u64")
    return bh + mr + keys_lifted.to_bytes(8, "little")


def manifest_payload(manifest: dict) -> bytes:
    return payload(manifest["source"]["block_hash"], manifest["manifest_root"], manifest["load"]["keys_lifted"])


def decode_payload(data: bytes) -> dict:
    if len(data) != PAYLOAD_LEN:
        raise ValueError(f"a Receipt payload is {PAYLOAD_LEN} bytes, got {len(data)}")
    return {"mainnet_block_hash": "0x" + data[:32].hex(), "manifest_root": "0x" + data[32:64].hex(),
            "keys_lifted": int.from_bytes(data[64:], "little")}


def compare_to_manifest(decoded: dict, manifest: dict) -> list[str]:
    """Names of the fields that differ between an emitted payload and the manifest (empty = match)."""
    expected = decode_payload(manifest_payload(manifest))
    return [k for k in expected if str(decoded[k]).lower() != str(expected[k]).lower()]


def _bytes_of(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str) and value.startswith("0x"):
        return bytes.fromhex(value[2:])
    if isinstance(value, list):
        return bytes(value)
    raise ValueError(f"cannot read bytes from {value!r}")


def emitted(inc: tx.Inclusion) -> list[dict]:
    """``Contracts.ContractEmitted`` events of an extrinsic → [{contract, data, decoded}]."""
    out = []
    for e in inc.events_named("Contracts.ContractEmitted"):
        contract, data = (a["value"] for a in e.attributes)
        raw = _bytes_of(data)
        out.append({"contract": tx.as_ss58(contract), "data": "0x" + raw.hex(),
                    "decoded": decode_payload(raw) if len(raw) == PAYLOAD_LEN else None})
    return out


def state_path(name: str) -> Path:
    from forklift.lift import default_cache
    from forklift.node import pid_file

    pid_file(name)  # the same name rules as the fork itself (raises NodeError on a bad name)
    return default_cache() / "forks" / name / "receipt.json"


def load_state(name: str, genesis_hash: str) -> dict | None:
    """The Receipt this fork name deployed, if it was deployed on this same chain (genesis)."""
    try:
        st = json.loads(state_path(name).read_text())
    except (OSError, ValueError):
        return None
    return st if isinstance(st, dict) and st.get("genesis_hash") == genesis_hash and st.get("address") else None


def save_state(name: str, st: dict) -> None:
    p = state_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=2) + "\n")


def decode_contract_info(raw: bytes) -> dict | None:
    """``ContractInfo`` of spec 1002 (rent-era): Alive → its fields; Tombstone → None.

    Alive = trie_id (Vec<u8>) ‖ storage_size u32 ‖ pair_count u32 ‖ code_hash [32] ‖
    rent_allowance u128 ‖ rent_payed u128 ‖ deduct_block u32 ‖ last_write Option<u32> ‖
    _reserved Option<()>. Decoded by layout: the SDK's preset type for it does not
    match these bytes (it stops at byte 34 of 112, measured 2026-10-14).
    """
    if not raw:
        raise ValueError("empty ContractInfo")
    if raw[0] == 1:
        return None  # Tombstone
    if raw[0] != 0:
        raise ValueError(f"unknown ContractInfo variant {raw[0]}")
    pos = 1
    b0 = raw[pos]
    if b0 & 3 == 0:
        n, pos = b0 >> 2, pos + 1
    elif b0 & 3 == 1:
        n, pos = int.from_bytes(raw[pos:pos + 2], "little") >> 2, pos + 2
    else:
        raise ValueError("trie id length out of range")

    def take(k: int) -> bytes:
        nonlocal pos
        if pos + k > len(raw):
            raise ValueError("truncated ContractInfo")
        out = raw[pos:pos + k]
        pos += k
        return out

    trie_id = take(n)
    info = {"trie_id": "0x" + trie_id.hex(),
            "storage_size": int.from_bytes(take(4), "little"),
            "pair_count": int.from_bytes(take(4), "little"),
            "code_hash": "0x" + take(32).hex(),
            "rent_allowance": int.from_bytes(take(16), "little"),
            "rent_paid": int.from_bytes(take(16), "little"),
            "deduct_block": int.from_bytes(take(4), "little")}
    info["last_write"] = int.from_bytes(take(4), "little") if take(1) == b"\x01" else None
    take(1)  # _reserved: Option<()>
    if pos != len(raw):
        raise ValueError(f"ContractInfo has {len(raw) - pos} unread bytes: layout differs from spec 1002")
    return info


def contract_info(client: RpcClient, rt: tx.Runtime, address: str, at: str | None = None) -> dict | None:
    """``Contracts.ContractInfoOf`` → the Alive record, or None (absent or a tombstone)."""
    key = rt.storage_key("Contracts", "ContractInfoOf", tx.ss58_to_public(address))
    raw = client.call("state_getStorage", [key, at] if at else [key])
    return None if raw is None else decode_contract_info(bytes.fromhex(raw[2:]))


def _mul_ceil_perbill(parts: int, value: int) -> int:
    return -(-parts * value // PERBILL)


@dataclass
class Rent:
    free: int
    deposit: int  # what the free balance must cover for the rent to be 0
    subsistence: int
    fee_per_block: int
    blocks_until_eviction: int | None  # at the current rate; None = never (rent 0)
    code_bytes: int = 0
    constants: dict = field(default_factory=dict)

    def as_json(self) -> dict:
        return {"free": self.free, "deposit_bound": self.deposit, "code_bytes_counted": self.code_bytes, "subsistence_threshold": self.subsistence,
                "rent_per_block": self.fee_per_block, "blocks_until_eviction": self.blocks_until_eviction,
                "constants": self.constants}


def rent_constants(rt: tx.Runtime) -> dict:
    c = {name: rt.constant_int("Contracts", name) for name in
         ("DepositPerContract", "DepositPerStorageByte", "DepositPerStorageItem", "TombstoneDeposit")}
    c["RentFraction_perbill"] = rt.constant_int("Contracts", "RentFraction")
    c["ExistentialDeposit"] = rt.constant_int("Balances", "ExistentialDeposit")
    return c


def rent_status(free: int, info: dict | None, code_bytes: int, consts: dict) -> Rent:
    storage_size = int((info or {}).get("storage_size", 0))
    pair_count = int((info or {}).get("pair_count", 0))
    deposit = (consts["DepositPerContract"] + consts["DepositPerStorageByte"] * (storage_size + code_bytes)
               + consts["DepositPerStorageItem"] * pair_count)
    fee = _mul_ceil_perbill(consts["RentFraction_perbill"], max(0, deposit - free))
    subsistence = consts["ExistentialDeposit"] + consts["TombstoneDeposit"]
    if fee == 0:
        left = None
    else:
        left = max(0, (free - subsistence) // fee)
    return Rent(free=free, deposit=deposit, subsistence=subsistence, fee_per_block=fee,
                blocks_until_eviction=left, code_bytes=code_bytes, constants=consts)


def top_up_amount(rent: Rent) -> int:
    """Planck to transfer so the free balance covers the deposit plus the margin (0 if it already does)."""
    target = rent.deposit + rent.deposit * TOP_UP_MARGIN_PERMILLE // 1000
    return max(0, target - rent.free) if rent.fee_per_block > 0 else 0


def stored_code_bytes(client: RpcClient, rt: tx.Runtime, code_hash_hex: str, at: str | None = None) -> int:
    """Bytes the chain stores for a code hash: the PrefabWasmModule record + the pristine wasm."""
    h = bytes.fromhex(code_hash_hex.removeprefix("0x"))
    total = 0
    for item in ("CodeStorage", "PristineCode"):
        key = rt.storage_key("Contracts", item, h)
        raw = client.call("state_getStorage", [key, at] if at else [key])
        if raw is None:
            raise ReceiptError(f"Contracts.{item} has no entry for code {code_hash_hex}")
        total += len(raw) // 2 - 1
    return total


def free_of(client: RpcClient, rt: tx.Runtime, address: str, at: str | None = None) -> int:
    from forklift.act import account_info

    return int(account_info(client, rt, tx.ss58_to_public(address), at)["data"]["free"])


def instantiate(client: RpcClient, rt: tx.Runtime, data: bytes, endowment: int, signer) -> tuple[tx.Inclusion, str]:
    call = rt.compose("Contracts", "instantiate_with_code", {
        "endowment": endowment, "gas_limit": GAS_LIMIT, "code": "0x" + wasm().hex(),
        "data": "0x" + data.hex(), "salt": "0x" + SALT.hex()})
    inc = tx.sign_and_submit(client, rt, call, signer)
    if not inc.success:
        raise ReceiptError(f"instantiate_with_code failed: {inc.error} (extrinsic {inc.extrinsic_id})")
    inst = inc.events_named("Contracts.Instantiated")
    if not inst:
        raise ReceiptError(f"no Contracts.Instantiated event in {inc.extrinsic_id}")
    return inc, tx.as_ss58(inst[0].attributes[1]["value"])


def attest(client: RpcClient, rt: tx.Runtime, address: str, data: bytes, signer) -> tx.Inclusion:
    call = rt.compose("Contracts", "call", {"dest": address, "value": 0, "gas_limit": GAS_LIMIT,
                                            "data": "0x" + data.hex()})
    return tx.sign_and_submit(client, rt, call, signer)


def transfer(client: RpcClient, rt: tx.Runtime, dest: str, planck: int, signer) -> tx.Inclusion:
    return tx.sign_and_submit(client, rt, rt.compose("Balances", "transfer", {"dest": dest, "value": planck}), signer)


@dataclass
class ReceiptRun:
    address: str
    code_hash: str
    deployed_now: bool
    steps: list[dict]  # {kind, inclusion, ...} in order
    rent_before: Rent | None
    rent_after: Rent
    rent_proof: dict
    attest_emitted: list[dict]
    mismatch: list[str]
    manifest_payload: str

    @property
    def exit_code(self) -> int:
        if any(not s["inclusion"].success for s in self.steps):
            from forklift.act import EXIT_DISPATCH_FAILED

            return EXIT_DISPATCH_FAILED
        return EXIT_PAYLOAD_MISMATCH if self.mismatch or not self.attest_emitted else 0

    def as_json(self) -> dict:
        return {"address": self.address, "code_hash": self.code_hash, "deployed_now": self.deployed_now,
                "steps": [{**{k: v for k, v in s.items() if k != "inclusion"}, "extrinsic": s["inclusion"].as_json()}
                          for s in self.steps],
                "rent": {"before_top_up": self.rent_before.as_json() if self.rent_before else None,
                         "after": self.rent_after.as_json(), "proof": self.rent_proof},
                "attest": {"emitted": self.attest_emitted, "manifest_payload": self.manifest_payload,
                           "matches_manifest": not self.mismatch and bool(self.attest_emitted),
                           "mismatched_fields": self.mismatch},
                "exit_code": self.exit_code}


def run_receipt(client: RpcClient, rt: tx.Runtime, manifest: dict, *, name: str, address: str | None = None,
                top_up: bool = True, signer=None) -> ReceiptRun:
    """Deploy (first run on this fork) → rent check + top-up → attest → compare the event with the manifest."""
    signer = signer or tx.dev_keypair()
    data = manifest_payload(manifest)
    genesis = client.call("chain_getBlockHash", [0])
    consts = rent_constants(rt)
    steps: list[dict] = []
    st = load_state(name, genesis)
    address = address or (st or {}).get("address")
    deployed_now = False
    if address and contract_info(client, rt, address) is None:
        if st and st.get("address") == address:
            address = None  # recorded, but no longer alive on this chain: deploy a new one
        else:
            raise ReceiptError(f"{address} is not a live contract on this fork")
    if not address:
        # Alive but not rent-free: rent is charged from the first block, so the rent check has work to do.
        endowment = 2 * (consts["ExistentialDeposit"] + consts["TombstoneDeposit"])
        inc, address = instantiate(client, rt, data, endowment, signer)
        deployed_now = True
        steps.append({"kind": "instantiate", "inclusion": inc, "endowment": endowment,
                      "emitted": emitted(inc)})
        save_state(name, {"genesis_hash": genesis, "address": address, "code_hash": code_hash(),
                          "instantiate": inc.as_json(), "salt": "0x" + SALT.hex()})
    info = contract_info(client, rt, address)
    code_bytes = stored_code_bytes(client, rt, info["code_hash"])
    rent_before = rent_status(free_of(client, rt, address), info, code_bytes, consts)
    amount = top_up_amount(rent_before) if top_up else 0
    if amount:
        inc = transfer(client, rt, address, amount, signer)
        steps.append({"kind": "rent_top_up", "inclusion": inc, "amount": amount,
                      "why": (f"rent was up to {rent_before.fee_per_block:,} planck/block: eviction in about "
                              f"{rent_before.blocks_until_eviction:,} blocks (~"
                              f"{rent_before.blocks_until_eviction * BLOCK_SECONDS / 86400:.1f} days of 6 s blocks) "
                              f"at that rate; topped up to the deposit bound + {TOP_UP_MARGIN_PERMILLE / 10:g} % "
                              f"so the rent is 0 and the Receipt is never evicted")})
    info_before = contract_info(client, rt, address)
    free_before = free_of(client, rt, address)
    inc = attest(client, rt, address, data, signer)
    out = emitted(inc)
    steps.append({"kind": "attest", "inclusion": inc, "emitted": out})
    info_after = contract_info(client, rt, address, at=inc.block_hash)
    free_after = free_of(client, rt, address, at=inc.block_hash)
    rent_after = rent_status(free_after, info_after, code_bytes, consts)
    paid = lambda i: int((i or {}).get("rent_paid", 0))  # noqa: E731
    proof = {"free_before_attest": free_before, "free_after_attest": free_after,
             "rent_paid_before": paid(info_before), "rent_paid_after": paid(info_after),
             "deduct_block_before": int((info_before or {}).get("deduct_block", 0)),
             "deduct_block_after": int((info_after or {}).get("deduct_block", 0))}
    proof["no_rent_charged"] = (proof["rent_paid_after"] == proof["rent_paid_before"]
                                and free_after >= free_before)
    mine = [e for e in out if e["contract"] == address]
    mismatch = compare_to_manifest(mine[0]["decoded"], manifest) if mine and mine[0]["decoded"] else []
    return ReceiptRun(address=address, code_hash=code_hash(), deployed_now=deployed_now, steps=steps,
                      rent_before=rent_before if amount or deployed_now else None, rent_after=rent_after,
                      rent_proof=proof, attest_emitted=mine, mismatch=mismatch,
                      manifest_payload="0x" + data.hex())
