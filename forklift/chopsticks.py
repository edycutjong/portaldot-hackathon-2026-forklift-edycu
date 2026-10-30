"""Fork the V3 testnet (or, for ``--runtime exact``, mainnet) with Chopsticks, at the block Forklift lifted.

There is no public V3 node binary or chain spec, so the 1002 path (splice the
lift into ``portaldot_dev``'s chain spec and boot it) cannot run V3's runtime.
Chopsticks can: it executes the chain's own runtime wasm and reads state from
the live endpoint at a pinned block, lazily. Forklift pins that block, lifts it
independently, starts Chopsticks on loopback, and checks the fork against the
lift (``verify``) before anything is sent to it.

Measured on 2026-10-20 at #117,696: with default settings every state call
fails with ``Unresolved function env:ext_statement_store_submit_statement_version_1``
(the V3 runtime's Statement pallet imports a host function Chopsticks 1.5.2 does
not provide). With ``allow-unresolved-imports`` the fork builds blocks and
accepts ``author_submitExtrinsic``. The flag is on and the manifest says so.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from forklift.chains import V3, Chain
from forklift.node import NodeError, log_tail, pid_file, port_free, process_command, read_pid
from forklift.rpc import RpcClient, RpcError, RpcUnavailable

PACKAGE = "@acala-network/chopsticks@1.5.2"
UNRESOLVED_IMPORT = "ext_statement_store_submit_statement_version_1"
# Which host function each chain's runtime imports that Chopsticks 1.5.2 does not provide (why the flag is on).
# Mainnet 1002, measured 2026-10-31 WIB at #2,725,953 (evidence/chopsticks-1002-2725953.json): without the flag
# chain_getHeader fails on an ext_sandbox_* host import and dev_newBlock builds nothing.
UNRESOLVED_IMPORTS = {"v3": UNRESOLVED_IMPORT, "mainnet": "ext_sandbox_instance_teardown_version_1"}


def chopsticks_args(chain: Chain, block_hash: str, ws_port: int, db: Path) -> list[str]:
    return [
        "npx", "-y", PACKAGE,
        f"--endpoint={chain.endpoint}",
        f"--block={block_hash}",
        f"--port={ws_port}",
        "--host=127.0.0.1",
        "--build-block-mode=Instant",
        "--mock-signature-host=true",
        "--allow-unresolved-imports=true",
        f"--db={db}",
    ]


def owns(record: dict, command: str | None) -> bool:
    """Is ``command`` the Chopsticks process this record started (same package, same port)?"""
    if not command or record.get("executor") != "chopsticks":
        return False
    tokens = command.split()
    return PACKAGE in tokens[:4] and f"--port={record['ports']['ws']}" in tokens


def running(name: str) -> dict | None:
    rec = read_pid(name)
    if rec and owns(rec, process_command(rec["pid"])):
        return rec
    return None


def start(name: str, chain: Chain, block_hash: str, ws_port: int, fork_dir: Path) -> dict:
    """Spawn Chopsticks detached on 127.0.0.1:<ws_port> and write its PID file."""
    if running(name):
        raise NodeError(f"a fork named {name!r} is already running: forklift down --name {name}")
    if not port_free(ws_port):
        raise NodeError(f"port ws {ws_port} in use", hint="a port is already in use: pick other ports")
    fork_dir.mkdir(parents=True, exist_ok=True)
    db = fork_dir / f"chopsticks-{block_hash[2:10]}.sqlite"
    log_path = fork_dir / "chopsticks.log"
    args = chopsticks_args(chain, block_hash, ws_port, db)
    try:
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
    except OSError as exc:
        raise NodeError(f"cannot start Chopsticks via npx: {exc}", hint="Chopsticks needs Node.js 18+ and npx") from exc
    rec = {"pid": proc.pid, "name": name, "executor": "chopsticks", "package": PACKAGE,
           "ports": {"ws": ws_port}, "endpoint": f"ws://127.0.0.1:{ws_port}", "chain": chain.id,
           "block_hash": block_hash, "db": str(db), "log": str(log_path),
           "started_unix": round(time.time(), 3), "args": args[1:]}
    path = pid_file(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, indent=2) + "\n")
    except OSError as exc:
        os.killpg(proc.pid, signal.SIGTERM)
        raise NodeError(f"cannot write the PID file {path}: {exc}") from exc
    rec["_proc"] = proc
    return rec


def stop(rec: dict, *, timeout: float = 30.0) -> str:
    """Stop the recorded Chopsticks process group (npx and the node process it runs), only if it is ours."""
    pid = int(rec["pid"])
    path = pid_file(rec["name"])
    cmd = process_command(pid)
    if cmd is None:
        path.unlink(missing_ok=True)
        return f"not running (PID {pid} gone); PID file removed"
    if not owns(rec, cmd):
        path.unlink(missing_ok=True)
        return f"PID {pid} is now another process; left it alone and removed the stale PID file"
    try:
        os.killpg(pid, signal.SIGTERM)  # start_new_session=True: the group is npx + chopsticks only
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not port_free(rec["ports"]["ws"]):
            time.sleep(0.25)
        if not port_free(rec["ports"]["ws"]):
            os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise NodeError(f"not allowed to stop PID {pid}: {exc}") from exc
    path.unlink(missing_ok=True)
    return f"stopped PID {pid} (Chopsticks)"


def wait_ready(rec: dict, *, timeout: float = 300.0) -> float:
    """Seconds until the fork answers ``chain_getHeader``. Raises NodeError if the process dies or times out."""
    t0 = time.monotonic()
    proc = rec.get("_proc")
    while time.monotonic() - t0 < timeout:
        if proc is not None and proc.poll() is not None:
            raise NodeError(f"Chopsticks exited with code {proc.returncode}:\n{log_tail(rec['log'])}")
        if not port_free(rec["ports"]["ws"]):
            try:
                with RpcClient(rec["endpoint"], timeout=30, retries=0) as c:
                    if c.call("chain_getHeader"):
                        return round(time.monotonic() - t0, 2)
            except (RpcError, RpcUnavailable, OSError, TimeoutError):
                pass
        time.sleep(1.0)
    raise NodeError(f"Chopsticks did not answer within {timeout:.0f} s:\n{log_tail(rec['log'])}")


def fork_identity(client: RpcClient) -> dict:
    """What the fork serves at its head: genesis, head, ``:code`` hash, runtime, Chopsticks dev methods."""
    from forklift.hashing import blake2_256

    head = client.call("chain_getHeader")
    head_hash = client.call("chain_getBlockHash", [int(head["number"], 16)])
    code = client.call("state_getStorage", ["0x3a636f6465", head_hash])
    version = client.call("state_getRuntimeVersion", [head_hash])
    methods = (client.call("rpc_methods") or {}).get("methods", [])
    return {
        "genesis_hash": client.call("chain_getBlockHash", [0]),
        "head_number": int(head["number"], 16),
        "head_hash": head_hash,
        "code_hash": "0x" + blake2_256(bytes.fromhex(code[2:])).hex() if code else None,
        "spec": [version.get("specName"), version.get("specVersion")],
        "dev_methods": sorted(m for m in methods if m.startswith("dev_")),
    }


def fork_label(chain: Chain = V3) -> str:
    return f"fork-{chain.id}"
