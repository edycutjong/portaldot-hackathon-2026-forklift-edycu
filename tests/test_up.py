import json

from forklift.hashing import manifest_root
from forklift.up import cached_lift

H = "0x" + "ab" * 32


def _write(d, storage, root):
    d.mkdir()
    (d / "state.json").write_text(json.dumps({"block_hash": H, "storage": storage}))
    (d / "manifest.json").write_text(json.dumps({"source": {"block_hash": H}, "load": {"keys_lifted": len(storage)},
                                                 "manifest_root": root}))


def test_cached_lift_is_reused_only_when_its_root_matches_its_state(tmp_path):
    storage = {"0x01": "0xaa", "0x02": "0xbb"}
    root = "0x" + manifest_root((bytes.fromhex(k[2:]), bytes.fromhex(v[2:])) for k, v in storage.items()).hex()
    _write(tmp_path / "good", storage, root)
    m, s = cached_lift(tmp_path, H)
    assert s == storage and m["manifest_root"] == root
    (tmp_path / "good" / "state.json").write_text(json.dumps({"block_hash": H, "storage": {"0x01": "0xaa",
                                                                                              "0x02": "0xbc"}}))
    assert cached_lift(tmp_path, H) is None  # same key count, different value: not the lift the manifest describes
