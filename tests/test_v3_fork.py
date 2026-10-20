"""The V3 path: Chopsticks args and ownership, and `verify` of a V3 fork against the live chain and the lift."""

import gzip
from pathlib import Path

import pytest

from forklift import chopsticks
from forklift.chains import V3
from forklift.hashing import twox128
from forklift.rpc import RpcClient
from forklift.splice import account_key
from forklift.verify import VerifyError, verify_v3
from tests.fakes import FakeChain, fake_connect

FIX = Path(__file__).parent / "fixtures"
META = "0x" + gzip.decompress((FIX / "metadata-3002.scale.gz").read_bytes()).hex()
PIN = "0x" + "cd" * 32


def test_chopsticks_args_pin_the_block_bind_loopback_and_name_the_flags():
    args = chopsticks.chopsticks_args(V3, PIN, 59944, Path("/tmp/x.sqlite"))
    assert args[:3] == ["npx", "-y", "@acala-network/chopsticks@1.5.2"]
    assert f"--block={PIN}" in args and "--port=59944" in args and "--host=127.0.0.1" in args
    assert "--allow-unresolved-imports=true" in args and f"--endpoint={V3.endpoint}" in args


def test_owns_matches_only_our_chopsticks_on_our_port():
    rec = {"executor": "chopsticks", "ports": {"ws": 59944}}
    ours = "npm exec @acala-network/chopsticks@1.5.2 --endpoint=wss://x --port=59944 --host=127.0.0.1"
    assert chopsticks.owns(rec, ours)
    assert not chopsticks.owns(rec, ours.replace("59944", "19944"))  # another project's port
    assert not chopsticks.owns(rec, "portaldot_dev --ws-port 59944")
    assert not chopsticks.owns({**rec, "executor": None}, ours)
    assert not chopsticks.owns(rec, None)


class FakeV3(FakeChain):
    """A V3 block: V3's genesis hash, a head header, Chopsticks dev methods."""

    def __init__(self, storage, *, served=None, **kw):
        super().__init__(storage, number=117790, block_hash=PIN, metadata=META, **kw)
        self.served = served  # what the fork answers when it differs from the stored values

    def __call__(self, method, params):
        if method == "chain_getBlockHash" and params == [0]:
            return V3.genesis_hash
        if method == "chain_getHeader" and not params:
            return {"number": hex(self.number)}
        if method == "rpc_methods":
            return {"methods": ["dev_newBlock", "dev_setStorage"]}
        if method == "state_queryStorageAt" and self.served is not None:
            keys, _ = params
            return [{"block": PIN, "changes": [[k, self.served.get(k, self.storage.get(k))] for k in keys]}]
        return super().__call__(method, params)


def _state():
    acct = {"0x" + account_key(bytes([i]) * 32).hex(): "0x" + f"{i:02x}" * 80 for i in range(1, 6)}
    child = "0x" + (b":child_storage:default:" + b"\x01" * 32).hex()
    issuance = "0x" + (twox128("Balances") + twox128("TotalIssuance")).hex()
    return {**acct, child: "0x" + "99" * 32, issuance: "0x" + "00" * 16, "0x3a636f6465": "0x0061736d"}, child


def _manifest(state):
    per_item = {"System.Account": 5, "Balances.TotalIssuance": 1, ":code": 1,
                ":child_storage:default:\x01": 1}
    return {"source": {"block_hash": PIN, "block_number": 117790}, "load": {"per_item": per_item},
            "fork": {"chain": "v3", "genesis_hash": V3.genesis_hash, "fork_block": {"hash": PIN}}}


def _run(source_chain, fork_chain, state, **kw):
    src = RpcClient("wss://test-node-v3.portaldot.dev", connect=fake_connect(source_chain), sleep=lambda s: None)
    fork = RpcClient("ws://127.0.0.1:59944", connect=fake_connect(fork_chain), sleep=lambda s: None)
    return verify_v3(_manifest(state), src, fork, state, sample=20, seed=7, **kw)


def test_identical_fork_passes_every_check_but_the_runtime_of_a_fake_code_blob():
    state, child = _state()
    r = _run(FakeV3(state), FakeV3(state), state)
    assert r.classes == {"identical": 20}
    assert r.checks["key_set"]["ok"] and r.checks["all_lifted_values"]["ok"]
    assert r.checks["all_lifted_values"]["child_root_not_served"] == 0
    assert not r.checks["runtime"]["ok"]  # the fake :code is not V3's pinned runtime
    assert not r.ok


def test_child_root_read_null_is_named_not_hidden_and_not_a_mismatch():
    state, child = _state()
    r = _run(FakeV3(state), FakeV3(state, served={child: None}), state)
    assert r.checks["all_lifted_values"]["child_root_not_served"] == 1
    assert r.checks["all_lifted_values"]["child_root_keys"] == [child]
    assert r.checks["all_lifted_values"]["ok"] and not r.mismatches


def test_a_changed_balance_on_the_fork_is_a_named_mismatch():
    state, _ = _state()
    key = next(iter(state))
    r = _run(FakeV3(state), FakeV3(state, served={key: "0x" + "ff" * 80}), state)
    assert not r.checks["all_lifted_values"]["ok"]
    assert any(m["key"] == key and m["item"] == "System.Account" for m in r.mismatches)


def test_a_fork_at_another_block_than_its_pin_is_refused():
    state, _ = _state()
    m = _manifest(state)
    m["fork"]["fork_block"]["hash"] = "0x" + "01" * 32
    src = RpcClient("wss://x", connect=fake_connect(FakeV3(state)), sleep=lambda s: None)
    fork = RpcClient("ws://127.0.0.1:59944", connect=fake_connect(FakeV3(state)), sleep=lambda s: None)
    with pytest.raises(VerifyError, match="not its pin"):
        verify_v3(m, src, fork, state, sample=5, seed=1)
