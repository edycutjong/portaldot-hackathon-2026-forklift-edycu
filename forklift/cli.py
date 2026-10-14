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
