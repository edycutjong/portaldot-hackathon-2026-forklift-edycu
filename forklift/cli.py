"""Command-line entry point."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from forklift import __version__
from forklift.rpc import DEFAULT_MAINNET, RpcClient


def _parse_at(value: str | None) -> str | int | None:
    if value is None or value == "head" or value.startswith("0x"):
        return value
    try:
        return int(value.replace(",", "").replace("_", ""))
    except ValueError:
        raise click.BadParameter("expected a block number, a 0x block hash, or 'head'") from None


def run_lift(endpoint: str, at: str | int | None, out_root: Path, *, resume: bool = True) -> dict:
    """Lift, write ``state.json`` + ``manifest.json`` under ``out_root/<block>/``, return the manifest."""
    from forklift.lift import Checkpoint, LiftError, _atomic_write, lift, mmr_summary
    from forklift.manifest import build_manifest
    from forklift.metadata import MetadataError, StorageIndex, decode_hex
    from forklift.rpc import RpcError, RpcUnavailable

    with RpcClient(endpoint) as client:
        result = lift(client, at, out_root=out_root, resume=resume, keep_checkpoint=True)
        try:
            index = StorageIndex(decode_hex(client.call("state_getMetadata", [result.pin.hash])))
            mmr = mmr_summary(client, result.pin.hash)
            manifest = build_manifest(result, index, endpoint=endpoint, mmr=mmr)
        except (RpcError, RpcUnavailable, MetadataError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # state.json is on disk but without a manifest it is not a complete lift;
            # the checkpoint stays, so a re-run resumes instead of paging again.
            raise LiftError(f"manifest step failed: {exc}", cursor=None, phase="manifest",
                            block_hash=result.pin.hash) from exc
    manifest["load"]["resumed_from_cursor"] = result.resumed_from
    path = result.out_dir / "manifest.json"
    try:
        _atomic_write(path, json.dumps(manifest, indent=2) + "\n")
    except OSError as exc:
        raise LiftError(f"cannot write manifest.json: {exc}", cursor=None, phase="write",
                        block_hash=result.pin.hash) from exc
    Checkpoint(result.out_dir / "checkpoint.json", result.pin.hash).clear()
    manifest["_paths"] = {"dir": str(result.out_dir), "manifest": str(path)}
    return manifest


def record_evidence(m: dict, evidence_dir: Path) -> Path:
    """Write or extend ``evidence/lift-<block>.json``. Every run at a pin is kept, with its root."""
    import datetime as dt

    src, load = m["source"], m["load"]
    path = evidence_dir / f"lift-{src['block_number']}.json"
    prior = json.loads(path.read_text()) if path.exists() else {}
    if prior and prior["pin"]["block_hash"] != src["block_hash"]:
        # Same number, different block (a reorg below finality should not happen, but never merge them).
        path = evidence_dir / f"lift-{src['block_number']}-{src['block_hash'][2:10]}.json"
        prior = json.loads(path.read_text()) if path.exists() else {}
    runs = prior.get("runs", [])
    runs.append({
        "recorded_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "seconds": load["seconds"],
        "key_pages": load["key_pages"],
        "manifest_root": m["manifest_root"],
        "resumed_from_cursor": load.get("resumed_from_cursor"),
    })
    ev = {
        "profile": m["profile"],
        "forklift_version": m["forklift_version"],
        "endpoint": src["endpoint"],
        "pin": {"block_number": src["block_number"], "block_hash": src["block_hash"]},
        "spec_name": src["spec_name"],
        "spec_version": src["spec_version"],
        "code_hash": src["code_hash"],
        "code_bytes": src["code_bytes"],
        "keys_lifted": load["keys_lifted"],
        "key_bytes": load["key_bytes"],
        "value_bytes": load["value_bytes"],
        "skipped": load["skipped"],
        "per_pallet": load["per_pallet"],
        "per_item": load["per_item"],
        "manifest_root": m["manifest_root"],
        "roots_identical_across_runs": len({r["manifest_root"] for r in runs}) == 1,
        "runs": runs,
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ev, indent=2) + "\n")
    return path


def data_plate(m: dict) -> str:
    src, load = m["source"], m["load"]
    sk = load["skipped"] or {}
    secs = load["seconds"]
    return "\n".join([
        f"lift    {m['profile']} · {src['endpoint']}",
        f"pin     #{src['block_number']:,}  {src['block_hash']}",
        f"keys    {load['keys_lifted']:,} keys · {load['value_bytes']:,} B values · {load['key_pages']} pages"
        f"  (MerkleMountainRange skipped: ~{sk.get('keys_in_range_derived', 0):,} keys, derived)",
        f"time    keys {secs['keys_s']} s · values {secs['values_s']} s · total {secs['total_s']} s",
        f"code    {src['code_hash']}  ({src['code_bytes']:,} B)",
        f"root    {m['manifest_root']}",
        f"wrote   {m['_paths']['manifest']}",
    ])


@click.group()
@click.version_option(__version__, prog_name="forklift")
def main() -> None:
    """Lift Portaldot mainnet state onto a local fork of the official node."""


@main.command("lift")
@click.option("--at", "at", default="head", show_default=True,
              help="Block to pin: a number, a 0x block hash, or 'head' (latest finalized).")
@click.option("--from", "endpoint", default=DEFAULT_MAINNET, show_default=True,
              help="Archive node to read from (read-only).")
@click.option("--out", "out_root", type=click.Path(path_type=Path), default=None,
              help="Lift directory root [default: ~/.cache/forklift/lifts].")
@click.option("--no-resume", is_flag=True, help="Ignore a checkpoint left by an interrupted lift.")
@click.option("--evidence", "evidence_dir", type=click.Path(path_type=Path), default=None,
              help="Also record this run in <DIR>/lift-<block>.json.")
@click.option("--json", "as_json", is_flag=True, help="Print the manifest as JSON.")
def lift_cmd(at: str, endpoint: str, out_root: Path | None, no_resume: bool, evidence_dir: Path | None,
             as_json: bool) -> None:
    """Read every storage key of a pinned block (MMR range skipped) and write a manifest.

    Exit code 4 if the lift cannot finish; the last key cursor is printed and kept
    as a checkpoint, and nothing is written as a complete lift.
    """
    from forklift.lift import EXIT_LIFT_FAILED, LiftError, default_cache

    out_root = out_root or default_cache() / "lifts"
    try:
        manifest = run_lift(endpoint, _parse_at(at), out_root, resume=not no_resume)
    except LiftError as exc:
        err = {"error": "lift_failed", "phase": exc.phase, "message": str(exc),
               "block_hash": exc.block_hash, "last_cursor": exc.cursor}
        if as_json:
            click.echo(json.dumps(err))
        else:
            click.echo(f"lift failed during {exc.phase}: {exc}", err=True)
            click.echo(f"last cursor: {exc.cursor}  (re-run the same --at to resume)", err=True)
        sys.exit(EXIT_LIFT_FAILED)
    if evidence_dir is not None:
        manifest["_paths"]["evidence"] = str(record_evidence(manifest, evidence_dir))
    if as_json:
        click.echo(json.dumps(manifest, indent=2))
    else:
        click.echo(data_plate(manifest))


def _node_failed(exc, as_json: bool) -> None:
    from forklift.node import EXIT_NODE_FAILED

    if as_json:
        click.echo(json.dumps({"error": "node_failed", "message": str(exc), "hint": exc.hint,
                               "log_tail": exc.log_tail}))
    else:
        click.echo(f"node failed: {exc}", err=True)
        if exc.hint:
            click.echo(f"likely cause: {exc.hint}", err=True)
        if exc.log_tail:
            click.echo("--- node log tail ---\n" + exc.log_tail, err=True)
    sys.exit(EXIT_NODE_FAILED)


def _port_options(f):
    f = click.option("--p2p-port", type=int, default=None, help="p2p port, bound to 127.0.0.1 "
                     "[env FORKLIFT_P2P_PORT, default 30333].")(f)
    f = click.option("--rpc-port", type=int, default=None, help="HTTP RPC port [env FORKLIFT_RPC_PORT, default 9933].")(f)
    f = click.option("--ws-port", type=int, default=None, help="WebSocket RPC port [env FORKLIFT_WS_PORT, default 9944].")(f)
    return f


@main.command("up")
@click.option("--at", "at", default="head", show_default=True,
              help="Mainnet block to fork: a number, a 0x block hash, or 'head' (latest finalized).")
@click.option("--runtime", type=click.Choice(["fast"]), default="fast", show_default=True,
              help="fast = the dev runtime wasm (6 s blocks).")
@click.option("--from", "endpoint", default=DEFAULT_MAINNET, show_default=True,
              help="Archive node to lift from (read-only).")
@click.option("--name", default="fork", show_default=True, help="Name of this fork (PID file, fork directory).")
@click.option("--base-path", type=click.Path(path_type=Path), default=None,
              help="Persistent node database [default: --tmp, deleted when the node stops].")
@click.option("--node-bin", default=None, help="portaldot_dev binary [env FORKLIFT_NODE_BIN; else found or downloaded].")
@click.option("--relift", is_flag=True, help="Lift again even if a complete lift of this block is cached.")
@_port_options
@click.option("--evidence", "evidence_dir", type=click.Path(path_type=Path), default=None,
              help="Also record this boot in <DIR>/fork-<block>.json.")
@click.option("--json", "as_json", is_flag=True, help="Print the fork manifest as JSON.")
def up_cmd(at: str, runtime: str, endpoint: str, name: str, base_path: Path | None, node_bin: str | None,
           relift: bool, ws_port: int | None, rpc_port: int | None, p2p_port: int | None,
           evidence_dir: Path | None, as_json: bool) -> None:
    """Lift mainnet at a pinned block, splice it into the dev chain spec, and boot the official node on it.

    The node keeps running after this command returns. Exit 3 if the node does
    not start, author a block within 2 slots, or finalize; exit 4 if the lift fails.
    """
    from forklift.lift import EXIT_LIFT_FAILED, LiftError
    from forklift.node import NodeError, resolve_ports
    from forklift.splice import SpliceError
    from forklift.up import run_up, up_plate

    try:
        ports = resolve_ports(ws_port, rpc_port, p2p_port)
    except NodeError as exc:
        raise click.UsageError(str(exc)) from None
    try:
        m = run_up(endpoint, _parse_at(at), name=name, runtime=runtime, ports=ports,
                   base_path=base_path.expanduser().resolve() if base_path else None,
                   node_bin=node_bin, relift=relift)
    except LiftError as exc:
        err = {"error": "lift_failed", "phase": exc.phase, "message": str(exc), "last_cursor": exc.cursor}
        click.echo(json.dumps(err) if as_json else f"lift failed during {exc.phase}: {exc}", err=not as_json)
        sys.exit(EXIT_LIFT_FAILED)
    except (NodeError, SpliceError, OSError) as exc:
        if isinstance(exc, SpliceError):
            exc = NodeError(f"splice failed: {exc}")
        elif isinstance(exc, OSError):
            exc = NodeError(f"{type(exc).__name__}: {exc}")
        _node_failed(exc, as_json)
    if evidence_dir is not None:
        m["_paths"]["evidence"] = str(record_fork(m, evidence_dir))
    click.echo(json.dumps(m, indent=2) if as_json else up_plate(m))


def record_fork(m: dict, evidence_dir: Path) -> Path:
    """Write or extend ``evidence/fork-<block>.json``: one run per boot (genesis, timings, accounting)."""
    import datetime as dt

    src, fork, node = m["source"], m["fork"], m["node"]
    path = evidence_dir / f"fork-{src['block_number']}.json"
    prior = json.loads(path.read_text()) if path.exists() else {}
    if prior and prior["pin"]["block_hash"] != src["block_hash"]:
        path = evidence_dir / f"fork-{src['block_number']}-{src['block_hash'][2:10]}.json"
        prior = json.loads(path.read_text()) if path.exists() else {}
    runs = prior.get("runs", [])
    runs.append({
        "recorded_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "label": fork["label"],
        "name": node["name"],
        "persistent_base_path": node["base_path"] is not None,
        "genesis_hash": fork["genesis_hash"],
        "genesis_keys": fork["genesis_keys"],
        "genesis_key_set_hash": fork["genesis_key_set_hash"],
        "boot": node["boot"],
        "timings": m["timings"],
    })
    ev = {
        "pin": {"block_number": src["block_number"], "block_hash": src["block_hash"]},
        "source_endpoint": src["endpoint"],
        "spec_version": src["spec_version"],
        "source_code_hash": src["code_hash"],
        "manifest_root": m["manifest_root"],
        "binary": {"version": node["binary"], "sha256": node["sha256"], "known_build": node["known_build"]},
        "runtime_mode": node["runtime_mode"],
        "fork_code_hash": fork["code_hash"],
        "chain_id": fork["chain_id"],
        "accounting": fork["accounting"],
        "issuance": fork["issuance"],
        "overrides": m["overrides"],
        "genesis_hash_stable_across_runs": len({r["genesis_hash"] for r in runs} | {fork["genesis_hash"]}) == 1,
        "runs": runs,
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ev, indent=2) + "\n")
    return path


@main.command("down")
@click.option("--name", default="fork", show_default=True, help="Name the fork was started with.")
def down_cmd(name: str) -> None:
    """Stop a fork started by `forklift up` (only the PID it recorded, only if it is still that node)."""
    from forklift.node import NodeError, pid_file, stop

    try:
        pid_file(name)
    except NodeError as exc:
        raise click.UsageError(str(exc)) from None
    try:
        click.echo(f"{name}: {stop(name)}")
    except NodeError as exc:
        _node_failed(exc, False)


def record_verify(report, m: dict, evidence_dir: Path, endpoint: str) -> Path:
    """Write or extend ``evidence/verify-<block>.json``: one run per verify, with its seed to reproduce it."""
    import datetime as dt

    src, fork = m["source"], m["fork"]
    path = evidence_dir / f"verify-{src['block_number']}.json"
    prior = json.loads(path.read_text()) if path.exists() else {}
    if prior and prior["pin"]["block_hash"] != src["block_hash"]:
        path = evidence_dir / f"verify-{src['block_number']}-{src['block_hash'][2:10]}.json"
        prior = json.loads(path.read_text()) if path.exists() else {}
    runs = prior.get("runs", [])
    runs.append({
        "recorded_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "label": fork["label"],
        "fork_genesis_hash": fork["genesis_hash"],
        "fork_endpoint": endpoint,
        "seed": report.seed,
        "draws": report.draws,
        "unique_keys": report.unique_keys,
        "classes": report.classes,
        "mismatches": report.mismatches,
        "checks": report.checks,
        "fork_head_at_verify": report.fork_head,
        "exit_code": report.exit_code,
    })
    ev = {
        "pin": {"block_number": src["block_number"], "block_hash": src["block_hash"]},
        "source_endpoint": src["endpoint"],
        "spec_version": src["spec_version"],
        "keys_lifted": m["load"]["keys_lifted"],
        "manifest_root": m["manifest_root"],
        "accounting": fork["accounting"],
        "overrides": [{"name": o["name"], "reason": o["reason"]} for o in m["overrides"]],
        "reproduce": "forklift verify --sample <draws> --seed <seed> against a fork of this pin",
        "runs": runs,
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ev, indent=2) + "\n")
    return path


def _genesis_top(path: str | None) -> dict | None:
    """The raw genesis ``{key: value}`` of a chain spec file, or None if there is none at ``path``."""
    if not path or not Path(path).is_file():
        return None
    return json.loads(Path(path).read_text())["genesis"]["raw"]["top"]


def _load_fork_manifest(name: str, manifest_path: Path | None) -> dict:
    from forklift.node import NodeError, read_pid

    if manifest_path is None:
        try:
            rec = read_pid(name)
        except NodeError as exc:
            raise click.UsageError(str(exc)) from None
        if not rec or not rec.get("manifest"):
            raise click.UsageError(f"no running fork named {name!r} with a manifest; pass --manifest")
        manifest_path = Path(rec["manifest"])
    try:
        return json.loads(Path(manifest_path).read_text())
    except (OSError, ValueError) as exc:
        raise click.UsageError(f"cannot read {manifest_path}: {exc}") from None


@main.command("verify")
@click.option("--sample", "sample", type=click.IntRange(1, 100_000), default=200, show_default=True,
              help="Keys to draw from mainnet (inside storage maps, weighted by per-item counts).")
@click.option("--seed", type=int, default=None, help="Random seed of the draw (printed; reuse it to reproduce).")
@click.option("--name", default="fork", show_default=True, help="Fork to check (from its PID file).")
@click.option("--manifest", "manifest_path", type=click.Path(path_type=Path), default=None,
              help="fork.manifest.json to check instead of --name's.")
@click.option("--endpoint", default=None, help="Fork WebSocket endpoint [default: the manifest's].")
@click.option("--from", "source", default=None, help="Mainnet archive node [default: the manifest's source].")
@click.option("--evidence", "evidence_dir", type=click.Path(path_type=Path), default=None,
              help="Also record this run in <DIR>/verify-<block>.json.")
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
def verify_cmd(sample: int, seed: int | None, name: str, manifest_path: Path | None, endpoint: str | None,
               source: str | None, evidence_dir: Path | None, as_json: bool) -> None:
    """Check the fork at genesis against mainnet at the pin, from keys drawn on mainnet.

    Exit 0 when every sampled key is identical or explained (dev-kept, override,
    reset) and the key-set, issuance and runtime checks pass; exit 2 otherwise,
    naming every mismatch; exit 1 if the check could not run.
    """
    from forklift.rpc import RpcError, RpcUnavailable
    from forklift.verify import VerifyError, verify

    m = _load_fork_manifest(name, manifest_path)
    endpoint = endpoint or (m.get("node") or {}).get("endpoint")
    if not endpoint:
        raise click.UsageError("the manifest has no fork endpoint; pass --endpoint")
    source = source or m["source"]["endpoint"]
    node = m.get("node") or {}
    spec_path = (m.get("fork") or {}).get("chain_spec")
    dev_path = node.get("dev_spec")
    if not dev_path and node.get("sha256"):
        from forklift.node import dev_spec_path

        dev_path = str(dev_spec_path(node["sha256"]))
    try:
        spliced = _genesis_top(spec_path)
        dev = _genesis_top(dev_path)
        if dev is None:
            click.echo(f"warning: dev chain spec not found ({dev_path}): dev-kept keys are counted, not checked",
                       err=True)
        with RpcClient(source) as main_c, RpcClient(endpoint) as fork_c:
            report = verify(m, main_c, fork_c, sample=sample, seed=seed, spliced=spliced, dev=dev)
    except (VerifyError, RpcError, RpcUnavailable, OSError, ValueError, KeyError) as exc:
        click.echo(json.dumps({"error": "verify_failed", "message": str(exc)}) if as_json
                   else f"verify could not run: {exc}", err=not as_json)
        sys.exit(1)
    if evidence_dir is not None:
        record_verify(report, m, evidence_dir, endpoint)
    if as_json:
        out = {"ok": report.ok, "exit_code": report.exit_code, "label": m["fork"]["label"],
               "pin": {"block_number": m["source"]["block_number"], "block_hash": m["source"]["block_hash"]},
               "fork_genesis_hash": m["fork"]["genesis_hash"], "seed": report.seed, "draws": report.draws,
               "unique_keys": report.unique_keys, "classes": report.classes, "mismatches": report.mismatches,
               "checks": report.checks, "fork_head": report.fork_head, "sample": report.sample}
        click.echo(json.dumps(out, indent=2))
    else:
        click.echo(verify_plate(report, m))
    sys.exit(report.exit_code)


def _n(value: str | None) -> str:
    return "n/a" if value is None else f"{int(value):,}"


def verify_plate(report, m: dict) -> str:
    src, fork, chk = m["source"], m["fork"], report.checks
    lines = [
        f"verify  {fork['label']} genesis {fork['genesis_hash']}",
        f"        vs {src['endpoint']} #{src['block_number']:,} {src['block_hash']}",
        f"fork    best #{report.fork_head['best']:,} · finalized #{report.fork_head['finalized']:,}",
        f"sample  {report.draws} draws from mainnet (seed {report.seed}) → {report.unique_keys} keys: "
        + ", ".join(f"{k} {v}" for k, v in report.classes.items())
        + ("" if chk["compared_with_mainnet"]["dev_kept_checked_against_dev_genesis"]
           else " (dev-kept not checked: dev chain spec not found)"),
        f"keys    fork genesis {chk['genesis_key_set']['fork_keys']:,} keys, set hash "
        f"{'matches' if chk['genesis_key_set']['ok'] else 'DIFFERS from'} the splice",
        f"issue   fork {_n(chk['issuance']['fork'])} = mainnet {_n(chk['issuance']['mainnet'])}"
        f" + {_n(chk['issuance']['added'])}: {'ok' if chk['issuance']['ok'] else 'MISMATCH'}",
        f"over    {chk['overrides']['count']} named overrides read on both sides: "
        f"{'ok' if chk['overrides']['ok'] else 'MISMATCH'}",
        f"code    mainnet {chk['runtime']['mainnet_code_hash']} · fork {chk['runtime']['fork_code_hash']}"
        f" ({chk['runtime']['fork_runtime']}): {'ok' if chk['runtime']['ok'] else 'MISMATCH'}",
    ]
    for mm in report.mismatches:
        lines.append(f"MISMATCH {mm['item']}  {mm['key']}\n         mainnet {mm['mainnet']}\n         fork    {mm['fork']}")
    for side in ("missing_on_fork", "extra_on_fork"):
        if chk["genesis_key_set"].get(side):
            lines.append(f"{side.replace('_', ' ')}: {', '.join(chk['genesis_key_set'][side])}")
    lines.append("OK      nothing unexplained" if report.ok else "FAIL    unexplained differences (exit 2)")
    return "\n".join(lines)


EXIT_USAGE = 64  # click's default (2) is reserved for "verify found a mismatch"


def run() -> None:
    """Console entry point: like ``main()`` but with usage errors on exit code 64."""
    try:
        main(standalone_mode=False)
    except click.UsageError as exc:
        exc.show()
        sys.exit(EXIT_USAGE)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.Abort:
        click.echo("Aborted!", err=True)
        sys.exit(1)


if __name__ == "__main__":
    run()
