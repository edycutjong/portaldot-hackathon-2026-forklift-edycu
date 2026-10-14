import pytest

from forklift.rpc import RpcClient, RpcError, RpcUnavailable, WriteGuardError, is_write_method
from tests.fakes import fake_connect


def client(handler, **kw):
    kw.setdefault("sleep", lambda s: None)
    return RpcClient(kw.pop("endpoint", "wss://mainnet.portaldot.io"), connect=fake_connect(handler), **kw)


def test_call_returns_result_and_uses_increasing_ids():
    log = []
    c = RpcClient("ws://127.0.0.1:59944", connect=fake_connect(lambda m, p: m, log), sleep=lambda s: None)
    assert c.call("system_name") == "system_name"
    assert c.call("system_chain") == "system_chain"
    assert [r["id"] for r in log[0].sent] == [1, 2]


def test_write_guard_refuses_author_methods_on_mainnet():
    seen = []
    c = client(lambda m, p: seen.append(m))
    for method in ("author_submitExtrinsic", "author_submitAndWatchExtrinsic", "author_insertKey"):
        with pytest.raises(WriteGuardError):
            c.call(method, ["0x00"])
    assert seen == []  # nothing ever reached the transport


def test_write_guard_allows_author_on_loopback_fork():
    c = client(lambda m, p: "0x" + "11" * 32, endpoint="ws://127.0.0.1:59944", fork=True)
    assert c.call("author_submitExtrinsic", ["0x00"]).startswith("0x11")


@pytest.mark.parametrize("endpoint", [
    "wss://mainnet.portaldot.io",
    "wss://mainnet.portaldot.io:443",
    "ws://192.168.1.20:9944",
    "wss://example.org",
])
def test_fork_flag_refused_for_non_loopback_or_mainnet(endpoint):
    with pytest.raises(WriteGuardError):
        RpcClient(endpoint, fork=True)


def test_is_write_method():
    assert is_write_method("author_rotateKeys")
    assert not is_write_method("state_getKeysPaged")
    assert not is_write_method("chain_getBlockHash")


def test_retries_with_backoff_then_succeeds():
    calls = {"n": 0}
    sleeps = []

    def flaky(method, params):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ConnectionResetError("dropped")
        return "ok"

    c = RpcClient("wss://mainnet.portaldot.io", connect=fake_connect(flaky), retries=3, backoff=0.5,
                  sleep=sleeps.append)
    assert c.call("chain_getFinalizedHead") == "ok"
    assert sleeps == [0.5, 1.0]
    assert c.reconnects == 2


def test_gives_up_after_retries():
    def down(method, params):
        raise TimeoutError("slow")

    c = RpcClient("wss://mainnet.portaldot.io", connect=fake_connect(down), retries=2, sleep=lambda s: None)
    with pytest.raises(RpcUnavailable):
        c.call("chain_getFinalizedHead")


def test_rpc_error_is_not_retried():
    calls = []

    def bad(method, params):
        calls.append(method)
        return {"__error__": {"code": -32602, "message": "Invalid params"}}

    c = client(bad, retries=5)
    with pytest.raises(RpcError) as err:
        c.call("state_getKeysPaged", [])
    assert err.value.code == -32602
    assert calls == ["state_getKeysPaged"]


@pytest.mark.parametrize("method", ["system_addReservedPeer", "offchain_localStorageSet", "system_addLogFilter",
                                    "engine_createBlock", "dev_newBlock", "Author_submitExtrinsic"])
def test_read_only_client_refuses_everything_off_the_read_allowlist(method):
    seen = []
    with pytest.raises(WriteGuardError):
        client(lambda m, p: seen.append(m)).call(method, [])
    assert seen == []


@pytest.mark.parametrize("endpoint", [
    "ws://127.0.0.1@mainnet.portaldot.io",
    "wss://localhost.mainnet.portaldot.io",
    "ws://[2001:db8::1]:9944",
])
def test_fork_flag_refused_for_disguised_hosts(endpoint):
    with pytest.raises(WriteGuardError):
        RpcClient(endpoint, fork=True)


def test_fork_flag_allowed_for_ipv6_loopback():
    assert RpcClient("ws://[::1]:59944", fork=True).fork


def test_loopback_fork_serving_mainnet_genesis_is_refused():
    from forklift.rpc import MAINNET_GENESIS

    sent = []

    def tunnel(method, params):
        sent.append(method)
        return MAINNET_GENESIS if method == "chain_getBlockHash" else "0x00"

    c = client(tunnel, endpoint="ws://127.0.0.1:59944", fork=True)
    with pytest.raises(WriteGuardError):
        c.call("author_submitExtrinsic", ["0x00"])
    assert "author_submitExtrinsic" not in sent


def test_non_json_reply_is_retried():
    calls = {"n": 0}

    class Gateway:
        def __init__(self):
            self.q = []

        def send(self, message):
            import json
            calls["n"] += 1
            req = json.loads(message)
            self.q.append("<html>502 Bad Gateway</html>" if calls["n"] == 1
                          else json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": "ok"}))

        def recv(self, timeout=None):
            return self.q.pop(0)

        def close(self):
            pass

    def connect(endpoint, timeout):
        g = Gateway()
        return g, g.close

    c = RpcClient("wss://mainnet.portaldot.io", connect=connect, sleep=lambda s: None)
    assert c.call("chain_getFinalizedHead") == "ok" and c.reconnects == 1
