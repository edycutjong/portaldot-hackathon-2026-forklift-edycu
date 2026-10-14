"""Minimal JSON-RPC client over a WebSocket, with the endpoint write-guard.

Bulk storage paging goes through this client rather than a full SDK: a lift
is a few thousand sequential reads, and the SDK adds nothing to them.

Invariant I3 (no mainnet writes) lives here. A client is created either as a
read-only client (the default, used for mainnet) or as a fork client. Only a
fork client may call ``author_*`` methods, and a fork client can only be
created for a loopback endpoint that is not a known mainnet host.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import time
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

DEFAULT_MAINNET = "wss://mainnet.portaldot.io"

# Hosts that are Portaldot mainnet. A fork client can never point at them.
MAINNET_HOSTS = frozenset({"mainnet.portaldot.io"})

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# RPC methods that change chain state or node keys. Refused on non-fork clients.
WRITE_PREFIXES = ("author_",)


class RpcError(Exception):
    """The node answered with a JSON-RPC error object."""

    def __init__(self, method: str, error: dict[str, Any]):
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


def is_write_method(method: str) -> bool:
    return method.startswith(WRITE_PREFIXES)


# Errors that mean "the connection is gone or slow", worth a reconnect and retry.
def _retryable() -> tuple[type[BaseException], ...]:
    errors: list[type[BaseException]] = [OSError, TimeoutError, EOFError]
    with contextlib.suppress(ImportError):
        from websockets.exceptions import ConnectionClosed, InvalidHandshake

        errors += [ConnectionClosed, InvalidHandshake]
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

    def _ensure(self) -> Transport:
        if self._conn is None:
            self._conn, self._close = self._connect(self.endpoint, self.timeout)
        return self._conn

    def _once(self, method: str, params: list[Any]) -> Any:
        conn = self._ensure()
        req_id = next(self._ids)
        conn.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method}: no answer in {self.timeout}s")
            msg = json.loads(conn.recv(timeout=remaining))
            if msg.get("id") != req_id:
                continue  # a subscription notification or a stale answer
            if "error" in msg:
                raise RpcError(method, msg["error"])
            return msg.get("result")

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        if is_write_method(method) and not self.fork:
            raise WriteGuardError(f"{method} refused: {self.endpoint} is not a local fork (I3)")
        params = list(params or [])
        retryable = _retryable()
        last: BaseException | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                self._sleep(self.backoff * 2 ** (attempt - 1))
                self.reconnects += 1
            try:
                self.requests += 1
                return self._once(method, params)
            except RpcError:
                raise
            except retryable as exc:
                last = exc
                self.close()
        raise RpcUnavailable(
            f"{method} on {self.endpoint} failed after {self.retries + 1} attempts: {last!r}"
        ) from last
