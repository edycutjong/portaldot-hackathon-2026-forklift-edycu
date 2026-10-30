"""``forklift migrate-report``: type trees, the SCALE reader, classification and the report totals (offline)."""


from forklift import migrate as m
from forklift.migrate import CARRIES, NO_HOME, REENCODE, Item, V3Index


def reg():
    from scalecodec.base import RuntimeConfigurationObject
    from scalecodec.type_registry import load_type_registry_preset

    r = RuntimeConfigurationObject()
    r.update_type_registry(load_type_registry_preset("core"))
    r.update_type_registry(load_type_registry_preset("legacy"))
    return r


def ty(path, d):
    return {"path": path, "params": [], "def": d, "docs": []}


TYPES = {
    0: ty(["sp_core", "crypto", "AccountId32"], {"composite": {"fields": [{"name": None, "type": 1}]}}),
    1: ty([], {"array": {"len": 32, "type": 2}}),
    2: ty([], {"primitive": "u8"}),
    3: ty([], {"primitive": "u128"}),
    4: ty([], {"sequence": {"type": 2}}),
    5: ty(["Option"], {"variant": {"variants": [{"name": "None", "fields": [], "index": 0},
                                                 {"name": "Some", "fields": [{"name": None, "type": 3}], "index": 1}]}}),
    6: ty(["pallet", "Data"], {"composite": {"fields": [{"name": "free", "type": 3}, {"name": "owner", "type": 0}]}}),
    7: ty(["wrap", "Newtype"], {"composite": {"fields": [{"name": None, "type": 3}]}}),
    8: ty([], {"tuple": [3, 2]}),
}


def test_v14_trees_reduce_account_ids_options_newtypes_and_byte_vectors():
    assert m.tree14(TYPES, 0) == "bytes32"
    assert m.tree14(TYPES, 4) == "bytes"
    assert m.tree14(TYPES, 5) == {"option": "u128"}
    assert m.tree14(TYPES, 7) == "u128"  # a one-field newtype is transparent
    assert m.tree14(TYPES, 6) == {"struct": [["free", "u128"], ["owner", "bytes32"]]}
    assert m.tree14(TYPES, 8) == {"tuple": ["u128", "u8"]}


def test_v14_recursion_is_cut_not_followed_forever():
    types = {0: ty(["List"], {"composite": {"fields": [{"name": "next", "type": 1}]}}),
             1: ty(["Option"], {"variant": {"variants": [{"name": "None", "fields": [], "index": 0},
                                                          {"name": "Some", "fields": [{"name": None, "type": 0}],
                                                           "index": 1}]}})}
    assert m.tree14(types, 0) == {"struct": [["next", {"option": {"recursive": "List"}}]]}


def test_v13_trees_match_the_v14_shape_of_the_same_type():
    r = reg()
    t = m.tree13(r, "AccountInfo<Index, AccountData<Balance>>")
    assert t["struct"][0] == ["nonce", "u32"]
    assert ["data", {"struct": [["free", "u128"], ["reserved", "u128"], ["misc_frozen", "u128"],
                                ["fee_frozen", "u128"]]}] in t["struct"]
    assert m.tree13(r, "Vec<u8>") == "bytes"
    assert m.tree13(r, "Option<BlockNumber>") == {"option": "u32"}
    assert m.tree13(r, "T::AccountId") == "bytes32"


def test_v13_bounded_vec_and_tuple_strings_split_at_top_level_commas():
    r = reg()
    assert m.split_top("A<B, C>, D,") == ["A<B, C>", "D"]
    assert m.tree13(r, "BoundedVec<u32, T::MaxLocks>") == {"vec": "u32"}
    t = m.tree13(r, "(BoundedVec<u32, T::Max,>, BalanceOf<T>,)")
    assert t == {"tuple": [{"vec": "u32"}, "u128"]}


def test_v13_btreemap_is_a_vec_of_pairs():
    assert m.tree13(reg(), "BTreeMap<AccountId, u32>") == {"vec": {"tuple": ["bytes32", "u32"]}}


def test_unresolvable_types_are_opaque_not_guessed():
    assert m.opaque_in(m.tree13(reg(), "TotallyUnknownThing<X>")) is not None


def test_the_scale_reader_wants_every_byte_and_valid_tags():
    t = {"struct": [["a", "u32"], ["b", {"option": "u8"}], ["c", "bytes"]]}
    assert m.parses(t, bytes([1, 0, 0, 0, 0, 0])) == (True, None)
    assert m.parses(t, bytes([1, 0, 0, 0, 1, 7, 8]) + bytes([1] * 8))[0] is False
    assert m.parses(t, bytes([1, 0, 0, 0, 0, 0, 9]))[1] == "1 trailing bytes"
    assert m.parses(t, bytes([1, 0, 0, 0, 2, 0]))[1] == "bad Option tag"
    assert m.parses({"enum": [[0, "A", "null"], [1, "B", "u8"]]}, bytes([2]))[1] == "enum tag 2 not in 0..1"
    assert m.parses({"opaque": "x"}, b"")[0] is None


def test_compact_lengths_in_the_reader():
    assert m.parses("bytes", bytes([8, 1, 2]))[0] is True  # compact(2) = 0x08
    assert m.parses({"vec": "u8"}, bytes([8, 1]))[0] is False


def item(name, value, prefix="P", kind="Plain", hashers=(), keys=(), described=True):
    return Item(prefix, name, kind, list(hashers), list(keys), value, described)


def v3(*items):
    by = {}
    for i in items:
        by.setdefault(i.name, []).append(i)
    return V3Index({i.full: i for i in items}, {i.prefix for i in items}, by)


def test_equal_types_carry_over():
    r = m.classify(item("A", "u32"), v3(item("A", "u32")))
    assert r["class"] == CARRIES and r["v3"] == "P.A"


def test_a_renamed_field_with_the_same_layout_needs_re_encoding_but_is_layout_compatible():
    old = item("Acc", {"struct": [["free", "u128"], ["misc_frozen", "u128"]]})
    new = item("Acc", {"struct": [["free", "u128"], ["frozen", "u128"]]})
    r = m.classify(old, v3(new))
    assert r["class"] == REENCODE and r["layout_compatible"] is True
    assert "misc_frozen" in r["reason"] and "frozen" in r["reason"]


def test_a_changed_width_is_not_layout_compatible():
    r = m.classify(item("A", {"struct": [["x", "u64"]]}), v3(item("A", {"struct": [["x", "u128"]]})))
    assert r["class"] == REENCODE and r["layout_compatible"] is False and "value.x: u64 → u128" in r["reason"]


def test_hasher_change_needs_re_encoding():
    old = item("M", "u8", kind="Map", hashers=["Twox64Concat"], keys=["bytes32"])
    new = item("M", "u8", kind="Map", hashers=["Blake2_128Concat"], keys=["bytes32"])
    r = m.classify(old, v3(new))
    assert r["class"] == REENCODE and "Twox64Concat → Blake2_128Concat" in r["reason"]


def test_missing_items_have_no_home_and_a_unique_name_elsewhere_is_a_move():
    assert m.classify(item("Gone", "u8", prefix="Old"), v3(item("X", "u8", prefix="New")))["class"] == NO_HOME
    assert "no item Gone" in m.classify(item("Gone", "u8", prefix="New"), v3(item("X", "u8", prefix="New")))["reason"]
    moved = m.classify(item("Count", "u32", prefix="Treasury"), v3(item("Count", "u32", prefix="Bounties")))
    assert moved["class"] == REENCODE and moved["v3"] == "Bounties.Count" and "prefix changes" in moved["reason"]
    # two candidates elsewhere: no guess
    two = v3(item("Count", "u32", prefix="A"), item("Count", "u32", prefix="B"))
    assert m.classify(item("Count", "u32", prefix="Old"), two)["class"] == NO_HOME


def test_an_unresolvable_1002_type_is_never_claimed_as_carried_over():
    r = m.classify(item("A", {"opaque": "v13:Mystery"}), v3(item("A", {"opaque": "v13:Mystery"})))
    assert r["class"] == REENCODE and "Mystery" in r["reason"]


def test_report_totals_and_the_self_check_demotion():
    items = [item("Ok", "u32"), item("Wrong", "u32"), item("Gone", "u8", prefix="Old")]
    idx = v3(item("Ok", "u32"), item("Wrong", "u32"))
    rep = m.build_report(
        mainnet={}, v3={}, md13_items=items, v3_index=idx, counts={"P.Ok": 5, "P.Wrong": 3, "Old.Gone": 2, ":code": 1,
                                                                   "Foo.:__PALLET_VERSION__:": 1},
        samples={"P.Ok": [bytes([1, 0, 0, 0])], "P.Wrong": [bytes([1])]}, v3_raw_names=set())
    by = {r["item"]: r for r in rep["rows"]}
    assert by["P.Ok"]["class"] == CARRIES and by["P.Ok"]["sample"]["parse_v3"]["parsed"] == 1
    assert by["P.Wrong"]["class"] == REENCODE and by["P.Wrong"]["type_unreliable"] is True  # 1 byte is not a u32
    assert by[":code"]["class"] == NO_HOME and by["Old.Gone"]["class"] == NO_HOME
    assert by["Foo.:__PALLET_VERSION__:"]["class"] == NO_HOME  # V3 has no storage under Foo
    assert rep["keys_lifted_total"] == 12 and sum(t["items"] for t in rep["totals"].values()) == rep["items_total"] == 5
    assert m.render_json(rep) == m.render_json(rep)
    assert "## Carries over (1 items)" in m.render_markdown({**rep, "mainnet": {"spec": "s", "block_number": 1, "block_hash": "0x", "code_hash": "0x", "items_described": 3},
                                                              "v3": {"spec": "s", "block_number": 1, "block_hash": "0x", "code_hash": "0x", "items_described": 2}})


def test_first_difference_names_only_what_changed():
    a = {"struct": [["x", "u8"]]}
    assert m.first_difference(a, {"struct": [["x", "u8"], ["y", "u8"]]}) == "value fields: x → x, y (added y)"
    assert m.first_difference({"struct": [["x", "u8"], ["y", "u8"]]}, a) == "value fields: x, y → x (removed y)"
    assert m.first_difference({"struct": [["x", "u8"], ["y", "u8"]]}, {"struct": [["y", "u8"], ["x", "u8"]]}) \
        == "value fields: x, y → y, x (reordered)"
