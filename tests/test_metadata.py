import json
from pathlib import Path

import pytest

from forklift.metadata import MetadataError, StorageIndex, decode_v13

FIX = Path(__file__).parent / "fixtures"
META = (FIX / "metadata-1002.scale").read_bytes()
SLICE = json.loads((FIX / "lift-slice-2725639.json").read_text())


@pytest.fixture(scope="module")
def pallets():
    return decode_v13(META)


def test_decodes_every_pallet_of_spec_1002(pallets):
    assert len(pallets) == 31
    names = {p.name: p for p in pallets}
    assert names["System"].index == 0
    assert names["Mmr"].storage_prefix == "MerkleMountainRange"
    assert {i.name for i in names["Staking"].items} >= {"Ledger", "Nominators", "Validators", "Bonded"}
    account = next(i for i in names["System"].items if i.name == "Account")
    assert account.kind == "Map" and account.hashers == ["Blake2_128Concat"]


def test_every_lifted_key_in_the_slice_has_a_name(pallets):
    idx = StorageIndex(pallets)
    for key in SLICE["storage"]:
        raw = bytes.fromhex(key[2:])
        assert "unknown" not in idx.item_of(raw), key
        assert "unknown" not in idx.pallet_of(raw), key


def test_pallet_version_and_unlisted_items_are_named(pallets):
    idx = StorageIndex(pallets)
    from forklift.hashing import twox128

    assert idx.item_of(twox128("Mmr") + twox128(":__PALLET_VERSION__:")) == "Mmr.:__PALLET_VERSION__:"
    assert idx.item_of(twox128("Session") + twox128("HistoricalSessions") + b"\x00" * 12) == \
        "Session.HistoricalSessions"
    assert idx.item_of(b":code") == ":code"
    assert idx.pallet_of(b"\x99" * 32).startswith("unknown:")


def test_rejects_other_metadata_versions():
    with pytest.raises(MetadataError):
        decode_v13(b"meta\x0e" + META[5:])


def test_rejects_truncated_metadata():
    with pytest.raises(MetadataError):
        decode_v13(META[:5000])


def _compact(n):
    assert n < 1 << 30
    if n < 1 << 6:
        return bytes([n << 2])
    if n < 1 << 14:
        return ((n << 2) | 1).to_bytes(2, "little")
    return ((n << 2) | 2).to_bytes(4, "little")


def _s(text):
    b = text.encode()
    return _compact(len(b)) + b


def _vec(items):
    return _compact(len(items)) + b"".join(items)


def test_hand_built_v13_covers_every_storage_entry_kind_and_compact_mode():
    from forklift.metadata import _Reader

    long_doc = _s("d" * 70)  # 70 > 63: two-byte compact length
    entries = [
        _s("Plain") + b"\x00" + b"\x00" + _s("u32") + _s("0x00") + _vec([long_doc]),
        _s("Map") + b"\x01" + b"\x01" + b"\x02" + _s("K") + _s("V") + b"\x00" + _s("") + _vec([]),
        _s("Double") + b"\x00" + b"\x02" + b"\x05" + _s("K1") + _s("K2") + _s("V") + b"\x06" + _s("") + _vec([]),
        _s("N") + b"\x00" + b"\x03" + _vec([_s("A"), _s("B"), _s("C")]) + _vec([b"\x02", b"\x05", b"\x06"])
        + _s("V") + _s("") + _vec([]),
    ]
    module = (_s("Demo") + b"\x01" + _s("DemoPrefix") + _vec(entries)
              + b"\x01" + _vec([_s("call") + _vec([_s("a") + _s("u8")]) + _vec([])])  # calls
              + b"\x00"  # no events
              + _vec([_s("C") + _s("u8") + _s("\x07") + _vec([])])  # constants
              + _vec([_s("E") + _vec([])])  # errors
              + b"\x07")  # index
    pallets = decode_v13(b"meta\x0d" + _vec([module]))
    (p,) = pallets
    assert (p.name, p.index, p.storage_prefix) == ("Demo", 7, "DemoPrefix")
    assert [(i.name, i.kind, i.hashers) for i in p.items] == [
        ("Plain", "Plain", []),
        ("Map", "Map", ["Blake2_128Concat"]),
        ("Double", "DoubleMap", ["Twox64Concat", "Identity"]),
        ("N", "NMap", ["Blake2_128Concat", "Twox64Concat", "Identity"]),
    ]
    assert _Reader(_compact(1_000_000)).compact() == 1_000_000  # four-byte mode
    assert _Reader(b"\x13" + (2**40).to_bytes(8, "little")).compact() == 2**40  # big-integer mode


def test_pallet_prefix_starting_with_colon_is_not_taken_for_a_well_known_key(pallets):
    idx = StorageIndex(pallets)
    idx.pallets[b":" + b"\x00" * 15] = "Colon"
    assert idx.pallet_of(b":" + b"\x00" * 15 + b"\x01" * 16) == "Colon"
    assert idx.pallet_of(b":code") == ":well-known"


def test_v14_metadata_of_v3_names_its_storage():
    import gzip

    from forklift.metadata import decode_hex

    raw = gzip.decompress((FIX / "metadata-3002.scale.gz").read_bytes())
    pallets = decode_hex("0x" + raw.hex())  # dispatches on the version byte (14)
    names = {p.name for p in pallets}
    assert {"System", "Balances", "Staking", "Revive", "Contracts", "Sudo", "Mmr"} <= names
    assert "MerkleMountainRange" not in names
    index = StorageIndex(pallets)
    from forklift.splice import account_key

    assert index.item_of(account_key(b"\x00" * 32)) == "System.Account"
    acct = next(i for p in pallets if p.name == "System" for i in p.items if i.name == "Account")
    assert acct.kind == "Map" and acct.hashers == ["Blake2_128Concat"]
