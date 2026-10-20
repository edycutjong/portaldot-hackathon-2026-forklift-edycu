import os

import pytest

from forklift.rehearse import Outcome, RehearseError, accounts_in, advance, compare, load_signer
from forklift.rpc import RpcClient
from tests.fakes import fake_connect

ALICE = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
BOB = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


def test_accounts_in_finds_every_address_once_including_nested_calls():
    params = {"calls": [{"call_module": "Balances", "call_function": "transfer_keep_alive",
                         "call_args": {"dest": BOB, "value": 10}},
                        {"call_module": "System", "call_function": "remark", "call_args": {"remark": "0x01"}}],
              "other": [ALICE, BOB]}
    assert accounts_in(params) == [BOB, ALICE]
    assert accounts_in({"remark": "0x" + "ab" * 24}) == []


def _o(**kw):
    base = dict(where="fork", endpoint="ws://x", block_number=10, block_hash="0x1", parent_hash="0x0", index=1,
                tx_hash="0xaa", success=True, fee=100, before={}, after={},
                events=[{"pallet": "Balances", "event": "Withdraw", "attributes": {"who": ALICE, "amount": 100}},
                        {"pallet": "System", "event": "Remarked", "attributes": {"sender": ALICE}},
                        {"pallet": "TransactionPayment", "event": "TransactionFeePaid",
                         "attributes": {"who": ALICE, "actual_fee": 100, "tip": 0}},
                        {"pallet": "System", "event": "ExtrinsicSuccess", "attributes": {}}])
    base.update(kw)
    o = Outcome(**base)
    o.deltas = kw.get("deltas", {ALICE: -100})
    return o


def test_compare_matches_identical_outcomes_field_by_field():
    c = compare(_o(), _o(where="chain", block_hash="0x2"))
    assert c["match"] and not c["differs"]
    assert {r["field"] for r in c["rows"]} >= {"tx_hash", "fee", "event_sequence", f"delta {ALICE}",
                                               "inclusion_height"}


def test_compare_names_every_difference_and_never_hides_one():
    actual = _o(where="chain", fee=101, block_number=11, deltas={ALICE: -101})
    c = compare(_o(), actual)
    assert not c["match"]
    assert set(c["differs"]) == {"fee", f"delta {ALICE}", "inclusion_height"}
    note = next(r for r in c["rows"] if r["field"] == "inclusion_height")["note"]
    assert "fee multiplier" in note


def test_compare_flags_a_different_event_sequence():
    actual = _o(where="chain", events=_o().events[:1] + _o().events[2:])
    assert "event_sequence" in compare(_o(), actual)["differs"]


def test_signer_file_must_be_private_and_its_secret_never_echoed(tmp_path):
    f = tmp_path / "k.seed"
    secret = "bottom drive obey lake curtain smoke basket hold race lonely fit walk"
    f.write_text(secret + "\n")
    os.chmod(f, 0o644)
    with pytest.raises(RehearseError, match="chmod 600"):
        load_signer(f)
    os.chmod(f, 0o600)
    assert load_signer(f).ss58_address  # the well-known dev phrase
    f.write_text("not a key at all\n")
    with pytest.raises(RehearseError) as exc:
        load_signer(f)
    assert "not a key" not in str(exc.value)


class _SI:
    def __init__(self, best):
        self.best = best

    def get_chain_head(self):
        return "0xhead"

    def get_block_number(self, h):
        return self.best

    def get_block_hash(self, n):
        return f"0x{n:064x}"

    def query(self, *a, **kw):
        class V:
            value = 7
        return V()


def _fork(head, built):
    state = {"head": head}

    def handler(method, params):
        if method == "chain_getBlockHash" and params == [0]:
            from forklift.chains import V3
            return V3.genesis_hash
        if method == "rpc_methods":
            return {"methods": ["dev_newBlock", "dev_setStorage"]}
        if method == "chain_getHeader":
            return {"number": hex(state["head"])}
        if method == "chain_getBlockHash":
            return "0x" + "ab" * 32
        if method == "state_getStorage":
            return "0x" + (7).to_bytes(16, "little").hex()
        if method == "dev_newBlock":
            built.append(params[0]["count"])
            state["head"] += params[0]["count"]
            return "0x"
        raise AssertionError(method)

    return handler


def test_advance_builds_empty_blocks_in_chunks_up_to_the_chain_best(monkeypatch):
    built = []
    handler = _fork(100, built)
    monkeypatch.setattr("forklift.rehearse.RpcClient",
                        lambda ep, **kw: RpcClient(ep, connect=fake_connect(handler), sleep=lambda s: None, **kw))
    fc = RpcClient("ws://127.0.0.1:59944", fork=True, connect=fake_connect(handler), sleep=lambda s: None)
    out = advance(fc, 'ws://127.0.0.1:59944', best_of=lambda: 112)
    assert built == [5, 5, 2] and out["fork_head"] == 112 and out["built_empty_blocks"] == 12
    assert out["fee_multiplier"]["equal"]


@pytest.mark.parametrize("fork_head,best,msg", [(120, 112, "ahead"), (1, 500, "behind")])
def test_advance_refuses_a_fork_ahead_of_or_far_behind_the_chain(fork_head, best, msg):
    built = []
    fc = RpcClient("ws://127.0.0.1:59944", fork=True, connect=fake_connect(_fork(fork_head, built)),
                   sleep=lambda s: None)
    with pytest.raises(RehearseError, match=msg):
        advance(fc, 'wss://chain', best_of=lambda: best)
    assert built == []


def test_advance_resyncs_a_drifted_fee_multiplier_from_the_chain_and_records_both(monkeypatch):
    from forklift.rehearse import FEE_MULTIPLIER_KEY

    fork_state = {"head": 50, "mult": 7}
    sets = []

    def fork(method, params):
        if method == "chain_getBlockHash" and params == [0]:
            from forklift.chains import V3
            return V3.genesis_hash
        if method == "rpc_methods":
            return {"methods": ["dev_newBlock", "dev_setStorage"]}
        if method == "chain_getHeader":
            return {"number": hex(fork_state["head"])}
        if method == "chain_getBlockHash":
            return "0x" + "ab" * 32
        if method == "state_getStorage":
            return "0x" + fork_state["mult"].to_bytes(16, "little").hex()
        if method == "dev_setStorage":
            (key, value), = params[0]
            assert key == FEE_MULTIPLIER_KEY
            sets.append(value)
            fork_state["mult"] = int.from_bytes(bytes.fromhex(value[2:]), "little")
            return "0x"
        raise AssertionError(method)

    def chain(method, params):
        if method == "chain_getBlockHash":
            return "0x" + "cd" * 32
        if method == "state_getStorage":
            return "0x" + (9).to_bytes(16, "little").hex()
        raise AssertionError(method)

    monkeypatch.setattr("forklift.rehearse.RpcClient",
                        lambda ep, **kw: RpcClient(ep, connect=fake_connect(chain), sleep=lambda s: None, **kw))
    fc = RpcClient("ws://127.0.0.1:59944", fork=True, connect=fake_connect(fork), sleep=lambda s: None)
    out = advance(fc, "wss://chain", best_of=lambda: 50)
    fm = out["fee_multiplier"]
    assert fm == {"fork_after_empty_blocks": "7", "chain": "9", "equal": False, "synced_from_chain": True}
    assert len(sets) == 1
