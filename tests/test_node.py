import hashlib
import io
import json
import os
import socket
import sys
import tarfile
from pathlib import Path

import pytest

from forklift import node as nodes
from forklift.node import NodeConfig, NodeError
from tests.fakes import fake_connect


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("FORKLIFT_CACHE", str(tmp_path))
    for env in nodes.PORT_ENV.values():
        monkeypatch.delenv(env, raising=False)
    return tmp_path


def test_ports_explicit_then_env_then_default(monkeypatch):
    assert nodes.resolve_ports() == {"ws": 9944, "rpc": 9933, "p2p": 30333}
    monkeypatch.setenv("FORKLIFT_WS_PORT", "59944")
    monkeypatch.setenv("FORKLIFT_RPC_PORT", "59933")
    monkeypatch.setenv("FORKLIFT_P2P_PORT", "30359")
    assert nodes.resolve_ports() == {"ws": 59944, "rpc": 59933, "p2p": 30359}
    assert nodes.resolve_ports(ws=59945, rpc=59946, p2p=59947) == {"ws": 59945, "rpc": 59946, "p2p": 59947}
    with pytest.raises(NodeError, match="distinct"):
        nodes.resolve_ports(ws=59945, rpc=59945, p2p=59947)
    with pytest.raises(NodeError, match="outside"):
        nodes.resolve_ports(ws=80)


def test_node_args_bind_loopback_never_peer_and_keep_genesis_state():
    cfg = NodeConfig("fork", Path("/c/fork.json"), {"ws": 59944, "rpc": 59933, "p2p": 30359})
    args = nodes.node_args(Path("/bin/portaldot_dev"), cfg)
    s = " ".join(args)
    assert "--listen-addr /ip4/127.0.0.1/tcp/30359" in s
    assert "--port" not in args  # this binary refuses --port with --listen-addr
    for flag in ("--reserved-only", "--no-mdns", "--no-prometheus", "--no-telemetry", "--tmp", "--alice"):
        assert flag in args
    assert "--ws-port 59944" in s and "--rpc-port 59933" in s and "--pruning archive" in s
    assert "--ws-external" not in s and "--rpc-external" not in s
    persistent = nodes.node_args(Path("/b"), NodeConfig("soak", Path("/c"), cfg.ports, base_path=Path("/d/soak")))
    assert "--tmp" not in persistent and persistent[persistent.index("--base-path") + 1] == "/d/soak"


def test_pid_names_are_restricted():
    for bad in ("../x", "Fork", "", "a" * 40, "a b"):
        with pytest.raises(NodeError):
            nodes.pid_file(bad)
    assert nodes.pid_file("soak").name == "soak.pid"


def test_owns_requires_our_binary_and_our_ws_port():
    rec = {"ports": {"ws": 59944}}
    assert nodes.owns(rec, "/x/portaldot_dev --chain f.json --alice --ws-port 59944 --rpc-port 59933")
    assert not nodes.owns(rec, "/x/portaldot_dev --dev --tmp --ws-port 19944")  # another project's node
    assert not nodes.owns(rec, "/usr/bin/python3 something --ws-port 59944")
    assert not nodes.owns(rec, None)


def test_owns_is_token_exact_and_follows_the_recorded_binary_and_chain():
    rec = {"ports": {"ws": 3000}, "binary": "/opt/pd", "chain_spec": "/c/forks/fork/fork.json"}
    assert nodes.owns(rec, "/opt/pd --chain /c/forks/fork/fork.json --alice --ws-port 3000")  # renamed binary
    assert not nodes.owns(rec, "/opt/pd --chain /c/forks/fork/fork.json --ws-port 30000")  # no substring match
    assert not nodes.owns(rec, "/opt/pd --chain /c/forks/soak/fork.json --ws-port 3000")  # another fork's chain
    assert not nodes.owns(rec, "/opt/pd --dev --ws-port 3000")


def test_stop_never_kills_a_process_that_is_not_our_node(cache):
    # A PID file that points at this very test process (not a portaldot_dev): stop must leave it alone.
    path = nodes.pid_file("fork")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": os.getpid(), "ports": {"ws": 59944}}))
    msg = nodes.stop("fork")
    assert "left it alone" in msg and not path.exists()
    assert nodes.stop("fork") == "not running (no PID file)"


def test_stop_cleans_a_pid_file_whose_process_is_gone(cache):
    path = nodes.pid_file("fork")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 2**22 + 12345, "ports": {"ws": 59944}}))
    assert "gone" in nodes.stop("fork") and not path.exists()


def test_stop_terminates_an_owned_process_and_reaps_it(cache, tmp_path):
    fake = tmp_path / "portaldot_dev"
    fake.write_text("#!/bin/sh\nexec sleep 60\n")
    fake.chmod(0o755)
    import subprocess
    proc = subprocess.Popen(["/bin/sh", str(fake), "--chain", "/c/f.json", "--ws-port", "59990"])
    path = nodes.pid_file("fork")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": proc.pid, "binary": str(fake), "chain_spec": "/c/f.json",
                                "ports": {"ws": 59990}}))
    # The shell has exec'd sleep, so its command line no longer names the node: stop must refuse it.
    import time
    time.sleep(0.3)
    assert "left it alone" in nodes.stop("fork")
    assert proc.poll() is None
    proc.kill()
    proc.wait()
    # A process whose command line does match is terminated, and stop does not wait out its timeout.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(fake), "--chain",
                             "/c/f.json", "--ws-port", "59990"])
    path.write_text(json.dumps({"pid": proc.pid, "binary": sys.executable, "chain_spec": "/c/f.json",
                                "ports": {"ws": 59990}}))
    t = time.monotonic()
    assert nodes.stop("fork", timeout=10) == f"stopped PID {proc.pid}"
    assert time.monotonic() - t < 5 and proc.poll() is not None and not path.exists()


def test_bad_port_env_is_a_node_error(monkeypatch):
    monkeypatch.setenv("FORKLIFT_WS_PORT", "fiftynine")
    with pytest.raises(NodeError, match="not a port number"):
        nodes.resolve_ports()


def test_start_refuses_a_busy_port(cache):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = s.getsockname()[1]
        cfg = NodeConfig("fork", cache / "fork.json", {"ws": busy, "rpc": 59933, "p2p": 30359})
        with pytest.raises(NodeError, match="in use"):
            nodes.start(Path("/nonexistent/portaldot_dev"), cfg, cache / "node.log")
    assert not nodes.pid_file("fork").exists()


def _tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_download_checks_the_tarball_sha256_before_unpacking(cache, monkeypatch):
    blob = _tarball({"portaldot-testnet-macos/portaldot_dev": b"#!bin", "portaldot-testnet-macos/subkey": b"k",
                     "../evil": b"x"})
    with pytest.raises(NodeError, match="does not match"):
        nodes.download_binary(cache / "bin", fetch=lambda url: blob, os_name="macos")
    assert not (cache / "bin" / "portaldot_dev").exists()
    monkeypatch.setitem(nodes.TARBALL_SHA256, "macos", hashlib.sha256(blob).hexdigest())
    seen = []
    out = nodes.download_binary(cache / "bin", fetch=lambda url: seen.append(url) or blob, os_name="macos")
    assert out.read_bytes() == b"#!bin" and os.access(out, os.X_OK)
    assert sorted(p.name for p in (cache / "bin").iterdir()) == ["portaldot_dev"]  # only the node, nothing else
    assert not (cache / "evil").exists()
    assert seen == ["https://github.com/portaldotVolunteer/Portaldot-node/raw/main/portaldot-testnet-macos.tar.gz"]


def test_locate_prefers_explicit_then_env(cache, monkeypatch, tmp_path):
    b = tmp_path / "pd"
    b.write_text("x")
    assert nodes.locate_binary(b) == b
    monkeypatch.setenv("FORKLIFT_NODE_BIN", str(b))
    assert nodes.locate_binary() == b
    with pytest.raises(NodeError, match="does not exist"):
        nodes.locate_binary(tmp_path / "missing")


def test_known_causes_give_hints():
    assert "port" in nodes._hint("Error: Address already in use (os error 48)")
    assert "--execution Wasm" in nodes._hint("Timestamp slot must match CurrentSlot")
    assert "Rosetta" in nodes._hint("Bad CPU type in executable")
    assert nodes._hint("all good") is None


class FakeProc:
    def __init__(self, code=None):
        self.returncode = code

    def poll(self):
        return self.returncode


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def boot_handler(blocks_after: float, clock: Clock, finalize_after: float = 1e9, peers: int = 0,
                 base: int = 0, chain: str = "Forklift fork", break_after: float | None = None):
    babe = (6000).to_bytes(8, "little") + (1200).to_bytes(8, "little")

    def h(method, params):
        # After break_after, only the bare readiness probe still answers.
        if break_after is not None and clock.t >= break_after and (method, bool(params)) != ("chain_getHeader", False):
            raise ConnectionResetError("node went away")
        if method == "system_chain":
            return chain
        if method == "chain_getBlockHash":
            return "0x" + "75" * 32
        if method == "state_call":
            return "0x" + babe.hex()
        if method == "chain_getHeader":
            if params:  # the finalized head's header
                return {"number": hex(base + (1 if clock.t >= finalize_after else 0))}
            return {"number": hex(base + (1 if clock.t >= blocks_after else 0))}
        if method == "chain_getFinalizedHead":
            return "0x" + "fe" * 32
        if method == "system_health":
            return {"peers": peers}
        raise AssertionError(method)
    return h


def test_wait_boot_passes_when_block_one_lands_within_two_slots(cache):
    clock = Clock()
    cfg = NodeConfig("fork", cache / "f.json", {"ws": 59944, "rpc": 59933, "p2p": 30359})
    boot = nodes.wait_boot(FakeProc(), cfg, cache / "node.log", t0=0.0, clock=clock, sleep=clock.sleep,
                           connect=fake_connect(boot_handler(8.0, clock, finalize_after=20.0)))
    assert boot.best == 1 and boot.finalized == 1 and boot.slot_ms == 6000 and boot.epoch_length == 1200
    assert boot.first_block_s <= 13.0 and boot.peers == 0


def test_wait_boot_fails_i5_when_no_block_within_two_slots(cache):
    clock = Clock()
    (cache / "node.log").write_text("Idle (0 peers)\nError: Address already in use\n")
    cfg = NodeConfig("fork", cache / "f.json", {"ws": 59944, "rpc": 59933, "p2p": 30359})
    with pytest.raises(NodeError, match="I5") as e:
        nodes.wait_boot(FakeProc(), cfg, cache / "node.log", t0=0.0, clock=clock, sleep=clock.sleep,
                        connect=fake_connect(boot_handler(60.0, clock)))
    assert "Address already in use" in e.value.log_tail and "port" in e.value.hint


def test_wait_boot_reports_a_node_that_exited(cache):
    clock = Clock()
    cfg = NodeConfig("fork", cache / "f.json", {"ws": 59944, "rpc": 59933, "p2p": 30359})
    with pytest.raises(NodeError, match="exited with code 1"):
        nodes.wait_boot(FakeProc(code=1), cfg, cache / "node.log", t0=0.0, clock=clock, sleep=clock.sleep,
                        connect=fake_connect(boot_handler(0.0, clock)))


def test_i5_on_a_restarted_fork_needs_a_new_block_not_an_old_one(cache):
    clock = Clock()
    cfg = NodeConfig("soak", cache / "f.json", {"ws": 59945, "rpc": 59946, "p2p": 59947}, base_path=cache / "db")
    with pytest.raises(NodeError, match="I5"):  # best is already 500 at readiness, and never moves
        nodes.wait_boot(FakeProc(), cfg, cache / "node.log", t0=0.0, clock=clock, sleep=clock.sleep,
                        connect=fake_connect(boot_handler(1e9, clock, base=500)))
    clock = Clock()
    boot = nodes.wait_boot(FakeProc(), cfg, cache / "node.log", t0=0.0, clock=clock, sleep=clock.sleep,
                           connect=fake_connect(boot_handler(5.0, clock, base=500, finalize_after=0)))
    assert boot.best_at_rpc_ready == 500 and boot.best == 501


def test_readiness_refuses_another_chain_on_our_port(cache):
    clock = Clock()
    cfg = NodeConfig("fork", cache / "f.json", {"ws": 59944, "rpc": 59933, "p2p": 30359}, chain_name="Forklift fork")
    with pytest.raises(NodeError, match="not this fork"):
        nodes.wait_boot(FakeProc(), cfg, cache / "node.log", t0=0.0, clock=clock, sleep=clock.sleep,
                        connect=fake_connect(boot_handler(0.0, clock, chain="Development")))


def test_rpc_failure_after_readiness_is_a_node_error_with_the_log(cache):
    clock = Clock()
    (cache / "node.log").write_text("Thread 'main' panicked\n")
    cfg = NodeConfig("fork", cache / "f.json", {"ws": 59944, "rpc": 59933, "p2p": 30359})
    with pytest.raises(NodeError, match="stopped answering") as e:
        nodes.wait_boot(FakeProc(), cfg, cache / "node.log", t0=0.0, clock=clock, sleep=clock.sleep,
                        connect=fake_connect(boot_handler(1e9, clock, break_after=0.0)))
    assert "panicked" in e.value.log_tail


def test_ensure_no_peers_stops_a_peered_fork(cache, monkeypatch):
    stopped = []
    monkeypatch.setattr(nodes, "stop", lambda name: stopped.append(name) or "stopped")
    boot = nodes.Boot("ws://127.0.0.1:59944", 1, 2, 3, 3, 1, 6000, 1200, "0x00", peers=2)
    with pytest.raises(NodeError, match="must never peer"):
        nodes.ensure_no_peers(boot, "fork")
    assert stopped == ["fork"]


@pytest.mark.skipif(sys.platform == "win32", reason="posix ps")
def test_process_command_of_a_missing_pid_is_none():
    assert nodes.process_command(2**22 + 54321) is None
