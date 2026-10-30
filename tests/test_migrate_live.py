"""Live: ``forklift migrate-report`` reads both pinned blocks and is byte-identical across runs (needs network)."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from forklift.cli import main

pytestmark = pytest.mark.live
EVIDENCE = Path(__file__).resolve().parent.parent / "evidence"


def test_report_is_byte_identical_twice(tmp_path):
    outs = []
    for i in (1, 2):
        md, js = tmp_path / f"r{i}.md", tmp_path / f"r{i}.json"
        res = CliRunner().invoke(main, ["migrate-report", "--mainnet-evidence", str(EVIDENCE / "lift-2725953.json"),
                                        "--v3-evidence", str(EVIDENCE / "lift-v3-118061.json"),
                                        "--out", str(md), "--json-out", str(js)])
        assert res.exit_code == 0, res.output
        outs.append((md.read_bytes(), js.read_bytes()))
    assert outs[0] == outs[1]
    r = json.loads(outs[0][1])
    assert r["keys_lifted_total"] == 16324 and sum(t["keys_lifted"] for t in r["totals"].values()) == 16324
