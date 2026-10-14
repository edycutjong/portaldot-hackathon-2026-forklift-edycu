"""Storage names from runtime metadata V13 (Portaldot mainnet, spec 1002).

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


def decode_hex(metadata_hex: str) -> list[Pallet]:
    return decode_v13(bytes.fromhex(metadata_hex.removeprefix("0x")))


class StorageIndex:
    """Name lookup for raw storage keys."""

    def __init__(self, pallets: list[Pallet]):
        self.pallets: dict[bytes, str] = {}
        self.items: dict[bytes, StorageItem] = {}
        for p in pallets:
            if p.storage_prefix is None:
                continue
            pp = twox128(p.storage_prefix)
            self.pallets[pp] = p.storage_prefix
            for it in p.items:
                self.items[pp + twox128(it.name)] = it

    def pallet_of(self, key: bytes) -> str:
        if key.startswith(b":"):
            return ":well-known"
        return self.pallets.get(key[:16], f"unknown:0x{key[:16].hex()}")

    def item_of(self, key: bytes) -> str:
        if key.startswith(b":"):
            return key.decode(errors="replace")
        item = self.items.get(key[:32])
        if item is not None:
            return item.full_name
        pallet = self.pallets.get(key[:16])
        if pallet is not None:
            return f"{pallet}.unknown:0x{key[16:32].hex()}"
        return f"unknown:0x{key[:16].hex()}"
