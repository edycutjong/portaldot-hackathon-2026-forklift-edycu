"""Live exact path (pytest -m live): a fresh-process Chopsticks fork of mainnet at the P5 pin on mainnet's own
runtime, verified against mainnet and the lift, a sudo_as transfer applied on it, then stopped. Mainnet is
only read.

The WebSocket port comes from FORKLIFT_WS_PORT (else 9944).
"""

import json
import os
import subprocess
import sys

import pytest

from forklift.chains import MAINNET

PIN = "2725953"
MAINNET_CODE = "0xc8e7e77fe84f1231787e75a751af48c52e68675ad55de062c81cc8df24066e55"  # :code at #2,725,953
WHALE = "5DvRSgerqB5KHPqWGGnCtRJgJokj91XyGLm3aZ5DKH1rMndf"
DEST = "5Gmi3tL5sUrUxXNzHmFhm7tsfs8xr2Ht89s4r3sA2pDKDAaE"


def forklift(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "forklift.cli", *args], capture_output=True, text=True,
                          timeout=900, env=os.environ.copy())


@pytest.mark.live
def test_fresh_process_exact_fork_runs_mainnets_runtime_verifies_and_takes_a_call():
    name = "liveexact"
    try:
        up = forklift("up", "--runtime", "exact", "--at", PIN, "--name", name, "--json")
        assert up.returncode == 0, up.stderr + up.stdout
        m = json.loads(up.stdout)
        assert m["fork"]["label"] == "fork-1002-exact" and m["fork"]["genesis_hash"] == MAINNET.genesis_hash
        assert m["fork"]["code_hash"] == m["source"]["code_hash"] == MAINNET_CODE
        assert m["fork"]["overrides_at"]["number"] == int(PIN) + 1
        v = forklift("verify", "--name", name, "--sample", "50", "--json")
        assert v.returncode == 0, v.stderr + v.stdout
        r = json.loads(v.stdout)
        assert r["checks"]["key_set"]["ok"] and r["checks"]["all_lifted_values"]["ok"] and r["checks"]["mmr_range"]["ok"]
        a = forklift("as", WHALE, "Balances.transfer", json.dumps({"dest": DEST, "value": 10 ** 17}),
                     "--name", name, "--json")
        assert a.returncode == 0, a.stderr + a.stdout
        assert "Balances.Transfer" in [e["event"] for e in json.loads(a.stdout)["inner_events"]]
    finally:
        forklift("down", "--name", name)
