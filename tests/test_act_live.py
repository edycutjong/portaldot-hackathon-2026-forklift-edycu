"""Live: fund, as and the Receipt on a fresh fork, each command in a fresh process. Run with: pytest -m live

Ports come from FORKLIFT_WS_PORT / FORKLIFT_RPC_PORT / FORKLIFT_P2P_PORT (else 9944 / 9933 / 30333).
The test stops its own fork.
"""

import json

import pytest

from forklift import receipt
from forklift.node import stop
from tests.test_fork_live import forklift, pinned_block

WHALE = "5DvRSgerqB5KHPqWGGnCtRJgJokj91XyGLm3aZ5DKH1rMndf"
NOMINATOR = "5Cf8KJFLVrY43yEZN3vgFktGZvhUhrt7WBvfjJoTr54EjchW"
DEMO = "5F1KbjNTUu6xmMCcSz2wrv7b627BKS7hUTzWFqdmpvb3rMcE"  # //ForkliftDemo


@pytest.mark.live
def test_fund_as_and_receipt_on_a_fresh_fork():
    name = "acttest"
    try:
        up = forklift("up", "--at", pinned_block(), "--name", name, "--json")
        assert up.returncode == 0, up.stderr + up.stdout
        m = json.loads(up.stdout)

        f = forklift("fund", DEMO, "1000", "--name", name, "--json")
        assert f.returncode == 0, f.stdout + f.stderr
        fj = json.loads(f.stdout)
        assert fj["changed"]["System.Account"]["after"]["data"]["free"] == 1000 * 10**14

        a = forklift("as", WHALE, "Balances.transfer", json.dumps([DEMO, 10**14]), "--name", name, "--json")
        assert a.returncode == 0, a.stdout + a.stderr
        aj = json.loads(a.stdout)
        acct = aj["changed"]["System.Account"]
        assert acct["before"]["data"]["free"] - acct["after"]["data"]["free"] == 10**14
        assert acct["before"]["nonce"] == acct["after"]["nonce"]  # sudo_as does not bump the nonce

        bad = forklift("as", WHALE, "Balances.transfer", json.dumps([DEMO, 10**30]), "--name", name, "--json")
        assert bad.returncode == 5, bad.stdout + bad.stderr  # landed, but the call failed
        assert json.loads(bad.stdout)["dispatch"]["error"] == "Balances.InsufficientBalance"

        r = forklift("receipt", "--name", name, "--json")
        assert r.returncode == 0, r.stdout + r.stderr
        rj = json.loads(r.stdout)
        assert rj["deployed_now"] and rj["code_hash"] == receipt.code_hash()
        assert [s["kind"] for s in rj["steps"]] == ["instantiate", "rent_top_up", "attest"]
        assert rj["attest"]["matches_manifest"] and rj["rent"]["proof"]["no_rent_charged"]
        decoded = rj["attest"]["emitted"][0]["decoded"]
        assert decoded == {"mainnet_block_hash": m["source"]["block_hash"], "manifest_root": m["manifest_root"],
                           "keys_lifted": m["load"]["keys_lifted"]}

        again = forklift("receipt", "--name", name, "--json")  # second run: reuse, no deploy, no top-up
        assert again.returncode == 0, again.stdout + again.stderr
        aj2 = json.loads(again.stdout)
        assert not aj2["deployed_now"] and aj2["address"] == rj["address"]
        assert [s["kind"] for s in aj2["steps"]] == ["attest"]

        t = forklift("tx", rj["steps"][-1]["extrinsic"]["extrinsic_id"], "--name", name, "--json")
        assert t.returncode == 0 and json.loads(t.stdout)["tx_hash"] == rj["steps"][-1]["extrinsic"]["tx_hash"]
    finally:
        stop(name)
