import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from forklift import act, tx
from forklift.cli import main

FIX = Path(__file__).parent / "fixtures"
META_HEX = "0x" + (FIX / "metadata-1002.scale").read_bytes().hex()
WHALE = "5DvRSgerqB5KHPqWGGnCtRJgJokj91XyGLm3aZ5DKH1rMndf"
VALIDATOR = "5CtnsMuscb2qYf7L3DQCD1AKwJ8S3YLvsjRMmaCWr7YZXwpT"


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("FORKLIFT_CACHE", str(tmp_path))
    return tmp_path


@pytest.fixture(scope="module")
def rt():
    return tx.Runtime(META_HEX, {"specName": "portaldot", "specVersion": 1002, "transactionVersion": 2},
                      "0x" + "22" * 32)


def test_parse_call():
    assert act.parse_call("Staking.nominate") == ("Staking", "nominate")
    for bad in ("Staking", ".nominate", "Staking.", ""):
        with pytest.raises(ValueError):
            act.parse_call(bad)


def test_parse_args_named_and_positional(rt):
    named = act.parse_args(rt, "Balances", "transfer", json.dumps({"dest": WHALE, "value": 5}))
    positional = act.parse_args(rt, "Balances", "transfer", json.dumps([WHALE, 5]))
    assert named == positional == {"dest": WHALE, "value": 5}
    assert act.parse_args(rt, "Staking", "chill", None) == {}


@pytest.mark.parametrize("raw,match", [
    ('{"dest": "x"}', "missing"), ('{"dest": "x", "value": 1, "extra": 2}', "unknown"),
    ("[1]", "takes 2"), ("not json", "not JSON"), ('"a string"', "object or array"),
])
def test_parse_args_rejects_bad_shapes(rt, raw, match):
    with pytest.raises(ValueError, match=match):
        act.parse_args(rt, "Balances", "transfer", raw)


def test_nested_calls_and_height_warnings(rt):
    vest = rt.compose("Vesting", "vest", {})
    batch = rt.compose("Utility", "batch", {"calls": [vest.value, rt.compose("Staking", "chill", {}).value]})
    assert act.nested_calls(batch.value) == [("Utility", "batch"), ("Vesting", "vest"), ("Staking", "chill")]
    warnings = act.height_warnings(batch.value)
    assert len(warnings) == 1 and warnings[0].startswith("Vesting.vest:") and "restarts at 1" in warnings[0]
    announce = rt.compose("Proxy", "announce", {"real": WHALE, "call_hash": "0x" + "00" * 32})
    assert "Proxy.announce" in act.height_warnings(announce.value)[0]
    assert act.height_warnings(rt.compose("Staking", "nominate", {"targets": [VALIDATOR]}).value) == []


def test_delta_lists_only_changed_items():
    before = {"a": 1, "b": {"x": 1}, "c": None}
    after = {"a": 1, "b": {"x": 2}, "d": 3}
    assert act.delta(before, after) == {"b": {"before": {"x": 1}, "after": {"x": 2}},
                                        "d": {"before": None, "after": 3}}


def _inclusion(events, success=True, error=None):
    return tx.Inclusion(tx_hash="0x" + "aa" * 32, block_number=7, block_hash="0x" + "bb" * 32, index=1,
                        events=events, success=success, error=error, seconds=1.0, finalized=True)


def _ev(name, value):
    pallet, n = name.split(".")
    return tx.Event(index=0, phase="ApplyExtrinsic", extrinsic_index=1, pallet=pallet, name=n,
                    attributes=[{"type": "DispatchResult", "value": value}])


def test_wrapper_result_decodes_sudo_as_done(rt):
    ok = _inclusion([_ev("Sudo.SudoAsDone", "Ok")])
    assert act._wrapper_result(rt, ok, "Sudo.SudoAsDone") == (True, None)
    err = _inclusion([_ev("Sudo.SudoAsDone", {"Err": {"Module": {"index": 6, "error": 2}}})])
    assert act._wrapper_result(rt, err, "Sudo.SudoAsDone") == (False, "Balances.InsufficientBalance")
    assert act._wrapper_result(rt, _inclusion([]), "Sudo.SudoAsDone") == (False, "no Sudo.SudoAsDone event")
    failed = _inclusion([], success=False, error="Sudo.RequireSudo")
    assert act._wrapper_result(rt, failed, "Sudo.SudoAsDone") == (False, "Sudo.RequireSudo")


def test_exit_code_is_5_when_the_dispatched_call_failed():
    r = act.ActResult(kind="as", who=WHALE, call="x", inclusion=_inclusion([]), dispatch_ok=False,
                      dispatch_error="Balances.InsufficientBalance", before={}, after={})
    assert r.exit_code == act.EXIT_DISPATCH_FAILED == 5
    assert r.as_json()["dispatch"] == {"ok": False, "error": "Balances.InsufficientBalance"}
    r.dispatch_ok = True
    assert r.exit_code == 0


def test_fund_needs_exactly_one_amount():
    runner = CliRunner()
    res = runner.invoke(main, ["fund", WHALE, "--endpoint", "ws://127.0.0.1:1", "--name", "t"])
    assert res.exit_code != 0 and "exactly one" in res.output
    res = runner.invoke(main, ["fund", WHALE, "5", "--planck", "5", "--endpoint", "ws://127.0.0.1:1", "--name", "t"])
    assert res.exit_code != 0 and "exactly one" in res.output


def test_fund_refuses_a_bad_address_and_a_bad_amount():
    runner = CliRunner()
    assert "SS58" in runner.invoke(main, ["fund", "nobody", "5", "--endpoint", "ws://127.0.0.1:1"]).output
    assert "finer than one planck" in runner.invoke(
        main, ["fund", WHALE, "0.000000000000001", "--endpoint", "ws://127.0.0.1:1"]).output


@pytest.mark.parametrize("argv", [
    ["fund", WHALE, "1"], ["as", WHALE, "Balances.transfer", json.dumps([WHALE, 1])],
])
def test_act_commands_refuse_mainnet_before_any_connection(argv):
    """I3: fund/as never reach a non-loopback endpoint; the client refuses at construction."""
    res = CliRunner().invoke(main, argv + ["--endpoint", "wss://mainnet.portaldot.io", "--json"])
    assert res.exit_code == 1
    assert json.loads(res.output)["error"] == "write_refused"


def test_act_commands_need_a_running_fork_or_an_endpoint():
    res = CliRunner().invoke(main, ["as", WHALE, "Staking.chill", "--name", "absent"])
    assert res.exit_code != 0 and "no running fork named 'absent'" in res.output


def test_own_events_drop_only_the_fee_deposit():
    def ev(name):
        p, n = name.split(".")
        return tx.Event(0, "ApplyExtrinsic", 1, p, n, [])

    inc = _inclusion([ev("Treasury.Deposit"), ev("Balances.Transfer"), ev("Sudo.SudoAsDone"),
                      ev("Treasury.Deposit"), ev("System.ExtrinsicSuccess")])
    assert [e["event"] for e in act.own_events(inc)] == ["Treasury.Deposit", "Balances.Transfer"]


def test_evidence_without_a_manifest_is_refused_before_anything_is_sent():
    """--endpoint without a fork manifest + --evidence: usage error (64) before connecting (port 1 is closed)."""
    for argv in (["fund", WHALE, "1"], ["as", WHALE, "Staking.chill"]):
        res = CliRunner().invoke(main, argv + ["--endpoint", "ws://127.0.0.1:1", "--evidence", "ev"])
        assert res.exit_code != 0 and "--evidence needs the fork's manifest" in res.output


def test_act_refuses_a_chain_that_is_not_the_manifest_fork():
    from forklift.cli import _bind_fork
    from forklift.rpc import RpcClient, WriteGuardError
    from tests.fakes import fake_connect

    client = RpcClient("ws://127.0.0.1:59944", fork=True,
                       connect=fake_connect(lambda m, p: "0x" + "99" * 32 if m == "chain_getBlockHash" else None))
    assert _bind_fork(client, None) == "0x" + "99" * 32
    with pytest.raises(WriteGuardError, match="not the fork in its manifest"):
        _bind_fork(client, {"fork": {"genesis_hash": "0x" + "75" * 32}})


def test_a_new_connection_is_checked_again_before_its_first_write():
    from forklift.rpc import RpcClient
    from tests.fakes import fake_connect

    calls = []

    def handler(method, params):
        calls.append(method)
        return "0x" + "12" * 32 if method == "chain_getBlockHash" else "0x" + "34" * 32

    client = RpcClient("ws://127.0.0.1:59944", fork=True, connect=fake_connect(handler))
    client.call("author_submitExtrinsic", ["0x00"])
    client.close()
    client.call("author_submitExtrinsic", ["0x00"])
    assert calls == ["chain_getBlockHash", "author_submitExtrinsic"] * 2
