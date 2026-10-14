"""In-memory stand-ins for a JSON-RPC node, for offline tests."""

from __future__ import annotations

import json
from typing import Any, Callable


class FakeTransport:
    """Answers each request with ``handler(method, params)``.

    ``handler`` may raise an exception to simulate a dropped connection; it is
    raised from ``recv`` so the client sees it the way it would see a real
    socket error.
    """

    def __init__(self, handler: Callable[[str, list[Any]], Any]):
        self.handler = handler
        self.sent: list[dict[str, Any]] = []
        self._pending: list[str] = []
        self.closed = False

    def send(self, message: str) -> None:
        req = json.loads(message)
        self.sent.append(req)
        try:
            result = self.handler(req["method"], req["params"])
        except Exception as exc:  # delivered on recv
            self._pending.append(exc)  # type: ignore[arg-type]
            return
        if isinstance(result, dict) and "__error__" in result:
            self._pending.append(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": result["__error__"]}))
        else:
            self._pending.append(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}))

    def recv(self, timeout: float | None = None) -> str:
        item = self._pending.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def fake_connect(handler: Callable[[str, list[Any]], Any], log: list[FakeTransport] | None = None):
    def connect(endpoint: str, timeout: float):
        t = FakeTransport(handler)
        if log is not None:
            log.append(t)
        return t, t.close

    return connect


class FakeChain:
    """A pinned block with a sorted key/value store, served like a Substrate node."""

    def __init__(self, storage: dict[str, str], *, number: int = 100, block_hash: str = "0x" + "ab" * 32,
                 spec_version: int = 1002, metadata: str = "0x", clamp: int | None = None):
        self.storage = dict(sorted(storage.items()))
        self.number = number
        self.block_hash = block_hash
        self.spec_version = spec_version
        self.metadata = metadata
        self.clamp = clamp  # a node/gateway that returns at most this many keys per page
        self.calls: list[tuple[str, list[Any]]] = []

    def __call__(self, method: str, params: list[Any]) -> Any:
        self.calls.append((method, params))
        if method == "chain_getFinalizedHead":
            return self.block_hash
        if method == "chain_getBlockHash":
            return self.block_hash if params[0] == self.number else None
        if method == "chain_getHeader":
            return {"number": hex(self.number), "parentHash": "0x" + "00" * 32} \
                if params[0] == self.block_hash else None
        if method == "state_getRuntimeVersion":
            assert params[0] == self.block_hash, "read not pinned to the block hash"
            return {"specName": "portaldot", "specVersion": self.spec_version}
        if method == "state_getMetadata":
            assert params[0] == self.block_hash, "read not pinned to the block hash"
            return self.metadata
        if method == "state_getStorage":
            assert params[1] == self.block_hash, "read not pinned to the block hash"
            return self.storage.get(params[0])
        if method == "state_getKeysPaged":
            prefix, count, start = params[0], params[1], params[2]
            assert params[3] == self.block_hash, "read not pinned to the block hash"
            keys = [k for k in self.storage if k.startswith(prefix) and (start is None or k > start)]
            return keys[:min(count, self.clamp or count)]
        if method == "state_queryStorageAt":
            keys, at = params
            assert at == self.block_hash, "read not pinned to the block hash"
            return [{"block": at, "changes": [[k, self.storage.get(k)] for k in keys]}]
        raise AssertionError(f"unexpected RPC {method}")
