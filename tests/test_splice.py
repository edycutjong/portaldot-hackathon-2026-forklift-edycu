import pytest

from forklift.hashing import blake2_256, twox128
from forklift.splice import (
    CODE, DEV_KEY, FORCE_ERA, SUDO_KEY, TOTAL_ISSUANCE, SpliceError, account_key, balance_of,
    fork_chain_spec, genesis_key_set_hash, rule_of, splice_state, u128,
)


def h(raw: bytes) -> str:
    return "0x" + raw.hex()


def item(pallet: str, name: str, suffix: bytes = b"") -> str:
    return h(twox128(pallet) + twox128(name) + suffix)


def account_info(free: int, reserved: int = 0, nonce: int = 0) -> str:
    return h(nonce.to_bytes(4, "little") + (0).to_bytes(4, "little") + (1).to_bytes(4, "little")
             + (0).to_bytes(4, "little") + u128(free) + u128(reserved) + u128(0) + u128(0))


WHALE = bytes(range(32))
ALICE_KEY = h(account_key(DEV_KEY))


def dev_genesis():
    return {
        h(b":code"): "0x0061736d0100000001",
        h(b":grandpa_authorities"): "0x01aa",
        h(b":extrinsic_index"): "0x00000000",
        item("Babe", "Authorities"): "0xdeadbeef",
        item("Session", "Validators"): "0x04" + DEV_KEY.hex(),
        item("System", "ParentHash"): "0x" + "45" * 32,
        item("System", "Number"): "0x00000000",
        ALICE_KEY: account_info(5_000, 0, nonce=0),
        h(account_key(b"\x8e" * 32)): account_info(1_000),  # Bob-like dev account: replaced by mainnet
        item("Sudo", "Key"): h(DEV_KEY),
        item("Staking", "ValidatorCount"): "0x01000000",
        item("Balances", "TotalIssuance"): h(u128(6_000)),
    }


def mainnet_lift():
    return {
        h(b":code"): "0x0061736d01000000ff",
        h(b":grandpa_authorities"): "0x01bb",
        item("Babe", "Authorities"): "0xcafe",
        item("Babe", "EpochIndex"): "0x0100000000000000",
        item("Grandpa", "SetIdSession", b"\x01" * 8): "0x02000000",
        item("Session", "Validators"): "0x04" + WHALE.hex(),
        item("System", "ParentHash"): "0x" + "77" * 32,
        item("System", "BlockHash", b"\x02" * 12): "0x" + "88" * 32,
        h(account_key(WHALE)): account_info(10**20, 7),
        item("Sudo", "Key"): "0x" + "ec" * 32,
        item("Staking", "ForceEra"): "0x00",
        item("Staking", "ValidatorCount"): "0x0a000000",
        item("Staking", "Nominators", b"\x03" * 40): "0x0408",
        item("Balances", "TotalIssuance"): h(u128(10**20 + 7)),
    }


def test_rules_keep_consensus_and_system_bookkeeping_from_dev():
    assert rule_of(bytes.fromhex(item("Babe", "Authorities")[2:])) == "dev:Babe"
    assert rule_of(twox128("Session") + twox128("HistoricalSessions")) == "dev:Session"  # Historical shares Session
    assert rule_of(b":grandpa_authorities") == "dev::grandpa_authorities"
    assert rule_of(twox128("System") + twox128("BlockHash") + b"\x00" * 12) == "dev:System"
    assert rule_of(account_key(WHALE)) == "mainnet"  # System.Account is the exception
    assert rule_of(twox128("MerkleMountainRange") + twox128("Nodes")) == "reset:MerkleMountainRange"
    assert rule_of(twox128("Mmr") + twox128(":__PALLET_VERSION__:")) == "mainnet"  # version key, not the range
    assert rule_of(b":code") == "mainnet"
    assert rule_of(twox128("Staking") + twox128("Ledger")) == "mainnet"


def test_splice_takes_mainnet_outside_the_dev_rules():
    s = splice_state(mainnet_lift(), dev_genesis())
    top = s.top
    assert top[item("Babe", "Authorities")] == "0xdeadbeef"  # dev
    assert item("Babe", "EpochIndex") not in top  # mainnet value under a dev prefix is dropped
    assert item("Grandpa", "SetIdSession", b"\x01" * 8) not in top
    assert top[item("Session", "Validators")] == "0x04" + DEV_KEY.hex()
    assert top[item("System", "ParentHash")] == "0x" + "45" * 32
    assert item("System", "BlockHash", b"\x02" * 12) not in top
    assert top[h(account_key(WHALE))] == mainnet_lift()[h(account_key(WHALE))]
    assert top[item("Staking", "ValidatorCount")] == "0x0a000000"  # mainnet beats dev outside the rules
    assert h(account_key(b"\x8e" * 32)) not in top  # a dev account is not mainnet state
    assert top[h(b":extrinsic_index")] == "0x00000000"


def test_the_five_overrides_are_named_with_reasons():
    s = splice_state(mainnet_lift(), dev_genesis())
    names = [o.name for o in s.overrides]
    assert names == ["Sudo.Key", "Staking.ForceEra", "System.Account[dev key]", "Balances.TotalIssuance", ":code"]
    assert all(o.reason for o in s.overrides)
    assert s.top[h(SUDO_KEY)] == h(DEV_KEY)
    assert s.top[h(FORCE_ERA)] == "0x02"
    assert s.top[h(CODE)] == dev_genesis()[h(CODE)]
    code = next(o for o in s.overrides if o.name == ":code").as_json()
    assert code["value"].startswith("blake2_256 0x") and "B)" in code["value"]


def test_issuance_adds_exactly_the_dev_accounts_balance():
    s = splice_state(mainnet_lift(), dev_genesis())
    assert int.from_bytes(bytes.fromhex(s.top[h(TOTAL_ISSUANCE)][2:]), "little") == 10**20 + 7 + 5_000
    assert s.issuance["added"] == "5000"
    assert s.issuance["fork"] == str(10**20 + 7 + 5_000)


def test_issuance_counts_free_plus_reserved_and_replaces_an_existing_dev_account():
    dev = dev_genesis()
    dev[ALICE_KEY] = account_info(5_000, 300)
    main = mainnet_lift()
    main[ALICE_KEY] = account_info(1_000, 0)  # the dev key already holds 1,000 on mainnet
    s = splice_state(main, dev)
    assert s.issuance["added"] == str(5_300 - 1_000)
    assert next(o for o in s.overrides if o.name == "System.Account[dev key]").mainnet_value == main[ALICE_KEY]
    assert s.accounting["overridden"] == 5 and s.accounting["genesis_added_by_overrides"] == 0


def test_accounting_sums_exactly():
    main = mainnet_lift()
    a = splice_state(main, dev_genesis()).accounting
    assert a["keys_lifted"] == len(main)
    assert a["genesis_identical"] + a["dev_kept"] + a["overridden"] + a["reset"] == a["keys_lifted"]
    assert a["dev_kept_per_prefix"] == {":grandpa_authorities": 1, "Babe": 2, "Grandpa": 1, "Session": 1,
                                       "System": 2}
    assert a["overridden"] == 4  # :code, Sudo.Key, ForceEra, TotalIssuance exist on mainnet; Alice's account does not
    assert a["genesis_added_by_overrides"] == 1
    assert a["genesis_keys"] == a["genesis_identical"] + a["overridden"] + a["genesis_from_dev"] + 1


def test_mmr_keys_are_reset_on_both_sides():
    main = mainnet_lift()
    main[item("MerkleMountainRange", "RootHash")] = "0x" + "11" * 32
    dev = dev_genesis()
    dev[item("MerkleMountainRange", "NumberOfLeaves")] = "0x0000000000000000"
    s = splice_state(main, dev)
    assert not any(k.startswith(h(twox128("MerkleMountainRange"))) for k in s.top)
    assert s.accounting["reset"] == 1 and s.accounting["sums"]


def test_splice_refuses_bad_inputs():
    dev = dev_genesis()
    del dev[ALICE_KEY]
    with pytest.raises(SpliceError, match="dev key"):
        splice_state(mainnet_lift(), dev)
    main = mainnet_lift()
    del main[item("Balances", "TotalIssuance")]
    with pytest.raises(SpliceError, match="TotalIssuance"):
        splice_state(main, dev_genesis())
    with pytest.raises(SpliceError, match="exact"):
        splice_state(mainnet_lift(), dev_genesis(), runtime="exact")
    with pytest.raises(SpliceError, match="80 bytes"):
        balance_of(b"\x00" * 76)


def test_balance_of_reads_free_and_reserved():
    assert balance_of(bytes.fromhex(account_info(123, 45)[2:])) == (123, 45)


def test_fork_chain_spec_never_peers_and_has_its_own_id():
    s = splice_state(mainnet_lift(), dev_genesis())
    dev_spec = {"name": "Development", "id": "dev", "chainType": "Development",
                "bootNodes": ["/ip4/127.0.0.1/tcp/30333/p2p/12D3Koo"], "protocolId": None,
                "telemetryEndpoints": None, "properties": None, "genesis": {"raw": {"top": {}}}}
    spec = fork_chain_spec(dev_spec, s.top, block_number=2725639, runtime="fast",
                           properties={"tokenSymbol": "POT", "tokenDecimals": 14})
    assert spec["bootNodes"] == []
    assert spec["id"] == "forklift_1002_2725639_fast" and spec["id"] != "dev"
    assert spec["protocolId"] == "forklift-2725639"
    assert spec["properties"]["tokenSymbol"] == "POT"
    assert spec["genesis"]["raw"]["top"] == dict(sorted(s.top.items()))
    assert dev_spec["genesis"]["raw"]["top"] == {}  # input not mutated
    named = fork_chain_spec(dev_spec, s.top, block_number=2725639, runtime="fast", fork_name="soak")
    assert named["name"].endswith("(fast, soak)") and named["genesis"] == spec["genesis"]


def test_genesis_key_set_hash_is_order_independent_and_sensitive():
    keys = ["0x01", "0x0203", "0xff"]
    assert genesis_key_set_hash(keys) == genesis_key_set_hash(list(reversed(keys)))
    assert genesis_key_set_hash(keys) != genesis_key_set_hash(keys[:2])
    assert genesis_key_set_hash(["0x0102"]) != genesis_key_set_hash(["0x01", "0x02"])  # framed
    assert len(blake2_256(b"")) == 32
