"""``up --runtime exact``: mainnet's own runtime under Chopsticks, the fast fork's overrides on top of the pin."""

import json
from pathlib import Path

from click.testing import CliRunner

from forklift.chains import MAINNET
from forklift.cli import main, record_fork_v3
from forklift.hashing import blake2_256, twox128
from forklift.lift import MMR_PREFIX_HEX
from forklift.rpc import RpcClient
from forklift.splice import (DEV_KEY, EXACT_DEV_FREE, FORCE_ERA, SUDO_KEY, TOTAL_ISSUANCE, account_info_1002,
                             account_key, balance_of, exact_overrides)
from forklift.verify import verify_v3
from tests.fakes import FakeChain, fake_connect

FIX = Path(__file__).parent / "fixtures"
META = "0x" + (FIX / "metadata-1002.scale").read_bytes().hex()
PIN = "0x" + "f9" * 32
CODE = "0x0061736d01000000"
CODE_HASH = "0x" + blake2_256(bytes.fromhex(CODE[2:])).hex()


def _h(b: bytes) -> str:
    return "0x" + b.hex()


def _lift():
    acct = {_h(account_key(bytes([i]) * 32)): "0x" + f"{i:02x}" * 80 for i in range(1, 6)}
    return {**acct, _h(TOTAL_ISSUANCE): _h((1_000).to_bytes(16, "little")), _h(SUDO_KEY): "0x" + "ec" * 32,
            "0x3a636f6465": CODE}


def test_exact_overrides_are_the_fast_ones_without_code():
    lift = _lift()
    ov = {o.name: o for o in exact_overrides(lift)}
    assert list(ov) == ["Sudo.Key", "Staking.ForceEra", "System.Account[dev key]", "Balances.TotalIssuance"]
    assert ov["Sudo.Key"].key == _h(SUDO_KEY) and ov["Sudo.Key"].value == _h(DEV_KEY)
    assert ov["Sudo.Key"].mainnet_value == "0x" + "ec" * 32
    assert ov["Staking.ForceEra"].key == _h(FORCE_ERA) and ov["Staking.ForceEra"].value == "0x02"
    assert balance_of(bytes.fromhex(ov["System.Account[dev key]"].value[2:])) == (EXACT_DEV_FREE, 0)
    issuance = int.from_bytes(bytes.fromhex(ov["Balances.TotalIssuance"].value[2:]), "little")
    assert issuance == 1_000 + EXACT_DEV_FREE  # the dev key had nothing on mainnet: issuance = sum of balances


def test_exact_issuance_counts_a_dev_key_balance_mainnet_already_had():
    lift = _lift()
    lift[_h(account_key(DEV_KEY))] = _h(account_info_1002(400))
    ti = next(o for o in exact_overrides(lift) if o.name == "Balances.TotalIssuance")
    assert int.from_bytes(bytes.fromhex(ti.value[2:]), "little") == 1_000 + EXACT_DEV_FREE - 400


def test_account_info_1002_is_the_80_byte_layout():
    raw = account_info_1002(7)
    assert len(raw) == 80 and raw[8:12] == (1).to_bytes(4, "little") and balance_of(raw) == (7, 0)


class FakeMainnetFork(FakeChain):
    """Mainnet at the pin as Chopsticks serves it: mainnet's genesis, dev methods, an MMR range to skip."""

    def __init__(self, storage, **kw):
        super().__init__(storage, number=2725953, block_hash=PIN, metadata=META, **kw)

    def __call__(self, method, params):
        if method == "chain_getBlockHash" and params == [0]:
            return MAINNET.genesis_hash
        if method == "chain_getHeader" and not params:
            return {"number": hex(self.number + 1)}
        if method == "rpc_methods":
            return {"methods": ["dev_newBlock", "dev_setStorage"]}
        return super().__call__(method, params)


def _manifest(lift):
    return {"source": {"block_hash": PIN, "block_number": 2725953, "code_hash": CODE_HASH},
            "load": {"per_item": {"System.Account": 5, "Balances.TotalIssuance": 1, "Sudo.Key": 1, ":code": 1}},
            "fork": {"chain": "mainnet", "runtime": "exact", "genesis_hash": MAINNET.genesis_hash,
                     "fork_block": {"hash": PIN}}}


def test_verify_exact_skips_the_mmr_range_and_pins_mainnets_own_code():
    lift = _lift()
    mmr = {MMR_PREFIX_HEX + f"{i:064x}": "0x01" for i in range(3)}
    leaves = "0x" + (twox128("MerkleMountainRange") + twox128("NumberOfLeaves")).hex()
    chain_state = {**lift, **mmr, leaves: "0x0200000000000000"}
    src = RpcClient("wss://mainnet.portaldot.io", connect=fake_connect(FakeMainnetFork(chain_state)),
                    sleep=lambda s: None)
    fork = RpcClient("ws://127.0.0.1:59945", connect=fake_connect(FakeMainnetFork(chain_state)), sleep=lambda s: None)
    r = verify_v3(_manifest(lift), src, fork, lift, sample=10, seed=3)
    ks = r.checks["key_set"]  # the fork serves 4 MMR keys more than the lift: paged past, as the lift does
    assert (ks["fork"], ks["lift"], ks["ok"]) == (len(lift), len(lift), True)
    assert r.checks["runtime"] == {"fork_code_hash": CODE_HASH, "pinned": CODE_HASH, "ok": True}
    assert r.checks["mmr_range"]["ok"] and r.checks["mmr_range"]["source"]["leaves"] == 2
    assert r.ok and r.exit_code == 0


def test_verify_exact_names_a_fork_whose_mmr_differs():
    lift = _lift()
    leaves = "0x" + (twox128("MerkleMountainRange") + twox128("NumberOfLeaves")).hex()
    src = RpcClient("wss://mainnet.portaldot.io",
                    connect=fake_connect(FakeMainnetFork({**lift, leaves: "0x0200000000000000"})), sleep=lambda s: None)
    fork = RpcClient("ws://127.0.0.1:59945",
                     connect=fake_connect(FakeMainnetFork({**lift, leaves: "0x0300000000000000"})), sleep=lambda s: None)
    r = verify_v3(_manifest(lift), src, fork, lift, sample=10, seed=3)
    assert not r.checks["mmr_range"]["ok"] and not r.ok and r.exit_code == 2


def test_exact_refuses_the_portaldot_dev_flags():
    r = CliRunner().invoke(main, ["up", "--runtime", "exact", "--rpc-port", "59946"])
    assert r.exit_code == 2 and "apply to --runtime fast" in r.output


def test_exact_evidence_is_its_own_file_with_the_overrides(tmp_path):
    m = {"source": {"block_number": 2725953, "block_hash": PIN, "endpoint": MAINNET.endpoint, "spec_name": "portaldot",
                    "spec_version": 1002, "code_hash": CODE_HASH},
         "load": {"keys_lifted": 6}, "manifest_root": "0x" + "aa" * 32,
         "overrides": [o.as_json() for o in exact_overrides(_lift())],
         "fork": {"label": "fork-1002-exact", "runtime": "exact", "executor": "@acala-network/chopsticks@1.5.2",
                  "executor_flags": {}, "unresolved_import_without_flag": "ext_sandbox_instance_teardown_version_1",
                  "genesis_hash": MAINNET.genesis_hash, "code_hash": CODE_HASH, "spec": ["portaldot", 1002],
                  "fork_block": {"number": 2725953, "hash": PIN}, "code_hash_equals_source": True,
                  "overrides_at": {"number": 2725954, "hash": "0x" + "bb" * 32}},
         "timings": {"ready_s": 2.0}}
    path = record_fork_v3(m, tmp_path)
    ev = json.loads(path.read_text())
    assert path.name == "fork-1002-exact-2725953.json" and ev["profile"] == "fork-1002-exact"
    assert [o["name"] for o in ev["overrides"]][0] == "Sudo.Key"
    assert ev["fork"]["code_hash_equals_source"] and ev["fork"]["overrides_at"]["number"] == 2725954
