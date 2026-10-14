"""Live fork tests: boot the official portaldot_dev on a lift of public mainnet. Run with: pytest -m live

Ports come from FORKLIFT_WS_PORT / FORKLIFT_RPC_PORT / FORKLIFT_P2P_PORT (else 9944 / 9933 / 30333).
Each test stops its own fork.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from forklift import up as up_mod
from forklift.node import resolve_ports, stop
from forklift.splice import SYSTEM_ACCOUNT, account_key, DEV_KEY

EVIDENCE = Path(__file__).resolve().parents[1] / "evidence"


def pinned_block() -> str:
    recorded = sorted(EVIDENCE.glob("lift-[0-9]*.json"))
    if not recorded:
        pytest.skip("no recorded lift in evidence/")
    return json.loads(recorded[0].read_text())["pin"]["block_hash"]


def forklift(*args: str) -> subprocess.CompletedProcess:
    """Run the CLI in a fresh process."""
    return subprocess.run([sys.executable, "-m", "forklift.cli", *args], capture_output=True, text=True,
                          timeout=900, env=os.environ.copy())


@pytest.mark.live
def test_fresh_process_up_authors_finalizes_and_verifies():
    name = "livetest"
    try:
        up = forklift("up", "--at", pinned_block(), "--name", name, "--json")
        assert up.returncode == 0, up.stderr + up.stdout
        m = json.loads(up.stdout)
        assert m["fork"]["label"] == "fork-1002-fast"
        assert m["node"]["boot"]["finalized"] >= 1 and m["node"]["boot"]["peers"] == 0
        acc = m["fork"]["accounting"]
        assert acc["genesis_identical"] + acc["dev_kept"] + acc["overridden"] + acc["reset"] == acc["keys_lifted"]
        v = forklift("verify", "--name", name, "--sample", "60", "--json")
        assert v.returncode == 0, v.stdout + v.stderr
        report = json.loads(v.stdout)
        assert report["ok"] and not report["mismatches"] and report["classes"].get("identical")
    finally:
        stop(name)


@pytest.mark.live
def test_a_corrupted_splice_makes_verify_exit_2(monkeypatch):
    """Test-only corruption: every mainnet System.Account value gets a different nonce byte."""
    real = up_mod.splice_state
    dev_account = "0x" + account_key(DEV_KEY).hex()
    prefix = "0x" + SYSTEM_ACCOUNT.hex()

    def corrupted(*a, **kw):
        sp = real(*a, **kw)
        for k, v in sp.top.items():
            if k.startswith(prefix) and k != dev_account:
                sp.top[k] = "0x" + format((int(v[2:4], 16) + 1) % 256, "02x") + v[4:]
        return sp

    monkeypatch.setattr(up_mod, "splice_state", corrupted)
    name = "corrupttest"
    try:
        m = up_mod.run_up("wss://mainnet.portaldot.io", pinned_block(), name=name, ports=resolve_ports())
        assert m["node"]["boot"]["finalized"] >= 1  # the corrupted fork still runs: only verify can tell
        v = forklift("verify", "--name", name, "--sample", "50", "--seed", "1", "--json")
        assert v.returncode == 2, v.stdout + v.stderr
        report = json.loads(v.stdout)
        assert not report["ok"] and report["mismatches"]
        assert {mm["item"] for mm in report["mismatches"]} == {"System.Account"}
    finally:
        stop(name)
