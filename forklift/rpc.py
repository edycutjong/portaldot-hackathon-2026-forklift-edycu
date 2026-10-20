"""Minimal JSON-RPC client over a WebSocket, with the endpoint write-guard.

Bulk storage paging goes through this client rather than a full SDK: a lift
is a few thousand sequential reads, and the SDK adds nothing to them.

Invariant I3 (no mainnet writes) lives here. A client is created either as a
read-only client (the default, used for mainnet) or as a fork client.

- A read-only client sends only methods on an explicit read allowlist; every
  other method (``author_*``, ``system_addReservedPeer``,
  ``offchain_localStorageSet``, ...) is refused before it reaches the socket.
- A fork client can only be created for a loopback endpoint that is not a
  known mainnet host, and before its first non-read call it checks that the
  node's genesis hash is not Portaldot mainnet's (a loopback port can still be
  a tunnel to mainnet). A Chopsticks fork of the V3 testnet serves V3's own
  genesis hash, so a V3 genesis is accepted only if the node also answers the
  Chopsticks ``dev_*`` methods; the live V3 chain does not.
- A testnet client is the one path to a real chain: it may send only
  ``author_submitExtrinsic``, and only after the node's genesis is a chain
  pinned as writable (the V3 testnet), never mainnet.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import re
import time
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from forklift.chains import BY_GENESIS, V3

DEFAULT_MAINNET = "wss://mainnet.portaldot.io"

# Hosts that are Portaldot mainnet. A fork client can never point at them.
MAINNET_HOSTS = frozenset({"mainnet.portaldot.io"})

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

MAINNET_GENESIS = "0x83b5212c69f85f3996f8696bb35bf913af51b176a5f53d0a74fd3a83dc0b8a54"

# The only methods a read-only (mainnet) client may send.
READ_PREFIXES = ("chain_get", "state_get", "state_query", "childstate_get")
READ_METHODS = frozenset({
    "system_chain", "system_name", "system_version", "system_properties", "system_health",
    "system_chainType", "system_accountNextIndex", "rpc_methods", "payment_queryInfo",
})

# The only write a testnet client may send.
TESTNET_WRITES = frozenset({"author_submitExtrinsic"})


class RpcError(Exception):
    """The node answered with a JSON-RPC error object."""

    def __init__(self, method: str, error: Any):
        error = error if isinstance(error, dict) else {"message": str(error)}
        self.method = method
        self.code = error.get("code")
        self.error = error
        super().__init__(f"{method}: {error.get('message', error)} (code {self.code})")


class RpcUnavailable(Exception):
    """The endpoint could not be reached after every retry."""


class WriteGuardError(Exception):
    """A write method was called on an endpoint that is not a local fork (I3)."""


class Transport(Protocol):
    def send(self, message: str) -> None: ...
    def recv(self, timeout: float | None = None) -> str: ...
    def close(self) -> None: ...


def _ws_connect(endpoint: str, open_timeout: float) -> tuple[Transport, Callable[[], None]]:
    from websockets.sync.client import connect

    stack = contextlib.ExitStack()
    # max_size=None: the runtime code value alone is ~2.5 MB.
    conn = stack.enter_context(connect(endpoint, max_size=None, open_timeout=open_timeout))
    return conn, stack.close


def is_loopback(endpoint: str) -> bool:
    return (urlparse(endpoint).hostname or "") in LOOPBACK_HOSTS


def is_read_method(method: str) -> bool:
    return method in READ_METHODS or method.startswith(READ_PREFIXES)


def is_write_method(method: str) -> bool:
    return not is_read_method(method)


class BadReply(Exception):
    """The node sent something that is not a JSON-RPC object (e.g. a gateway error page)."""


# Errors that mean "the connection is gone or slow", worth a reconnect and retry.
def _retryable() -> tuple[type[BaseException], ...]:
    errors: list[type[BaseException]] = [OSError, TimeoutError, EOFError, BadReply]
    with contextlib.suppress(ImportError):
        from websockets.exceptions import WebSocketException

        errors.append(WebSocketException)
    return tuple(errors)


class RpcClient:
    """One WebSocket connection, one request in flight at a time.

    Sequential on purpose: the public mainnet node is shared, and a lift must
    not flood it with parallel paging.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_MAINNET,
        *,
        fork: bool = False,
        testnet: bool = False,
        timeout: float = 60.0,
        retries: int = 5,
        backoff: float = 0.5,
        connect: Callable[[str, float], tuple[Transport, Callable[[], None]]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        host = urlparse(endpoint).hostname or ""
        if fork and (host in MAINNET_HOSTS or not is_loopback(endpoint)):
            raise WriteGuardError(
                f"refusing to treat {endpoint} as a fork: fork endpoints must be loopback "
                "and never a mainnet host"
            )
        if testnet and (fork or host in MAINNET_HOSTS):
            raise WriteGuardError(f"refusing {endpoint} as a testnet endpoint: never mainnet, never also a fork")
        self.testnet = testnet
        self.endpoint = endpoint
        self.fork = fork
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._connect = connect or _ws_connect
        self._sleep = sleep
        self._ids = itertools.count(1)
        self._conn: Transport | None = None
        self._close: Callable[[], None] | None = None
        self.requests = 0
        self.reconnects = 0
        self._fork_checked = False
        self._sent = False
        self.resent_writes = 0  # writes sent again after an earlier attempt had already reached the socket

    def __enter__(self) -> "RpcClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._close is not None:
            with contextlib.suppress(Exception):
                self._close()
        self._conn = None
        self._close = None
        self._fork_checked = False  # a new connection is checked again before its first write

    def _ensure(self) -> Transport:
        if self._conn is None:
            self._conn, self._close = self._connect(self.endpoint, self.timeout)
        return self._conn

    def _once(self, method: str, params: list[Any]) -> Any:
        conn = self._ensure()
        req_id = next(self._ids)
        conn.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))
        self._sent = True
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method}: no answer in {self.timeout}s")
            raw = conn.recv(timeout=remaining)
            try:
                msg = json.loads(raw)
            except ValueError as exc:
                raise BadReply(f"{method}: non-JSON reply {str(raw)[:80]!r}") from exc
            if not isinstance(msg, dict):
                raise BadReply(f"{method}: reply is not a JSON-RPC object: {str(raw)[:80]!r}")
            if "error" in msg and msg.get("id") is None:
                raise RpcError(method, msg["error"])  # the node could not even parse our request
            if msg.get("id") != req_id:
                continue  # a subscription notification or a stale answer
            if "error" in msg:
                raise RpcError(method, msg["error"])
            return msg.get("result")

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        if is_write_method(method):
            self._guard_write(method)
        return self._call(method, params)

    def _guard_write(self, method: str) -> None:
        if self.testnet:
            if method not in TESTNET_WRITES:
                raise WriteGuardError(f"{method} refused: a testnet client sends only {sorted(TESTNET_WRITES)}")
            self._check_testnet_genesis()
        elif not self.fork:
            raise WriteGuardError(f"{method} refused: {self.endpoint} is not a local fork (I3)")
        else:
            self._check_fork_genesis()

    def _genesis(self) -> str:
        genesis = self._call("chain_getBlockHash", [0])
        if not isinstance(genesis, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", genesis):
            raise WriteGuardError(f"{self.endpoint}: cannot read a genesis hash ({genesis!r}): refusing writes (I3)")
        return genesis.lower()

    def _check_testnet_genesis(self) -> None:
        if self._fork_checked:
            return
        genesis = self._genesis()
        chain = BY_GENESIS.get(genesis)
        if genesis == MAINNET_GENESIS or chain is None or not chain.writable:
            raise WriteGuardError(f"{self.endpoint} serves genesis {genesis}, not a testnet Forklift may write to")
        self._fork_checked = True

    def _check_fork_genesis(self) -> None:
        if self._fork_checked:
            return
        genesis = self._genesis()
        if genesis == MAINNET_GENESIS:
            raise WriteGuardError(f"{self.endpoint} serves Portaldot mainnet's genesis: refusing writes (I3)")
        if genesis == V3.genesis_hash and not self._is_chopsticks():
            raise WriteGuardError(f"{self.endpoint} serves the live {V3.label}, not a fork of it: refusing fork "
                                  "writes (a real V3 transaction goes through `forklift rehearse --send`)")
        self._fork_checked = True

    def _is_chopsticks(self) -> bool:
        methods = self._call("rpc_methods", [])
        names = methods.get("methods", []) if isinstance(methods, dict) else []
        return "dev_newBlock" in names and "dev_setStorage" in names

    def _call(self, method: str, params: list[Any] | None) -> Any:
        params = list(params or [])
        retryable = _retryable()
        last: BaseException | None = None
        reached_socket = False
        for attempt in range(self.retries + 1):
            if attempt:
                self._sleep(self.backoff * 2 ** (attempt - 1))
                self.reconnects += 1
            if attempt and (self.fork or self.testnet) and is_write_method(method):
                self._guard_write(method)  # a write re-sent on a new connection is guarded again (I3)
                if reached_socket:
                    self.resent_writes += 1
            try:
                self.requests += 1
                self._sent = False
                return self._once(method, params)
            except RpcError:
                raise
            except retryable as exc:
                last = exc
                reached_socket = reached_socket or self._sent
                self.close()
        raise RpcUnavailable(
            f"{method} on {self.endpoint} failed after {self.retries + 1} attempts: {last!r}"
        ) from last
