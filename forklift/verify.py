"""``forklift verify``: check a fork against mainnet, key by key, from the public node.

Keys are drawn **from mainnet** at the pinned hash, never from Forklift's own
lift: for a storage item drawn with weight = its lifted key count, the key is
the first one after ``itemPrefix ‖ random16`` (wrapping to the item's first
key). A cursor drawn over the whole keyspace would almost always land on the
first key of the next pallet, so the draw stays inside one item.

Each key is read on mainnet at the pin and on the fork at its genesis block,
then classified:

- ``identical``: same value;
- ``dev-kept``: under a prefix the splice keeps from the dev genesis (named rule);
- ``override``: one of the manifest's named overrides, and the fork holds the override's value;
- ``reset``: under the MMR prefix and absent on the fork;
- ``mismatch``: anything else. Exit code 2.

Three whole-fork checks run alongside: the fork's genesis key set equals the
spliced set (count and set hash), ``Balances.TotalIssuance`` equals mainnet's
plus the dev key's balance (I2), and both runtime code hashes match the
manifest (I4).
"""

from __future__ import annotations

import os
import random
from collections import Counter
from dataclasses import dataclass, field

from forklift.hashing import blake2_256
from forklift.metadata import StorageIndex, decode_hex
from forklift.rpc import RpcClient
from forklift.splice import CODE, TOTAL_ISSUANCE, genesis_key_set_hash, rule_of

CHILD_PREFIX = b":child_storage:"

EXIT_MISMATCH = 2


class VerifyError(Exception):
    """Verification could not run (unreachable node, wrong fork, bad manifest). Not a mismatch."""


def _b(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str.removeprefix("0x"))


def _h(raw: bytes) -> str:
    return "0x" + raw.hex()


def item_prefixes(index: StorageIndex, per_item: dict[str, int]) -> dict[str, bytes]:
    """Storage prefix of every item named in the manifest's per-item counts."""
    by_name = {it.full_name: prefix for prefix, it in index.items.items()}
    out: dict[str, bytes] = {}
    for name in per_item:
        if name in by_name:
            out[name] = by_name[name]
        elif name.startswith(":"):
            out[name] = name.encode()  # a well-known key is its own prefix
        else:
            raise VerifyError(f"cannot sample {name}: no storage prefix in the metadata at the pin")
    return out


WINDOW = 64


def draw_key(client: RpcClient, block_hash: str, prefix: bytes, rng: random.Random,
             window: int = WINDOW) -> tuple[str, bool]:
    """One key of the item under ``prefix`` at ``block_hash``. Returns (key, wrapped).

    A random start ``prefix ‖ random16`` lands somewhere in the item; the key is
    picked at random from the next ``window`` keys, wrapping to the item's first
    keys at the end. Taking just the first key after the start would never reach
    past the first key of a group in a nested map (``Staking.ErasStakers`` keys
    share an era group prefix), so the pick is from a window instead. Bound: a
    random start never falls inside a group, so only the first ``window`` keys
    of each group are reachable (at #2,725,639 a group is one era: 10 validators).
    """
    start = prefix + rng.randbytes(16)
    page = list(client.call("state_getKeysPaged", [_h(prefix), window, _h(start), block_hash]) or [])
    wrapped = len(page) < window
    if wrapped:
        # Paging starts strictly after its start key, so a key equal to the prefix itself
        # (a plain item, a well-known key) is never paged: read it directly.
        head = [_h(prefix)] if client.call("state_getStorage", [_h(prefix), block_hash]) is not None else []
        head += client.call("state_getKeysPaged", [_h(prefix), window, None, block_hash]) or []
        page += [k for k in head if k not in page][:window - len(page)]
    if not page:
        raise VerifyError(f"no key under {_h(prefix)} on mainnet at {block_hash}")
    bad = [k for k in page if not k.startswith(_h(prefix))]
    if bad:
        raise VerifyError(f"the node returned {bad[0]} outside the prefix {_h(prefix)}")
    return rng.choice(page), wrapped


def draw_sample(client: RpcClient, block_hash: str, prefixes: dict[str, bytes], weights: dict[str, int],
                n: int, rng: random.Random) -> list[tuple[str, str]]:
    """``n`` draws of (item name, key), items chosen with probability ∝ their lifted key count."""
    names = sorted(prefixes)
    w = [weights[name] for name in names]
    draws = []
    for _ in range(n):
        name = rng.choices(names, weights=w)[0]
        key, _ = draw_key(client, block_hash, prefixes[name], rng)
        draws.append((name, key))
    return draws


def read_values(client: RpcClient, keys: list[str], block_hash: str, batch: int = 200) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for i in range(0, len(keys), batch):
        chunk = keys[i:i + batch]
        sets = client.call("state_queryStorageAt", [chunk, block_hash])
        got = {k: v for s in sets for k, v in s["changes"]}
        for k in chunk:
            out[k] = got.get(k)
    return out


def _override_matches(expected: str, fork_value: str | None) -> bool:
    if fork_value is None:
        return False
    if expected.startswith("blake2_256 "):  # :code is recorded by hash
        return expected.split()[1] == _h(blake2_256(_b(fork_value)))
    return expected == fork_value


def classify(key: str, mainnet_value: str | None, fork_value: str | None, overrides: dict[str, dict],
             dev: dict[str, str] | None = None) -> str:
    """With ``dev`` (the node's own dev genesis, ``build-spec --dev --raw``), a dev-kept key must hold
    exactly its dev value, or be absent like the mainnet-only keys under a dev prefix."""
    if key in overrides:
        return "override" if _override_matches(overrides[key]["value"], fork_value) else "mismatch"
    rule = rule_of(_b(key))
    if rule.startswith("dev:"):
        if dev is not None and fork_value != dev.get(key):
            return "mismatch"
        return "dev-kept"
    if rule.startswith("reset:"):
        return "reset" if fork_value is None else "mismatch"
    if mainnet_value is not None and fork_value == mainnet_value:
        return "identical"
    return "mismatch"


def fork_keys(client: RpcClient, block_hash: str, page_size: int = 1000) -> list[str]:
    keys: list[str] = []
    cursor = None
    while True:
        page = client.call("state_getKeysPaged", ["0x", page_size, cursor, block_hash])
        if not page:
            return keys
        keys.extend(page)
        cursor = page[-1]


@dataclass
class Report:
    ok: bool
    seed: int
    draws: int
    unique_keys: int
    classes: dict[str, int]
    mismatches: list[dict]
    checks: dict
    sample: list[dict] = field(default_factory=list)
    fork_head: dict = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else EXIT_MISMATCH


def verify(manifest: dict, mainnet: RpcClient, fork: RpcClient, *, sample: int = 200, seed: int | None = None,
           index: StorageIndex | None = None, spliced: dict[str, str] | None = None,
           dev: dict[str, str] | None = None) -> Report:
    """``dev``: the node's dev genesis, if at hand; dev-kept keys are checked against it (independently of
    the splice's output). ``spliced``: the fork's chain-spec genesis, if at hand; it names the keys behind
    a key-set difference."""
    src, fk = manifest["source"], manifest["fork"]
    if not fk or not fk.get("genesis_hash"):
        raise VerifyError("the manifest describes no booted fork (fork.genesis_hash is empty)")
    pin = src["block_hash"]
    genesis = fork.call("chain_getBlockHash", [0])
    if genesis != fk["genesis_hash"]:
        raise VerifyError(f"{fork.endpoint} serves genesis {genesis}, not this manifest's fork {fk['genesis_hash']}")
    if seed is None:
        seed = int.from_bytes(os.urandom(8), "little")
    rng = random.Random(seed)
    if index is None:
        index = StorageIndex(decode_hex(mainnet.call("state_getMetadata", [pin])))
    per_item = manifest["load"]["per_item"]
    prefixes = item_prefixes(index, per_item)
    draws = draw_sample(mainnet, pin, prefixes, per_item, sample, rng)
    keys = sorted({k for _, k in draws})
    on_main = read_values(mainnet, keys, pin)
    on_fork = read_values(fork, keys, genesis)
    overrides = {o["key"]: o for o in manifest["overrides"]}
    classes: Counter[str] = Counter()
    mismatches = []
    rows = []
    for k in keys:
        c = classify(k, on_main[k], on_fork[k], overrides, dev)
        classes[c] += 1
        name = index.item_of(_b(k))
        rows.append({"key": k, "item": name, "class": c})
        if c == "mismatch":
            mismatches.append({"key": k, "item": name, "mainnet": _short(on_main[k]), "fork": _short(on_fork[k])})

    checks = {}
    # Every named override: too few keys for a random draw to reach, so all of them are read.
    o_keys = sorted(overrides)
    o_main = read_values(mainnet, o_keys, pin)
    o_fork = read_values(fork, o_keys, genesis)
    o_rows = [{"name": overrides[k]["name"], "fork_holds_override": _override_matches(overrides[k]["value"], o_fork[k]),
               "mainnet_as_recorded": _override_matches(overrides[k]["mainnet_value"], o_main[k])
               if overrides[k]["mainnet_value"] is not None else o_main[k] is None}
              for k in o_keys]
    checks["overrides"] = {"ok": all(r["fork_holds_override"] and r["mainnet_as_recorded"] for r in o_rows),
                           "count": len(o_rows), "rows": o_rows}
    # The fork's whole genesis key set vs the spliced set.
    got_keys = fork_keys(fork, genesis)
    got_hash = genesis_key_set_hash(got_keys)
    key_set = {"ok": got_hash == fk["genesis_key_set_hash"] and len(got_keys) == fk["genesis_keys"],
               "expected_keys": fk["genesis_keys"], "fork_keys": len(got_keys),
               "expected_set_hash": fk["genesis_key_set_hash"], "fork_set_hash": got_hash}
    if not key_set["ok"] and spliced is not None:
        want, have = set(spliced), set(got_keys)
        key_set["missing_on_fork"] = [index.item_of(_b(k)) for k in sorted(want - have)][:20]
        key_set["extra_on_fork"] = [index.item_of(_b(k)) for k in sorted(have - want)][:20]
    checks["genesis_key_set"] = key_set
    # I2: issuance.
    ti_key = _h(TOTAL_ISSUANCE)
    ti_main = _u128(mainnet.call("state_getStorage", [ti_key, pin]))
    ti_fork = _u128(fork.call("state_getStorage", [ti_key, genesis]))
    added = int(fk["issuance"]["added"])
    checks["issuance"] = {"ok": ti_main is not None and ti_fork == ti_main + added,
                          "mainnet": None if ti_main is None else str(ti_main), "added": str(added),
                          "fork": None if ti_fork is None else str(ti_fork)}
    # I4: both runtimes are the ones the manifest names.
    code_main = mainnet.call("state_getStorage", [_h(CODE), pin])
    code_fork = fork.call("state_getStorage", [_h(CODE), genesis])
    h_main = _h(blake2_256(_b(code_main))) if code_main else None
    h_fork = _h(blake2_256(_b(code_fork))) if code_fork else None
    checks["runtime"] = {"ok": h_main == src["code_hash"] and h_fork == fk["code_hash"],
                         "mainnet_code_hash": h_main, "fork_code_hash": h_fork,
                         "fork_runtime": manifest.get("node", {}).get("runtime_mode")}
    # Dev-kept and override draws do not compare with mainnet: at least one draw must.
    checks["compared_with_mainnet"] = {"ok": classes["identical"] + classes["mismatch"] >= 1,
                                       "keys": classes["identical"] + classes["mismatch"],
                                       "dev_kept_checked_against_dev_genesis": dev is not None}
    ok = not mismatches and all(c["ok"] for c in checks.values())
    head = {"best": int(fork.call("chain_getHeader")["number"], 16),
            "finalized": int(fork.call("chain_getHeader", [fork.call("chain_getFinalizedHead")])["number"], 16)}
    return Report(ok=ok, seed=seed, draws=len(draws), unique_keys=len(keys), classes=dict(sorted(classes.items())),
                  mismatches=mismatches, checks=checks, sample=rows, fork_head=head)


def _u128(value: str | None) -> int | None:
    return int.from_bytes(_b(value), "little") if value else None


def _short(value: str | None) -> str | None:
    if value is None or len(value) <= 74:
        return value
    return f"{value[:34]}…{value[-8:]} ({len(value) // 2 - 1} B)"


def verify_v3(manifest: dict, source: RpcClient, fork: RpcClient, lifted: dict[str, str], *, sample: int = 200,
              seed: int | None = None, index: StorageIndex | None = None) -> Report:
    """A Chopsticks fork (of the V3 testnet, or of mainnet for ``--runtime exact``) against the live chain at
    the pin and against Forklift's own lift.

    - the fork's whole key set at its fork block equals the lifted key set (count and set hash);
    - every lifted value equals the fork's value at its fork block (all keys, not a sample);
    - ``sample`` keys drawn **on the live chain** (as on the mainnet path) are identical on the fork;
    - genesis and runtime ``:code`` hash equal the V3 profile's (exact: mainnet's own ``:code`` at the pin).

    On mainnet the key set skips the MerkleMountainRange range, as the lift does (~5.4 M keys); its two
    plain values are compared on both sides instead.

    There is no splice on this path, so there is no dev-kept, override or reset class. One executor
    boundary is named instead of hidden: Chopsticks 1.5.2 answers ``null`` for the top-trie value of a
    child-trie root (``:child_storage:default:…``, Revive contract storage on V3) that the live node
    serves. Those keys are counted as ``child-root-not-served`` and listed; anything else that differs
    is a mismatch (exit 2), named by storage item.
    """
    from forklift.chains import CHAINS
    from forklift.lift import mmr_summary, page_keys

    src, fk = manifest["source"], manifest["fork"]
    chain = CHAINS[fk.get("chain", "v3")]
    pinned_code = chain.code_hash or src["code_hash"]
    pin = src["block_hash"]
    fork_block = fk["fork_block"]["hash"]
    genesis = fork.call("chain_getBlockHash", [0])
    if str(genesis).lower() != chain.genesis_hash or genesis != fk["genesis_hash"]:
        raise VerifyError(f"{fork.endpoint} serves genesis {genesis}, not the {chain.label} fork in this manifest")
    if fork_block != pin:
        raise VerifyError(f"the manifest's fork block {fork_block} is not its pin {pin}")
    if seed is None:
        seed = int.from_bytes(os.urandom(8), "little")
    rng = random.Random(seed)
    if index is None:
        index = StorageIndex(decode_hex(source.call("state_getMetadata", [pin])))
    per_item = {k: v for k, v in manifest["load"]["per_item"].items() if "unknown:" not in k and not k.startswith(":child_storage:")}  # no prefix to draw in (child-trie roots are named by their raw key); still in the full compare
    prefixes = item_prefixes(index, per_item)
    draws = draw_sample(source, pin, prefixes, per_item, sample, rng)
    keys = sorted({k for _, k in draws})
    on_source = read_values(source, keys, pin)
    on_fork = read_values(fork, keys, fork_block)
    classes: Counter[str] = Counter()
    mismatches: list[dict] = []
    rows = []
    for name, key in draws:
        same = on_source[key] == on_fork[key]
        classes["identical" if same else "mismatch"] += 1
        rows.append({"item": name, "key": key, "class": "identical" if same else "mismatch"})
        if not same and not any(m["key"] == key for m in mismatches):
            mismatches.append({"item": name, "key": key, "source": _short(on_source[key]),
                               "fork": _short(on_fork[key]), "why": "sampled key differs from the live chain"})

    fork_set = page_keys(fork, fork_block).keys if chain.id == "mainnet" else fork_keys(fork, fork_block)
    lift_set = sorted(lifted)
    added = sorted(set(fork_set) - set(lift_set))
    missing = sorted(set(lift_set) - set(fork_set))
    for key in added[:20] + missing[:20]:
        mismatches.append({"item": index.item_of(_b(key)), "key": key, "source": None, "fork": None,
                           "why": "on the fork, not in the lift" if key in added else "in the lift, not on the fork"})
    all_fork = read_values(fork, lift_set, fork_block)
    differs = [k for k in lift_set if all_fork.get(k) != lifted[k]]
    child_roots = [k for k in differs if _b(k).startswith(CHILD_PREFIX) and all_fork.get(k) is None]
    differs = [k for k in differs if k not in child_roots]
    for key in differs[:20]:
        mismatches.append({"item": index.item_of(_b(key)), "key": key, "source": _short(lifted[key]),
                           "fork": _short(all_fork.get(key)), "why": "lifted value differs from the fork's"})
    code = fork.call("state_getStorage", [_h(CODE), fork_block])
    code_hash = _h(blake2_256(_b(code))) if code else None
    checks = {
        "key_set": {"fork": len(fork_set), "lift": len(lift_set), "added": len(added), "missing": len(missing),
                    "fork_set_hash": genesis_key_set_hash(fork_set), "lift_set_hash": genesis_key_set_hash(lift_set),
                    "ok": not added and not missing},
        "all_lifted_values": {"compared": len(lift_set),
                              "identical": len(lift_set) - len(differs) - len(child_roots),
                              "child_root_not_served": len(child_roots), "child_root_keys": child_roots,
                              "ok": not differs},
        "runtime": {"fork_code_hash": code_hash, "pinned": pinned_code, "ok": code_hash == pinned_code},
        "genesis": {"fork": genesis, "pinned": chain.genesis_hash, "ok": True},
    }
    if chain.id == "mainnet":
        on_chain, on_fork_mmr = mmr_summary(source, pin), mmr_summary(fork, fork_block)
        checks["mmr_range"] = {"source": on_chain, "fork": on_fork_mmr, "ok": on_chain == on_fork_mmr}
    ok = not mismatches and all(c["ok"] for c in checks.values())
    head = fork.call("chain_getHeader")
    return Report(ok=ok, seed=seed, draws=len(draws), unique_keys=len(keys), classes=dict(classes),
                  mismatches=mismatches, checks=checks, sample=rows,
                  fork_head={"number": int(head["number"], 16), "fork_block": fork_block})
