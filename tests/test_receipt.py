"""Receipt contract, rent arithmetic and the submit path, against blocks captured from a real fork."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from forklift import receipt, tx
from forklift.hashing import blake2_256
from forklift.rpc import RpcClient
from tests.fakes import fake_connect

FIX = Path(__file__).parent / "fixtures"
CAP = json.loads((FIX / "fork-txs-2725639.json").read_text())
META_HEX = "0x" + (FIX / "metadata-1002-dev.scale").read_bytes().hex()
PIN_HASH = "0xc6fb8d8a10ae3045b7cb6b0f24ddacc538006a1fae975ec343c8654fb55a8212"
ROOT = "0x29f18c9250683ce3da0b6ee821674de99036bbc67afc32f42647d687b4c584e7"
MANIFEST = {"source": {"block_hash": PIN_HASH, "block_number": 2725639}, "manifest_root": ROOT,
            "load": {"keys_lifted": 16332}}


@pytest.fixture(scope="module")
def rt():
    return tx.Runtime(META_HEX, CAP["runtime_version"], CAP["genesis_hash"])


def _events(n: int):
    rt_ = tx.Runtime(META_HEX, CAP["runtime_version"], CAP["genesis_hash"])
    return rt_, rt_.decode("Vec<EventRecord<Event, Hash>>", CAP["blocks"][str(n)]["events"])


# --- contract -----------------------------------------------------------------------------------

def _wasm_sections(code: bytes) -> dict[int, bytes]:
    assert code[:8] == b"\x00asm\x01\x00\x00\x00"
    pos, out = 8, {}
    while pos < len(code):
        sid = code[pos]
        pos += 1
        size, shift = 0, 0
        while True:
            b = code[pos]
            pos += 1
            size |= (b & 0x7F) << shift
            shift += 7
            if b < 0x80:
                break
        out[sid] = code[pos:pos + size]
        pos += size
    return out


def _names(section: bytes, pairs: bool) -> list:
    count, pos, out = section[0], 1, []
    for _ in range(count):
        n = section[pos]
        a = section[pos + 1:pos + 1 + n].decode()
        pos += 1 + n
        if pairs:
            n2 = section[pos]
            b = section[pos + 1:pos + 1 + n2].decode()
            pos += 1 + n2
            kind = section[pos]
            pos += 1
            pos += {0: 1, 2: 3}[kind]  # func: type index; memory: flags min max (limits 1 1)
            out.append((a, b, kind))
        else:
            kind = section[pos]
            pos += 2
            out.append((a, kind))
    return out


def test_receipt_wasm_imports_only_seal0_and_env_memory():
    sections = _wasm_sections(receipt.wasm())
    assert _names(sections[2], pairs=True) == [("seal0", "seal_input", 0), ("seal0", "seal_deposit_event", 0),
                                               ("env", "memory", 2)]
    assert _names(sections[7], pairs=False) == [("deploy", 0), ("call", 0)]


def test_receipt_wasm_is_the_build_of_the_wat():
    if shutil.which("wat2wasm") is None:
        pytest.skip("wat2wasm not installed (brew install wabt)")
    src = Path(receipt.__file__).parent / "contracts" / "receipt.wat"
    built = subprocess.run(["wat2wasm", str(src), "--output=-"], capture_output=True, check=True).stdout
    assert built == receipt.wasm()


def test_code_hash_is_the_one_the_fork_stored(rt):
    info = receipt.decode_contract_info(bytes.fromhex(CAP["contract_info"]["90"][2:]))
    assert info["code_hash"] == receipt.code_hash() == "0x" + blake2_256(receipt.wasm()).hex()


# --- payload ------------------------------------------------------------------------------------

def test_payload_round_trip_and_manifest_compare():
    data = receipt.manifest_payload(MANIFEST)
    assert len(data) == 72 and data[64:] == (16332).to_bytes(8, "little")
    decoded = receipt.decode_payload(data)
    assert decoded == {"mainnet_block_hash": PIN_HASH, "manifest_root": ROOT, "keys_lifted": 16332}
    assert receipt.compare_to_manifest(decoded, MANIFEST) == []
    other = {**decoded, "keys_lifted": 16331, "manifest_root": "0x" + "00" * 32}
    assert receipt.compare_to_manifest(other, MANIFEST) == ["manifest_root", "keys_lifted"]


@pytest.mark.parametrize("bad", [dict(block_hash="0x12"), dict(root="0x" + "00" * 31), dict(keys=-1), dict(keys=2**64)])
def test_payload_refuses_bad_fields(bad):
    with pytest.raises(ValueError):
        receipt.payload(bad.get("block_hash", PIN_HASH), bad.get("root", ROOT), bad.get("keys", 1))
    with pytest.raises(ValueError):
        receipt.decode_payload(b"\x00" * 71)


@pytest.mark.parametrize("block", [90, 126])
def test_contract_emitted_on_deploy_and_attest_carries_the_manifest(rt, block):
    """Decoded from the real events of the instantiate (#90) and attest (#126) blocks."""
    _, records = _events(block)
    events = [tx.Event(i, r["phase"], r.get("extrinsic_idx"), r["module_id"], r["event_id"], r["attributes"])
              for i, r in enumerate(records)]
    inc = tx.Inclusion("0x", block, "0x", 1, [e for e in events if e.extrinsic_index == 1], True, None, 0.0)
    out = receipt.emitted(inc)
    assert len(out) == 1 and out[0]["contract"] == CAP["receipt_address"]
    assert receipt.compare_to_manifest(out[0]["decoded"], MANIFEST) == []


# --- contract info and rent ---------------------------------------------------------------------

def test_contract_info_decodes_the_rent_era_layout():
    info = receipt.decode_contract_info(bytes.fromhex(CAP["contract_info"]["90"][2:]))
    assert info["storage_size"] == 0 and info["pair_count"] == 0
    assert info["deduct_block"] == 90 and info["rent_paid"] == 4_514_614_000 and info["last_write"] is None
    assert info["rent_allowance"] == 2**128 - 1 - info["rent_paid"]
    later = receipt.decode_contract_info(bytes.fromhex(CAP["contract_info"]["119"][2:]))
    assert later["deduct_block"] == 119 and later["trie_id"] == info["trie_id"]
    assert receipt.decode_contract_info(b"\x01" + b"\x00" * 32) is None  # tombstone
    with pytest.raises(ValueError, match="unread bytes"):
        receipt.decode_contract_info(bytes.fromhex(CAP["contract_info"]["90"][2:]) + b"\x00")
    with pytest.raises(ValueError):
        receipt.decode_contract_info(b"\x07")


CONSTS = {"DepositPerContract": 735_000_000_000_000, "DepositPerStorageByte": 6_000_000_000_000,
          "DepositPerStorageItem": 15_000_000_000_000, "TombstoneDeposit": 735_000_000_000_000,
          "RentFraction_perbill": 2314, "ExistentialDeposit": 100_000_000_000_000}


def test_rent_formula_reproduces_both_measured_charges():
    """The two charges measured on the fork fit ceil(RentFraction x (deposit - free)) with 481 counted bytes."""
    first = receipt.rent_status(1_670_000_000_000_000, {}, 481, CONSTS)  # the endowment, charged in #90
    assert first.fee_per_block == 4_514_614_000  # = rent_paid at #90
    second = receipt.rent_status(1_923_900_000_000_000, {}, 481, CONSTS)  # balance during #91..#101
    assert second.fee_per_block * 11 == 47_712_597_400 - 4_514_614_000  # rent_paid at #101 minus at #90


def test_rent_bound_covers_the_measured_count_and_top_up_makes_rent_zero():
    bound = receipt.rent_status(1_923_856_802_016_600, {}, 502, CONSTS)
    measured = receipt.rent_status(1_923_856_802_016_600, {}, 481, CONSTS)
    assert bound.deposit > measured.deposit and bound.fee_per_block >= measured.fee_per_block > 0
    amount = receipt.top_up_amount(bound)
    after = receipt.rent_status(bound.free + amount, {}, 502, CONSTS)
    assert after.fee_per_block == 0 and after.blocks_until_eviction is None
    assert after.free == bound.deposit * 11 // 10
    assert receipt.top_up_amount(after) == 0
    assert bound.subsistence == 835_000_000_000_000
    assert bound.blocks_until_eviction == (bound.free - bound.subsistence) // bound.fee_per_block


# --- submit, against the captured chain ---------------------------------------------------------

class CapturedFork:
    """Serves the captured blocks: best moves from n-1 to n after submit; finalization follows."""

    def __init__(self, n: int, *, include: bool = True, wrong_hash: bool = False):
        self.n, self.include, self.wrong_hash = n, include, wrong_hash
        self.block = CAP["blocks"][str(n)]
        self.best = n - 1
        self.submitted: list[str] = []

    def __call__(self, method, params):
        b = self.block
        if method == "chain_getBlockHash":
            if params == [0]:
                return CAP["genesis_hash"]
            return b["hash"] if params[0] == self.n else "0x" + f"{params[0]:064x}"
        if method == "author_submitExtrinsic":
            self.submitted.append(params[0])
            self.best = self.n if self.include else self.n - 1
            return "0x" + ("00" * 32 if self.wrong_hash else blake2_256(bytes.fromhex(params[0][2:])).hex())
        if method == "chain_getHeader":
            if params and params[0] == b["hash"]:
                return {"number": hex(self.n)}
            return {"number": hex(self.best)}
        if method == "chain_getFinalizedHead":
            return b["hash"] if self.best >= self.n else "0x" + f"{self.best:064x}"
        if method == "chain_getBlock":
            return {"block": {"extrinsics": b["extrinsics"] if params[0] == b["hash"] else ["0x00"]}}
        if method == "state_getStorage":
            return b["events"] if params[1] == b["hash"] else None
        raise AssertionError(f"unexpected {method}")


def _fork_client(handler):
    return RpcClient("ws://127.0.0.1:59944", fork=True, connect=fake_connect(handler))


@pytest.mark.parametrize("n,expect_event", [(126, "Contracts.ContractEmitted"), (62, "Sudo.SudoAsDone"),
                                            (59, "Sudo.Sudid")])
def test_submit_finds_the_block_waits_for_finality_and_decodes_the_outcome(rt, n, expect_event):
    chain = CapturedFork(n)
    xt = bytes.fromhex(CAP["blocks"][str(n)]["extrinsics"][1][2:])
    inc = tx.submit(_fork_client(chain), rt, xt, sleep=lambda s: None)
    assert inc.extrinsic_id == f"{n}-1" and inc.block_hash == CAP["blocks"][str(n)]["hash"]
    assert inc.tx_hash == "0x" + blake2_256(xt).hex() and inc.finalized and inc.success
    assert expect_event in {e.full_name for e in inc.events}
    assert all(e.extrinsic_index == 1 for e in inc.events)


def test_submit_times_out_when_never_included(rt):
    chain = CapturedFork(126, include=False)
    t = iter(range(0, 10_000, 10))
    xt = bytes.fromhex(CAP["blocks"]["126"]["extrinsics"][1][2:])
    with pytest.raises(tx.TxError, match="not included"):
        tx.submit(_fork_client(chain), rt, xt, timeout=60, sleep=lambda s: None, clock=lambda: next(t))


def test_submit_refuses_a_node_that_reports_another_tx_hash(rt):
    xt = bytes.fromhex(CAP["blocks"]["126"]["extrinsics"][1][2:])
    with pytest.raises(tx.TxError, match="expected"):
        tx.submit(_fork_client(CapturedFork(126, wrong_hash=True)), rt, xt, sleep=lambda s: None)


def test_fetch_extrinsic_by_id_reads_call_signer_and_outcome(rt):
    got = tx.fetch_extrinsic(_fork_client(CapturedFork(62)), rt, "62-1")
    assert got["call"] == {"pallet": "Sudo", "function": "sudo_as"}
    assert got["signer"] == "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    assert got["success"] and "Sudo.SudoAsDone" in [e["event"] for e in got["events"]]
    with pytest.raises(ValueError):
        tx.fetch_extrinsic(_fork_client(CapturedFork(62)), rt, "62")
    with pytest.raises(tx.TxError, match="no index 9"):
        tx.fetch_extrinsic(_fork_client(CapturedFork(62)), rt, "62-9")
