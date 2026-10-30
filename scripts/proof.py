"""Regenerate docs/PROOF.md from evidence/*.json. Never edit PROOF.md by hand.

    python scripts/proof.py [--check]

--check exits 1 if docs/PROOF.md differs from what the evidence produces.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
PROOF = ROOT / "docs" / "PROOF.md"


def _load(pattern: str) -> list[dict]:
    rows = []
    for p in sorted(EVIDENCE.glob(pattern)):
        d = json.loads(p.read_text())
        d["_file"] = f"evidence/{p.name}"
        rows.append(d)
    return rows


def _short(v) -> str:
    if isinstance(v, dict) and isinstance(v.get("data"), dict) and "free" in v["data"]:
        return f"free {v['data']['free']:,} planck, nonce {v['nonce']}"
    if isinstance(v, dict) and "targets" in v:
        return f"{len(v['targets'])} target(s) " + ", ".join(f"`{t[:6]}…{t[-4:]}`" for t in v["targets"]) \
            + f" (submitted in era {v.get('submitted_in')})"
    return f"`{json.dumps(v)}`"


def _tpot(planck) -> str:
    if planck is None:
        return "n/a"
    whole, frac = divmod(abs(int(planck)), 10 ** 14)
    txt = f"{whole}.{frac:014d}".rstrip("0").rstrip(".")
    return ("-" if int(planck) < 0 else "") + txt + " tPOT"


def render_v3() -> list[str]:
    """The V3 testnet rows: identity, lifts, Chopsticks forks, verify, and predicted-vs-actual transactions."""
    sys.path.insert(0, str(ROOT))
    from forklift.chains import V3

    lifts = sorted(_load("lift-v3-*.json"), key=lambda d: d["pin"]["block_number"])
    forks = sorted(_load("fork-v3-*.json"), key=lambda d: d["pin"]["block_number"])
    verifies = sorted(_load("verify-v3-*.json"), key=lambda d: d["pin"]["block_number"])
    rehearsals = [r for d in _load("rehearse-v3.json") for r in d["runs"]]
    replays = [r for d in _load("replay-v3.json") for r in d["runs"]]
    receipts = [r for d in _load("receipt-v3.json") for r in d["runs"]]
    if not (lifts or forks or verifies or rehearsals or replays or receipts):
        return []
    out = [
        "## Portaldot V3 testnet (profile `v3`)",
        "",
        f"Chain identity pinned in `forklift/chains.py` and checked by every V3 command: genesis `{V3.genesis_hash}`,",
        f"runtime `:code` hash `{V3.code_hash}`, `{V3.spec[0]}`/{V3.spec[1]}, {V3.symbol}, {V3.decimals} decimals,",
        f"endpoint `{V3.endpoint}`. Lifts read the live chain at a fixed block hash. A V3 fork runs V3's own",
        "runtime under Chopsticks (there is no public V3 node binary), labelled `fork-v3`. Transactions marked",
        "**V3 testnet** were sent to the live chain; their fork rows are the predictions made first.",
        "",
    ]
    if lifts:
        out += ["### Lifts (V3)", "",
                "| Pin block | Block hash | Keys lifted | Child-trie roots (not descended) | Seconds (keys / values / total) | Runs, same root | manifest_root | Evidence |",
                "|---|---|---|---|---|---|---|---|"]
        for d in lifts:
            s = d["runs"][-1]["seconds"]
            child = sum(v for k, v in d["per_item"].items() if k.startswith(":child_storage:"))
            out.append(f"| #{d['pin']['block_number']:,} | `{d['pin']['block_hash']}` | {d['keys_lifted']:,} | {child} | "
                       f"{s['keys_s']} / {s['values_s']} / {s['total_s']} | {len(d['runs'])}, "
                       f"{'yes' if d['roots_identical_across_runs'] else '**NO**'} | `{d['manifest_root']}` | `{d['_file']}` |")
        out.append("")
    if forks:
        out += ["### Forks (`forklift up --target v3`)", "",
                "| Pin block | Executor | Fork genesis | Fork `:code` | Starts at the pin | Ready (s) | Evidence |",
                "|---|---|---|---|---|---|---|"]
        for d in forks:
            f = d["fork"]
            out.append(f"| #{d['pin']['block_number']:,} | `{f['executor']}` (allow-unresolved-imports; without it: "
                       f"`{f['unresolved_import_without_flag']}`) | `{f['genesis_hash']}` | `{f['code_hash']}` | "
                       f"{'yes' if f['fork_block']['hash'] == d['pin']['block_hash'] else '**NO**'} | "
                       f"{d['timings']['ready_s']} | `{d['_file']}` |")
        out.append("")
    if verifies:
        out += ["### Verify (V3 fork vs the live chain and the lift)", "",
                "| Pin block | Runs | Sampled draws (seed) | Key set fork = lift | Lifted values identical | Child roots read null | Runtime | Exit | Evidence |",
                "|---|---|---|---|---|---|---|---|---|"]
        for d in verifies:
            r = d["runs"][-1]
            c = r["checks"]
            av = c["all_lifted_values"]
            out.append(f"| #{d['pin']['block_number']:,} | {len(d['runs'])} | {r['draws']} → {', '.join(f'{k} {v}' for k, v in r['classes'].items())} (seed {r['seed']}) | "
                       f"{c['key_set']['fork']:,} = {c['key_set']['lift']:,}: {'yes' if c['key_set']['ok'] else '**NO**'} | "
                       f"{av['identical']:,} of {av['compared']:,} | {av.get('child_root_not_served', 0)} | "
                       f"{'pinned' if c['runtime']['ok'] else '**differs**'} | {r['exit_code']} | `{d['_file']}` |")
        out.append("")
    sent = [r for r in rehearsals if r.get("actual")]
    if sent:
        out += ["### Transactions: predicted on the fork, then sent to the V3 testnet", "",
                "Each row is one extrinsic, signed once: the same bytes (same tx hash) were applied on the V3 fork",
                "first, then sent to the live V3 testnet. `compare` lists every field that differs.", "",
                "| Call | Signer | Tx hash | V3 testnet block (extrinsic) | Block hash | Finalized | Inclusion predicted / actual | Fee predicted / actual | Events match | Compare | Explorer |",
                "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in sent:
            p, a, c = r["predicted"], r["actual"], r["compare"]
            ev = next(x for x in c["rows"] if x["field"] == "event_sequence")
            out.append(f"| `{r['call']}` | `{r['signer'][:8]}…{r['signer'][-4:]}` | `{a['tx_hash']}` | "
                       f"#{a['block_number']:,} ({a['extrinsic_id']}) | `{a['block_hash']}` | {'yes' if a['finalized'] else 'no'} | "
                       f"#{p['block_number']:,} / #{a['block_number']:,} | "
                       f"{_tpot(p['fee'])} / {_tpot(a['fee'])} | {'yes' if ev['match'] else '**no**'} | "
                       f"{c['fields'] - len(c['differs'])}/{c['fields']}"
                       + ("" if c["match"] else " · differs: " + ", ".join(c["differs"])) + f" | [node-console]({a['explorer']}) |")
        out += ["", "Balance deltas (predicted / actual, planck):", ""]
        for r in sent:
            for acct, d in r["predicted"]["deltas"].items():
                out.append(f"- `{r['call']}` `{acct[:8]}…{acct[-4:]}`: {d:,} / {r['actual']['deltas'].get(acct):,}")
        out.append("")
    if replays:
        out += ["### Replays: the same extrinsic on a fork of its parent block (`forklift replay`)", "",
                "The chain's own extrinsic bytes, re-applied on a Chopsticks fork started at the parent of the block that",
                "included them, compared with what the V3 testnet recorded. Nothing is sent.", "",
                "| Extrinsic | Tx hash | Fork parent block | Fee chain / replay | Compare | Exit | Explorer |",
                "|---|---|---|---|---|---|---|"]
        for r in replays:
            a, p, c = r["actual"], r["replay"], r["compare"]
            out.append(f"| {r['extrinsic_id']} | `{a['tx_hash']}` | #{r['fork_parent']['number']:,} `{r['fork_parent']['hash']}` | "
                       f"{_tpot(a['fee'])} / {_tpot(p['fee'])} | {c['fields'] - len(c['differs'])}/{c['fields']}"
                       + ("" if c["match"] else " · differs: " + ", ".join(c["differs"]))
                       + f" | {r['exit_code']} | [node-console]({a['explorer']}) |")
        out.append("")
    if receipts:
        out += ["### Fork Receipt v2: a Revive contract on the V3 testnet (`forklift receipt --target v3`)", "",
                "`forklift/contracts/ForkReceipt.yul` (interface `IForkReceipt.sol`), EVM bytecode run by V3's Revive pallet.",
                "`attest` stores the manifest root and key count under the pinned block hash and emits all three plus the",
                "attester. Each write was dry-run through `ReviveApi` first; the deposit column is the dry run, the funder",
                "delta is what the chain charged (deposit held + fee). `stored` is `receipt(blockHash)` read at the attest block.", ""]
        for r in receipts:
            m = r["manifest"]
            out += [f"Contract `{r.get('address')}` · attester `{r['signer']}` = `{r['attester_h160']}` · "
                    f"recorded {r.get('recorded_utc')} · exit {r['exit_code']}"
                    + ("" if not r.get("mismatch") else " · mismatch: " + ", ".join(r["mismatch"])), "",
                    "| Step | Call | V3 testnet block (extrinsic) | Tx hash | Finalized | Deposit (dry run) | Fee | Funder delta | Explorer |",
                    "|---|---|---|---|---|---|---|---|---|"]
            for kind in ("deploy", "attest"):
                st = r.get(kind)
                if st:
                    delta = st["deltas"].get(r["signer"])
                    out.append(f"| {kind} | `{st['call']}` | #{st['block_number']:,} ({st['extrinsic_id']}) | `{st['tx_hash']}` | "
                               f"{'yes' if st['finalized'] else 'no'} | {_tpot(st['dry_run']['storage_deposit'])} | "
                               f"{_tpot(st['fee'])} | {_tpot(delta)} | [node-console]({st['explorer']}) |")
            ev = (r.get("attest") or {}).get("emitted") or []
            d = ev[0]["decoded"] if ev and ev[0].get("decoded") else {}
            stored = r.get("stored") or {}
            out += ["", "| Field | Manifest (`" + str(m.get("profile")) + f"` #{m['block_number']:,}) | Emitted event | Stored `receipt()` |",
                    "|---|---|---|---|",
                    f"| block hash | `{m['block_hash']}` | `{d.get('block_hash')}` | (the lookup key) |",
                    f"| manifest root | `{m['manifest_root']}` | `{d.get('manifest_root')}` | `{stored.get('manifest_root')}` |",
                    f"| keys lifted | {m['keys_lifted']:,} | {d.get('keys_lifted', 0):,} | {stored.get('keys_lifted', 0):,} |",
                    f"| attester (H160) | `{r['attester_h160']}` | `{d.get('attester')}` | |", ""]
        out += ["Re-read any of them by extrinsic id: `forklift tx <block>-<index> --target v3` (decodes the Receipt",
                "event and reads `receipt()` at the finalized head).", ""]
    for d in _load("revive-deposits-v3.json"):
        sf, lf = d["code_size_fit"], d["slot_fit"]
        out += [f"Revive deposits on V3, from `ReviveApi_instantiate` dry runs at #{d['at']['number']:,} "
                f"(`scripts/revive_deposits.py`, `{d['_file']}`): {_tpot(sf['fixed_planck'])} + "
                f"{_tpot(sf['per_unit_planck'])} per byte of runtime code; {_tpot(lf['per_unit_planck'])} per 32-byte slot written.", "",
                "| Build | Bytes | Deposit (dry run) | Note |", "|---|---|---|---|"]
        for b in d["builds"]:
            note = (f"code already on chain: its {_tpot(b['stored_code_deposit_planck'])} code deposit not charged again"
                    if b.get("code_already_stored") else "" if b["ok"] else f"dry run failed: `{b['error']}`")
            out.append(f"| {b['label']} | {b['bytes']:,} | {_tpot(b['storage_deposit_planck'])} | {note} |")
        out.append("")
    unsent = [r for r in rehearsals if not r.get("actual")]
    if unsent:
        out += ["### Predictions not sent", "", "| Call | Signer | Fork outcome | Why not sent |", "|---|---|---|---|"]
        for r in unsent:
            p = r["predicted"]
            outcome = (f"pool rejected: `{json.dumps(p['pool_rejected'])}`" if "pool_rejected" in p
                       else f"{p['extrinsic_id']} {'ok' if p['success'] else 'failed'}, fee {_tpot(p['fee'])}")
            why = "predicted failure" if not p["success"] else "run without --send"
            out.append(f"| `{r['call']}` | `{r['signer'][:8]}…{r['signer'][-4:]}` | {outcome} | {why} |")
        out.append("")
    return out


def render() -> str:
    lifts = sorted(_load("lift-[0-9]*.json"), key=lambda d: d["pin"]["block_number"])
    complete = {d["pin"]["block_number"]: d for d in _load("lift-completeness-*.json")}
    benches = _load("bench-lift-*.json")
    bench_runs = _load("bench-runs-*.json")
    out = [
        "# PROOF",
        "",
        "Generated by `scripts/proof.py` from `evidence/*.json`. Do not edit by hand.",
        "",
        "Mainnet-path lift rows are measured runs against the public Portaldot mainnet node",
        "(`wss://mainnet.portaldot.io`, runtime spec 1002), read-only, at a fixed block hash:",
        "profile `mainnet-read (spec 1002)`. Fork rows are local forks of that state on the `portaldot_dev`",
        "2.0.0 (the `portaldot-testnet-*` tarballs in the community repo `portaldotVolunteer/Portaldot-node`, not a GitHub Release; sha256 pinned in",
        "`forklift/node.py`), labelled `fork-1002-fast` (dev runtime wasm). A local fork is not the",
        "Portaldot V3 testnet.",
        "",
    ]
    out += render_v3()
    out += [
        "## Mainnet (spec 1002) path",
        "",
        "Everything below is the mainnet-state path (1002 lift → `fork-1002-fast`), kept as the control.",
        "",
        "## Lifts",
        "",
        "| Pin block | Block hash | Keys lifted | MMR range skipped (derived) | Key pages | Seconds (keys / values / total) | Runs, same root | manifest_root | Evidence |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for d in lifts:
        last = d["runs"][-1]
        s = last["seconds"]
        sk = (d.get("skipped") or {}).get("keys_in_range_derived") or 0
        same = "yes" if d["roots_identical_across_runs"] else "**NO**"
        out.append(
            f"| #{d['pin']['block_number']:,} | `{d['pin']['block_hash']}` | {d['keys_lifted']:,} | "
            f"{sk:,} | {last['key_pages']} | {s['keys_s']} / {s['values_s']} / {s['total_s']} | "
            f"{len(d['runs'])}, {same} | `{d['manifest_root']}` | `{d['_file']}` |"
        )
    out += [
        "",
        "## Completeness (I1b)",
        "",
        "The lifted key **set** compared with an independent listing of every key at the same block hash",
        "(no skip), minus the MerkleMountainRange range.",
        "",
        "| Pin block | Lifted | Listing outside MMR | MMR range keys (listed / derived) | Missing from lift | Extra in lift | Listing time | Pass | Evidence |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for n in sorted(complete):
        c = complete[n]
        out.append(
            f"| #{n:,} | {c['lifted_keys']:,} | {c['listing_keys_outside_mmr']:,} | "
            f"{c['mmr_range_keys']:,} / {c['mmr_range_keys_derived_by_lift'] or 0:,} | {c['missing_from_lift']} | "
            f"{c['extra_in_lift']} | {c['listing_pages']:,} pages, {c['listing_seconds']} s | "
            f"{'yes' if c['pass'] else '**NO**'} | `{c['_file']}` |"
        )
    # *-1002-exact-*.json (a Chopsticks fork, `up --runtime exact`) has another shape (no splice accounting,
    # other checks): these tables cover the spliced `fast` forks only
    forks = sorted((d for d in _load("fork-[0-9]*.json") if "accounting" in d), key=lambda d: d["pin"]["block_number"])
    verifies = sorted((d for d in _load("verify-[0-9]*.json") if "executor" not in d),
                      key=lambda d: d["pin"]["block_number"])
    if forks:
        out += [
            "",
            "## Forks (`forklift up`)",
            "",
            "Lifted keys = genesis-identical + dev-kept + overridden (+ reset), per the splice rules. Times are",
            "seconds from node launch, read over RPC.",
            "",
            "| Pin block | Label | Fork genesis hash | Lifted = identical + dev-kept + overridden | Genesis keys | "
            "RPC up / first new block / finalized #1 (s) | Peers | Boots, same genesis | Evidence |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for d in forks:
            a, last = d["accounting"], d["runs"][-1]
            b = last["boot"]
            same = "yes" if d["genesis_hash_stable_across_runs"] else "**NO**"
            out.append(
                f"| #{d['pin']['block_number']:,} | `{last['label']}` | `{last['genesis_hash']}` | "
                f"{a['keys_lifted']:,} = {a['genesis_identical']:,} + {a['dev_kept']:,} + {a['overridden']} | "
                f"{last['genesis_keys']:,} | {b['rpc_ready_s']} / {b['first_block_s']} / {b['finalized_s']} | "
                f"{b['peers']} | {len(d['runs'])}, {same} | `{d['_file']}` |"
            )
    if verifies:
        out += [
            "",
            "## Verify (I1, I2, I4)",
            "",
            "Keys drawn from mainnet at the pin inside storage maps (weighted by per-item counts), read on the",
            "fork at genesis. Exit 0 = every key identical or explained, and the fork's genesis key set, issuance,",
            "the five overrides and both runtime code hashes match the manifest. Re-run a row with its seed.",
            "",
            "| Pin block | Fork genesis hash | Fork finalized at verify | Draws → keys | Classes | Key set / issuance / overrides / code | Exit | Seed | Evidence |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for d in verifies:
            for r in d["runs"]:
                chk = r["checks"]
                flags = " / ".join("n/a" if c not in chk else "ok" if chk[c]["ok"] else "**FAIL**"
                                   for c in ("genesis_key_set", "issuance", "overrides", "runtime"))
                head = r.get("fork_head_at_verify")
                fin = f"#{head['finalized']:,}" if head else "not recorded"
                classes = ", ".join(f"{k} {v}" for k, v in r["classes"].items())
                out.append(
                    f"| #{d['pin']['block_number']:,} | `{r['fork_genesis_hash']}` | {fin} | "
                    f"{r['draws']} → {r['unique_keys']} | {classes} | {flags} | {r['exit_code']} | "
                    f"{r['seed']} | `{d['_file']}` |"
                )
    for d in sorted(_load("txs-[0-9]*.json"), key=lambda d: d["pin"]["block_number"]):
        refetch = {}
        rp = EVIDENCE / d["_file"].split("/")[-1].replace("txs-", "refetch-")
        if rp.exists():
            rf = json.loads(rp.read_text())
            refetch = {r["extrinsic_id"]: (r["ok"], rf["refetched_utc"]) for r in rf["rows"]}
        rows = [r for r in d["rows"] if r.get("persistent_base_path")]
        if not rows:
            continue
        out += [
            "",
            f"## Transactions on the fork (pin #{d['pin']['block_number']:,})",
            "",
            "Every extrinsic is signed by the fork's dev sudo key `//Alice` (the `Sudo.Key` override) on a local",
            "fork with a persistent archive database; block numbers and hashes are on that fork, not on mainnet.",
            "`as` rows go through `Sudo.sudo_as`, which dispatches the inner call with the target account's",
            "origin: the target pays no fee and its nonce is not touched (Alice's outer extrinsic pays and is",
            "checked as usual). Re-fetched = read again by extrinsic id by `scripts/refetch.py`, run as its own",
            "process after the commands: same block hash, tx hash and outcome (and the Sudo-carried result).",
            "",
            "| Command | Call | Target (`fund`/`as`) or signer | Extrinsic | Block hash | Tx hash | Result | Label | Re-fetched |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            disp = r.get("dispatch") or {}
            result = ("Ok" if r["success"] and disp.get("ok", True) else "**Err** " + str(disp.get("error")))
            ok, when = refetch.get(r["extrinsic_id"], (None, None))
            ref = "not yet" if ok is None else f"yes ({when})" if ok else "**DIFFERS**"
            who = r["who"]
            out.append(
                f"| {'`receipt` (' + r['kind'] + ')' if r['kind'] in ('instantiate', 'rent_top_up', 'attest') else '`' + r['kind'] + '`'}"
                f" | `{r['call']}` | `{who[:6]}…{who[-4:]}` | `{r['extrinsic_id']}` | "
                f"`{r['block_hash']}` | `{r['tx_hash']}` | {result} | `{r['label']}` | {ref} |"
            )
        changes = [r for r in rows if r["kind"] in ("fund", "as") and r.get("changed")]
        if changes:
            out += ["", "What changed (read on the fork before the call and at the block that included it):", ""]
            for r in changes:
                for item, ch in r["changed"].items():
                    out.append(f"- `{r['extrinsic_id']}` {item}: {_short(ch['before'])} → {_short(ch['after'])}")
        receipts = [r for r in rows if r["kind"] in ("instantiate", "rent_top_up", "attest")]
        if receipts:
            att = [r for r in receipts if r["kind"] == "attest"][-1:]
            inst = [r for r in receipts if r["kind"] == "instantiate"][:1]
            top = [r for r in receipts if r["kind"] == "rent_top_up"]
            out += ["", "### Fork Receipt contract", ""]
            if inst:
                out.append(f"- Contract address: `{inst[0]['contract']}` (code hash `{inst[0]['code_hash']}`), "
                           f"instantiated in `{inst[0]['extrinsic_id']}`, tx `{inst[0]['tx_hash']}`.")
            for t in top:
                out.append(f"- Rent check, `{t['extrinsic_id']}`: {t['why']}.")
            for a in att:
                em = (a.get("emitted") or [{}])[0].get("decoded") or {}
                proof = a.get("rent_proof") or {}
                out.append(f"- Attest `{a['extrinsic_id']}`, tx `{a['tx_hash']}`: `Contracts.ContractEmitted` = mainnet "
                           f"block hash `{em.get('mainnet_block_hash')}` ‖ manifest root `{em.get('manifest_root')}` ‖ "
                           f"{em.get('keys_lifted', 0):,} keys lifted; equals the manifest: "
                           f"{'yes' if a.get('matches_manifest') else '**NO**'}.")
                if proof:
                    out.append(f"- Rent measured across the attest: `rent_paid` {proof['rent_paid_before']:,} → "
                               f"{proof['rent_paid_after']:,} planck, `deduct_block` {proof['deduct_block_before']} → "
                               f"{proof['deduct_block_after']}, balance {proof['free_before_attest']:,} → "
                               f"{proof['free_after_attest']:,} planck: "
                               f"{'no rent charged' if proof['no_rent_charged'] else '**rent charged**'}.")
        out.append(f"\nEvidence: `{d['_file']}`" + (f", `evidence/{rp.name}`" if rp.exists() else ""))
    soaks = _load("soak-*.json")
    if soaks:
        out += [
            "",
            "## Soak",
            "",
            "A fork left running on a persistent database, checked over RPC. An epoch is "
            "`Babe.EpochDuration` slots of the dev runtime (1,200 x 6 s = 2 h).",
            "",
            "| Fork | Genesis hash | Started (UTC) | Checked (UTC) | Measured | Epoch index | Epoch transitions (block) | "
            "Authoring / finalizing at check | Peers | Pass | Evidence |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for d in soaks:
            tr = ", ".join(f"epoch {t['epoch']} at #{t['block']:,}" for t in d["epochs"]["transitions"]) or "none"
            out.append(
                f"| `{d['fork']['label']}` `{d['fork']['name']}` | `{d['fork']['genesis_hash']}` | {d['started_utc']} | "
                f"{d['checked_utc']} | {d['measured_hours']} h | {d['epochs']['epoch_index']} | {tr} | "
                f"best #{d['authoring']['best_before']:,} → #{d['authoring']['best_after']:,}, finalized "
                f"#{d['finalizing']['finalized_before']:,} → #{d['finalizing']['finalized_after']:,} | "
                f"{d['peers']} | {'yes' if d['pass'] else '**NO**'} | `{d['_file']}` |"
            )
    if bench_runs:
        b = bench_runs[-1]
        out += [
            "",
            f"## Bench (p50 / p95 over {max(s['runs_all'] for s in b['scenarios'].values())} fresh-process runs)",
            "",
            f"Recorded {b['recorded_utc']} on {b['host']['cpu']}; full tables in [`docs/BENCHMARKS.md`](BENCHMARKS.md).",
            "",
            "| Command | Against | Runs ok / all | p50 s | p95 s | Evidence |",
            "|---|---|---|---|---|---|",
        ]
        for s in b["scenarios"].values():
            out.append(f"| `{s['command']}` | {s['against']} | {s['runs_ok']} / {s['runs_all']} | "
                       f"{s.get('p50_s', 'n/a')} | {s.get('p95_s', 'n/a')} | `{b['_file']}` |")
    if benches:
        out += [
            "",
            "## Bench, earlier single lift run (2026-10-14)",
            "",
            "| Date (UTC) | Pin block | Keys | keys s | values s | total s | Host | Evidence |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for b in benches:
            h = b["host"]
            out.append(
                f"| {b['recorded_utc']} | #{b['pin']['block_number']:,} | {b['keys_lifted']:,} | {b['keys_s']} | "
                f"{b['values_s']} | {b['total_s']} | {h['system']} {h['machine']}, Python {h['python']} | "
                f"`{b['_file']}` |"
            )
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    text = render()
    if ap.parse_args().check:
        same = PROOF.exists() and PROOF.read_text() == text
        print("PROOF.md is current" if same else "PROOF.md is stale: run scripts/proof.py")
        return 0 if same else 1
    PROOF.parent.mkdir(exist_ok=True)
    PROOF.write_text(text)
    print(f"wrote {PROOF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
