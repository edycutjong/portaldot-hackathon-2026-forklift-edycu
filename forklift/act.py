"""Act on a fork as root: ``fund`` (Sudo → Balances.set_balance) and ``as`` (Sudo → sudo_as).

Both sign with the fork's dev sudo key (``//Alice``, set by the splice override
``Sudo.Key``) and go through a fork client, so the I3 write-guard refuses any
endpoint that is not a loopback fork of a non-mainnet genesis.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from forklift import tx
from forklift.rpc import RpcClient

EXIT_DISPATCH_FAILED = 5  # the extrinsic landed, but the call it carried returned an error

SUDO_AS_NOTE = ("sudo_as dispatches the call with the account's own origin, but skips what a signed "
                "extrinsic would run: no fee is withdrawn, the nonce is not checked or bumped, and no "
                "signed extension runs.")

# Calls whose behaviour depends on the block number, which restarts at 1 on a fork (spec §Boundaries).
_HEIGHT_WARNINGS = {
    ("Vesting", None): "Vesting schedules start at mainnet block heights; on a fork the block number restarts "
                       "at 1, so every schedule reads as fully locked and vest() re-locks tokens that are "
                       "spendable on mainnet.",
    ("Proxy", "announce"): "Proxy announcement delays count from mainnet heights and never mature on a fork "
                           "whose block number restarted at 1.",
    ("Proxy", "proxy_announced"): "Proxy announcement delays count from mainnet heights and never mature on a "
                                  "fork whose block number restarted at 1.",
    ("Scheduler", None): "Scheduler agendas are keyed by mainnet block heights and never come due on a fork "
                         "whose block number restarted at 1.",
}


def nested_calls(call_value: Any) -> list[tuple[str, str]]:
    """Every (pallet, function) in a composed call, including calls nested in its arguments."""
    found: list[tuple[str, str]] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            if "call_module" in v and "call_function" in v:
                found.append((v["call_module"], v["call_function"]))
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(call_value)
    return found


def height_warnings(call_value: Any) -> list[str]:
    out: list[str] = []
    for pallet, function in nested_calls(call_value):
        for (p, f), text in _HEIGHT_WARNINGS.items():
            if p == pallet and f in (None, function) and text not in out:
                out.append(f"{pallet}.{function}: {text}")
    return out


def parse_call(spec: str) -> tuple[str, str]:
    """'Staking.nominate' → ('Staking', 'nominate')."""
    pallet, sep, function = spec.partition(".")
    if not sep or not pallet or not function:
        raise ValueError(f"expected Pallet.call, e.g. Balances.transfer, got {spec!r}")
    return pallet, function


def parse_args(rt: tx.Runtime, pallet: str, function: str, raw: str | None) -> dict[str, Any]:
    """JSON object of named args, or a JSON array in the metadata's argument order."""
    names = [a["name"] for a in rt.call_args(pallet, function)]
    if raw is None or raw.strip() == "":
        value: Any = {}
    else:
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"call arguments are not JSON: {exc}") from None
    if isinstance(value, list):
        if len(value) != len(names):
            raise ValueError(f"{pallet}.{function} takes {len(names)} argument(s) {names}, got {len(value)}")
        value = dict(zip(names, value))
    if not isinstance(value, dict):
        raise ValueError("call arguments must be a JSON object or array")
    missing, extra = set(names) - set(value), set(value) - set(names)
    if missing or extra:
        raise ValueError(f"{pallet}.{function} takes {names}; missing {sorted(missing)}, unknown {sorted(extra)}")
    return value


def read(client: RpcClient, rt: tx.Runtime, pallet: str, item: str, *keys: bytes, at: str | None = None) -> Any:
    key = rt.storage_key(pallet, item, *keys)
    raw = client.call("state_getStorage", [key, at] if at else [key])
    return rt.decode_storage(pallet, item, raw)


def account_info(client: RpcClient, rt: tx.Runtime, public: bytes, at: str | None = None) -> dict:
    return read(client, rt, "System", "Account", public, at=at)


def watch_reads(client: RpcClient, rt: tx.Runtime, public: bytes, pallets: set[str], at: str | None = None) -> dict:
    """The state an ``as`` call is expected to change, keyed by storage name."""
    out: dict[str, Any] = {"System.Account": account_info(client, rt, public, at)}
    if "Staking" in pallets:
        controller = read(client, rt, "Staking", "Bonded", public, at=at)
        out["Staking.Bonded"] = tx.as_ss58(controller)
        nominations = read(client, rt, "Staking", "Nominators", public, at=at)
        if nominations:
            nominations = {**nominations, "targets": [tx.as_ss58(t) for t in nominations["targets"]]}
        out["Staking.Nominators"] = nominations
        if controller:
            ledger = read(client, rt, "Staking", "Ledger", bytes.fromhex(controller[2:]), at=at)
            if ledger:
                ledger = {**ledger, "stash": tx.as_ss58(ledger["stash"])}
            out["Staking.Ledger"] = ledger
    return out


def delta(before: dict, after: dict) -> dict:
    return {k: {"before": before.get(k), "after": after.get(k)} for k in sorted(set(before) | set(after))
            if before.get(k) != after.get(k)}


@dataclass
class ActResult:
    kind: str  # fund | as
    who: str
    call: str
    inclusion: tx.Inclusion
    dispatch_ok: bool
    dispatch_error: str | None
    before: dict
    after: dict
    warnings: list[str] = field(default_factory=list)
    inner_events: list[dict] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.inclusion.success and self.dispatch_ok else EXIT_DISPATCH_FAILED

    def as_json(self) -> dict:
        return {"kind": self.kind, "who": self.who, "call": self.call,
                "dispatch": {"ok": self.dispatch_ok, "error": self.dispatch_error},
                "extrinsic": self.inclusion.as_json(), "changed": delta(self.before, self.after),
                "inner_events": self.inner_events, "warnings": self.warnings, "exit_code": self.exit_code}


def _wrapper_result(rt: tx.Runtime, inc: tx.Inclusion, event: str) -> tuple[bool, str | None]:
    if not inc.success:
        return False, inc.error
    res = tx.wrapper_result(rt, inc.success, inc.error, [e for e in inc.events if e.full_name == event])
    if res is None:
        return False, f"no {event} event"
    return res["ok"], res["error"]


def own_events(inc: tx.Inclusion) -> list[dict]:
    """The carried call's own events: everything but the wrapper, the outcome and the fee deposit.

    The fee goes to the treasury as the ``Treasury.Deposit`` right before ``ExtrinsicSuccess`` /
    ``ExtrinsicFailed``; a ``Treasury.Deposit`` the call itself emits stays.
    """
    events = list(inc.events)
    out = []
    for i, e in enumerate(events):
        if e.full_name in _WRAPPER_EVENTS:
            continue
        nxt = events[i + 1].full_name if i + 1 < len(events) else None
        if e.full_name == "Treasury.Deposit" and nxt in ("System.ExtrinsicSuccess", "System.ExtrinsicFailed"):
            continue
        out.append(e.as_json())
    return out


_WRAPPER_EVENTS = {"Sudo.Sudid", "Sudo.SudoAsDone", "System.ExtrinsicSuccess", "System.ExtrinsicFailed"}


def fund(client: RpcClient, rt: tx.Runtime, who: str, planck: int, *, signer=None) -> ActResult:
    """Set ``who``'s free balance to ``planck`` (its reserved balance is kept as it is)."""
    signer = signer or tx.dev_keypair()
    public = tx.ss58_to_public(who)
    before = {"System.Account": account_info(client, rt, public)}
    reserved = int(before["System.Account"]["data"]["reserved"])
    inner = rt.compose("Balances", "set_balance", {"who": who, "new_free": planck, "new_reserved": reserved})
    call = rt.compose("Sudo", "sudo", {"call": inner.value})
    inc = tx.sign_and_submit(client, rt, call, signer)
    after = {"System.Account": account_info(client, rt, public, at=inc.block_hash)}
    ok, err = _wrapper_result(rt, inc, "Sudo.Sudid")
    return ActResult(kind="fund", who=who, call="Sudo.sudo(Balances.set_balance)", inclusion=inc,
                     dispatch_ok=ok, dispatch_error=err, before=before, after=after,
                     inner_events=own_events(inc))


def act_as(client: RpcClient, rt: tx.Runtime, who: str, pallet: str, function: str, args: dict[str, Any], *,
           signer=None) -> ActResult:
    """Dispatch ``pallet.function(args)`` with ``who``'s origin through ``Sudo.sudo_as``."""
    signer = signer or tx.dev_keypair()
    public = tx.ss58_to_public(who)
    inner = rt.compose(pallet, function, args)
    warnings = height_warnings(inner.value)
    pallets = {p for p, _ in nested_calls(inner.value)}
    before = watch_reads(client, rt, public, pallets)
    call = rt.compose("Sudo", "sudo_as", {"who": who, "call": inner.value})
    inc = tx.sign_and_submit(client, rt, call, signer)
    after = watch_reads(client, rt, public, pallets, at=inc.block_hash)
    ok, err = _wrapper_result(rt, inc, "Sudo.SudoAsDone")
    return ActResult(kind="as", who=who, call=f"Sudo.sudo_as({pallet}.{function})", inclusion=inc,
                     dispatch_ok=ok, dispatch_error=err, before=before, after=after, warnings=warnings,
                     inner_events=own_events(inc))
