"""fund / as / receipt at the CLI layer, with the chain faked: exit codes, evidence rows, and what is never sent."""

import json
import sys

import pytest
from click.testing import CliRunner

from forklift import act, cli, receipt, tx
from forklift.cli import main
from forklift.rpc import RpcClient, WriteGuardError
from tests.fakes import fake_connect

GENESIS = "0x" + "75" * 32
WHALE = "5DvRSgerqB5KHPqWGGnCtRJgJokj91XyGLm3aZ5DKH1rMndf"


@pytest.fixture
def fork(tmp_path, monkeypatch):
    """A recorded fork named 'f' (PID file + manifest) and a fake RPC client in the CLI."""
    monkeypatch.setenv("FORKLIFT_CACHE", str(tmp_path))
    manifest = {"source": {"block_number": 7, "block_hash": "0x" + "ab" * 32, "endpoint": "wss://m"},
                "fork": {"label": "fork-1002-fast", "genesis_hash": GENESIS},
                "node": {"base_path": "/x", "endpoint": "ws://127.0.0.1:59944"},
                "manifest_root": "0x" + "cd" * 32, "load": {"keys_lifted": 3}}
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps(manifest))
    (tmp_path / "pids").mkdir()
    (tmp_path / "pids" / "f.pid").write_text(json.dumps({"pid": 1, "ports": {"ws": 59944},
                                                         "endpoint": "ws://127.0.0.1:59944", "manifest": str(mpath)}))
    state = {"genesis": GENESIS, "sent": []}

    class FakeClient:
        def __init__(self, endpoint, fork=False, **kw):
            self.endpoint = endpoint

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def call(self, method, params=None):
            if method == "chain_getBlockHash":
                return state["genesis"]
            state["sent"].append(method)

    monkeypatch.setattr(cli, "RpcClient", FakeClient)
    monkeypatch.setattr(tx.Runtime, "load", classmethod(lambda cls, c, h=None: object()))
    state["evidence"] = tmp_path / "ev"
    return state


def _inc(n, ok=True, err=None):
    return tx.Inclusion(f"0x{n:064x}", n, f"0x{n + 100:064x}", 1, [], ok, err, 1.0, True)


def _rows(state):
    return json.loads((state["evidence"] / "txs-7.json").read_text())["rows"]


def test_failed_top_up_exits_5_with_each_landed_step_recorded_once(fork, monkeypatch):
    def fake_run(client, rt, manifest, *, on_step, **kw):
        on_step({"kind": "instantiate", "inclusion": _inc(1), "contract": WHALE, "code_hash": receipt.code_hash()})
        bad = _inc(2, ok=False, err="Balances.InsufficientBalance")
        on_step({"kind": "rent_top_up", "inclusion": bad, "contract": WHALE, "amount": 5})
        raise receipt.ReceiptDispatchError("rent top-up failed", bad, "rent_top_up")

    monkeypatch.setattr(receipt, "run_receipt", fake_run)
    res = CliRunner().invoke(main, ["receipt", "--name", "f", "--evidence", str(fork["evidence"])])
    assert res.exit_code == 5
    rows = _rows(fork)
    assert [(r["kind"], r["success"]) for r in rows] == [("instantiate", True), ("rent_top_up", False)]
    assert all(r["fork_genesis_hash"] == GENESIS and r["who"] == tx.dev_keypair().ss58_address for r in rows)
    assert rows[0]["contract"] == WHALE and rows[0]["code_hash"] == receipt.code_hash()


def test_failed_instantiate_exits_5_and_is_recorded_once(fork, monkeypatch):
    def fake_run(client, rt, manifest, *, on_step, **kw):
        raise receipt.ReceiptDispatchError("instantiate_with_code failed", _inc(3, ok=False, err="x"), "instantiate")

    monkeypatch.setattr(receipt, "run_receipt", fake_run)
    res = CliRunner().invoke(main, ["receipt", "--name", "f", "--evidence", str(fork["evidence"]), "--json"])
    assert res.exit_code == 5 and json.loads(res.output)["error"] == "dispatch_failed"
    assert [(r["kind"], r["success"]) for r in _rows(fork)] == [("instantiate", False)]


@pytest.mark.parametrize("argv", [["receipt"], ["fund", WHALE, "1"], ["as", WHALE, "Staking.chill"]])
def test_a_chain_with_another_genesis_is_refused_before_anything_is_sent(fork, monkeypatch, argv):
    fork["genesis"] = "0x" + "01" * 32
    called = []
    monkeypatch.setattr(receipt, "run_receipt", lambda *a, **k: called.append("receipt"))
    monkeypatch.setattr(act, "fund", lambda *a, **k: called.append("fund"))
    monkeypatch.setattr(act, "act_as", lambda *a, **k: called.append("as"))
    res = CliRunner().invoke(main, argv + ["--name", "f", "--json"])
    assert res.exit_code == 1 and json.loads(res.output)["error"] == "write_refused"
    assert called == [] and fork["sent"] == []


@pytest.mark.parametrize("argv", [
    ["fund", WHALE, "1", "--endpoint", "ws://127.0.0.1:1", "--evidence", "ev"],
    ["receipt", "--name", "f", "--address", "not-an-address"],
])
def test_usage_errors_exit_64_before_connecting(fork, monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["forklift", *argv])
    with pytest.raises(SystemExit) as ex:
        cli.run()
    assert ex.value.code == 64 and fork["sent"] == []


def test_a_write_retried_on_a_new_connection_is_guarded_again():
    """The socket drops mid-submit; the port now serves mainnet; the re-send must be refused (I3)."""
    from forklift.rpc import MAINNET_GENESIS

    seen = {"genesis": 0, "submits": 0}

    def handler(method, params):
        if method == "chain_getBlockHash":
            seen["genesis"] += 1
            return GENESIS if seen["genesis"] == 1 else MAINNET_GENESIS
        if method == "author_submitExtrinsic":
            seen["submits"] += 1
            raise OSError("connection reset")
        raise AssertionError(method)

    client = RpcClient("ws://127.0.0.1:59944", fork=True, connect=fake_connect(handler), sleep=lambda s: None)
    with pytest.raises(WriteGuardError, match="mainnet"):
        client.call("author_submitExtrinsic", ["0x00"])
    assert seen["submits"] == 1  # never re-sent to the mainnet genesis


def test_stale_answer_after_a_resend_waits_for_the_block_scan():
    from forklift.rpc import RpcError

    class C:
        reconnects = 0

        def call(self, method, params):
            self.reconnects = 1  # the client re-sent once inside this call
            raise RpcError(method, {"code": 1010, "message": "Invalid Transaction: Transaction is outdated"})

    assert tx._send(C(), "0x00", "0xexpected") == "0xexpected"

    class First(C):
        def call(self, method, params):  # a first send: a real rejection
            raise RpcError(method, {"code": 1010, "message": "Invalid Transaction"})

    with pytest.raises(RpcError):
        tx._send(First(), "0x00", "0xexpected")
