"""Live V3 path (pytest -m live): a fresh-process Chopsticks fork of the V3 testnet at its finalized head,
verified against the live chain and the lift, then stopped. Reads V3 only; sends nothing to it.

The WebSocket port comes from FORKLIFT_WS_PORT (else 9944).
"""

import json
import os
import subprocess
import sys

import pytest

from forklift.chains import V3


def forklift(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "forklift.cli", *args], capture_output=True, text=True,
                          timeout=900, env=os.environ.copy())


@pytest.mark.live
def test_fresh_process_v3_fork_serves_the_lifted_block_and_verifies():
    name = "livev3"
    try:
        up = forklift("up", "--target", "v3", "--at", "head", "--name", name, "--json")
        assert up.returncode == 0, up.stderr + up.stdout
        m = json.loads(up.stdout)
        assert m["fork"]["genesis_hash"] == V3.genesis_hash and m["fork"]["code_hash"] == V3.code_hash
        assert m["fork"]["fork_block"]["hash"] == m["source"]["block_hash"]
        v = forklift("verify", "--name", name, "--sample", "50", "--json")
        assert v.returncode == 0, v.stderr + v.stdout
        r = json.loads(v.stdout)
        assert r["checks"]["key_set"]["ok"] and r["checks"]["all_lifted_values"]["ok"]
        assert r["classes"] == {"identical": 50}
    finally:
        forklift("down", "--name", name)


@pytest.mark.live
def test_v3_target_refuses_mainnet_only_options():
    r = forklift("up", "--target", "v3", "--runtime", "fast")
    assert r.returncode == 64 and "mainnet target" in r.stderr
