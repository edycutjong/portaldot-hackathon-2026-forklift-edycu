"""fund / as / receipt at the CLI layer, with the chain faked: exit codes, evidence rows, and what is never sent."""

import json
import sys

import pytest
from click.testing import CliRunner

from forklift import act, cli, receipt, tx
from forklift.cli import main
from forklift.hashing import blake2_256
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
        if method == "rpc_methods":  # a tunnel to the live chain does not answer Chopsticks' dev_* methods
            return {"methods": ["chain_getHeader", "author_submitExtrinsic"]}
        raise AssertionError(method)

    client = RpcClient("ws://127.0.0.1:59944", fork=True, connect=fake_connect(handler), sleep=lambda s: None)
    with pytest.raises(WriteGuardError, match="mainnet"):
        client.call("author_submitExtrinsic", ["0x00"])
    assert seen["submits"] == 1  # never re-sent to the mainnet genesis


def test_stale_answer_after_a_resend_waits_for_the_block_scan():
    from forklift.rpc import RpcError

    class C:
        resent_writes = 0

        def call(self, method, params):
            self.resent_writes = 1  # an earlier attempt reached the node, the client re-sent once
            raise RpcError(method, {"code": 1010, "message": "Invalid Transaction: Transaction is outdated"})

    assert tx._send(C(), "0x00", "0xexpected") == "0xexpected"

    class First(C):
        def call(self, method, params):  # a first send: a real rejection
            raise RpcError(method, {"code": 1010, "message": "Invalid Transaction"})

    with pytest.raises(RpcError):
        tx._send(First(), "0x00", "0xexpected")


def test_resent_writes_counts_only_attempts_that_reached_the_socket():
    from forklift.rpc import RpcUnavailable

    def drop_after_send(method, params):
        if method == "chain_getBlockHash":
            return GENESIS
        raise OSError("reset after the request went out")

    c = RpcClient("ws://127.0.0.1:59944", fork=True, connect=fake_connect(drop_after_send), sleep=lambda s: None,
                  retries=1)
    with pytest.raises(RpcUnavailable):
        c.call("author_submitExtrinsic", ["0x00"])
    assert c.resent_writes == 1

    def refuse_connect(endpoint, timeout):
        raise OSError("connection refused")  # nothing ever reaches the node

    c = RpcClient("ws://127.0.0.1:59944", fork=True, connect=refuse_connect, sleep=lambda s: None, retries=1)
    c._fork_checked = True
    with pytest.raises(RpcUnavailable):
        c.call("author_submitExtrinsic", ["0x00"])
    assert c.resent_writes == 0


class _Chain:
    """Heights → block hashes that can change between polls; the extrinsic sits in one block hash."""

    def __init__(self, script, home):
        self.script, self.home, self.poll = script, home, 0

    def view(self):
        return self.script[min(self.poll, len(self.script) - 1)]

    def __call__(self, method, params):
        v = self.view()
        if method == "chain_getHeader":
            if params and params[0] == "fin":
                return {"number": hex(max(v))}
            return {"number": hex(max(v))}
        if method == "chain_getBlockHash":
            return GENESIS if params == [0] else v.get(params[0])
        if method == "author_submitExtrinsic":
            self.poll = 1
            return "0x" + blake2_256(bytes.fromhex(params[0][2:])).hex()
        if method == "chain_getBlock":
            return {"block": {"extrinsics": ["0x00", "0x0102"] if params[0] == self.home else ["0x00"]}}
        if method == "chain_getFinalizedHead":
            return "fin"
        if method == "state_getStorage":
            return None
        raise AssertionError(method)


def _submit_on(chain, monkeypatch):
    monkeypatch.setattr(tx, "events_at", lambda c, rt, h: [tx.Event(0, "ApplyExtrinsic", 1, "System",
                                                                     "ExtrinsicSuccess", [])])
    def sleep(_):
        chain.poll += 1
    return tx.submit(RpcClient("ws://127.0.0.1:59944", fork=True, connect=fake_connect(chain)), None,
                     bytes.fromhex("0102"), sleep=sleep)


def test_scan_steps_back_when_the_best_chain_gets_shorter(monkeypatch):
    chain = _Chain([{8: "a8"}, {8: "a8", 9: "a9", 10: "a10"}, {8: "b8"}, {8: "b8", 9: "b9"}], home="b9")
    inc = _submit_on(chain, monkeypatch)
    assert (inc.block_number, inc.block_hash) == (9, "b9")


def test_scan_notices_a_same_height_reorg(monkeypatch):
    chain = _Chain([{5: "a5"}, {5: "a5", 6: "a6"}, {5: "a5", 6: "b6"}], home="b6")
    inc = _submit_on(chain, monkeypatch)
    assert (inc.block_number, inc.block_hash) == (6, "b6")


def test_bind_fork_refuses_a_manifest_without_a_genesis():
    from forklift.cli import _bind_fork

    client = RpcClient("ws://127.0.0.1:59944", fork=True, connect=fake_connect(lambda m, p: GENESIS))
    with pytest.raises(WriteGuardError, match="no genesis"):
        _bind_fork(client, {"fork": {}})


def test_a_landed_tx_whose_evidence_cannot_be_written_keeps_its_exit_code(fork, monkeypatch, tmp_path):
    def fake_run(client, rt, manifest, *, on_step, **kw):
        bad = _inc(4, ok=False, err="x")
        on_step({"kind": "rent_top_up", "inclusion": bad, "amount": 1})
        raise receipt.ReceiptDispatchError("rent top-up failed", bad, "rent_top_up")

    monkeypatch.setattr(receipt, "run_receipt", fake_run)
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("")
    res = CliRunner().invoke(main, ["receipt", "--name", "f", "--evidence", str(blocker / "ev")])
    assert res.exit_code == 5
    assert "NOT recorded" in res.stderr and "extrinsic 4-1" in res.stderr


def test_tx_id_must_look_like_block_index(fork, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["forklift", "tx", "abc", "--name", "f"])
    with pytest.raises(SystemExit) as ex:
        cli.run()
    assert ex.value.code == 64
