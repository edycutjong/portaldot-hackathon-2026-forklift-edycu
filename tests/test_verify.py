import random

import pytest

from forklift.hashing import blake2_256, twox128
from forklift.metadata import StorageIndex, StorageItem
from forklift.rpc import RpcClient
from forklift.splice import genesis_key_set_hash, splice_state
from forklift.verify import EXIT_MISMATCH, WINDOW, VerifyError, classify, draw_key, item_prefixes, verify
from tests.fakes import FakeChain, fake_connect
from tests.test_splice import dev_genesis, item, mainnet_lift

PIN = "0x" + "ab" * 32
GENESIS = "0x" + "cd" * 32


def index_for(names):
    idx = StorageIndex([])
    for full in names:
        pallet, name = full.split(".")
        idx.items[twox128(pallet) + twox128(name)] = StorageItem(pallet, name, "Map")
        idx.pallets[twox128(pallet)] = pallet
    return idx


NAMES = ["Babe.Authorities", "Babe.EpochIndex", "Grandpa.SetIdSession", "Session.Validators", "System.ParentHash",
         "System.BlockHash", "System.Account", "Sudo.Key", "Staking.ForceEra", "Staking.ValidatorCount",
         "Staking.Nominators", "Balances.TotalIssuance"]
INDEX = index_for(NAMES)


def manifest_and_chains(corrupt=None):
    main = mainnet_lift()
    sp = splice_state(main, dev_genesis())
    top = dict(sp.top)
    if corrupt:
        corrupt(top)
    per_item = {}
    for k in main:
        per_item[INDEX.item_of(bytes.fromhex(k[2:]))] = per_item.get(INDEX.item_of(bytes.fromhex(k[2:])), 0) + 1
    m = {
        "source": {"block_hash": PIN, "block_number": 100, "endpoint": "wss://mainnet.portaldot.io",
                   "code_hash": "0x" + blake2_256(bytes.fromhex(main["0x3a636f6465"][2:])).hex()},
        "load": {"per_item": per_item, "keys_lifted": len(main)},
        "overrides": [o.as_json() for o in sp.overrides],
        "fork": {"genesis_hash": GENESIS, "genesis_keys": len(sp.top), "genesis_key_set_hash": genesis_key_set_hash(sp.top),
                 "issuance": sp.issuance, "code_hash": sp.dev_code_hash, "label": "fork-1002-fast"},
        "node": {"runtime_mode": "fast"},
    }
    mainnet = FakeChain(main, number=100, block_hash=PIN)
    fork = FakeChain(top, number=0, block_hash=GENESIS)
    return m, mainnet, fork, sp


def head_aware(chain):
    """Answer the head reads verify makes on the fork (best/finalized) on top of the pinned fake."""
    inner = chain.__call__

    def handler(method, params):
        if method == "chain_getFinalizedHead":
            return chain.block_hash
        if method == "chain_getHeader" and not params:
            return {"number": "0x7"}
        return inner(method, params)
    return handler


def run(m, mainnet, fork, sample=60, seed=1, spliced=None):
    main_c = RpcClient("wss://mainnet.portaldot.io", connect=fake_connect(mainnet))
    fork_c = RpcClient("ws://127.0.0.1:59944", connect=fake_connect(head_aware(fork)))
    return verify(m, main_c, fork_c, sample=sample, seed=seed, index=INDEX, spliced=spliced)


def test_clean_fork_verifies_with_every_class_explained():
    m, mainnet, fork, sp = manifest_and_chains()
    r = run(m, mainnet, fork, spliced=dict(sp.top))
    assert r.ok and r.exit_code == 0
    assert set(r.classes) <= {"identical", "dev-kept", "override"}
    assert r.classes["identical"] and r.classes["dev-kept"]
    assert all(c["ok"] for c in r.checks.values())
    assert r.checks["overrides"]["count"] == 5
    assert r.checks["compared_with_mainnet"]["dev_kept_checked_against_splice"]
    assert r.fork_head == {"best": 7, "finalized": 0}


def test_samples_are_drawn_from_mainnet_at_the_pin_inside_item_prefixes():
    m, mainnet, fork, _ = manifest_and_chains()
    run(m, mainnet, fork, sample=30)
    paged = [p for meth, p in mainnet.calls if meth == "state_getKeysPaged"]
    assert paged and all(p[3] == PIN for p in paged)  # pinned reads
    starts = [p for p in paged if p[2] is not None]
    assert len(starts) == 30  # one random start per draw; a None start is only the wrap to the item's head
    for prefix, count, start, _ in starts:
        assert count == WINDOW and start.startswith(prefix) and len(start) == len(prefix) + 32  # prefix ‖ random16


def test_every_key_of_a_nested_map_group_is_reachable():
    """Double-map keys share a group prefix (era); the window pick must reach past each group's first key."""
    stakers = {item("Staking", "ErasStakers", bytes([era]) * 12 + bytes([v]) * 40): "0x00"
               for era in (1, 2) for v in range(5)}
    chain = FakeChain(stakers, block_hash=PIN)
    c = RpcClient("wss://mainnet.portaldot.io", connect=fake_connect(chain))
    prefix = twox128("Staking") + twox128("ErasStakers")
    rng = random.Random(7)
    seen = {draw_key(c, PIN, prefix, rng)[0] for _ in range(300)}
    assert seen == set(stakers)


def test_a_changed_value_is_a_named_mismatch_and_exit_2():
    whale_key = [k for k in mainnet_lift() if k.startswith(item("System", "Account"))][0]

    def corrupt(top):
        top[whale_key] = top[whale_key][:-2] + "ff"
    m, mainnet, fork, _ = manifest_and_chains(corrupt)
    r = run(m, mainnet, fork, sample=120)
    assert not r.ok and r.exit_code == EXIT_MISMATCH == 2
    assert [mm["item"] for mm in r.mismatches] == ["System.Account"]
    assert r.mismatches[0]["key"] == whale_key


def test_a_missing_key_fails_the_key_set_check_and_names_it():
    gone = item("Staking", "Nominators", b"\x03" * 40)
    m, mainnet, fork, sp = manifest_and_chains(lambda top: top.pop(gone))
    main_c, fork_c = (RpcClient("wss://mainnet.portaldot.io", connect=fake_connect(mainnet)),
                      RpcClient("ws://127.0.0.1:59944", connect=fake_connect(head_aware(fork))))
    r = verify(m, main_c, fork_c, sample=5, seed=3, index=INDEX, spliced=dict(sp.top))
    assert not r.ok
    ks = r.checks["genesis_key_set"]
    assert ks["fork_keys"] == ks["expected_keys"] - 1
    assert ks["missing_on_fork"] == ["Staking.Nominators"] and ks["extra_on_fork"] == []


def test_wrong_issuance_and_wrong_override_fail():
    ti = item("Balances", "TotalIssuance")
    m, mainnet, fork, _ = manifest_and_chains(lambda top: top.__setitem__(ti, "0x" + "00" * 16))
    r = run(m, mainnet, fork, sample=5)
    assert not r.checks["issuance"]["ok"] and not r.checks["overrides"]["ok"] and r.exit_code == 2


def test_a_fork_with_another_genesis_is_refused_not_compared():
    m, mainnet, fork, _ = manifest_and_chains()
    m["fork"]["genesis_hash"] = "0x" + "99" * 32
    with pytest.raises(VerifyError, match="not this manifest's fork"):
        run(m, mainnet, fork)


def test_a_dev_kept_key_must_hold_the_spliced_dev_value():
    babe = item("Babe", "Authorities")
    m, mainnet, fork, sp = manifest_and_chains(lambda top: top.__setitem__(babe, "0xcafe"))  # mainnet's value leaked
    m["load"]["per_item"] = {"Babe.Authorities": 1, "System.Account": 1}
    r = run(m, mainnet, fork, sample=40, spliced=dict(sp.top))
    assert [mm["item"] for mm in r.mismatches] == ["Babe.Authorities"] and r.exit_code == 2
    assert classify(babe, "0xcafe", "0xcafe", {}, {babe: "0xdeadbeef"}) == "mismatch"
    assert classify(babe, "0xcafe", "0xdeadbeef", {}, {babe: "0xdeadbeef"}) == "dev-kept"
    epoch = item("Babe", "EpochIndex")  # mainnet-only key under a dev prefix: must be absent on the fork
    assert classify(epoch, "0x01", None, {}, {}) == "dev-kept"
    assert classify(epoch, "0x01", "0x01", {}, {}) == "mismatch"


def test_a_sample_that_compares_nothing_with_mainnet_fails():
    m, mainnet, fork, sp = manifest_and_chains()
    m["load"]["per_item"] = {"Babe.Authorities": 1, "Grandpa.SetIdSession": 1}  # dev-kept items only
    r = run(m, mainnet, fork, sample=10, spliced=dict(sp.top))
    assert not r.mismatches and not r.checks["compared_with_mainnet"]["ok"] and r.exit_code == 2


def test_missing_issuance_is_reported_as_none_not_a_crash():
    ti = item("Balances", "TotalIssuance")
    m, mainnet, fork, _ = manifest_and_chains(lambda top: top.pop(ti))
    r = run(m, mainnet, fork, sample=5)
    assert r.checks["issuance"]["fork"] is None and not r.checks["issuance"]["ok"]


def test_classify_rules():
    over = {"0x01": {"value": "0xaa"}, "0x3a636f6465": {"value": "blake2_256 0x" + blake2_256(b"\x00a").hex() + " (2 B)"}}
    assert classify("0x01", "0xbb", "0xaa", over) == "override"
    assert classify("0x01", "0xbb", "0xbb", over) == "mismatch"  # the fork must hold the override's value
    assert classify("0x3a636f6465", "0x00", "0x0061", over) == "override"  # :code compared by hash
    babe = item("Babe", "Randomness")
    assert classify(babe, "0x01", "0x02", over) == "dev-kept"
    mmr = "0x" + (twox128("MerkleMountainRange") + twox128("Nodes")).hex() + "00" * 8
    assert classify(mmr, "0x01", None, over) == "reset"
    assert classify(mmr, "0x01", "0x01", over) == "mismatch"
    acct = item("System", "Account", b"\x01" * 48)
    assert classify(acct, "0x05", "0x05", over) == "identical"
    assert classify(acct, "0x05", None, over) == "mismatch"


def test_draw_key_wraps_and_reads_a_plain_item_the_pager_cannot_return():
    plain = item("Staking", "ValidatorCount")
    ledgers = {item("Staking", "Ledger", bytes([i]) * 40): "0x00" for i in range(3)}
    chain = FakeChain({plain: "0x0a000000", **ledgers}, block_hash=PIN)
    c = RpcClient("wss://mainnet.portaldot.io", connect=fake_connect(chain))
    key, wrapped = draw_key(c, PIN, bytes.fromhex(plain[2:]), random.Random(0))
    assert key == plain and wrapped
    ledger_prefix = twox128("Staking") + twox128("Ledger")
    seen = {draw_key(c, PIN, ledger_prefix, random.Random(i))[0] for i in range(40)}
    assert seen == set(ledgers)  # a short item wraps, and every key is reachable


def test_item_prefixes_cover_well_known_keys_and_reject_unknown():
    p = item_prefixes(INDEX, {"System.Account": 3, ":code": 1})
    assert p[":code"] == b":code" and p["System.Account"] == twox128("System") + twox128("Account")
    with pytest.raises(VerifyError):
        item_prefixes(INDEX, {"Nope.unknown:0x00": 1})


def test_same_seed_draws_the_same_keys():
    m, mainnet, fork, _ = manifest_and_chains()
    a = run(m, mainnet, fork, sample=25, seed=42)
    b = run(m, mainnet, fork, sample=25, seed=42)
    assert [r["key"] for r in a.sample] == [r["key"] for r in b.sample]
