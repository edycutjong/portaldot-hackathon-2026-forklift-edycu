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
