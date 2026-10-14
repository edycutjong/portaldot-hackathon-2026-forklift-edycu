"""``fork.manifest.json``: the data plate of a lift.

The ``source`` and ``load`` blocks are filled from the lift. ``node``,
``fork`` and ``overrides`` describe the spliced fork and stay empty until a
fork is built from the lift.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from forklift import __version__
from forklift.hashing import blake2_256, manifest_root
from forklift.lift import CODE_KEY, LiftResult
from forklift.metadata import StorageIndex


def _b(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str.removeprefix("0x"))


def per_prefix_counts(keys: Iterable[str], index: StorageIndex) -> tuple[dict[str, int], dict[str, int]]:
    """Key counts per pallet storage prefix and per storage item, sorted by name."""
    pallets: Counter[str] = Counter()
    items: Counter[str] = Counter()
    for k in keys:
        raw = _b(k)
        pallets[index.pallet_of(raw)] += 1
        items[index.item_of(raw)] += 1
    return dict(sorted(pallets.items())), dict(sorted(items.items()))


def root_of(values: dict[str, str]) -> str:
    return "0x" + manifest_root((_b(k), _b(v)) for k, v in values.items()).hex()


def profile_label(spec_version: int) -> str:
    return f"mainnet-read (spec {spec_version})"


def build_manifest(result: LiftResult, index: StorageIndex, *, endpoint: str, mmr: dict | None) -> dict:
    code = result.values.get(CODE_KEY)
    per_pallet, per_item = per_prefix_counts(result.keys, index)
    return {
        "forklift_version": __version__,
        "profile": profile_label(result.spec_version),
        "source": {
            "endpoint": endpoint,
            "block_number": result.pin.number,
            "block_hash": result.pin.hash,
            "spec_name": result.spec_name,
            "spec_version": result.spec_version,
            "code_hash": "0x" + blake2_256(_b(code)).hex() if code is not None else None,
            "code_bytes": len(_b(code)) if code is not None else None,
        },
        "load": {
            "keys_lifted": len(result.keys),
            "key_bytes": sum(len(_b(k)) for k in result.keys),
            "value_bytes": sum(len(_b(v)) for v in result.values.values()),
            "key_pages": result.pages,
            "seconds": result.timings,
            "skipped": mmr,
            "per_pallet": per_pallet,
            "per_item": per_item,
        },
        "node": None,
        "fork": None,
        "overrides": [],
        "manifest_root": root_of(result.values),
    }
