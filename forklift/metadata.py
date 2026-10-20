"""Storage names from runtime metadata: V13 (Portaldot mainnet, spec 1002) and V14 (V3 testnet, spec 3002).

Only the storage section is kept: it maps each 32-byte storage item prefix
``twox128(pallet prefix) ‖ twox128(item)`` to a readable ``Pallet.Item`` name.
Calls, events, constants and errors are decoded only to be skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from forklift.hashing import twox128

HASHERS = ["Blake2_128", "Blake2_256", "Blake2_128Concat", "Twox128", "Twox256", "Twox64Concat", "Identity"]


class MetadataError(ValueError):
    pass


@dataclass
class StorageItem:
    pallet: str
    name: str
    kind: str  # Plain | Map | DoubleMap | NMap
    hashers: list[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.pallet}.{self.name}"


@dataclass
class Pallet:
    name: str
    index: int
    storage_prefix: str | None
    items: list[StorageItem]


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise MetadataError(f"truncated metadata at byte {self.pos}")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def compact(self) -> int:
        b0 = self.u8()
        mode = b0 & 3
        if mode == 0:
            return b0 >> 2
        if mode == 1:
            return (b0 | self.u8() << 8) >> 2
        if mode == 2:
            return int.from_bytes(bytes([b0]) + self.take(3), "little") >> 2
        return int.from_bytes(self.take((b0 >> 2) + 4), "little")

    def bytes_(self) -> bytes:
        return self.take(self.compact())

    def str_(self) -> str:
        return self.bytes_().decode()

    def vec(self, item):
        return [item() for _ in range(self.compact())]

    def option(self, item):
        flag = self.u8()
        if flag == 0:
            return None
        if flag != 1:
            raise MetadataError(f"bad Option flag {flag} at byte {self.pos - 1}")
        return item()

    def hasher(self) -> str:
        i = self.u8()
        if i >= len(HASHERS):
            raise MetadataError(f"unknown hasher {i}")
        return HASHERS[i]


def _storage_entry(r: _Reader, pallet: str) -> StorageItem:
    name = r.str_()
    r.u8()  # modifier: Optional | Default
    kind = r.u8()
    if kind == 0:  # Plain(ty)
        r.str_()
        item = StorageItem(pallet, name, "Plain")
    elif kind == 1:  # Map { hasher, key, value, unused }
        h = r.hasher()
        r.str_(); r.str_(); r.u8()
        item = StorageItem(pallet, name, "Map", [h])
    elif kind == 2:  # DoubleMap { hasher, key1, key2, value, key2_hasher }
        h1 = r.hasher()
        r.str_(); r.str_(); r.str_()
        item = StorageItem(pallet, name, "DoubleMap", [h1, r.hasher()])
    elif kind == 3:  # NMap { keys, hashers, value }
        r.vec(r.str_)
        hs = r.vec(r.hasher)
        r.str_()
        item = StorageItem(pallet, name, "NMap", hs)
    else:
        raise MetadataError(f"unknown storage entry type {kind} for {pallet}.{name}")
    r.bytes_()  # default value
    r.vec(r.str_)  # docs
    return item


def decode_v13(raw: bytes) -> list[Pallet]:
    r = _Reader(raw)
    if r.take(4) != b"meta":
        raise MetadataError("missing 'meta' magic")
    version = r.u8()
    if version != 13:
        raise MetadataError(f"metadata V{version}; this decoder reads V13 (Portaldot spec 1002)")
    pallets = []
    for _ in range(r.compact()):
        name = r.str_()
        storage = r.option(lambda: (r.str_(), r.compact()))
        prefix, items = None, []
        if storage is not None:
            prefix, n = storage
            items = [_storage_entry(r, prefix) for _ in range(n)]
        r.option(lambda: r.vec(lambda: (r.str_(), r.vec(lambda: (r.str_(), r.str_())), r.vec(r.str_))))  # calls
        r.option(lambda: r.vec(lambda: (r.str_(), r.vec(r.str_), r.vec(r.str_))))  # events
        r.vec(lambda: (r.str_(), r.str_(), r.bytes_(), r.vec(r.str_)))  # constants
        r.vec(lambda: (r.str_(), r.vec(r.str_)))  # errors
        pallets.append(Pallet(name, r.u8(), prefix, items))
    return pallets


def decode_v14(raw: bytes) -> list[Pallet]:
    """Storage section of a V14 blob. V14 is self-describing (a type registry), so the SDK's
    scalecodec decodes it; only names, kinds and hashers are kept, as for V13."""
    from scalecodec.base import RuntimeConfiguration, ScaleBytes
    from scalecodec.type_registry import load_type_registry_preset

    rc = RuntimeConfiguration()
    rc.update_type_registry(load_type_registry_preset("core"))
    obj = rc.create_scale_object("MetadataVersioned", data=ScaleBytes(raw))
    obj.decode()
    pallets = []
    for p in obj.value[1]["V14"]["pallets"]:
        storage = p.get("storage")
        items = []
        for e in (storage or {}).get("entries", []):
            kind, shape = next(iter(e["type"].items()))
            hashers = shape.get("hashers", []) if isinstance(shape, dict) else []
            items.append(StorageItem(p["name"], e["name"], kind, list(hashers)))
        pallets.append(Pallet(p["name"], p["index"], storage["prefix"] if storage else None, items))
    return pallets


def decode_hex(metadata_hex: str) -> list[Pallet]:
    raw = bytes.fromhex(metadata_hex.removeprefix("0x"))
    if raw[:4] == b"meta" and len(raw) > 4 and raw[4] == 14:
        return decode_v14(raw)
    return decode_v13(raw)


# Every FRAME pallet of this era stores its version under twox128(pallet name).
PALLET_VERSION_KEY = ":__PALLET_VERSION__:"

# Storage that exists on spec 1002 but is not described in its V13 metadata
# (both pallets have storage=None there; keys found in a 2026-10-14 lift, names
# matched by twox128). pallet name -> (storage prefix, [(item, kind)]).
UNLISTED_ITEMS = {
    "AuthorityDiscovery": ("AuthorityDiscovery", [("Keys", "Plain"), ("NextKeys", "Plain")]),
    "Historical": ("Session", [("HistoricalSessions", "Map"), ("StoredRange", "Plain")]),
}


class StorageIndex:
    """Name lookup for raw storage keys."""

    def __init__(self, pallets: list[Pallet]):
        self.pallets: dict[bytes, str] = {}
        self.items: dict[bytes, StorageItem] = {}
        for p in pallets:
            # Pallet-version key: under the pallet *name*, which may differ from its storage prefix.
            np = twox128(p.name)
            self.pallets.setdefault(np, p.storage_prefix or p.name)
            self.items[np + twox128(PALLET_VERSION_KEY)] = StorageItem(p.name, PALLET_VERSION_KEY, "Plain")
            if p.storage_prefix is None and p.name in UNLISTED_ITEMS:
                prefix, unlisted = UNLISTED_ITEMS[p.name]
                for name, kind in unlisted:
                    self.items[twox128(prefix) + twox128(name)] = StorageItem(prefix, name, kind)
            if p.storage_prefix is None:
                continue
            pp = twox128(p.storage_prefix)
            self.pallets[pp] = p.storage_prefix
            for it in p.items:
                self.items[pp + twox128(it.name)] = it

    # Pallet lookups come first: a pallet prefix may itself start with 0x3a (":").
    def pallet_of(self, key: bytes) -> str:
        item = self.items.get(key[:32])
        if item is not None:
            return item.pallet
        if key[:16] in self.pallets:
            return self.pallets[key[:16]]
        if key.startswith(b":"):
            return ":well-known"
        return f"unknown:0x{key[:16].hex()}"

    def item_of(self, key: bytes) -> str:
        item = self.items.get(key[:32])
        if item is not None:
            return item.full_name
        pallet = self.pallets.get(key[:16])
        if pallet is not None:
            return f"{pallet}.unknown:0x{key[16:32].hex()}"
        if key.startswith(b":"):
            return key.decode(errors="replace")
        return f"unknown:0x{key[:16].hex()}"
