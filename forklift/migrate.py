"""``forklift migrate-report``: which lifted mainnet (spec 1002) state has a home on the V3 testnet.

For every storage item of the mainnet runtime (metadata V13) the report looks the item up in the V3
runtime (metadata V14) and files it under one of three headings:

* **carries over**: V3 has the same storage prefix and item, with the same key hashers, the same key
  types and a value type of the same shape *and names*; the lifted key and value bytes are valid there as they are.
* **needs re-encoding**: V3 has the item but a key or value type differs (or the item moved to another
  prefix). The reason names the first difference; ``layout_compatible`` says whether the bytes would
  still parse (same shape, other field names or tags, so the meaning has to be mapped by hand).
* **no home on V3**: V3 has no such item, or the thing is the chain's own (``:code``).

Types are compared as trees, not as strings: a V14 type id is resolved through the portable registry and
a V13 type string through the SDK's type registry, and both are reduced to the same small vocabulary
(integers, ``AccountId32``, ``H256``, ``bytes``, ``vec``, ``option``, ``tuple``, ``array``, ``compact``,
``struct``, ``enum``). A newtype with one unnamed field is transparent on both sides.

A sample of real lifted values per item (the first ``SAMPLE`` keys at the pinned block) is parsed
by a small SCALE reader under both trees, so the report states how many real values the V3 type
accepts byte-for-byte (every byte consumed). Counts of lifted keys come from the lift's own manifest.

Everything is read at pinned block hashes, and nothing in the output depends on the time or the machine, so
the report is reproducible byte for byte. Mainnet is read-only throughout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from forklift.hashing import twox128
from forklift.metadata import PALLET_VERSION_KEY, UNLISTED_ITEMS

SAMPLE = 20  # lifted values parsed per item
STORAGE_VERSION_KEY = ":__STORAGE_VERSION__:"
CARRIES, REENCODE, NO_HOME = "carries_over", "needs_re_encoding", "no_home_on_v3"
CLASSES = (CARRIES, REENCODE, NO_HOME)
INT_WIDTH = {"u8": 1, "u16": 2, "u32": 4, "u64": 8, "u128": 16, "u256": 32,
             "i8": 1, "i16": 2, "i32": 4, "i64": 8, "i128": 16, "i256": 32, "bool": 1, "char": 4}
FIXED_LEAF = {"bytes32": 32, "bytes20": 20, "bytes64": 64}
Tree = Any  # str | dict


# --------------------------------------------------------------------------------------------- V14 trees

def _array(n: int, el: Tree) -> Tree:
    """A fixed array; 20/32/64 raw bytes are the same leaf however the runtime spells them (H160/H256/AccountId32/[u8; n])."""
    if el == "u8" and n in (20, 32, 64):
        return f"bytes{n}"
    return {"array": [n, el]}


def _leaf_for_path(path: list[str]) -> str | None:
    if path and path[-1] in ("AccountId32", "H256"):
        return "bytes32"
    if path and path[-1] == "H160":
        return "bytes20"
    if path and path[-1] == "H512":
        return "bytes64"
    return None


def tree14(types: dict[int, dict], tid: int, stack: tuple[int, ...] = ()) -> Tree:
    """The canonical tree of V14 type ``tid`` (``types``: id → the registry entry's ``type`` object)."""
    t = types[tid]
    path = t.get("path") or []
    leaf = _leaf_for_path(path)
    if leaf:
        return leaf
    if tid in stack:
        return {"recursive": "::".join(path) or f"#{tid}"}
    stack = stack + (tid,)
    d = t["def"]
    kind, body = next(iter(d.items()))
    if kind == "primitive":
        return "str" if body == "str" else body
    if kind == "composite":
        fields = body["fields"]
        if not fields:
            return "null"
        if len(fields) == 1 and fields[0]["name"] is None:
            return tree14(types, fields[0]["type"], stack)
        if all(f["name"] is None for f in fields):
            return {"tuple": [tree14(types, f["type"], stack) for f in fields]}
        return {"struct": [[f["name"], tree14(types, f["type"], stack)] for f in fields]}
    if kind == "variant":
        variants = sorted(body["variants"], key=lambda v: v["index"])
        if path == ["Option"]:
            return {"option": tree14(types, variants[1]["fields"][0]["type"], stack)}
        out = []
        for v in variants:
            fs = v["fields"]
            if not fs:
                sub = "null"
            elif len(fs) == 1 and fs[0]["name"] is None:
                sub = tree14(types, fs[0]["type"], stack)
            elif all(f["name"] is None for f in fs):
                sub = {"tuple": [tree14(types, f["type"], stack) for f in fs]}
            else:
                sub = {"struct": [[f["name"], tree14(types, f["type"], stack)] for f in fs]}
            out.append([v["index"], v["name"], sub])
        return {"enum": out}
    if kind == "sequence":
        el = tree14(types, body["type"], stack)
        return "bytes" if el == "u8" else {"vec": el}
    if kind == "array":
        el = tree14(types, body["type"], stack)
        return _array(body["len"], el)
    if kind == "tuple":
        return "null" if not body else {"tuple": [tree14(types, x, stack) for x in body]}
    if kind == "compact":
        return {"compact": tree14(types, body["type"], stack)}
    return {"opaque": f"v14:{kind}"}


# --------------------------------------------------------------------------------------------- V13 trees

def split_top(ts: str) -> list[str]:
    """Split ``A, B<C, D>, E`` at the commas outside <> () [] (a trailing comma is ignored)."""
    parts, depth, cur = [], 0, ""
    for ch in ts:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def tree13(reg, ts: str, stack: tuple[str, ...] = ()) -> Tree:
    """The canonical tree of a V13 type string, through the SDK's type registry (``legacy`` preset)."""
    import scalecodec.types as st

    ts = " ".join(ts.split()).replace(":: ", "::")
    if ts in stack:
        return {"recursive": ts}
    stack = stack + (ts,)
    try:
        cls = reg.get_decoder_class(ts)
    except Exception:  # noqa: BLE001  the registry raises plain Exceptions for unknown names
        cls = None
    if cls is None:
        return {"opaque": f"v13:{ts}"}
    sub = getattr(cls, "sub_type", None)
    mapping = getattr(cls, "type_mapping", None)
    if issubclass(cls, st.GenericAccountId) or issubclass(cls, st.H256):
        return "bytes32"
    if issubclass(cls, st.H160):
        return "bytes20"
    if issubclass(cls, st.H512):
        return "bytes64"
    if issubclass(cls, st.Bool):
        return "bool"
    for name in INT_WIDTH:
        if name != "bool" and cls is getattr(st, name.upper(), None):
            return name
    if issubclass(cls, (st.Bytes, st.HexBytes)):
        return "bytes"
    if ts.startswith(("BTreeMap<", "BoundedBTreeMap<")):  # SCALE: a vec of (key, value)
        kv = split_top(ts[ts.index("<") + 1:ts.rindex(">")])[:2]
        return {"vec": {"tuple": [tree13(reg, x, stack) for x in kv]}} if len(kv) == 2 else {"opaque": f"v13:{ts}"}
    if issubclass(cls, st.String):
        return "str"
    if issubclass(cls, st.Null):
        return "null"
    if issubclass(cls, (st.Option, st.Compact)):
        if sub is None and "<" in ts and ts.endswith(">"):  # a generic name (Compact<T::Balance>) the registry left unbound
            sub = ts[ts.index("<") + 1:ts.rindex(">")]
        if sub is None:
            return {"opaque": f"v13:{ts}"}
        return {"option" if issubclass(cls, st.Option) else "compact": tree13(reg, sub, stack)}
    if issubclass(cls, st.FixedLengthArray):
        return _array(cls.element_count, tree13(reg, sub, stack))
    if issubclass(cls, st.Vec):  # also BoundedVec, whose sub_type can still carry the bound: BoundedVec<T, N>
        inner = ts[ts.index("<") + 1:ts.rindex(">")] if "<" in ts and ts.endswith(">") else sub
        el = tree13(reg, split_top(inner)[0], stack)
        return "bytes" if el == "u8" else {"vec": el}
    if issubclass(cls, st.Tuple):
        text = ts.strip()
        parts = [tree13(reg, x, stack) for x in (split_top(text[1:-1]) if text.startswith("(") and text.endswith(")")
                                                  else (mapping or []))]
        return "null" if not parts else parts[0] if len(parts) == 1 else {"tuple": parts}
    if issubclass(cls, st.Struct):
        fields = [[n, tree13(reg, t, stack)] for n, t in mapping]
        if len(fields) == 1 and fields[0][0] in (None, "0"):
            return fields[0][1]
        return {"struct": fields}
    if issubclass(cls, st.Enum):
        if mapping:
            entries = mapping.items() if isinstance(mapping, dict) else mapping
            return {"enum": [[i, n, "null" if t in (None, "Null") else tree13(reg, t, stack)]
                             for i, (n, t) in enumerate(entries)]}
        names = getattr(cls, "value_list", []) or []
        return {"enum": [[i, n, "null"] for i, n in enumerate(names)]} if names else {"opaque": f"v13:{ts}"}
    return {"opaque": f"v13:{ts}"}


# --------------------------------------------------------------------------------------------- compare

def strip_names(t: Tree) -> Tree:
    """The layout of a tree: shape and widths only (field names, variant names and variant tags dropped)."""
    if isinstance(t, str):
        n = INT_WIDTH.get(t) or FIXED_LEAF.get(t)
        return f"w{n}" if n and t != "bool" else t
    (k, v), = t.items()
    if k == "array" and v[1] == "u8":
        return f"w{v[0]}"
    if k == "struct":
        return {"tuple": [strip_names(x) for _, x in v]}
    if k == "enum":
        return {"enum": [strip_names(x) for _, _, x in v]}
    if k in ("option", "vec", "compact"):
        return {k: strip_names(v)}
    if k == "array":
        return {k: [v[0], strip_names(v[1])]}
    if k == "tuple":
        return {k: [strip_names(x) for x in v]}
    return t


def opaque_in(t: Tree) -> str | None:
    """The first type string the SDK registry could not turn into a shape, if any."""
    if isinstance(t, str):
        return None
    (k, v), = t.items()
    if k == "opaque":
        return v
    if k in ("option", "vec", "compact"):
        return opaque_in(v)
    if k == "array":
        return opaque_in(v[1])
    if k == "tuple":
        return next((o for o in map(opaque_in, v) if o), None)
    if k == "struct":
        return next((o for o in (opaque_in(x) for _, x in v) if o), None)
    if k == "enum":
        return next((o for o in (opaque_in(x) for _, _, x in v) if o), None)
    return None


def first_difference(a: Tree, b: Tree, path: str = "") -> str:
    """A short human description of where two trees first differ (``a`` = mainnet, ``b`` = V3)."""
    here = path or "value"
    if a == b:
        return ""
    if isinstance(a, str) or isinstance(b, str):
        return f"{here}: {_show(a)} → {_show(b)}"
    (ka, va), (kb, vb) = next(iter(a.items())), next(iter(b.items()))
    if ka != kb:
        return f"{here}: {_show(a)} → {_show(b)}"
    if ka == "struct":
        na, nb = [n for n, _ in va], [n for n, _ in vb]
        if na != nb:
            gone, new = [n for n in na if n not in nb], [n for n in nb if n not in na]
            what = "; ".join(x for x in (f"removed {', '.join(gone)}" if gone else "",
                                         f"added {', '.join(new)}" if new else "") if x)
            return f"{here} fields: {', '.join(na)} → {', '.join(nb)} ({what or 'reordered'})"
        for (n, x), (_, y) in zip(va, vb):
            d = first_difference(x, y, f"{path}.{n}" if path else n)
            if d:
                return d
    elif ka == "enum":
        na, nb = [(i, n) for i, n, _ in va], [(i, n) for i, n, _ in vb]
        if na != nb:
            return f"{here} variants: {', '.join(n for _, n in na)} → {', '.join(n for _, n in nb)}"
        for (i, n, x), (_, _, y) in zip(va, vb):
            d = first_difference(x, y, f"{here}::{n}")
            if d:
                return d
    elif ka in ("option", "vec", "compact"):
        return first_difference(va, vb, f"{here}.{ka}")
    elif ka == "array":
        if va[0] != vb[0]:
            return f"{here}: array length {va[0]} → {vb[0]}"
        return first_difference(va[1], vb[1], f"{here}[]")
    elif ka == "tuple":
        if len(va) != len(vb):
            return f"{here}: tuple of {len(va)} → {len(vb)}"
        for i, (x, y) in enumerate(zip(va, vb)):
            d = first_difference(x, y, f"{here}.{i}")
            if d:
                return d
    return f"{here}: {_show(a)} → {_show(b)}"


def _show(t: Tree) -> str:
    if isinstance(t, str):
        return t
    (k, v), = t.items()
    if k in ("option", "vec", "compact"):
        return f"{k}<{_show(v)}>"
    if k == "struct":
        return "{" + ", ".join(f"{n}: {_show(x)}" for n, x in v) + "}"
    if k == "tuple":
        return "(" + ", ".join(_show(x) for x in v) + ")"
    if k == "array":
        return f"[{_show(v[1])}; {v[0]}]"
    if k == "enum":
        return "enum{" + " | ".join(n for _, n, _ in v) + "}"
    return f"{k}:{v}"


# --------------------------------------------------------------------------------------------- SCALE reader

class Unsupported(Exception):
    """The tree holds a type this reader cannot size (opaque / recursive)."""


def _compact(b: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(b):
        raise ValueError("truncated")
    mode = b[pos] & 3
    if mode == 0:
        return b[pos] >> 2, pos + 1
    if mode == 1:
        return int.from_bytes(b[pos:pos + 2], "little") >> 2, pos + 2
    if mode == 2:
        return int.from_bytes(b[pos:pos + 4], "little") >> 2, pos + 4
    n = (b[pos] >> 2) + 4
    return int.from_bytes(b[pos + 1:pos + 1 + n], "little"), pos + 1 + n


def skip(t: Tree, b: bytes, pos: int = 0) -> int:
    """Position after one value of tree ``t`` at ``pos``. Raises ValueError if ``b`` is not such a value."""
    if isinstance(t, str):
        if t in INT_WIDTH or t in FIXED_LEAF:
            n = INT_WIDTH.get(t) or FIXED_LEAF[t]
            if pos + n > len(b):
                raise ValueError(f"truncated {t}")
            if t == "bool" and b[pos] > 1:
                raise ValueError("bool byte > 1")
            return pos + n
        if t == "null":
            return pos
        if t in ("bytes", "str"):
            n, pos = _compact(b, pos)
            if pos + n > len(b):
                raise ValueError("truncated bytes")
            return pos + n
        raise Unsupported(t)
    (k, v), = t.items()
    if k == "compact":
        return _compact(b, pos)[1]
    if k == "option":
        if pos >= len(b) or b[pos] > 1:
            raise ValueError("bad Option tag")
        return pos + 1 if b[pos] == 0 else skip(v, b, pos + 1)
    if k == "vec":
        n, pos = _compact(b, pos)
        if n > len(b):
            raise ValueError("vec longer than the data")
        for _ in range(n):
            pos = skip(v, b, pos)
        return pos
    if k == "array":
        for _ in range(v[0]):
            pos = skip(v[1], b, pos)
        return pos
    if k == "tuple":
        for x in v:
            pos = skip(x, b, pos)
        return pos
    if k == "struct":
        for _, x in v:
            pos = skip(x, b, pos)
        return pos
    if k == "enum":
        if pos >= len(b):
            raise ValueError("truncated enum")
        by_tag = {i: x for i, _, x in v}
        if b[pos] not in by_tag:
            raise ValueError(f"enum tag {b[pos]} not in 0..{max(by_tag)}")
        return skip(by_tag[b[pos]], b, pos + 1)
    raise Unsupported(k)


def parses(t: Tree, b: bytes) -> tuple[bool | None, str | None]:
    """(True, None) if ``b`` is exactly one value of ``t``; (False, why); (None, why) if the reader cannot say."""
    try:
        end = skip(t, b)
    except Unsupported as exc:
        return None, f"unsupported {exc}"
    except (ValueError, IndexError) as exc:
        return False, str(exc)
    return (True, None) if end == len(b) else (False, f"{len(b) - end} trailing bytes")


# --------------------------------------------------------------------------------------------- items

@dataclass
class Item:
    prefix: str
    name: str
    kind: str  # Plain | Map | DoubleMap | NMap | Raw
    hashers: list[str] = field(default_factory=list)
    keys: list[Tree] = field(default_factory=list)  # key types, one per hasher
    value: Tree | None = None
    described: bool = True  # False: the runtime's own metadata does not describe it

    @property
    def full(self) -> str:
        return f"{self.prefix}.{self.name}"


def items13(rt) -> list[Item]:
    """Every storage item the mainnet runtime's metadata describes, plus the two it does not (UNLISTED_ITEMS)."""
    reg, out, seen = rt.registry, [], set()
    for m in rt._raw()["modules"]:
        if not m["storage"]:
            continue
        prefix = m["storage"]["prefix"]
        for e in m["storage"]["entries"]:
            kind, s = next(iter(e["type"].items()))
            if kind == "Plain":
                it = Item(prefix, e["name"], "Plain", [], [], tree13(reg, s))
            elif kind == "Map":
                it = Item(prefix, e["name"], "Map", [s["hasher"]], [tree13(reg, s["key"])], tree13(reg, s["value"]))
            elif kind == "DoubleMap":
                it = Item(prefix, e["name"], "DoubleMap", [s["hasher"], s["key2_hasher"]],
                          [tree13(reg, s["key1"]), tree13(reg, s["key2"])], tree13(reg, s["value"]))
            else:
                it = Item(prefix, e["name"], kind, list(s.get("hashers", [])),
                          [tree13(reg, k) for k in s.get("keys", [])], tree13(reg, s["value"]))
            out.append(it)
            seen.add(it.full)
    for _, (prefix, entries) in sorted(UNLISTED_ITEMS.items()):
        for name, kind in entries:
            if f"{prefix}.{name}" not in seen:
                out.append(Item(prefix, name, kind, described=False))
    return out


@dataclass
class V3Index:
    items: dict[str, Item]  # "Prefix.Item" → Item
    prefixes: set[str]
    by_name: dict[str, list[Item]]

    @classmethod
    def from_metadata(cls, md_v14: dict) -> "V3Index":
        types = {t["id"]: t["type"] for t in md_v14["types"]["types"]}
        items: dict[str, Item] = {}
        prefixes: set[str] = set()
        for p in md_v14["pallets"]:
            st = p.get("storage")
            if not st:
                continue
            prefixes.add(st["prefix"])
            for e in st["entries"]:
                kind, s = next(iter(e["type"].items()))
                if kind == "Plain":
                    it = Item(st["prefix"], e["name"], "Plain", [], [], tree14(types, s))
                else:
                    ktree = tree14(types, s["key"])
                    hs = list(s["hashers"])
                    keys = [ktree] if len(hs) == 1 else list(ktree["tuple"]) if isinstance(ktree, dict) \
                        and "tuple" in ktree else [ktree]
                    it = Item(st["prefix"], e["name"], "Map" if len(hs) == 1 else "NMap", hs, keys,
                              tree14(types, s["value"]))
                items[it.full] = it
        by_name: dict[str, list[Item]] = {}
        for it in items.values():
            by_name.setdefault(it.name, []).append(it)
        return cls(items, prefixes, by_name)


def classify(it: Item, v3: V3Index) -> dict:
    """One row's class, reason and V3 target; the sample check is added by the caller."""
    row: dict[str, Any] = {"class": None, "reason": "", "v3": None, "layout_compatible": None}
    target = v3.items.get(it.full)
    moved = False
    if target is None:
        others = [t for t in v3.by_name.get(it.name, []) if t.prefix != it.prefix]
        if len(others) == 1 and it.described:
            target, moved = others[0], True  # the same item name exists once on V3, under another prefix
        elif it.prefix in v3.prefixes:
            return {**row, "class": NO_HOME, "reason": f"V3 has {it.prefix} but no item {it.name}"}
        else:
            return {**row, "class": NO_HOME, "reason": f"V3 has no storage under {it.prefix}"}
    row["v3"] = target.full
    if it.described is False:
        return {**row, "reason": "1002 metadata does not describe this item; V3 has it"}
    unresolved = next((o for o in map(opaque_in, [*it.keys, it.value]) if o), None)
    if unresolved:
        return {**row, "class": REENCODE, "layout_compatible": None,
                "reason": f"not claimed as carried over: the SDK registry cannot resolve 1002 type `{unresolved}` to a shape"}
    problems = [f"the key prefix changes (V3 keeps it as {target.full})"] if moved else []
    layout = True
    if it.hashers != target.hashers:
        problems.append(f"key hashers {'+'.join(it.hashers) or 'none'} → {'+'.join(target.hashers) or 'none'}")
        layout = False
    if len(it.keys) == len(target.keys):
        for i, (a, b) in enumerate(zip(it.keys, target.keys)):
            if a != b:
                problems.append(first_difference(a, b, f"key{i + 1}"))
                layout &= strip_names(a) == strip_names(b)
    else:
        problems.append(f"{len(it.keys)} key(s) → {len(target.keys)}")
        layout = False
    if it.value != target.value:
        problems.append(first_difference(it.value, target.value, "value"))
        layout &= strip_names(it.value) == strip_names(target.value)
    if not problems:
        return {**row, "class": CARRIES, "reason": "same prefix, item, key hashers, key types and value type"}
    return {**row, "class": REENCODE, "reason": "; ".join(problems), "layout_compatible": layout}


# --------------------------------------------------------------------------------------------- report

def raw_rows(counts: dict[str, int], v3_keys: set[str], v3_prefixes: set[str],
             v3_versioned: set[str] = frozenset()) -> list[dict]:
    """Lifted keys that are not a pallet's storage item: the runtime code, well-known keys, pallet versions.

    ``v3_keys``: names of the same kind the V3 lift holds (``:code`` …), so the verdict rests on V3's own state.
    """
    rows = []
    for name, n in sorted(counts.items()):
        if name == ":code":
            rows.append({"item": name, "kind": "Raw", "keys_lifted": n, "class": NO_HOME, "v3": None,
                         "layout_compatible": None,
                         "reason": "the runtime wasm is the chain's own: V3 runs portaldot-v3 3002, not 1002"})
        elif name.startswith(":"):
            here = name in v3_keys
            rows.append({"item": name, "kind": "Raw", "keys_lifted": n, "class": CARRIES if here else NO_HOME,
                         "v3": name if here else None, "layout_compatible": None,
                         "reason": "well-known key; V3 holds the same key" if here else "V3 does not hold this key"})
        elif name.endswith("." + PALLET_VERSION_KEY):
            pallet = name.split(".", 1)[0]
            has = pallet in v3_prefixes
            rows.append({"item": name, "kind": "Raw", "keys_lifted": n,
                         "class": REENCODE if has else NO_HOME,
                         "v3": f"{pallet}.{STORAGE_VERSION_KEY}" if has else None,
                         "layout_compatible": False if has else None,
                         "reason": (("V3 holds this pallet's storage version under " + STORAGE_VERSION_KEY
                                     + " (key present in V3's lift)") if pallet in v3_versioned else
                                    f"V3 has {pallet}; no storage-version key for it in V3's lift at its pin"
                                    if has else f"V3 has no storage under {pallet}")})
    return rows


def build_report(*, mainnet: dict, v3: dict, md13_items: list[Item], v3_index: V3Index, counts: dict[str, int],
                 samples: dict[str, list[bytes]], v3_raw_names: set[str], v3_versioned: set[str] = frozenset()) -> dict:
    """The report as a JSON-able dict. ``counts``: lifted key count per ``Prefix.Item`` / raw name."""
    rows: list[dict] = []
    for it in md13_items:
        row = {"item": it.full, "kind": it.kind, "keys_lifted": counts.get(it.full, 0), **classify(it, v3_index)}
        target = v3_index.items.get(row["v3"]) if row["v3"] else None
        vals = samples.get(it.full, [])
        if vals and (it.value is not None or target is not None):
            ok13 = [parses(it.value, v) for v in vals] if it.value is not None else None
            ok14 = [parses(target.value, v) for v in vals] if target is not None and target.value is not None else None
            row["sample"] = {"values_read": len(vals),
                             "parse_1002": _tally(ok13), "parse_v3": _tally(ok14)}
            if row["class"] is None and ok14 is not None:  # not described on 1002: decided by real bytes
                good = all(x[0] for x in ok14)
                row["class"] = CARRIES if good else REENCODE
                row["layout_compatible"] = None if good else False
                row["reason"] += f"; decided on the sample: V3's type parses {sum(1 for x in ok14 if x[0])}/{len(ok14)} values"
        p13 = row.get("sample", {}).get("parse_1002")
        if p13 and p13["failed"]:
            # The 1002 type the SDK resolved does not even parse mainnet's own lifted values, so a difference
            # against V3 may be an artifact of that type: never report such an item as carried over.
            row["type_unreliable"] = True
            row["reason"] += (f"; caution: the SDK's 1002 type fails on {p13['failed']}/{p13['failed'] + p13['parsed']} "
                              "of mainnet's own values, so this difference may come from an incomplete 1002 type")
            if row["class"] == CARRIES:
                row["class"], row["layout_compatible"] = REENCODE, None
        if row["class"] is None:
            row["class"] = REENCODE
            row["reason"] += "; no value to check, so not claimed as carried over"
        rows.append(row)
    raw_counts = {k: v for k, v in counts.items() if k not in {i.full for i in md13_items}}
    rows += raw_rows(raw_counts, v3_raw_names, v3_index.prefixes, v3_versioned)
    rows.sort(key=lambda r: r["item"])
    totals = {c: {"items": 0, "keys_lifted": 0} for c in CLASSES}
    for r in rows:
        totals[r["class"]]["items"] += 1
        totals[r["class"]]["keys_lifted"] += r["keys_lifted"]
    self_check = {"items_sampled": sum(1 for r in rows if r.get("sample", {}).get("parse_1002")),
                  "items_failing_on_1002": [r["item"] for r in rows if r.get("type_unreliable")]}
    sample_bad = [r["item"] for r in rows if r["class"] == CARRIES and r.get("sample", {}).get("parse_v3", {}).get("failed")]
    return {"about": "Which lifted Portaldot mainnet (spec 1002) state has a home on the V3 testnet; "
                     "read at the two pinned blocks below, mainnet read-only",
            "mainnet": mainnet, "v3": v3, "sample_per_item": SAMPLE, "totals": totals,
            "keys_lifted_total": sum(r["keys_lifted"] for r in rows), "items_total": len(rows),
            "reader_self_check": self_check,
            "carried_but_sample_failed": sample_bad, "rows": rows}


def _tally(results: list[tuple[bool | None, str | None]] | None) -> dict | None:
    if results is None:
        return None
    first = next((w for ok, w in results if ok is False), None)
    return {"parsed": sum(1 for ok, _ in results if ok), "failed": sum(1 for ok, _ in results if ok is False),
            "not_checked": sum(1 for ok, _ in results if ok is None), **({"first_error": first} if first else {})}


def render_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def render_markdown(r: dict) -> str:
    m, v, t = r["mainnet"], r["v3"], r["totals"]
    total = r["keys_lifted_total"]
    names = {CARRIES: "carries over", REENCODE: "needs re-encoding", NO_HOME: "no home on V3"}
    L = ["# Mainnet (1002) → V3 migration report", "",
         "Which of the state Forklift lifts from Portaldot mainnet has a home on the V3 testnet. Generated by "
         "`forklift migrate-report`; read-only on mainnet, reproducible byte for byte from the two pinned blocks.", "",
         f"- **Mainnet** (spec {m['spec']}): block #{m['block_number']:,} `{m['block_hash']}`, "
         f":code `{m['code_hash']}`; {m['items_described']} storage items described by its V13 metadata.",
         f"- **V3 testnet** (spec {v['spec']}): block #{v['block_number']:,} `{v['block_hash']}`, "
         f":code `{v['code_hash']}`; {v['items_described']} storage items in its V14 metadata.",
         f"- **Lifted keys counted**: {total:,} (the lift of block #{m['block_number']:,}"
         f"{'; ' + m['skipped_note'] if m.get('skipped_note') else ''}).",
         f"- **Value check**: the first {r['sample_per_item']} lifted values of each item are parsed byte for byte "
         "under the mainnet type and under the V3 type.", "",
         "## Summary", "", "| | items | lifted keys | share of keys |", "|---|---:|---:|---:|"]
    for c in CLASSES:
        pct = f"{100 * t[c]['keys_lifted'] / total:.1f} %" if total else "n/a"
        L.append(f"| {names[c]} | {t[c]['items']} | {t[c]['keys_lifted']:,} | {pct} |")
    L += ["", "How to read the headings:", "",
          "- **carries over**: V3 has the same prefix and item with the same key hashers, key types and value "
          "type (shape and field names); the lifted bytes are valid on V3 as they are.",
          "- **needs re-encoding**: V3 has the item but a key or value type differs (or the item moved). "
          "`layout-compatible` means the bytes would still parse under V3's type; the meaning of renamed "
          "fields has to be mapped by hand.",
          "- **no home on V3**: V3 has no such item (or it is the chain's own, like `:code`).", ""]
    sc = r["reader_self_check"]
    L += [f"**Reader self-check.** The type reader is also run on mainnet's own values under the resolved 1002 types: "
          f"{sc['items_sampled'] - len(sc['items_failing_on_1002'])} of {sc['items_sampled']} sampled items parse "
          "in full; " + (f"the {len(sc['items_failing_on_1002'])} that do not ({', '.join(f'`{x}`' for x in sc['items_failing_on_1002'])}) "
                         "carry a caution in their row, because the SDK's 1002 type for them is incomplete." if sc['items_failing_on_1002'] else "none fail."), ""]
    if r["carried_but_sample_failed"]:
        L += [f"> Warning: these carried-over items have lifted values V3's type did not parse: "
              f"{', '.join(r['carried_but_sample_failed'])}", ""]
    for c in CLASSES:
        rows = [x for x in r["rows"] if x["class"] == c]
        L += [f"## {names[c].capitalize()} ({len(rows)} items)", "",
              "| item | kind | lifted keys | V3 item | value check on V3 | why |", "|---|---|---:|---|---|---|"]
        for x in rows:
            s = x.get("sample", {}).get("parse_v3")
            check = f"{s['parsed']}/{s['parsed'] + s['failed'] + s['not_checked']} parse" if s else "n/a"
            lay = "" if c != REENCODE else (" (layout-compatible)" if x["layout_compatible"] else " (layout differs)")
            L.append(f"| `{x['item']}` | {x['kind']} | {x['keys_lifted']:,} | "
                     f"{'`' + x['v3'] + '`' if x['v3'] else '—'} | {check} | {x['reason'].replace('|', '/')}{lay} |")
        L.append("")
    L += ["## Limits", "",
          "- Classification is by metadata shape and a value sample, not by what a V3 pallet *means* by a field: "
          "a renamed field with the same layout is reported as needing re-encoding on purpose.",
          "- Only the top trie is lifted; the MMR range is skipped by the lift and is not counted.",
          "- `carries over` means the encoding is valid on V3; V3's own runtime would still have to accept the state "
          "(consensus keys, pallet storage versions and the like are not meant to be copied).", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------------------------- generate

def _v14_json(raw_hex: str) -> dict:
    from scalecodec.base import RuntimeConfiguration, ScaleBytes
    from scalecodec.type_registry import load_type_registry_preset

    rc = RuntimeConfiguration()
    rc.update_type_registry(load_type_registry_preset("core"))
    obj = rc.create_scale_object("MetadataVersioned", data=ScaleBytes(raw_hex))
    obj.decode()
    return obj.value[1]["V14"]


def read_samples(client, block_hash: str, items: list[Item], per_item: int = SAMPLE,
                 progress: Callable[[str], None] | None = None) -> dict[str, list[bytes]]:
    """The first ``per_item`` stored values of every item with keys at ``block_hash`` (read-only)."""
    from forklift.lift import fetch_values

    out: dict[str, list[bytes]] = {}
    for it in items:
        prefix = "0x" + (twox128(it.prefix) + twox128(it.name)).hex()
        # A plain value lives at the prefix itself, which the node's key paging does not return: read it directly.
        exact = client.call("state_getStorage", [prefix, block_hash])
        keys = client.call("state_getKeysPaged", [prefix, per_item, None, block_hash])
        values = fetch_values(client, keys, block_hash) if keys else {}
        got = ([bytes.fromhex(exact[2:])] if exact else []) + [bytes.fromhex(values[k][2:]) for k in keys if values.get(k)]
        if not got:
            continue
        out[it.full] = got[:per_item]
        if progress:
            progress(it.full)
    return out


def generate(*, mainnet_evidence: dict, v3_evidence: dict, mainnet_endpoint: str, v3_endpoint: str,
             progress: Callable[[str], None] | None = None) -> dict:
    """Read both runtimes at their pinned blocks and build the report. Mainnet is only ever read."""
    from forklift.chains import MAINNET, V3
    from forklift.hashing import blake2_256
    from forklift.rpc import RpcClient
    from forklift.tx import Runtime

    mpin, vpin = mainnet_evidence["pin"], v3_evidence["pin"]
    with RpcClient(mainnet_endpoint) as mc:
        if mc.call("chain_getBlockHash", [0]) != MAINNET.genesis_hash:
            raise ValueError(f"{mainnet_endpoint} is not {MAINNET.label}")
        if mc.call("chain_getBlockHash", [mpin["block_number"]]) != mpin["block_hash"]:
            raise ValueError(f"mainnet block #{mpin['block_number']} is not {mpin['block_hash']}")
        rt = Runtime.load(mc, mpin["block_hash"])
        m_items = items13(rt)
        samples = read_samples(mc, mpin["block_hash"], m_items, progress=progress)
    with RpcClient(v3_endpoint) as vc:
        if vc.call("chain_getBlockHash", [0]) != V3.genesis_hash:
            raise ValueError(f"{v3_endpoint} is not {V3.label}")
        if vc.call("chain_getBlockHash", [vpin["block_number"]]) != vpin["block_hash"]:
            raise ValueError(f"V3 block #{vpin['block_number']} is not {vpin['block_hash']}")
        md = vc.call("state_getMetadata", [vpin["block_hash"]])
        v3_code = vc.call("state_getStorage", ["0x3a636f6465", vpin["block_hash"]])
    v3_code_hash = "0x" + blake2_256(bytes.fromhex(v3_code[2:])).hex()
    if v3_code_hash != V3.code_hash or v3_code_hash != v3_evidence["code_hash"]:
        raise ValueError(f"V3 :code at #{vpin['block_number']} is {v3_code_hash}, not the pinned profile")
    v3_index = V3Index.from_metadata(_v14_json(md))
    sk = mainnet_evidence.get("skipped") or {}
    report = build_report(
        mainnet={"endpoint": MAINNET.endpoint, "block_number": mpin["block_number"], "block_hash": mpin["block_hash"],
                 "spec": f"{mainnet_evidence['spec_name']} {mainnet_evidence['spec_version']}",
                 "code_hash": mainnet_evidence["code_hash"], "items_described": sum(1 for i in m_items if i.described),
                 "lift_manifest_root": mainnet_evidence["manifest_root"],
                 **({"skipped_note": f"the MerkleMountainRange range (~{sk['keys_in_range_derived']:,} derived keys) "
                                     "was skipped by the lift and is not counted"} if sk else {})},
        v3={"endpoint": V3.endpoint, "block_number": vpin["block_number"], "block_hash": vpin["block_hash"],
            "spec": f"{v3_evidence['spec_name']} {v3_evidence['spec_version']}", "code_hash": v3_code_hash,
            "items_described": len(v3_index.items), "lift_manifest_root": v3_evidence["manifest_root"]},
        md13_items=m_items, v3_index=v3_index, counts=dict(mainnet_evidence["per_item"]), samples=samples,
        v3_raw_names={k for k in v3_evidence["per_item"] if k.startswith(":") and not k.startswith(":child_storage")},
        v3_versioned={k.split(".unknown:")[0] for k in v3_evidence["per_item"]
                      if k.endswith(".unknown:0x" + twox128(STORAGE_VERSION_KEY).hex())})
    return report
