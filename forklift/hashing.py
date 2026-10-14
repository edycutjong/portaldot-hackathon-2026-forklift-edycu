"""Substrate storage hashing and the manifest root."""

from __future__ import annotations

import hashlib
from typing import Iterable

import xxhash


def twox128(name: str | bytes) -> bytes:
    """Substrate's twox128: two xxhash64 rounds (seeds 0 and 1), little-endian, concatenated."""
    data = name.encode() if isinstance(name, str) else name
    return b"".join(xxhash.xxh64_intdigest(data, seed=s).to_bytes(8, "little") for s in (0, 1))


def blake2_128(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=16).digest()


def blake2_256(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


def manifest_root(pairs: Iterable[tuple[bytes, bytes]]) -> bytes:
    """blake2_256 over the (key, value) pairs sorted by key.

    Each pair is framed as ``u32le(len(key)) ‖ key ‖ u32le(len(value)) ‖ value``
    so that no two different stores serialize to the same byte stream. Input
    order does not matter; duplicate keys are an error.
    """
    h = hashlib.blake2b(digest_size=32)
    last = None
    for key, value in sorted(pairs):
        if key == last:
            raise ValueError(f"duplicate key 0x{key.hex()}")
        last = key
        h.update(len(key).to_bytes(4, "little"))
        h.update(key)
        h.update(len(value).to_bytes(4, "little"))
        h.update(value)
    return h.digest()
