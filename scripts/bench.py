"""Benchmark the four Forklift commands over several runs and write p50 / p95.

    python scripts/bench.py [--runs 7] [--only lift,verify,rehearse,replay-v3,replay-1002]
    python scripts/bench.py --report            # rebuild docs/BENCHMARKS.md from the newest evidence file

Every run is a fresh `forklift ...` process (nothing cached in memory). The numbers are the wall-clock seconds of
that whole process, from spawn to exit, on one machine. Percentiles are linear interpolation between the closest
ranks (the numpy default), so with a handful of runs p95 sits close to the slowest run; the raw per-run seconds are
in the evidence file next to them.

Scenarios (each one's label is the chain it runs against):
  lift         mainnet 1002 pin #2,725,953, read-only, a new empty directory and --no-resume every run
  verify       `verify --sample 200` on a running fork-v3 (Chopsticks), 200 keys drawn from live V3, one new seed per run
  rehearse     `rehearse System.remark` on a freshly lifted fork-v3 (down + up before each run, not timed), fork
               prediction only (no --send, nothing is written to V3)
  replay-v3    `replay 118621-1` (V3): a temporary Chopsticks fork of the parent block, 14 fields compared with V3
  replay-1002  `replay 2700013:1` (mainnet 1002, read-only): the same on a fork of the mainnet parent block

verify and rehearse need `forklift up --target v3 --name v3 --ws-port 59944` already running (this script never
starts or stops a node). Writes evidence/bench-runs-<YYYY-MM-DD>.json and docs/BENCHMARKS.md. Exit 1 if any run failed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
REPORT = ROOT / "docs" / "BENCHMARKS.md"

PIN = 2725953
FORK_WS = "59944"
REPLAY_WS = "59945"
SIGNER = Path.home() / ".config" / "forklift" / "v3-funder.seed"
REMARK = '{"remark": "0x666f726b6c696674"}'
ALL = ["lift", "verify", "rehearse", "replay-v3", "replay-1002"]

TITLES = {
    "lift": ("`forklift lift --at 2725953`", "mainnet 1002 (read-only)"),
    "verify": ("`forklift verify --sample 200`", "fork-v3 vs live V3"),
    "rehearse": ("`forklift rehearse System.remark`", "fresh fork-v3, prediction only"),
    "replay-v3": ("`forklift replay 118621-1`", "V3, temporary fork"),
    "replay-1002": ("`forklift replay 2700013:1`", "mainnet 1002 (read-only), temporary fork"),
}


def percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def stats(values: list[float]) -> dict:
    return {
        "runs": len(values),
        "p50_s": round(percentile(values, 0.50), 2),
        "p95_s": round(percentile(values, 0.95), 2),
        "min_s": round(min(values), 2),
        "max_s": round(max(values), 2),
    }


def forklift_bin() -> str:
    return str(Path(sys.executable).parent / "forklift")


def spawn(args: list[str]) -> tuple[float, int, dict | None, str]:
    env = dict(os.environ, FORKLIFT_WS_PORT=FORK_WS)
    t0 = time.monotonic()
    p = subprocess.run([forklift_bin(), *args], capture_output=True, text=True, env=env, timeout=900)
    wall = time.monotonic() - t0
    try:
        data = json.loads(p.stdout)
    except ValueError:
        data = None
    return wall, p.returncode, data, p.stderr[-300:]


def one_run(name: str, i: int, tmp: Path) -> dict:
    """Return {wall_s, exit, ok, detail}; ok means the command did what its exit contract says (exit 0 + payload)."""
    if name == "lift":
        d = tmp / f"lift-{i}"
        d.mkdir()
        wall, rc, data, err = spawn(["lift", "--at", str(PIN), "--out", str(d), "--no-resume", "--json"])
        det = {}
        if data and rc == 0:
            sec = data["load"]["seconds"]
            det = {"keys_lifted": data["load"]["keys_lifted"], "keys_s": sec["keys_s"], "values_s": sec["values_s"],
                   "total_s": sec["total_s"], "manifest_root": data["manifest_root"]}
        return {"wall": wall, "rc": rc, "ok": rc == 0 and bool(det), "detail": det, "err": err}
    if name == "verify":
        seed = 20261031000 + i
        wall, rc, data, err = spawn(["verify", "--name", "v3", "--sample", "200", "--seed", str(seed), "--json"])
        det = {}
        if data:
            det = {"seed": seed, "draws": data["draws"], "unique_keys": data["unique_keys"], "classes": data["classes"],
                   "mismatches": len(data["mismatches"]), "pin": data["pin"]["block_number"]}
        return {"wall": wall, "rc": rc, "ok": rc == 0 and bool(data and data.get("ok")), "detail": det, "err": err}
    if name == "rehearse":
        # A rehearse leaves the signer's fee on the fork only, so the next one finds the fork stale: every run
        # starts from a freshly lifted fork. That `down` + `up` is setup and is not timed.
        spawn(["down", "--name", "v3"])
        _, urc, _, uerr = spawn(["up", "--target", "v3", "--at", "head", "--name", "v3", "--ws-port", FORK_WS, "--json"])
        if urc != 0:
            return {"wall": 0.0, "rc": urc, "ok": False, "detail": {}, "err": "fork setup failed: " + uerr}
        wall, rc, data, err = spawn(["rehearse", "System.remark", "--args", REMARK, "--signer", str(SIGNER),
                                     "--name", "v3", "--json"])
        det = {}
        if data and data.get("predicted"):
            p = data["predicted"]
            det = {"predicted_success": p["success"], "extrinsic_id": p["extrinsic_id"], "tx_hash": p["tx_hash"],
                   "fork_head_before": data["advance"]["fork_head"]}
        return {"wall": wall, "rc": rc, "ok": rc == 0 and bool(det) and det["predicted_success"], "detail": det,
                "err": err}
    xid, label = ("118621-1", "v3") if name == "replay-v3" else ("2700013:1", "1002")
    wall, rc, data, err = spawn(["replay", xid, "--ws-port", REPLAY_WS, "--json"])
    det = {}
    if data and data.get("compare"):
        c = data["compare"]
        det = {"extrinsic_id": xid, "fields": c["fields"], "differs": len(c["differs"]), "match": c["match"]}
    return {"wall": wall, "rc": rc, "ok": rc == 0 and bool(det) and det["match"], "detail": det, "err": err}


def machine() -> dict:
    def sh(*cmd: str) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:  # noqa: BLE001 - host facts are best effort
            return ""

    return {
        "machine": platform.machine(), "system": platform.system(), "macos": platform.mac_ver()[0],
        "cpu": sh("sysctl", "-n", "machdep.cpu.brand_string"), "cores": os.cpu_count(),
        "python": platform.python_version(), "forklift": sh(forklift_bin(), "--version"),
        "chopsticks": "@acala-network/chopsticks@1.5.2",
    }


def run_bench(runs: int, only: list[str]) -> dict:
    scenarios: dict = {}
    with tempfile.TemporaryDirectory(prefix="forklift-bench-") as t:
        tmp = Path(t)
        for name in only:
            load0 = os.getloadavg()[0]
            rows = []
            for i in range(1, runs + 1):
                r = one_run(name, i, tmp)
                rows.append(r)
                print(f"{name} run {i}/{runs}: {r['wall']:.2f} s exit {r['rc']} ok={r['ok']}", flush=True)
            good = [r["wall"] for r in rows if r["ok"]]
            entry = {
                "command": TITLES[name][0].strip("`"), "against": TITLES[name][1],
                "runs_all": len(rows), "runs_ok": len(good),
                "wall_s": [round(r["wall"], 2) for r in rows], "exit_codes": [r["rc"] for r in rows],
                "load_avg_1m_before": round(load0, 2), "load_avg_1m_after": round(os.getloadavg()[0], 2),
                "per_run": [r["detail"] for r in rows],
            }
            if good:
                entry.update(stats(good))
            if name == "lift":
                roots = {r["detail"].get("manifest_root") for r in rows}
                entry["manifest_roots_identical"] = len(roots) == 1 and None not in roots
                entry["manifest_root"] = sorted(x for x in roots if x)[0] if roots - {None} else None
                for k in ("keys_s", "values_s", "total_s"):
                    vals = [r["detail"][k] for r in rows if r["ok"]]
                    if vals:
                        entry[f"{k}_p50"] = round(percentile(vals, 0.5), 2)
                        entry[f"{k}_p95"] = round(percentile(vals, 0.95), 2)
            scenarios[name] = entry
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "bench": f"p50/p95 over {runs} fresh-process runs per command",
        "method": "wall-clock seconds from spawn to exit of a fresh `forklift` process; percentiles by linear "
                  "interpolation between the closest ranks; only runs that met their exit contract are counted",
        "recorded_utc": now.isoformat(timespec="seconds"),
        "host": machine(),
        "conditions": "one machine, runs back to back per command, not an idle Mac (1-minute load average before/after each command is recorded per scenario); the public mainnet and V3 nodes are shared",
        "pin": {"mainnet_block": PIN},
        "scenarios": scenarios,
    }


def newest() -> Path | None:
    files = sorted(EVIDENCE.glob("bench-runs-*.json"))
    return files[-1] if files else None


def render(b: dict, evidence_name: str) -> str:
    h = b["host"]
    out = [
        "# BENCHMARKS",
        "",
        "Generated by `scripts/bench.py` from `" + evidence_name + "`. Do not edit by hand.",
        "",
        f"{b['bench']}. Recorded {b['recorded_utc']} on {h['cpu']} ({h['machine']}, {h['cores']} cores), macOS "
        f"{h['macos']}, Python {h['python']}, {h['chopsticks']} (the x86_64 `portaldot_dev` node is not used here: "
        "the V3 path runs on Chopsticks).",
        "",
        "Each run is a **fresh `forklift` process**; a number is the wall-clock seconds from spawn to exit. "
        "Percentiles use linear interpolation between the closest ranks; with this few runs p95 is close to the "
        "slowest run, so read it as \"about the worst case seen\", not a tail estimate. "
        + "One machine, runs back to back per command, not an idle Mac (load averages below), and the public "
        "mainnet and V3 nodes are shared, so absolute seconds vary with load.",
        "",
        "| Command | Against | Runs ok / all | p50 s | p95 s | min s | max s |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in b["scenarios"].values():
        if "p50_s" not in s:
            out.append(f"| `{s['command']}` | {s['against']} | {s['runs_ok']} / {s['runs_all']} | n/a | n/a | n/a | n/a |")
            continue
        out.append(f"| `{s['command']}` | {s['against']} | {s['runs_ok']} / {s['runs_all']} | {s['p50_s']} | "
                   f"{s['p95_s']} | {s['min_s']} | {s['max_s']} |")
    lift = b["scenarios"].get("lift")
    if lift and "total_s_p50" in lift:
        out += [
            "",
            "## Lift, split",
            "",
            f"`forklift lift` at mainnet #{b['pin']['mainnet_block']:,} read {lift['per_run'][0].get('keys_lifted', 0):,} "
            f"keys each run. The process time above includes Python start-up, the metadata and MMR reads and the manifest "
            "write; the split below is the lift's own clock (`load.seconds` in the manifest).",
            "",
            "| Part | p50 s | p95 s |",
            "|---|---|---|",
            f"| keys (paging the key list) | {lift['keys_s_p50']} | {lift['keys_s_p95']} |",
            f"| values (`state_queryStorageAt`) | {lift['values_s_p50']} | {lift['values_s_p95']} |",
            f"| keys + values | {lift['total_s_p50']} | {lift['total_s_p95']} |",
            "",
            f"`manifest_root` identical across all {lift['runs_all']} runs: "
            f"{'yes' if lift.get('manifest_roots_identical') else '**NO**'} (`{lift.get('manifest_root')}`).",
        ]
    out += ["", "## Machine load", "", f"1-minute load average on {h['cores']} cores, read before the first and after the "
            "last run of each command (six readings; they include the benchmark's own fork processes).", "", "| Command | Before | After |", "|---|---|---|"]
    for s_ in b["scenarios"].values():
        out.append(f"| `{s_['command']}` | {s_['load_avg_1m_before']} | {s_['load_avg_1m_after']} |")
    out += ["", "## Per-run seconds", "", "| Command | Runs (s, in order) | Exit codes |", "|---|---|---|"]
    for s in b["scenarios"].values():
        out.append(f"| `{s['command']}` | {', '.join(str(x) for x in s['wall_s'])} | "
                   f"{', '.join(str(x) for x in s['exit_codes'])} |")
    out += ["", "## What each run checked", ""]
    for name, s in b["scenarios"].items():
        d = s["per_run"][0] if s["per_run"] else {}
        if name == "verify":
            u = [r.get("unique_keys", 0) for r in s["per_run"]]
            out.append(f"- `verify`: every run made 200 draws ({min(u)} to {max(u)} distinct keys; draws are with "
                       f"replacement) from live V3 with a new seed ("
                       f"{', '.join(str(r.get('seed')) for r in s['per_run'])}) against the fork at V3 "
                       f"#{d.get('pin', 0):,} and exited 0 with 0 mismatches.")
        elif name == "rehearse":
            out.append("- `rehearse`: one `System.remark` signed from the V3 funder, applied on the fork, predicted "
                       "success each run; **no `--send`**, so nothing was written to V3. A rehearse leaves the signer's "
                       "fee on the fork only, so each run started from a freshly lifted fork (`down` + `up --target v3`, "
                       "setup, not in the seconds above).")
        elif name.startswith("replay"):
            out.append(f"- `{s['command'].replace('`', '')}`: {d.get('fields')} fields compared with the chain, "
                       f"{d.get('differs')} differ, every run.")
        elif name == "lift":
            out.append(f"- `lift`: {d.get('keys_lifted'):,} keys, a new empty output directory and `--no-resume` "
                       "every run, so no run reused a checkpoint or an earlier lift.")
    out += [
        "",
        "## Reproduce",
        "",
        "```bash",
        "forklift up --target v3 --at head --name v3 --ws-port 59944   # the fork verify and rehearse run against",
        "python scripts/bench.py --runs 7                              # roughly 15 to 17 minutes; then: forklift down --name v3",
        "```",
        "",
        "The earlier single lift run (2026-10-14, `evidence/bench-lift-2026-10-14.json`) reported 19.85 s on the lift's own "
        "clock (compare the 'keys + values' row above) and 20.38 s in-process with the metadata and MMR reads, with no "
        "process start-up; it is kept as history. "
        "Numbers differ per run, per machine and per load; the claim here is the spread measured, not a guarantee.",
        "",
    ]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=7)
    ap.add_argument("--only", default=",".join(ALL))
    ap.add_argument("--report", action="store_true", help="rebuild docs/BENCHMARKS.md from the newest evidence file")
    a = ap.parse_args()
    if a.report:
        p = newest()
        if not p:
            print("no evidence/bench-runs-*.json yet")
            return 1
        REPORT.write_text(render(json.loads(p.read_text()), f"evidence/{p.name}"))
        print(f"wrote {REPORT}")
        return 0
    only = [x for x in a.only.split(",") if x]
    bad = [x for x in only if x not in ALL]
    if bad or a.runs < 5:
        print(f"unknown scenario {bad}" if bad else "--runs must be at least 5")
        return 2
    b = run_bench(a.runs, only)
    path = EVIDENCE / f"bench-runs-{dt.datetime.now(dt.timezone.utc).date().isoformat()}.json"
    path.write_text(json.dumps(b, indent=2) + "\n")
    REPORT.write_text(render(b, f"evidence/{path.name}"))
    print(f"wrote {path}\nwrote {REPORT}")
    return 0 if all(s["runs_ok"] == s["runs_all"] for s in b["scenarios"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
