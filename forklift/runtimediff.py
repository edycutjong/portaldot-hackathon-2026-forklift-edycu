"""``forklift runtime-diff`` and ``forklift runtime-compare``: what changes when the runtime changes.

``runtime-diff`` compares two runtimes from their own metadata and runtime version, read at pinned blocks:
mainnet (spec 1002, metadata V13) and the V3 testnet (spec 3002, metadata V14). Per pallet it lists the
calls that are the same (argument names and layouts), changed, or only on one side, whether a call's
encoded index moved (pallet index, call index: an extrinsic encoded for one runtime does not decode as the
same call on the other), the storage items on each side, and the constants whose bytes differ. Types are
compared as trees through ``migrate`` (the same reduction the migration report uses). The report is a pure
function of the two metadata blobs and versions, so the same pins give the same bytes.

``runtime-compare`` rehearses one call with the same origin on two forks of the same mainnet pin, one per
runtime: ``fork-1002-fast`` (portaldot_dev, the dev runtime wasm) and ``fork-1002-exact`` (Chopsticks,
mainnet's own wasm). Both forks carry the same state overrides, so what differs is the runtime (and the
chain each fork builds on: a new genesis for fast, mainnet's own history for exact). It records, per fork,
the outcome, events, balance changes, the fee the sudo key paid, the dispatch weight, the timestamp step of
the block, and the constants on which the two runtimes disagree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from forklift.migrate import INT_WIDTH, first_difference, strip_names, tree13, tree14


@dataclass
class Call:
    index: tuple[int, int]  # (pallet index, call index): the first two bytes of the encoded call
    args: list[tuple[str, Any]]  # (name, type tree)


@dataclass
class PalletInfo:
    name: str
    index: int
    calls: dict[str, Call] = field(default_factory=dict)
    storage: list[str] = field(default_factory=list)
    constants: dict[str, tuple[Any, str]] = field(default_factory=dict)  # name → (type tree, value hex)


def decode_metadata(md_hex: str) -> dict:
    """``{"V13": …}`` or ``{"V14": …}`` from a ``state_getMetadata`` blob."""
    from scalecodec.base import RuntimeConfiguration, ScaleBytes
    from scalecodec.type_registry import load_type_registry_preset

    rc = RuntimeConfiguration()
    rc.update_type_registry(load_type_registry_preset("core"))
    obj = rc.create_scale_object("MetadataVersioned", data=ScaleBytes(md_hex))
    obj.decode()
    return obj.value[1]


def _bytes_hex(v: Any) -> str:
    """A constant's value as 0x-hex: the SDK hands back valid-UTF-8 byte strings as ``str``."""
    if isinstance(v, str) and v.startswith("0x"):
        try:
            bytes.fromhex(v[2:])
            return v
        except ValueError:
            pass
    return "0x" + (v.encode("utf-8") if isinstance(v, str) else bytes(v)).hex()


# Names the SDK's "core" preset leaves unbound in the 1002 runtime's call arguments and constants. The runtime
# is Substrate's node runtime (implName substrate-node): AccountIndex is u32 there, BalanceOf is the u128 balance,
# and pallet_identity's IdentityFields is a u64 bit set.
TYPES_1002 = {"AccountIndex": "u32", "T::AccountIndex": "u32", "BalanceOf<T, I>": "u128", "IdentityFields": "u64"}


def is_runtime_call(type_name: str | None) -> bool:
    """A call argument that is itself a call (``Box<<T as Config>::Call>``, ``Box<RuntimeCall>``): its layout is
    the whole runtime's call enum, so it is compared as one leaf, not expanded."""
    t = (type_name or "").replace(" ", "")
    return t.endswith(("::Call", "::Call>", "RuntimeCall", "RuntimeCall>", "::RuntimeCall>"))


def pallets13(md13: dict, reg) -> dict[str, PalletInfo]:
    """Pallets of a V13 runtime; ``reg`` is the SDK type registry the V13 type strings resolve through."""
    reg.update_type_registry_types(TYPES_1002)
    out = {}
    for m in md13["modules"]:
        p = PalletInfo(m["name"], m["index"])
        for i, c in enumerate(m["calls"] or []):
            p.calls[c["name"]] = Call((m["index"], i), [(a["name"], "call" if is_runtime_call(a["type"])
                                                         else tree13(reg, a["type"])) for a in c["args"]])
        if m["storage"]:
            p.storage = sorted(e["name"] for e in m["storage"]["entries"])
        for k in m["constants"] or []:
            p.constants[k["name"]] = (tree13(reg, k["type"]), _bytes_hex(k["value"]))
        out[p.name] = p
    return out


def pallets14(md14: dict) -> dict[str, PalletInfo]:
    types = {t["id"]: t["type"] for t in md14["types"]["types"]}
    out = {}
    for m in md14["pallets"]:
        p = PalletInfo(m["name"], m["index"])
        if m.get("calls"):
            for v in types[m["calls"]["ty"]]["def"]["variant"]["variants"]:
                p.calls[v["name"]] = Call((m["index"], v["index"]),
                                          [(f["name"], "call" if is_runtime_call(f.get("typeName"))
                                            else tree14(types, f["type"])) for f in v["fields"]])
        if m.get("storage"):
            p.storage = sorted(e["name"] for e in m["storage"]["entries"])
        for k in m.get("constants") or []:
            p.constants[k["name"]] = (tree14(types, k["type"]), _bytes_hex(k["value"]))
        out[p.name] = p
    return out


def show_value(tree: Any, value_hex: str) -> str:
    """A constant's value: an integer when its type is one, else its hex (shortened)."""
    raw = bytes.fromhex(value_hex[2:])
    if isinstance(tree, str) and tree in INT_WIDTH and tree != "bool" and len(raw) == INT_WIDTH[tree]:
        return f"{int.from_bytes(raw, 'little', signed=tree.startswith('i')):,}"
    return value_hex if len(value_hex) <= 34 else f"{value_hex[:18]}…{value_hex[-8:]} ({len(raw)} B)"


def _type_name(tree: Any) -> str:
    return tree if isinstance(tree, str) else next(iter(tree))


def compare_call(a: Call, b: Call) -> str:
    """Empty when the two calls take the same arguments (names and layouts); else the first difference."""
    # A leading underscore marks an unused parameter in Rust (``_remark``): same position, same bytes.
    names_a, names_b = [n.lstrip("_") for n, _ in a.args], [n.lstrip("_") for n, _ in b.args]
    if names_a != names_b:
        return f"arguments ({', '.join(names_a)}) → ({', '.join(names_b)})"
    for (name, ta), (_, tb) in zip(a.args, b.args):
        if strip_names(ta) != strip_names(tb) or ta != tb:
            return f"`{name}`: {first_difference(ta, tb, name)}"
    return ""


def compare_runtimes(a: dict[str, PalletInfo], b: dict[str, PalletInfo]) -> dict:
    """Per pallet: calls, storage items and constants of runtime ``a`` (mainnet) against ``b`` (V3)."""
    rows = []
    for name in sorted(set(a) | set(b)):
        pa, pb = a.get(name), b.get(name)
        row: dict[str, Any] = {"pallet": name, "index": [pa.index if pa else None, pb.index if pb else None]}
        if pa is None or pb is None:
            only = pa or pb
            row.update({"only_on": "mainnet" if pb is None else "v3", "calls": len(only.calls),
                        "storage": len(only.storage), "constants": len(only.constants)})
            rows.append(row)
            continue
        same, changed, moved = [], [], []
        for c in sorted(set(pa.calls) & set(pb.calls)):
            why = compare_call(pa.calls[c], pb.calls[c])
            (changed.append({"call": c, "why": why}) if why else same.append(c))
            if pa.calls[c].index != pb.calls[c].index:
                moved.append({"call": c, "mainnet": list(pa.calls[c].index), "v3": list(pb.calls[c].index)})
        consts = []
        for k in sorted(set(pa.constants) & set(pb.constants)):
            (ta, va), (tb, vb) = pa.constants[k], pb.constants[k]
            if va != vb:
                sa, sb = show_value(ta, va), show_value(tb, vb)
                if sa == sb:  # same number, other encoding width: say so instead of printing "100 → 100"
                    sa, sb = f"{sa} ({_type_name(ta)})", f"{sb} ({_type_name(tb)})"
                consts.append({"constant": k, "mainnet": sa, "v3": sb})
        row.update({
            "calls": {"same": same, "changed": changed, "only_mainnet": sorted(set(pa.calls) - set(pb.calls)),
                      "only_v3": sorted(set(pb.calls) - set(pa.calls)), "index_moved": moved},
            "storage": {"both": sorted(set(pa.storage) & set(pb.storage)),
                        "only_mainnet": sorted(set(pa.storage) - set(pb.storage)),
                        "only_v3": sorted(set(pb.storage) - set(pa.storage))},
            "constants": {"same": sum(1 for k in set(pa.constants) & set(pb.constants)
                                      if pa.constants[k][1] == pb.constants[k][1]),
                          "differ": consts, "only_mainnet": sorted(set(pa.constants) - set(pb.constants)),
                          "only_v3": sorted(set(pb.constants) - set(pa.constants))},
        })
        rows.append(row)
    return {"pallets": rows, "totals": _totals(rows)}


def _totals(rows: list[dict]) -> dict:
    both = [r for r in rows if "only_on" not in r]
    t = {"pallets_both": len(both), "pallets_only_mainnet": sum(1 for r in rows if r.get("only_on") == "mainnet"),
         "pallets_only_v3": sum(1 for r in rows if r.get("only_on") == "v3")}
    for part, keys in (("calls", ("same", "changed", "only_mainnet", "only_v3", "index_moved")),
                       ("storage", ("both", "only_mainnet", "only_v3"))):
        for k in keys:
            t[f"{part}_{k}"] = sum(len(r[part][k]) for r in both)
    t["calls_in_pallets_only_mainnet"] = sum(r["calls"] for r in rows if r.get("only_on") == "mainnet")
    t["calls_in_pallets_only_v3"] = sum(r["calls"] for r in rows if r.get("only_on") == "v3")
    t["constants_same"] = sum(r["constants"]["same"] for r in both)
    t["constants_differ"] = sum(len(r["constants"]["differ"]) for r in both)
    return t


VERSION_FIELDS = ("specName", "implName", "specVersion", "implVersion", "authoringVersion", "transactionVersion",
                  "stateVersion")


def compare_versions(a: dict, b: dict) -> dict:
    fields = [{"field": f, "mainnet": a.get(f), "v3": b.get(f)} for f in VERSION_FIELDS]
    apis_a, apis_b = dict(a.get("apis") or []), dict(b.get("apis") or [])
    apis = [{"id": k, "mainnet": apis_a.get(k), "v3": apis_b.get(k)} for k in sorted(set(apis_a) | set(apis_b))]
    return {"fields": fields, "apis": apis,
            "apis_same_version": sum(1 for x in apis if x["mainnet"] is not None and x["mainnet"] == x["v3"]),
            "apis_other_version": sum(1 for x in apis if None not in (x["mainnet"], x["v3"]) and x["mainnet"] != x["v3"]),
            "apis_only_mainnet": sum(1 for x in apis if x["v3"] is None),
            "apis_only_v3": sum(1 for x in apis if x["mainnet"] is None)}


def build_report(*, mainnet: dict, v3: dict, mainnet_md: str, mainnet_version: dict, mainnet_reg,
                 v3_md: str, v3_version: dict) -> dict:
    """The whole report from the two metadata blobs and versions (``mainnet``/``v3``: pins, for the header)."""
    a = pallets13(decode_metadata(mainnet_md)["V13"], mainnet_reg)
    b = pallets14(decode_metadata(v3_md)["V14"])
    return {"mainnet": mainnet, "v3": v3, "versions": compare_versions(mainnet_version, v3_version),
            **compare_runtimes(a, b)}


def generate(*, mainnet_evidence: dict, v3_evidence: dict, mainnet_endpoint: str, v3_endpoint: str) -> dict:
    """Read both runtimes at their pinned blocks (mainnet read-only) and build the report."""
    from forklift.chains import MAINNET, V3
    from forklift.hashing import blake2_256
    from forklift.rpc import RpcClient
    from forklift.tx import Runtime

    sides = {}
    for key, chain, ev, endpoint in (("mainnet", MAINNET, mainnet_evidence, mainnet_endpoint),
                                     ("v3", V3, v3_evidence, v3_endpoint)):
        pin = ev["pin"]
        with RpcClient(endpoint) as c:  # read-only client: cannot write (I3)
            if c.call("chain_getBlockHash", [0]) != chain.genesis_hash:
                raise ValueError(f"{endpoint} is not {chain.label}")
            if c.call("chain_getBlockHash", [pin["block_number"]]) != pin["block_hash"]:
                raise ValueError(f"{chain.label} block #{pin['block_number']} is not {pin['block_hash']}")
            md = c.call("state_getMetadata", [pin["block_hash"]])
            version = c.call("state_getRuntimeVersion", [pin["block_hash"]])
            code = c.call("state_getStorage", ["0x3a636f6465", pin["block_hash"]])
        code_hash = "0x" + blake2_256(bytes.fromhex(code[2:])).hex()
        if code_hash != ev["code_hash"]:
            raise ValueError(f"{chain.label} :code at #{pin['block_number']} is {code_hash}, not the lift's {ev['code_hash']}")
        sides[key] = {"endpoint": chain.endpoint, "block_number": pin["block_number"], "block_hash": pin["block_hash"],
                      "code_hash": code_hash, "_md": md, "_version": version}
    reg = Runtime(sides["mainnet"]["_md"], sides["mainnet"]["_version"], MAINNET.genesis_hash).registry
    return build_report(mainnet={k: v for k, v in sides["mainnet"].items() if not k.startswith("_")},
                        v3={k: v for k, v in sides["v3"].items() if not k.startswith("_")},
                        mainnet_md=sides["mainnet"]["_md"], mainnet_version=sides["mainnet"]["_version"],
                        mainnet_reg=reg, v3_md=sides["v3"]["_md"], v3_version=sides["v3"]["_version"])


def render_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _ids(items: list[str], limit: int = 12) -> str:
    shown = ", ".join(f"`{x}`" for x in items[:limit])
    return shown + (f" … (+{len(items) - limit})" if len(items) > limit else "") if items else "—"


def render_markdown(r: dict, rehearsal: dict | None = None) -> str:
    m, v, t, ver = r["mainnet"], r["v3"], r["totals"], r["versions"]
    out = [
        "# Runtime difference: Portaldot mainnet (spec 1002) vs the V3 testnet (spec 3002)",
        "",
        "Generated by `forklift runtime-diff` from each runtime's own metadata and `state_getRuntimeVersion`, read",
        "at a pinned block of each chain (mainnet read-only). Same pins → same bytes: nothing here depends on the",
        "time or the machine. Storage *types* item by item are in [MIGRATION-1002-V3.md](MIGRATION-1002-V3.md).",
        "",
        "| | mainnet | V3 testnet |",
        "|---|---|---|",
        f"| pinned block | #{m['block_number']:,} `{m['block_hash']}` | #{v['block_number']:,} `{v['block_hash']}` |",
        f"| runtime `:code` (blake2_256) | `{m['code_hash']}` | `{v['code_hash']}` |",
        "| metadata | V13 | V14 |",
        "",
        "## Summary",
        "",
        "| | count |",
        "|---|---:|",
        f"| pallets on both | {t['pallets_both']} |",
        f"| pallets only on mainnet | {t['pallets_only_mainnet']} |",
        f"| pallets only on V3 | {t['pallets_only_v3']} |",
        f"| calls on both, same arguments | {t['calls_same']} |",
        f"| calls on both, arguments changed | {t['calls_changed']} |",
        f"| calls removed in V3 (pallet kept) | {t['calls_only_mainnet']} |",
        f"| calls added in V3 (pallet kept) | {t['calls_only_v3']} |",
        f"| calls on both whose encoded index moved | {t['calls_index_moved']} |",
        f"| storage items on both (types: migration report) | {t['storage_both']} |",
        f"| storage items removed / added (pallet kept) | {t['storage_only_mainnet']} / {t['storage_only_v3']} |",
        f"| constants on both, same bytes / different | {t['constants_same']} / {t['constants_differ']} |",
        "",
        "## Versions",
        "",
        "| field | mainnet | V3 |",
        "|---|---|---|",
        *(f"| `{f['field']}` | {_cell(f['mainnet'])} | {_cell(f['v3'])} |" for f in ver["fields"]),
        "",
        f"Runtime APIs (by id): {ver['apis_same_version']} at the same version, {ver['apis_other_version']} at "
        f"another version, {ver['apis_only_mainnet']} only on mainnet, {ver['apis_only_v3']} only on V3.",
        "",
        "| API id | mainnet | V3 |",
        "|---|---:|---:|",
        *(f"| `{a['id']}` | {_cell(a['mainnet'])} | {_cell(a['v3'])} |" for a in ver["apis"]
          if a["mainnet"] != a["v3"]),
        "",
        "## Pallets",
        "",
        "Index = the pallet's position in the encoded call (mainnet → V3). Calls: same / changed / removed /",
        "added. Storage: on both / removed / added. Constants: same / different.",
        "",
        "| pallet | index | calls | storage | constants |",
        "|---|---|---|---|---|",
    ]
    for p in r["pallets"]:
        idx = " → ".join("—" if i is None else str(i) for i in p["index"])
        if "only_on" in p:
            side = "mainnet only" if p["only_on"] == "mainnet" else "V3 only"
            out.append(f"| {p['pallet']} | {idx} | {side}: {p['calls']} | {side}: {p['storage']} | {side}: {p['constants']} |")
            continue
        c, s, k = p["calls"], p["storage"], p["constants"]
        out.append(f"| {p['pallet']} | {idx} | {len(c['same'])} / {len(c['changed'])} / {len(c['only_mainnet'])} / "
                   f"{len(c['only_v3'])} | {len(s['both'])} / {len(s['only_mainnet'])} / {len(s['only_v3'])} | "
                   f"{k['same']} / {len(k['differ'])} |")
    both = [p for p in r["pallets"] if "only_on" not in p]
    out += ["", "## Calls whose arguments changed", "", "| call | first difference (mainnet → V3) |", "|---|---|"]
    out += [f"| `{p['pallet']}.{c['call']}` | {_clip(c['why'])} |" for p in both for c in p["calls"]["changed"]] \
        or ["| — | — |"]
    out += ["", "## Calls removed or added (pallet on both)", "", "| pallet | removed in V3 | added in V3 |", "|---|---|---|"]
    out += [f"| {p['pallet']} | {_ids(p['calls']['only_mainnet'])} | {_ids(p['calls']['only_v3'])} |"
            for p in both if p["calls"]["only_mainnet"] or p["calls"]["only_v3"]] or ["| — | — | — |"]
    out += ["", "## Pallets on one side only", "",
            f"- mainnet only: {_ids([p['pallet'] for p in r['pallets'] if p.get('only_on') == 'mainnet'], 99)}",
            f"- V3 only: {_ids([p['pallet'] for p in r['pallets'] if p.get('only_on') == 'v3'], 99)}",
            "", "## Constants that differ", "", "| constant | mainnet | V3 |", "|---|---:|---:|"]
    out += [f"| `{p['pallet']}.{k['constant']}` | {k['mainnet']} | {k['v3']} |"
            for p in both for k in p["constants"]["differ"]] or ["| — | — | — |"]
    if rehearsal is not None:
        out += ["", *render_rehearsal(rehearsal)]
    out += ["", "## Reproduce", "", "```bash",
            f"forklift runtime-diff --mainnet-evidence evidence/lift-{m['block_number']}.json \\",
            f"  --v3-evidence evidence/lift-v3-{v['block_number']}.json"
            + (f" \\\n  --rehearsal evidence/runtime-rehearse-{rehearsal['pin']['block_number']}.json" if rehearsal else "")
            + " \\\n  --out docs/RUNTIME-DIFF.md --json-out evidence/runtime-diff-1002-v3.json", "```", ""]
    return "\n".join(out)


def _clip(text: str, limit: int = 160) -> str:
    text = text.replace("|", "\\|")
    return text if len(text) <= limit else text[:limit - 1] + "… (full text in the evidence JSON)"


def _cell(v: Any) -> str:
    return "—" if v is None else f"`{v}`"


# --- runtime-compare: one call, two runtimes, same pin -------------------------------------------------

def _free(info: dict | None) -> int:
    return int(((info or {}).get("data") or {}).get("free") or 0)


def _dispatch_info(events: list[dict]) -> dict | None:
    for e in events:
        if e["event"] == "System.ExtrinsicSuccess":
            for a in e["attributes"]:
                v = a.get("value") if isinstance(a, dict) else None
                if isinstance(v, dict) and "weight" in v:
                    return v
    return None


def rehearse_on(client, label: str, who: str, pallet: str, function: str, raw_args: str | None) -> dict:
    """``as`` once on one fork, with the reads around it that a runtime difference shows up in."""
    from forklift import act, tx
    from forklift.hashing import blake2_256
    from forklift.rehearse import accounts_in
    from forklift.splice import DEV_KEY, DEV_KEY_SS58

    rt = tx.Runtime.load(client)
    call_args = act.parse_args(rt, pallet, function, raw_args)
    accounts = [who] + [a for a in accounts_in(call_args) if a != who]
    before = {a: act.account_info(client, rt, tx.ss58_to_public(a)) for a in accounts}
    key_before = act.account_info(client, rt, DEV_KEY)
    r = act.act_as(client, rt, who, pallet, function, call_args)
    inc = r.inclusion
    after = {a: act.account_info(client, rt, tx.ss58_to_public(a), at=inc.block_hash) for a in accounts}
    key_after = act.account_info(client, rt, DEV_KEY, at=inc.block_hash)
    parent = client.call("chain_getHeader", [inc.block_hash])["parentHash"]
    now, then = (act.read(client, rt, "Timestamp", "Now", at=h) for h in (inc.block_hash, parent))
    version = client.call("state_getRuntimeVersion", [inc.block_hash])
    code = client.call("state_getStorage", ["0x3a636f6465", inc.block_hash])
    events = [e.as_json() for e in inc.events]
    return {
        "label": label, "extrinsic_id": inc.extrinsic_id, "block_hash": inc.block_hash, "tx_hash": inc.tx_hash,
        "success": inc.success, "dispatch": {"ok": r.dispatch_ok, "error": r.dispatch_error},
        "submitted_via": inc.extra.get("submitted_via", "author_submitExtrinsic"),
        "genesis_hash": client.call("chain_getBlockHash", [0]),
        "spec": [version.get("specName"), version.get("specVersion")], "impl_version": version.get("implVersion"),
        "code_hash": "0x" + blake2_256(bytes.fromhex(code[2:])).hex(),
        "events": events, "inner_events": r.inner_events,
        "deltas": {a: _free(after[a]) - _free(before[a]) for a in accounts},
        "sudo_key": DEV_KEY_SS58, "sudo_key_fee": _free(key_before) - _free(key_after),
        "dispatch_info": _dispatch_info(events),
        "timestamp_step_ms": (int(now) - int(then)) if now is not None and then is not None else None,
        "_metadata": client.call("state_getMetadata", [inc.block_hash]),
        "_registry": rt.registry,
    }


WHY = {
    "block_number": "fast starts a new chain on its own genesis (block numbers restart at 1); exact builds on "
                    "mainnet's own history (pin + 1 carries the overrides, the call lands after it)",
    "tx_hash": "sr25519 (schnorrkel) signing is randomised, and the recorded runs gave different hashes on the same "
               "chain; the signed payload also differs: it covers each chain's genesis hash (immortal era)",
    "code_hash": "fast runs the dev runtime wasm; exact runs mainnet's wasm, the one at the pin",
    "impl_version": "the dev runtime and mainnet's runtime are different builds of spec 1002",
    "sudo_key_fee": "the outer extrinsic's fee, computed by each runtime from its own weights and fee constants",
    "dispatch_weight": "each runtime's own benchmarked weight for the call",
    "timestamp_step_ms": "the block's timestamp step follows each runtime's block time (6 s dev, 60 s mainnet: "
                         "Babe.ExpectedBlockTime below); the fast node stamps wall-clock time, so its step is a few ms "
                         "off 6,000",
}


def compare_rehearsals(fast: dict, exact: dict) -> list[dict]:
    def w(r):
        di = r.get("dispatch_info") or {}
        return di.get("weight")

    def ev(r):
        return [(e["event"], json.dumps(e.get("attributes"), sort_keys=True)) for e in r["inner_events"]]

    rows = [
        ("dispatch_ok", fast["dispatch"]["ok"], exact["dispatch"]["ok"]),
        ("inner_events", [e[0] for e in ev(fast)], [e[0] for e in ev(exact)]),
        ("inner_event_attributes", [e[1] for e in ev(fast)], [e[1] for e in ev(exact)]),
        *((f"delta {a}", fast["deltas"].get(a), exact["deltas"].get(a)) for a in fast["deltas"]),
        ("spec", fast["spec"], exact["spec"]),
        ("impl_version", fast["impl_version"], exact["impl_version"]),
        ("code_hash", fast["code_hash"], exact["code_hash"]),
        ("sudo_key_fee", fast["sudo_key_fee"], exact["sudo_key_fee"]),
        ("dispatch_weight", w(fast), w(exact)),
        ("timestamp_step_ms", fast["timestamp_step_ms"], exact["timestamp_step_ms"]),
        ("block_number", int(fast["extrinsic_id"].split("-")[0]), int(exact["extrinsic_id"].split("-")[0])),
        ("tx_hash", fast["tx_hash"], exact["tx_hash"]),
    ]
    return [{"field": f, "fast": a, "exact": b, "same": a == b} for f, a, b in rows]  # why-notes: WHY, at render


def constants_between(fast_md: str, fast_reg, exact_md: str, exact_reg) -> dict:
    """Constants and calls on which the dev runtime (fast) and mainnet's runtime (exact) disagree."""
    a = pallets13(decode_metadata(fast_md)["V13"], fast_reg)
    b = pallets13(decode_metadata(exact_md)["V13"], exact_reg)
    cmp = compare_runtimes(b, a)  # mainnet first, as in runtime-diff
    rows = [{"constant": f"{p['pallet']}.{k['constant']}", "exact": k["mainnet"], "fast": k["v3"]}
            for p in cmp["pallets"] if "only_on" not in p for k in p["constants"]["differ"]]
    t = cmp["totals"]
    calls = [{"call": f"{p['pallet']}.{c['call']}", "why": c["why"]}
             for p in cmp["pallets"] if "only_on" not in p for c in p["calls"]["changed"]]
    return {"constants_differ": rows, "constants_same": t["constants_same"], "calls_changed_list": calls,
            "pallets_only_exact": t["pallets_only_mainnet"], "pallets_only_fast": t["pallets_only_v3"],
            "calls_changed": t["calls_changed"], "calls_same": t["calls_same"]}


def render_rehearsal(rh: dict) -> list[str]:
    run = rh["runs"][-1]
    f, e, cons = run["fast"], run["exact"], run["constants"]
    out = [
        "## One call on both 1002 runtimes: `fork-1002-fast` vs `fork-1002-exact`",
        "",
        f"`forklift runtime-compare` ran `{run['call']}` as `{run['who']}` (through `Sudo.sudo_as`, signed by the",
        f"fork's sudo key) on two forks of the same mainnet pin #{rh['pin']['block_number']:,}, recorded",
        f"{run['recorded_utc']} in `evidence/runtime-rehearse-{rh['pin']['block_number']}.json`. Both forks carry the same",
        "four state keys (sudo key, era policy, the dev key's account with 50,000 POT free, issuance); the fast fork's",
        "dev-key account also keeps the dev genesis's staking lock (30,000 POT frozen, 3 consumers), and only fast",
        "overrides `:code`: it boots portaldot_dev on the dev runtime wasm, while `exact` runs mainnet's own wasm under",
        "Chopsticks. Why `exact` bypasses the pool: [`evidence/chopsticks-1002-2725953.json`]"
        "(../evidence/chopsticks-1002-2725953.json).",
        "",
        "| | fast | exact |",
        "|---|---|---|",
        f"| extrinsic | `{f['extrinsic_id']}` | `{e['extrinsic_id']}` |",
        f"| runtime `:code` | `{f['code_hash'][:18]}…` | `{e['code_hash'][:18]}…` |",
        "| submitted via | `author_submitExtrinsic` | `dev_newBlock` (pool bypassed) |",
        "",
        "| field | fast | exact | same? | why it differs |",
        "|---|---|---|---|---|",
    ]
    runs = rh["runs"]
    differ = [tuple(x["field"] for x in r["compare"] if not x["same"]) for r in runs]
    if len(runs) > 1:
        steps = lambda side: ", ".join(f"{r[side]['timestamp_step_ms']:,}" for r in runs)  # noqa: E731
        out[-2:-2] = [f"{len(runs)} runs are recorded, each on a fast and an exact fork started for that run. The fields that differ are "
                      + ("the same in every run" if len(set(differ)) == 1 else "NOT the same in every run")
                      + f"; the timestamp step was {steps('fast')} ms on fast and {steps('exact')} ms "
                      "on exact. The table shows the last run.", ""]
    for row in run["compare"]:
        fv, ev_ = row["fast"], row["exact"]
        if row["field"] == "inner_event_attributes":  # long JSON: say what was compared, the bytes are in the evidence
            fv = ev_ = f"{len(row['fast'])} events, every attribute"
        note = (WHY.get(row["field"]) if not row["same"] else "") or ("measured: the sudo key's free balance did not move"
                                  if row["field"] == "sudo_key_fee" and row["fast"] == 0 else "")
        out.append(f"| {row['field']} | {_short(fv)} | {_short(ev_)} | {'yes' if row['same'] else '**no**'} | {note} |")
    out += ["", f"The two 1002 runtimes: {cons['constants_same']} constants with the same bytes, "
                f"{len(cons['constants_differ'])} different; calls {cons['calls_same']} same, {cons['calls_changed']} changed; "
                f"pallets only in exact {cons['pallets_only_exact']}, only in fast {cons['pallets_only_fast']}.", "",
            "| constant | fast (dev runtime) | exact (mainnet's runtime) |", "|---|---:|---:|"]
    out += [f"| `{c['constant']}` | {c['fast']} | {c['exact']} |" for c in cons["constants_differ"]] or ["| — | — | — |"]
    if cons.get("calls_changed_list"):
        out += ["", "Calls whose arguments differ between the two 1002 runtimes (exact → fast): "
                + "; ".join(f"`{c['call']}`: {_clip(c['why'], 400)}" for c in cons["calls_changed_list"]) + "."]
    return out


def _short(v: Any) -> str:
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}"
    s = json.dumps(v) if not isinstance(v, str) else v
    s = s.replace("|", "\\|")
    if s.startswith("0x") and len(s) > 22:
        return f"`{s[:18]}…`"
    return s if len(s) <= 60 else s[:57] + "…"
