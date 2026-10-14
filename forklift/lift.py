"""Lift: read every storage key of a pinned mainnet block, except the MMR.

All reads are made at one block hash. Keys are paged in lexicographic order
from ``0x``; when a page reaches the MerkleMountainRange prefix the cursor
jumps past the whole range, so none of its ~5.4 M node keys is paged or
fetched. Values are then fetched with ``state_queryStorageAt`` at the same
hash.

The key cursor is checkpointed after every page. A lift that fails is left
as a checkpoint, never as a complete lift: ``state.json`` and
``manifest.json`` are written only after every value has been read.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from forklift.rpc import RpcClient, RpcError, RpcUnavailable

MMR_PREFIX = bytes.fromhex("434e2055f3154c54233a8dabeeb2839e")  # twox128("MerkleMountainRange")
MMR_JUMP = "0x" + (MMR_PREFIX + b"\xff" * 32).hex()
MMR_PREFIX_HEX = "0x" + MMR_PREFIX.hex()
CODE_KEY = "0x" + b":code".hex()

PAGE_SIZE = 1000  # the node's maximum for state_getKeysPaged
VALUE_BATCH = 500

EXIT_LIFT_FAILED = 4


def default_cache() -> Path:
    return Path(os.environ.get("FORKLIFT_CACHE", Path.home() / ".cache" / "forklift"))


class PagingError(ValueError):
    """A key page broke the strictly-increasing order the node must return."""


class LiftError(Exception):
    """The lift could not finish. ``cursor`` is the last key paged successfully."""

    def __init__(self, message: str, *, cursor: str | None, phase: str, block_hash: str | None):
        super().__init__(message)
        self.cursor = cursor
        self.phase = phase
        self.block_hash = block_hash


@dataclass
class Pin:
    number: int
    hash: str


@dataclass
class KeyPages:
    keys: list[str]
    pages: int
    mmr_seen: int  # MMR keys returned in the page that reached the range (never fetched)
    jumps: int


@dataclass
class LiftResult:
    pin: Pin
    spec_name: str
    spec_version: int
    keys: list[str]
    values: dict[str, str]
    pages: int
    mmr_seen: int
    timings: dict[str, float]
    out_dir: Path | None = None
    resumed_from: str | None = None
    extra: dict = field(default_factory=dict)


def resolve_pin(client: RpcClient, at: str | int | None) -> Pin:
    """``None``/``"head"`` → the finalized head; a number → that block; ``0x…`` → that hash."""
    if at is None or at == "head":
        block_hash = client.call("chain_getFinalizedHead")
    elif isinstance(at, str) and at.startswith("0x"):
        block_hash = at
    else:
        number = int(at)
        block_hash = client.call("chain_getBlockHash", [number])
        if block_hash is None:
            raise ValueError(f"block {number} does not exist on {client.endpoint}")
    header = client.call("chain_getHeader", [block_hash])
    if header is None:
        raise ValueError(f"no header for {block_hash} on {client.endpoint}")
    return Pin(int(header["number"], 16), block_hash)


def in_mmr(key: str) -> bool:
    return key.startswith(MMR_PREFIX_HEX)


def page_keys(
    client: RpcClient,
    block_hash: str,
    *,
    page_size: int | None = None,
    start: str | None = None,
    on_page: Callable[[list[str], str], None] | None = None,
) -> KeyPages:
    """Page all keys at ``block_hash`` from ``start`` (exclusive), skipping the MMR range.

    ``on_page(new_keys, cursor)`` runs after every page with the keys kept from
    that page and the cursor the next page starts from. Paging ends only on an
    empty page: a node or gateway may return short pages before the end.
    """
    page_size = page_size or PAGE_SIZE
    keys: list[str] = []
    cursor = start
    pages = mmr_seen = jumps = 0
    while True:
        page = client.call("state_getKeysPaged", ["0x", page_size, cursor, block_hash])
        pages += 1
        if not isinstance(page, list) or not all(isinstance(k, str) and k.startswith("0x") for k in page):
            raise PagingError(f"malformed key page after cursor {cursor}: {str(page)[:80]}")
        if not page:
            break
        if any(b <= a for a, b in zip(page, page[1:])) or (cursor is not None and page[0] <= cursor):
            raise PagingError(f"page after cursor {cursor} is not strictly increasing")
        kept = [k for k in page if not in_mmr(k)]
        mmr_seen += len(page) - len(kept)
        keys.extend(kept)
        cursor = page[-1]
        if in_mmr(cursor) and cursor < MMR_JUMP:
            # The range is ~5.4 M keys long: jump past it instead of paging through.
            cursor = MMR_JUMP
            jumps += 1
        if on_page is not None:
            on_page(kept, cursor)
    return KeyPages(keys, pages, mmr_seen, jumps)


def fetch_values(client: RpcClient, keys: list[str], block_hash: str, *, batch: int = VALUE_BATCH) -> dict[str, str]:
    """Read every key's value at ``block_hash``. A key with no value at the pin is an error.

    If the node rejects a batch (e.g. a response-size limit), the batch is
    halved and retried, down to one key.
    """
    values: dict[str, str] = {}
    batch = max(1, batch)
    i = 0
    while i < len(keys):
        chunk = keys[i:i + batch]
        try:
            sets = client.call("state_queryStorageAt", [chunk, block_hash])
        except RpcError:
            if batch == 1:
                raise
            batch = max(1, batch // 2)
            continue
        i += len(chunk)
        if not isinstance(sets, list) or not all(isinstance(s, dict) and isinstance(s.get("changes"), list)
                                                 for s in sets):
            raise ValueError(f"malformed state_queryStorageAt reply: {str(sets)[:80]}")
        got = {k: v for s in sets for k, v in s["changes"]}
        missing = [k for k in chunk if got.get(k) is None]
        if missing:
            raise ValueError(f"{len(missing)} paged keys have no value at {block_hash}, first {missing[0]}")
        for k in chunk:
            values[k] = got[k]
    return values


class Checkpoint:
    """Per-page progress of the key phase, in the lift directory."""

    def __init__(self, path: Path, block_hash: str):
        self.path = path
        self.keys_path = path.with_name("keys.partial")
        self.block_hash = block_hash

    def load(self) -> tuple[str | None, list[str]]:
        """The saved cursor and keys, or ``(None, [])`` if absent, for another block, or damaged."""
        try:
            data = json.loads(self.path.read_text())
            if data.get("block_hash") != self.block_hash:
                return None, []
            keys = self.keys_path.read_text().split()
            if len(keys) != data["keys"] or (keys[-1] if keys else None) != data["last_key"]:
                return None, []
            return data["cursor"], keys
        except (OSError, ValueError, KeyError, TypeError):
            return None, []

    def reset(self) -> None:
        self.keys_path.write_text("")
        self.path.unlink(missing_ok=True)

    def save(self, new_keys: list[str], cursor: str, total: int, last_key: str | None) -> None:
        with self.keys_path.open("a") as f:
            for k in new_keys:
                f.write(k + "\n")
            f.flush()
            os.fsync(f.fileno())
        _atomic_write(self.path, json.dumps({"block_hash": self.block_hash, "cursor": cursor, "keys": total,
                                             "last_key": last_key}))

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
        self.keys_path.unlink(missing_ok=True)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def lift(
    client: RpcClient,
    at: str | int | None = None,
    *,
    out_root: Path | None = None,
    page_size: int | None = None,
    value_batch: int = VALUE_BATCH,
    resume: bool = True,
    keep_checkpoint: bool = False,
) -> LiftResult:
    """Lift the state of one pinned block. With ``out_root``, checkpoint and write the lift there.

    ``keep_checkpoint=True`` leaves the checkpoint for the caller to clear once
    it has written the manifest (the lift is complete only with a manifest).
    """
    t0 = time.monotonic()
    cursor: str | None = None
    try:
        pin = resolve_pin(client, at)
        version = client.call("state_getRuntimeVersion", [pin.hash])
        if not isinstance(version, dict) or "specVersion" not in version:
            raise ValueError(f"no runtime version at {pin.hash}")
    except (RpcError, RpcUnavailable, ValueError, KeyError, TypeError) as exc:
        raise LiftError(f"could not pin a block: {exc}", cursor=None, phase="pin", block_hash=None) from exc

    out_dir = None
    ckpt = None
    prior: list[str] = []
    if out_root is not None:
        out_dir = Path(out_root) / str(pin.number)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            # A lift is complete only with its manifest: drop any earlier one before rewriting.
            (out_dir / "manifest.json").unlink(missing_ok=True)
            ckpt = Checkpoint(out_dir / "checkpoint.json", pin.hash)
            if resume:
                cursor, prior = ckpt.load()
            if cursor is None:
                prior = []
                ckpt.reset()
        except OSError as exc:
            raise LiftError(f"cannot write the lift directory: {exc}", cursor=None, phase="write",
                            block_hash=pin.hash) from exc
    resumed_from = cursor
    total = len(prior)
    last_key = prior[-1] if prior else None

    def on_page(new_keys: list[str], next_cursor: str) -> None:
        nonlocal cursor, total, last_key
        total += len(new_keys)
        cursor = next_cursor
        last_key = new_keys[-1] if new_keys else last_key
        if ckpt is not None:
            ckpt.save(new_keys, next_cursor, total, last_key)

    try:
        paged = page_keys(client, pin.hash, page_size=page_size, start=cursor, on_page=on_page)
    except (RpcError, RpcUnavailable, PagingError, OSError) as exc:
        raise LiftError(f"key paging failed: {exc}", cursor=cursor, phase="keys", block_hash=pin.hash) from exc
    keys = prior + paged.keys
    t_keys = time.monotonic()

    if len(set(keys)) != len(keys) or keys != sorted(keys):
        raise LiftError("paged keys are not a strictly increasing sequence", cursor=cursor, phase="keys",
                        block_hash=pin.hash)
    try:
        values = fetch_values(client, keys, pin.hash, batch=value_batch)
    except (RpcError, RpcUnavailable, ValueError, KeyError, TypeError) as exc:
        raise LiftError(f"value fetch failed: {exc}", cursor=cursor, phase="values", block_hash=pin.hash) from exc
    t_end = time.monotonic()

    result = LiftResult(
        pin=pin,
        spec_name=version.get("specName", ""),
        spec_version=int(version["specVersion"]),
        keys=keys,
        values=values,
        pages=paged.pages,
        mmr_seen=paged.mmr_seen,
        timings={
            "keys_s": round(t_keys - t0, 2),
            "values_s": round(t_end - t_keys, 2),
            "total_s": round(t_end - t0, 2),
        },
        out_dir=out_dir,
        resumed_from=resumed_from,
    )
    if out_dir is not None:
        try:
            _atomic_write(out_dir / "state.json", json.dumps({"block_hash": pin.hash, "block_number": pin.number,
                                                              "storage": values}))
        except OSError as exc:
            raise LiftError(f"cannot write state.json: {exc}", cursor=cursor, phase="write",
                            block_hash=pin.hash) from exc
        if not keep_checkpoint:
            ckpt.clear()
    return result


def _u64(hex_value: str | None) -> int | None:
    if hex_value is None:
        return None
    return int.from_bytes(bytes.fromhex(hex_value.removeprefix("0x"))[:8], "little")


def mmr_nodes_for_leaves(leaves: int) -> int:
    """Nodes in a Merkle Mountain Range of ``leaves`` leaves: 2n − popcount(n)."""
    return 2 * leaves - bin(leaves).count("1")


def mmr_summary(client: RpcClient, block_hash: str) -> dict:
    """What the skipped range holds, from its two plain values (two reads, no paging).

    The node count is derived; the one-time completeness listing checks it.
    """
    from forklift.hashing import twox128

    pallet = twox128("MerkleMountainRange")
    leaves_key = "0x" + (pallet + twox128("NumberOfLeaves")).hex()
    root_key = "0x" + (pallet + twox128("RootHash")).hex()
    leaves = _u64(client.call("state_getStorage", [leaves_key, block_hash]))
    root = client.call("state_getStorage", [root_key, block_hash])
    nodes = mmr_nodes_for_leaves(leaves) if leaves is not None else 0
    plain = (leaves is not None) + (root is not None)
    return {
        "range_prefix": MMR_PREFIX_HEX,
        "pallet": "MerkleMountainRange",
        "leaves": leaves,
        "nodes_derived": nodes,
        "keys_in_range_derived": nodes + plain,
        "derivation": "Nodes = 2*NumberOfLeaves - popcount(NumberOfLeaves); + RootHash, NumberOfLeaves",
    }
