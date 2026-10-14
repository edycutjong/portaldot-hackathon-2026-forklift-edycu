"""Run the official Portaldot node (``portaldot_dev`` 2.0.0) on a spliced chain spec.

Forklift never ships a node of its own. It finds the Foundation's binary (or
downloads the release tarball and checks its sha256), records the binary's
sha256 in the manifest, and spawns it on loopback-only ports with no boot
nodes, no mDNS and ``--reserved-only``: a fork never peers with anything.

Readiness is read over RPC, not from the log: off a TTY the node buffers its
output, so a log poller sees block #1 long after it was authored.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from forklift.lift import default_cache
from forklift.rpc import RpcClient, RpcError, RpcUnavailable

EXIT_NODE_FAILED = 3

BINARY_NAME = "portaldot_dev"
RELEASE = "https://github.com/portaldotVolunteer/Portaldot-node/raw/main/portaldot-testnet-{os}.tar.gz"
# sha256 of the release tarballs, measured 2026-10-11 (macos) and 2026-10-13 (ubuntu).
TARBALL_SHA256 = {
    "macos": "0826f3da6473a0610c1764c0f94be16f8e03a37eb4ba4cc8cc8ac3e8bca78f72",
    "ubuntu": "9df4d6332e84ab81707b5d2e3376c572f058eac4212bd99ba6f1908b75a6327c",
}
# sha256 of the portaldot_dev binary inside a verified tarball.
KNOWN_BINARIES = {
    "301a311779e5658aa880953b052c0de3fff9d9d0f3c039030872510a6eedd6e2": "portaldot_dev 2.0.0 (macos x86_64)",
}
# Where an earlier install may have unpacked the official macOS tarball. Used read-only.
SHARED_INSTALL = Path.home() / ".cache" / "portaldot-dev-node" / "portaldot-testnet-macos" / BINARY_NAME

DEFAULT_PORTS = {"ws": 9944, "rpc": 9933, "p2p": 30333}
PORT_ENV = {"ws": "FORKLIFT_WS_PORT", "rpc": "FORKLIFT_RPC_PORT", "p2p": "FORKLIFT_P2P_PORT"}

# Log lines that explain a failed boot.
KNOWN_CAUSES = [
    (re.compile(r"Address already in use|os error 48|os error 98"), "a port is already in use: pick other ports"),
    (re.compile(r"Timestamp slot must match CurrentSlot"),
     "the native runtime is running mainnet's wasm: an exact-runtime fork needs --execution Wasm"),
    (re.compile(r"Bad CPU type|cannot execute binary|Exec format error"),
     "this binary is x86_64: on Apple silicon install Rosetta (softwareupdate --install-rosetta)"),
    (re.compile(r"cannot be used with|Found argument .* which wasn't expected"),
     "this portaldot_dev build rejects a flag combination: check --node-bin is portaldot_dev 2.0.0"),
    (re.compile(r"Error parsing spec|Invalid chain spec|Chain spec"), "the chain spec did not load: re-run the splice"),
    (re.compile(r"No space left on device"), "the disk is full"),
    (re.compile(r"[Ss]tate already discarded|pruned"), "the node pruned the state that was asked for"),
]


class NodeError(Exception):
    """The node could not be found, started, or did not author (exit code 3)."""

    def __init__(self, message: str, *, log_tail: str = "", hint: str | None = None):
        super().__init__(message)
        self.log_tail = log_tail
        self.hint = hint


def resolve_ports(ws: int | None = None, rpc: int | None = None, p2p: int | None = None) -> dict[str, int]:
    """Explicit value, else ``FORKLIFT_*_PORT`` from the environment, else the Substrate default."""
    given = {"ws": ws, "rpc": rpc, "p2p": p2p}
    ports = {}
    for name, value in given.items():
        if value is None:
            env = os.environ.get(PORT_ENV[name])
            value = int(env) if env else DEFAULT_PORTS[name]
        if not 1024 <= int(value) <= 65535:
            raise NodeError(f"{name} port {value} is outside 1024-65535")
        ports[name] = int(value)
    if len(set(ports.values())) != 3:
        raise NodeError(f"ports must be distinct: {ports}")
    return ports


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    """No listener answers on the port, and it can be bound (a just-stopped node's TIME_WAIT is fine)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        if s.connect_ex((host, port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Binary:
    path: str
    sha256: str
    version: str
    known: str | None  # label if the sha256 is a recognised official build

    def as_json(self) -> dict:
        return asdict(self)


def release_os() -> str:
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Linux":
        return "ubuntu"
    raise NodeError(f"no official portaldot_dev build for {system}")


def download_binary(dest_dir: Path, *, fetch: Callable[[str], bytes] | None = None, os_name: str | None = None) -> Path:
    """Download the official tarball, check its sha256, unpack only ``portaldot_dev``."""
    os_name = os_name or release_os()
    url = RELEASE.format(os=os_name)
    data = (fetch or _fetch)(url)
    got = hashlib.sha256(data).hexdigest()
    if got != TARBALL_SHA256[os_name]:
        raise NodeError(f"{url}: sha256 {got} does not match the pinned {TARBALL_SHA256[os_name]}")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile() and Path(m.name).name == BINARY_NAME]
        if len(members) != 1:
            raise NodeError(f"{url}: expected one {BINARY_NAME} in the tarball, found {len(members)}")
        src = tar.extractfile(members[0])
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / BINARY_NAME
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as out:
            shutil.copyfileobj(src, out)  # type: ignore[arg-type]
        tmp.chmod(0o755)
        tmp.replace(dest)
    return dest


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as r:  # noqa: S310 (fixed https URL)
        return r.read()


def locate_binary(explicit: str | Path | None = None, *, allow_download: bool = True) -> Path:
    """``--node-bin``, ``FORKLIFT_NODE_BIN``, Forklift's own cache, a shared install, PATH, else download."""
    candidates = [explicit, os.environ.get("FORKLIFT_NODE_BIN")]
    for c in candidates:
        if c:
            p = Path(c).expanduser()
            if not p.is_file():
                raise NodeError(f"node binary {p} does not exist")
            return p
    own = default_cache() / "bin" / BINARY_NAME
    for p in (own, SHARED_INSTALL):
        if p.is_file():
            return p
    on_path = shutil.which(BINARY_NAME)
    if on_path:
        return Path(on_path)
    if not allow_download:
        raise NodeError(f"{BINARY_NAME} not found; pass --node-bin or set FORKLIFT_NODE_BIN")
    return download_binary(own.parent)


def inspect_binary(path: Path) -> Binary:
    try:
        out = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=60)
    except OSError as exc:
        raise NodeError(f"cannot run {path}: {exc}", hint=_hint(str(exc))) from exc
    version = (out.stdout or out.stderr).strip()
    if out.returncode != 0 or not version:
        raise NodeError(f"{path} --version failed: {version[:200]}", hint=_hint(version))
    digest = sha256_file(path)
    return Binary(str(path), digest, version, KNOWN_BINARIES.get(digest))


def dev_spec(binary: Path, sha256: str, cache: Path | None = None) -> Path:
    """The binary's own ``build-spec --dev --raw``, cached per binary sha256."""
    out_dir = (cache or default_cache()) / "dev-spec"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"dev-raw-{sha256[:16]}.json"
    if path.is_file() and path.stat().st_size > 0:
        return path
    res = subprocess.run([str(binary), "build-spec", "--dev", "--raw"], capture_output=True, timeout=300)
    if res.returncode != 0 or not res.stdout.strip():
        raise NodeError(f"build-spec --dev --raw failed: {res.stderr.decode(errors='replace')[-400:]}")
    tmp = path.with_suffix(".part")
    tmp.write_bytes(res.stdout)
    tmp.replace(path)
    return path


@dataclass
class NodeConfig:
    name: str
    chain_spec: Path
    ports: dict[str, int]
    base_path: Path | None = None  # None: --tmp (deleted when the node exits)
    extra: list[str] = field(default_factory=list)

    @property
    def ws_endpoint(self) -> str:
        return f"ws://127.0.0.1:{self.ports['ws']}"


def node_args(binary: Path, cfg: NodeConfig) -> list[str]:
    p2p = cfg.ports["p2p"]
    args = [
        str(binary), "--chain", str(cfg.chain_spec), "--alice",
        "--ws-port", str(cfg.ports["ws"]), "--rpc-port", str(cfg.ports["rpc"]),
        # p2p on loopback only. This binary refuses --port together with --listen-addr.
        "--listen-addr", f"/ip4/127.0.0.1/tcp/{p2p}",
        "--reserved-only", "--no-mdns", "--no-prometheus", "--no-telemetry",
        "--pruning", "archive",  # verify reads the fork at genesis, long after boot
    ]
    args += ["--base-path", str(cfg.base_path)] if cfg.base_path is not None else ["--tmp"]
    return args + list(cfg.extra)


def pid_dir() -> Path:
    return default_cache() / "pids"


def pid_file(name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
        raise NodeError(f"node name {name!r}: use 1-32 of [a-z0-9_-]")
    return pid_dir() / f"{name}.pid"


def read_pid(name: str) -> dict | None:
    path = pid_file(name)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def process_command(pid: int) -> str | None:
    """The command line of ``pid``, or None if no such process."""
    res = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    out = res.stdout.strip()
    return out if res.returncode == 0 and out else None


def owns(record: dict, command: str | None) -> bool:
    """Is ``command`` the node this PID record started? (binary name and our own WS port both present)."""
    if not command:
        return False
    return BINARY_NAME in command and f"--ws-port {record['ports']['ws']}" in command


def running(name: str) -> dict | None:
    rec = read_pid(name)
    if rec and owns(rec, process_command(rec["pid"])):
        return rec
    return None


def start(binary: Path, cfg: NodeConfig, log_path: Path) -> subprocess.Popen:
    """Spawn the node detached (it outlives ``forklift up``) and write its PID file."""
    if running(cfg.name):
        raise NodeError(f"a fork named {cfg.name!r} is already running: forklift down --name {cfg.name}")
    busy = [f"{k} {v}" for k, v in cfg.ports.items() if not port_free(v)]
    if busy:
        raise NodeError(f"port(s) in use: {', '.join(busy)}", hint="a port is already in use: pick other ports")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    args = node_args(binary, cfg)
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    rec = {"pid": proc.pid, "name": cfg.name, "ports": cfg.ports, "endpoint": cfg.ws_endpoint,
           "chain_spec": str(cfg.chain_spec), "base_path": str(cfg.base_path) if cfg.base_path else None,
           "log": str(log_path), "started_unix": round(time.time(), 3), "args": args[1:]}
    path = pid_file(cfg.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, indent=2) + "\n")
    return proc


def update_pid_record(name: str, **fields) -> None:
    rec = read_pid(name)
    if rec is None:
        return
    rec.update(fields)
    pid_file(name).write_text(json.dumps(rec, indent=2) + "\n")


def stop(name: str, *, timeout: float = 30.0) -> str:
    """Stop the node started under ``name``: only that PID, only if it is still our node."""
    path = pid_file(name)
    rec = read_pid(name)
    if rec is None:
        return "not running (no PID file)"
    pid = int(rec["pid"])
    cmd = process_command(pid)
    if cmd is None:
        path.unlink(missing_ok=True)
        return f"not running (PID {pid} gone); PID file removed"
    if not owns(rec, cmd):
        path.unlink(missing_ok=True)
        return f"PID {pid} is now another process; left it alone and removed the stale PID file"
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process_command(pid) is not None:
        time.sleep(0.25)
    if process_command(pid) is not None and owns(rec, process_command(pid)):
        os.kill(pid, signal.SIGKILL)
    path.unlink(missing_ok=True)
    return f"stopped PID {pid}"


def log_tail(path: Path | str, lines: int = 30) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16384))
            return "\n".join(f.read().decode(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _hint(text: str) -> str | None:
    for pattern, hint in KNOWN_CAUSES:
        if pattern.search(text):
            return hint
    return None


def fail(message: str, log_path: Path | str | None) -> NodeError:
    tail = log_tail(log_path) if log_path else ""
    return NodeError(message, log_tail=tail, hint=_hint(tail) or _hint(message))


def _header_number(client: RpcClient, method: str) -> int:
    if method == "finalized":
        head = client.call("chain_getFinalizedHead")
        header = client.call("chain_getHeader", [head])
    else:
        header = client.call("chain_getHeader")
    return int(header["number"], 16)


@dataclass
class Boot:
    endpoint: str
    rpc_ready_s: float
    first_block_s: float
    finalized_s: float
    best: int
    finalized: int
    slot_ms: int
    epoch_length: int
    genesis_hash: str
    peers: int


def babe_config(client: RpcClient) -> tuple[int, int]:
    """(slot duration ms, epoch length in slots) from the runtime's ``BabeApi_configuration``."""
    raw = bytes.fromhex(client.call("state_call", ["BabeApi_configuration", "0x"]).removeprefix("0x"))
    return int.from_bytes(raw[0:8], "little"), int.from_bytes(raw[8:16], "little")


def wait_boot(proc: subprocess.Popen, cfg: NodeConfig, log_path: Path, *, rpc_timeout: float = 240.0,
              author_slots: int = 2, finalize_timeout: float = 180.0, t0: float | None = None,
              clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
              connect=None) -> Boot:
    """RPC up → ≥1 authored block within ``author_slots`` slots (I5) → finalized ≥ 1 → 0 peers."""
    t0 = clock() if t0 is None else t0

    def alive() -> None:
        if proc.poll() is not None:
            raise fail(f"the node exited with code {proc.returncode}", log_path)

    client = None
    while True:
        alive()
        try:
            client = RpcClient(cfg.ws_endpoint, fork=True, timeout=5.0, retries=0, connect=connect)
            client.call("chain_getHeader")
            break
        except (RpcUnavailable, RpcError, OSError):
            if client is not None:
                client.close()
            if clock() - t0 > rpc_timeout:
                raise fail(f"no RPC answer on {cfg.ws_endpoint} after {rpc_timeout:.0f} s", log_path) from None
            sleep(0.5)
    rpc_ready = clock()
    with client:
        client.timeout = 30.0
        client.retries = 2
        slot_ms, epoch_length = babe_config(client)
        # I5: the first block must land within author_slots slots of the RPC coming up.
        deadline = rpc_ready + author_slots * slot_ms / 1000.0 + 1.0
        best = _header_number(client, "best")
        while best < 1:
            alive()
            if clock() > deadline:
                raise fail(f"no block authored within {author_slots} slots ({author_slots * slot_ms / 1000:.0f} s)"
                           " of the RPC coming up (I5)", log_path)
            sleep(0.5)
            best = _header_number(client, "best")
        first_block = clock()
        finalized = _header_number(client, "finalized")
        while finalized < 1:
            alive()
            if clock() - first_block > finalize_timeout:
                raise fail(f"block 1 not finalized within {finalize_timeout:.0f} s", log_path)
            sleep(0.5)
            finalized = _header_number(client, "finalized")
        finalized_at = clock()
        health = client.call("system_health")
        peers = int(health.get("peers", 0))
        genesis = client.call("chain_getBlockHash", [0])
        best = _header_number(client, "best")
    return Boot(cfg.ws_endpoint, round(rpc_ready - t0, 1), round(first_block - t0, 1),
                round(finalized_at - t0, 1), best, finalized, slot_ms, epoch_length, genesis, peers)


def ensure_no_peers(boot: Boot, name: str) -> None:
    if boot.peers != 0:
        msg = stop(name)
        raise NodeError(f"the fork has {boot.peers} peer(s); a fork must never peer ({msg})")
