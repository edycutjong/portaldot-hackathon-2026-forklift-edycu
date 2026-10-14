"""Live test against the public Portaldot mainnet node (read-only). Run with: pytest -m live"""

import json
from pathlib import Path

import pytest

from forklift.cli import run_lift

EVIDENCE = Path(__file__).resolve().parents[1] / "evidence"


@pytest.mark.live
def test_lift_at_pinned_block_reproduces_the_recorded_manifest_root(tmp_path):
    recorded = sorted(EVIDENCE.glob("lift-*.json"))
    if not recorded:
        pytest.skip("no recorded lift in evidence/")
    ev = json.loads(recorded[0].read_text())
    m = run_lift("wss://mainnet.portaldot.io", ev["pin"]["block_hash"], tmp_path, resume=False)
    assert m["source"]["block_number"] == ev["pin"]["block_number"]
    assert m["load"]["keys_lifted"] == ev["keys_lifted"]
    assert m["manifest_root"] == ev["manifest_root"]
