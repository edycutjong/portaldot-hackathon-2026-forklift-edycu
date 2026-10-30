"""Splice: the node's own dev chain spec with its genesis replaced by a lift.

The node's own ``build-spec --dev --raw`` output is a flat ``{key: value}``
map of genesis storage, so a splice is dict work:

- **dev** values stay for the consensus and authority pallets (Babe, Grandpa,
  Session incl. Historical, AuthorityDiscovery, ImOnline, Authorship), for
  ``:grandpa_authorities`` and ``:extrinsic_index``, and for every ``System``
  item except ``System.Account``. Mainnet's validators have no keys on this
  machine; the dev keys let ``--alice`` author.
- **reset**: the MerkleMountainRange prefix is empty on the fork.
- **mainnet** values (at the pin) everywhere else.
- five named **overrides**, each listed in the manifest with its reason.

Every lifted key ends up in exactly one class: genesis-identical, dev-kept or
override. ``accounting`` checks that the three add up to the lift (I1b).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from forklift.hashing import blake2_128, blake2_256, twox128

DEV_KEPT_PALLETS = ("Babe", "Grandpa", "Session", "AuthorityDiscovery", "ImOnline", "Authorship")
DEV_KEPT_WELL_KNOWN = (b":grandpa_authorities", b":extrinsic_index")
RESET_PALLETS = ("MerkleMountainRange",)

SYSTEM = twox128("System")
SYSTEM_ACCOUNT = SYSTEM + twox128("Account")
SUDO_KEY = twox128("Sudo") + twox128("Key")
FORCE_ERA = twox128("Staking") + twox128("ForceEra")
TOTAL_ISSUANCE = twox128("Balances") + twox128("TotalIssuance")
CODE = b":code"

FORCE_NONE = b"\x02"  # pallet_staking::Forcing::ForceNone

# //Alice (sr25519). Public: its seed is the best-known key in Substrate.
DEV_KEY = bytes.fromhex("d43593c715fdd31c61141abd04a99fd6822c8558854ccde39a5684e7a56da27d")
DEV_KEY_SS58 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"  # public address  gitleaks:allow

RUNTIMES = ("fast",)  # spliced into portaldot_dev; "exact" runs mainnet's own :code under Chopsticks (up.py)

# The dev key's balance on the exact fork: the same free balance it has in portaldot_dev 2.0.0's dev genesis
# (5,000,000,000,000,000,000 planck), without that genesis's staking locks, since nothing is staked here.
EXACT_DEV_FREE = 5_000_000_000_000_000_000

_DEV_PREFIXES = tuple(twox128(p) for p in DEV_KEPT_PALLETS)
_RESET_PREFIXES = tuple(twox128(p) for p in RESET_PALLETS)


class SpliceError(ValueError):
    pass


def _b(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str.removeprefix("0x"))


def _h(raw: bytes) -> str:
    return "0x" + raw.hex()


def account_key(account: bytes) -> bytes:
    """The ``System.Account`` storage key of an AccountId (Blake2_128Concat hasher)."""
    return SYSTEM_ACCOUNT + blake2_128(account) + account


def rule_of(key: bytes) -> str:
    """Which side a genesis key comes from: ``dev:<what>``, ``reset:<what>`` or ``mainnet``."""
    for name, prefix in zip(DEV_KEPT_PALLETS, _DEV_PREFIXES):
        if key.startswith(prefix):
            return f"dev:{name}"
    if key in DEV_KEPT_WELL_KNOWN:
        return "dev:" + key.decode()
    if key.startswith(SYSTEM) and not key.startswith(SYSTEM_ACCOUNT):
        return "dev:System"
    for name, prefix in zip(RESET_PALLETS, _RESET_PREFIXES):
        if key.startswith(prefix):
            return f"reset:{name}"
    return "mainnet"


def balance_of(account_info: bytes) -> tuple[int, int]:
    """(free, reserved) from a spec-1002 ``AccountInfo``: 4×u32 refcounts then ``AccountData`` (4×u128)."""
    if len(account_info) != 80:
        raise SpliceError(f"unexpected AccountInfo length {len(account_info)} (spec 1002 is 80 bytes)")
    data = account_info[16:]
    return int.from_bytes(data[0:16], "little"), int.from_bytes(data[16:32], "little")


def u128(value: int) -> bytes:
    return value.to_bytes(16, "little")


@dataclass
class Override:
    key: str
    name: str
    value: str
    reason: str
    mainnet_value: str | None  # None: the key does not exist on mainnet at the pin

    def as_json(self) -> dict:
        def show(v: str | None) -> str | None:
            # :code is 0.7-2.5 MB: record its hash and size, not the bytes.
            if v is not None and self.name == ":code":
                raw = _b(v)
                return f"blake2_256 {_h(blake2_256(raw))} ({len(raw):,} B)"
            return v
        return {"key": self.key, "name": self.name, "value": show(self.value),
                "mainnet_value": show(self.mainnet_value), "reason": self.reason}


@dataclass
class Splice:
    top: dict[str, str]
    overrides: list[Override]
    accounting: dict
    issuance: dict
    dev_code_hash: str
    rules: dict = field(default_factory=dict)


def account_info_1002(free: int, *, providers: int = 1) -> bytes:
    """A spec-1002 ``AccountInfo``: nonce, consumers, providers, sufficients (u32 each), then free, reserved,
    misc_frozen, fee_frozen (u128 each)."""
    return (0).to_bytes(4, "little") * 2 + providers.to_bytes(4, "little") + (0).to_bytes(4, "little") \
        + u128(free) + u128(0) * 3


def exact_overrides(mainnet: dict[str, str]) -> list[Override]:
    """The state overrides of ``--runtime exact``: the four of ``fast`` without ``:code``.

    Same keys, same purpose (root, no era rotation, a funded dev key, issuance = sum of balances), so a call
    rehearsed on both forks differs by the runtime only. Applied on top of the pinned block, never into it.
    """
    alice_key = _h(account_key(DEV_KEY))
    ti_key = _h(TOTAL_ISSUANCE)
    if ti_key not in mainnet:
        raise SpliceError("the lift has no Balances.TotalIssuance")
    old_total = sum(balance_of(_b(mainnet[alice_key]))) if alice_key in mainnet else 0
    added = EXACT_DEV_FREE - old_total
    ti_fork = int.from_bytes(_b(mainnet[ti_key]), "little") + added
    return [
        Override(_h(SUDO_KEY), "Sudo.Key", _h(DEV_KEY),
                 f"root on the fork: the dev key {DEV_KEY_SS58} (//Alice) replaces mainnet's sudo key",
                 mainnet.get(_h(SUDO_KEY))),
        Override(_h(FORCE_ERA), "Staking.ForceEra", _h(FORCE_NONE),
                 "ForceNone: the same era policy as the fast fork, so no era rotates while rehearsing",
                 mainnet.get(_h(FORCE_ERA))),
        Override(alice_key, "System.Account[dev key]", _h(account_info_1002(EXACT_DEV_FREE)),
                 f"the dev key holds {EXACT_DEV_FREE} planck free, as in the dev genesis, so the sudo key can pay fees",
                 mainnet.get(alice_key)),
        Override(ti_key, "Balances.TotalIssuance", _h(u128(ti_fork)),
                 f"mainnet issuance + {added} planck added with the dev key's account, so issuance = sum of balances",
                 mainnet[ti_key]),
    ]


def splice_state(mainnet: dict[str, str], dev: dict[str, str], *, runtime: str = "fast") -> Splice:
    """Build the fork's genesis storage from a lift (``mainnet``) and the dev genesis (``dev``)."""
    if runtime not in RUNTIMES:
        raise SpliceError(f"runtime {runtime!r} is not available (have: {', '.join(RUNTIMES)})")
    top: dict[str, str] = {}
    dev_kept_genesis: Counter[str] = Counter()
    dev_dropped: Counter[str] = Counter()
    for k, v in dev.items():
        rule = rule_of(_b(k))
        if rule.startswith("dev:"):
            top[k] = v
            dev_kept_genesis[rule[4:]] += 1
        else:
            dev_dropped[rule if rule != "mainnet" else "replaced by mainnet"] += 1
    mainnet_dev_kept: Counter[str] = Counter()
    mainnet_reset: Counter[str] = Counter()
    for k, v in mainnet.items():
        rule = rule_of(_b(k))
        if rule == "mainnet":
            top[k] = v
        elif rule.startswith("dev:"):
            mainnet_dev_kept[rule[4:]] += 1
        else:
            mainnet_reset[rule[6:]] += 1

    # The five overrides.
    alice_key = _h(account_key(DEV_KEY))
    if alice_key not in dev:
        raise SpliceError("the dev genesis has no System.Account for the dev key")
    old_total = sum(balance_of(_b(mainnet[alice_key]))) if alice_key in mainnet else 0
    dev_free, dev_reserved = balance_of(_b(dev[alice_key]))
    added = dev_free + dev_reserved - old_total
    ti_key = _h(TOTAL_ISSUANCE)
    if ti_key not in mainnet:
        raise SpliceError("the lift has no Balances.TotalIssuance")
    ti_mainnet = int.from_bytes(_b(mainnet[ti_key]), "little")
    ti_fork = ti_mainnet + added
    code_key = _h(CODE)
    if code_key not in dev:
        raise SpliceError("the dev genesis has no :code")

    overrides = [
        Override(_h(SUDO_KEY), "Sudo.Key", _h(DEV_KEY),
                 f"root on the fork: the dev key {DEV_KEY_SS58} (//Alice) replaces mainnet's sudo key",
                 mainnet.get(_h(SUDO_KEY))),
        Override(_h(FORCE_ERA), "Staking.ForceEra", _h(FORCE_NONE),
                 "ForceNone: mainnet validators have no session keys here and must never be rotated in",
                 mainnet.get(_h(FORCE_ERA))),
        Override(alice_key, "System.Account[dev key]", dev[alice_key],
                 "the dev key's dev-genesis AccountInfo, so the sudo key can pay fees on block 1",
                 mainnet.get(alice_key)),
        Override(ti_key, "Balances.TotalIssuance", _h(u128(ti_fork)),
                 f"mainnet issuance + {added} planck added with the dev key's account, so issuance = sum of balances",
                 mainnet[ti_key]),
        Override(code_key, ":code", dev[code_key],
                 "runtime fast: the dev runtime wasm (6 s slots); mainnet's wasm is recorded as source.code_hash",
                 mainnet.get(code_key)),
    ]
    for o in overrides:
        if rule_of(_b(o.key)) != "mainnet":
            raise SpliceError(f"override {o.name} falls under a dev-kept or reset rule")
        top[o.key] = o.value

    # I1b: every lifted key is genesis-identical, dev-kept or overridden, exactly once.
    override_keys = {o.key for o in overrides}
    identical = sum(1 for k, v in mainnet.items()
                    if k not in override_keys and rule_of(_b(k)) == "mainnet" and top.get(k) == v)
    lifted_overridden = sum(1 for k in mainnet if k in override_keys)
    differs = [k for k, v in mainnet.items()
               if k not in override_keys and rule_of(_b(k)) == "mainnet" and top.get(k) != v]
    if differs:
        raise SpliceError(f"{len(differs)} lifted keys changed outside the rules, first {differs[0]}")
    dev_kept_total = sum(mainnet_dev_kept.values())
    reset_total = sum(mainnet_reset.values())
    accounting = {
        "keys_lifted": len(mainnet),
        "genesis_identical": identical,
        "dev_kept": dev_kept_total,
        "dev_kept_per_prefix": dict(sorted(mainnet_dev_kept.items())),
        "overridden": lifted_overridden,
        "reset": reset_total,
        "sums": identical + dev_kept_total + lifted_overridden + reset_total == len(mainnet),
        "genesis_keys": len(top),
        "genesis_from_dev": sum(dev_kept_genesis.values()),
        "genesis_from_dev_per_prefix": dict(sorted(dev_kept_genesis.items())),
        "genesis_added_by_overrides": sum(1 for o in overrides if o.mainnet_value is None),
        "dev_genesis_keys_dropped": sum(dev_dropped.values()),
    }
    # Genesis = copied mainnet keys + overridden lifted keys + dev-kept dev keys + keys the overrides add.
    accounting["genesis_sums"] = (identical + lifted_overridden + accounting["genesis_from_dev"]
                                  + accounting["genesis_added_by_overrides"]) == len(top)
    if not accounting["sums"] or not accounting["genesis_sums"]:
        raise SpliceError(f"accounting does not add up: {accounting}")
    issuance = {
        "mainnet": str(ti_mainnet),
        "added": str(added),
        "added_accounts": [{"account": DEV_KEY_SS58, "free": str(dev_free), "reserved": str(dev_reserved),
                            "previous_total_on_mainnet": str(old_total)}],
        "fork": str(ti_fork),
    }
    rules = {
        "dev_kept": list(DEV_KEPT_PALLETS) + [k.decode() for k in DEV_KEPT_WELL_KNOWN]
        + ["System.* except System.Account"],
        "reset": list(RESET_PALLETS),
        "mainnet": "every other key, at the pin",
    }
    return Splice(top=top, overrides=overrides, accounting=accounting, issuance=issuance,
                  dev_code_hash=_h(blake2_256(_b(dev[code_key]))), rules=rules)


def genesis_key_set_hash(keys) -> str:
    """blake2_256 over the sorted genesis keys, each ``u32le(len) ‖ key`` (the fork's key-set fingerprint)."""
    import hashlib

    h = hashlib.blake2b(digest_size=32)
    for k in sorted(_b(k) for k in keys):
        h.update(len(k).to_bytes(4, "little"))
        h.update(k)
    return _h(h.digest())


def fork_chain_spec(dev_spec: dict, top: dict[str, str], *, block_number: int, runtime: str,
                    properties: dict | None = None, fork_name: str | None = None) -> dict:
    """The dev spec with the spliced genesis, its own id and no boot nodes (never peers with anything).

    The fork's name goes into the chain name (what ``system_chain`` answers), so two forks of the same
    pin are told apart at readiness. Names do not enter genesis: the genesis hash depends on state only.
    """
    spec = {k: v for k, v in dev_spec.items() if k != "genesis"}
    label = runtime if fork_name is None else f"{runtime}, {fork_name}"
    spec["name"] = f"Forklift fork of Portaldot mainnet #{block_number} ({label})"
    spec["id"] = f"forklift_1002_{block_number}_{runtime}"
    spec["protocolId"] = f"forklift-{block_number}"
    spec["bootNodes"] = []
    spec["telemetryEndpoints"] = None
    if properties:
        spec["properties"] = properties
    spec["genesis"] = {"raw": {"top": dict(sorted(top.items())), "childrenDefault": {}}}
    return spec


def load_dev_spec(path: Path) -> dict:
    try:
        spec = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise SpliceError(f"cannot read the dev chain spec {path}: {exc}") from exc
    try:
        top = spec["genesis"]["raw"]["top"]
    except (KeyError, TypeError):
        raise SpliceError(f"{path} is not a raw chain spec (no genesis.raw.top)") from None
    if spec["genesis"]["raw"].get("childrenDefault"):
        raise SpliceError("the dev spec has child tries; splicing them is not supported")
    if not isinstance(top, dict) or not top:
        raise SpliceError(f"{path}: empty genesis")
    return spec
