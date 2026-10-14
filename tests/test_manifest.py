import json
from pathlib import Path

from forklift.hashing import blake2_256
from forklift.lift import CODE_KEY, LiftResult, Pin
from forklift.manifest import build_manifest, per_prefix_counts, root_of
from forklift.metadata import StorageIndex, decode_v13

FIX = Path(__file__).parent / "fixtures"
INDEX = StorageIndex(decode_v13((FIX / "metadata-1002.scale").read_bytes()))
SLICE = json.loads((FIX / "lift-slice-2725639.json").read_text())


def result(values):
    keys = sorted(values)
    return LiftResult(pin=Pin(SLICE["block_number"], SLICE["block_hash"]), spec_name="portaldot",
                      spec_version=1002, keys=keys, values=values, pages=1, mmr_seen=0,
                      timings={"keys_s": 0.1, "values_s": 0.2, "total_s": 0.3})


def test_per_prefix_counts_add_up_and_group_by_item():
    keys = list(SLICE["storage"])
    pallets, items = per_prefix_counts(keys, INDEX)
    assert sum(pallets.values()) == sum(items.values()) == len(keys)
    assert items["System.Account"] == 2
    assert pallets["System"] == sum(v for k, v in items.items() if k.startswith("System."))


def test_manifest_root_ignores_dict_order():
    s = SLICE["storage"]
    assert root_of(s) == root_of(dict(reversed(list(s.items()))))


def test_build_manifest_fills_source_and_load_only():
    values = dict(SLICE["storage"])
    values[CODE_KEY] = "0x0061736d01000000"
    mmr = {"range_prefix": "0x434e2055f3154c54233a8dabeeb2839e", "keys_in_range_derived": 9}
    m = build_manifest(result(values), INDEX, endpoint="wss://mainnet.portaldot.io", mmr=mmr)
    assert m["profile"] == "mainnet-read (spec 1002)"
    assert m["source"]["code_hash"] == "0x" + blake2_256(bytes.fromhex("0061736d01000000")).hex()
    assert m["source"]["block_number"] == SLICE["block_number"]
    assert m["load"]["keys_lifted"] == len(values)
    assert sum(m["load"]["per_pallet"].values()) == len(values)
    assert m["load"]["skipped"] == mmr
    # Filled only once a fork is spliced from the lift.
    assert m["node"] is None and m["fork"] is None and m["overrides"] == []
    assert m["manifest_root"] == root_of(values)
