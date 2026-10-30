"""Fork Receipt v2 on the live V3 testnet: re-read the recorded deploy and attest by extrinsic id (read-only)."""

import json
from pathlib import Path

import pytest

from forklift import revive_receipt as rr

pytestmark = pytest.mark.live

RUN = json.loads((Path(__file__).parent.parent / "evidence" / "receipt-v3.json").read_text())["runs"][0]


def test_attest_re_read_by_extrinsic_id_equals_the_manifest():
    got = rr.read_back(RUN["attest"]["extrinsic_id"])
    assert got["call"] == "Revive.call" and got["success"]
    assert got["tx_hash"] == RUN["attest"]["tx_hash"]
    assert got["contract"] == RUN["address"]
    decoded = got["emitted"][0]["decoded"]
    m = RUN["manifest"]
    assert decoded == {"block_hash": m["block_hash"], "manifest_root": m["manifest_root"],
                       "keys_lifted": m["keys_lifted"], "attester": RUN["attester_h160"]}
    assert got["stored_now"]["manifest_root"] == m["manifest_root"]
    assert got["stored_now"]["keys_lifted"] == m["keys_lifted"]


def test_deploy_re_read_by_extrinsic_id_names_the_contract():
    got = rr.read_back(RUN["deploy"]["extrinsic_id"])
    assert got["call"] == "Revive.instantiate_with_code" and got["success"]
    assert got["tx_hash"] == RUN["deploy"]["tx_hash"]
    assert got["contract"] == RUN["address"]
