"""``forklift up``: lift → splice → boot, and the manifest that describes the result."""

from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict
from pathlib import Path

from forklift import node as nodes
from forklift.hashing import manifest_root
from forklift.lift import LiftError, _atomic_write, default_cache
from forklift.rpc import RpcClient, RpcError, RpcUnavailable
from forklift.splice import fork_chain_spec, genesis_key_set_hash, load_dev_spec, splice_state


def fork_label(spec_version: int, runtime: str) -> str:
    return f"fork-{spec_version}-{runtime}"


def cached_lift(out_root: Path, block_hash: str) -> tuple[dict, dict] | None:
    """A complete lift of ``block_hash`` already on disk: (manifest, storage), else None."""
    for d in out_root.glob("*"):
        m_path, s_path = d / "manifest.json", d / "state.json"
        if not m_path.is_file() or not s_path.is_file():
            continue
        try:
            m = json.loads(m_path.read_text())
            if m["source"]["block_hash"] != block_hash:
                continue
            state = json.loads(s_path.read_text())
            complete = state["block_hash"] == block_hash and len(state["storage"]) == m["load"]["keys_lifted"]
            if complete:  # the root the Receipt attests must be the root of the state we boot
                pairs = ((bytes.fromhex(k[2:]), bytes.fromhex(v[2:])) for k, v in state["storage"].items())
                complete = "0x" + manifest_root(pairs).hex() == m["manifest_root"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        if complete:
            m["_paths"] = {"dir": str(d), "manifest": str(m_path)}
            return m, state["storage"]
    return None


def obtain_lift(endpoint: str, at, out_root: Path, *, relift: bool) -> tuple[dict, dict, bool]:
    """Reuse a complete lift of the same block hash, else lift. Returns (manifest, storage, reused)."""
    from forklift.cli import run_lift
    from forklift.lift import resolve_pin

    if not relift:
        try:
            with RpcClient(endpoint) as c:
                pin = resolve_pin(c, at)
        except (RpcError, RpcUnavailable, ValueError, KeyError, TypeError) as exc:
            raise LiftError(f"could not pin a block: {exc}", cursor=None, phase="pin", block_hash=None) from exc
        hit = cached_lift(out_root, pin.hash)
        if hit is not None:
            return hit[0], hit[1], True
        at = pin.hash
    manifest = run_lift(endpoint, at, out_root, resume=True)
    try:
        storage = json.loads((Path(manifest["_paths"]["dir"]) / "state.json").read_text())["storage"]
    except (OSError, ValueError, KeyError) as exc:
        raise LiftError(f"cannot read the lift back: {exc}", cursor=None, phase="write",
                        block_hash=manifest["source"]["block_hash"]) from exc
    return manifest, storage, False


def run_up(endpoint: str, at, *, name: str = "fork", runtime: str = "fast", ports: dict[str, int],
           base_path: Path | None = None, node_bin: str | None = None, relift: bool = False,
           out_root: Path | None = None) -> dict:
    cache = default_cache()
    out_root = out_root or cache / "lifts"
    t0 = time.monotonic()
    lift_m, storage, reused = obtain_lift(endpoint, at, out_root, relift=relift)
    t_lift = time.monotonic()

    if nodes.running(name):
        raise nodes.NodeError(f"a fork named {name!r} is already running: forklift down --name {name}")
    binary_path = nodes.locate_binary(node_bin)
    binary = nodes.inspect_binary(binary_path)
    dev = load_dev_spec(nodes.dev_spec(binary_path, binary.sha256))
    src = lift_m["source"]
    try:
        with RpcClient(endpoint) as main:
            properties = main.call("system_properties")  # token symbol and decimals, for wallets
    except (RpcError, RpcUnavailable) as exc:
        raise LiftError(f"cannot read system_properties: {exc}", cursor=None, phase="source",
                        block_hash=src["block_hash"]) from exc
    sp = splice_state(storage, dev["genesis"]["raw"]["top"], runtime=runtime)
    spec = fork_chain_spec(dev, sp.top, block_number=src["block_number"], runtime=runtime, fork_name=name,
                           properties=properties if isinstance(properties, dict) and properties else None)
    fork_dir = cache / "forks" / name
    spec_path = fork_dir / "fork.json"
    try:
        fork_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(spec_path, json.dumps(spec))
    except OSError as exc:
        raise nodes.NodeError(f"cannot write the fork chain spec {spec_path}: {exc}") from exc
    t_splice = time.monotonic()

    m = copy.deepcopy({k: v for k, v in lift_m.items() if k != "_paths"})
    label = fork_label(src["spec_version"], runtime)
    m["profile"] = label
    m["source"]["profile"] = lift_m["profile"]
    m["overrides"] = [o.as_json() for o in sp.overrides]
    m["fork"] = {
        "label": label,
        "chain_id": spec["id"],
        "chain_name": spec["name"],
        "genesis_hash": None,
        "genesis_keys": sp.accounting["genesis_keys"],
        "identical_to_source": sp.accounting["genesis_identical"],
        "genesis_key_set_hash": genesis_key_set_hash(sp.top),
        "accounting": sp.accounting,
        "rules": sp.rules,
        "issuance": sp.issuance,
        "code_hash": sp.dev_code_hash,
        "code_hash_equals_source": sp.dev_code_hash == src["code_hash"],
        "chain_spec": str(spec_path),
    }
    m["node"] = {"binary": binary.version, "sha256": binary.sha256, "known_build": binary.known,
                 "path": binary.path, "dev_spec": str(nodes.dev_spec_path(binary.sha256)),
                 "runtime_mode": runtime, "name": name, "endpoint": None,
                 "ports": ports, "base_path": str(base_path) if base_path else None, "boot": None}
    manifest_path = fork_dir / "fork.manifest.json"
    try:
        _atomic_write(manifest_path, json.dumps(m, indent=2) + "\n")
    except OSError as exc:
        raise nodes.NodeError(f"cannot write {manifest_path}: {exc}") from exc

    cfg = nodes.NodeConfig(name=name, chain_spec=spec_path, ports=ports, base_path=base_path,
                           chain_name=spec["name"])
    log_path = fork_dir / "node.log"
    if base_path is None:
        log_path.unlink(missing_ok=True)  # a fresh --tmp fork gets a fresh log
    t_start = time.monotonic()
    proc = nodes.start(binary_path, cfg, log_path)
    try:
        boot = nodes.wait_boot(proc, cfg, log_path, t0=t_start)
        nodes.ensure_no_peers(boot, name)
    except BaseException:
        try:
            nodes.stop(name)
        except Exception:  # noqa: BLE001  never mask the boot failure with a cleanup failure
            pass
        raise
    m["fork"]["genesis_hash"] = boot.genesis_hash
    m["node"]["endpoint"] = boot.endpoint
    m["node"]["pid"] = proc.pid
    m["node"]["boot"] = asdict(boot)
    m["timings"] = {"lift_s": round(t_lift - t0, 2), "lift_reused": reused, "splice_s": round(t_splice - t_lift, 2),
                    "boot_to_finalized_s": boot.finalized_s, "total_s": round(time.monotonic() - t0, 2)}
    try:
        _atomic_write(manifest_path, json.dumps(m, indent=2) + "\n")
        nodes.update_pid_record(name, manifest=str(manifest_path), genesis_hash=boot.genesis_hash)
    except OSError as exc:
        nodes.stop(name)
        raise nodes.NodeError(f"cannot write {manifest_path}: {exc}") from exc
    m["_paths"] = {"manifest": str(manifest_path), "chain_spec": str(spec_path), "log": str(log_path),
                   "lift": lift_m["_paths"]["dir"]}
    return m


def up_plate(m: dict) -> str:
    src, load, fork, node = m["source"], m["load"], m["fork"], m["node"]
    acc, boot, t = fork["accounting"], node["boot"], m["timings"]
    dev_kept = ", ".join(f"{k} {v:,}" for k, v in acc["dev_kept_per_prefix"].items())
    lifted = "reused the cached lift" if t["lift_reused"] else f"lifted in {t['lift_s']} s"
    return "\n".join([
        f"lift    {src['profile']} · #{src['block_number']:,}  {src['block_hash']}",
        f"        {load['keys_lifted']:,} keys · {lifted}",
        f"splice  {acc['genesis_identical']:,} identical · {acc['dev_kept']:,} dev-kept ({dev_kept})"
        f" · {acc['overridden']} overridden of {acc['keys_lifted']:,} lifted",
        f"        genesis {fork['genesis_keys']:,} keys · overrides: {', '.join(o['name'] for o in m['overrides'])}",
        f"        issuance {int(fork['issuance']['mainnet']):,} + {int(fork['issuance']['added']):,} planck (dev key)",
        f"boot    {node['binary']} · sha256 {node['sha256'][:16]}… · runtime {node['runtime_mode']}",
        f"        {fork['label']} · genesis {fork['genesis_hash']}",
        f"        block #1 at {boot['first_block_s']} s · finalized #1 at {boot['finalized_s']} s · peers {boot['peers']}",
        f"rpc     {node['endpoint']}  (PID {node['pid']}; stop with: forklift down --name {node['name']})",
        f"wrote   {m['_paths']['manifest']}",
    ])
