"""``runtime-diff`` and ``runtime-compare``, offline: the three captured metadata blobs (mainnet 1002, the dev
runtime that ``--runtime fast`` boots, V3 3002) and hand-made rehearsal records."""

import gzip
from functools import lru_cache
from pathlib import Path

from forklift import runtimediff as rd
from forklift import tx
from forklift.chains import MAINNET

FIX = Path(__file__).parent / "fixtures"
MAIN = "0x" + (FIX / "metadata-1002.scale").read_bytes().hex()
DEV = "0x" + (FIX / "metadata-1002-dev.scale").read_bytes().hex()
V3MD = "0x" + gzip.decompress((FIX / "metadata-3002.scale.gz").read_bytes()).hex()
VERSION_1002 = {"specName": "portaldot", "implName": "substrate-node", "specVersion": 1002, "transactionVersion": 2,
                "apis": [["0xdf6acb689907609b", 3], ["0x37e397fc7c91f5e4", 1]]}
VERSION_3002 = {"specName": "portaldot-v3", "specVersion": 3002, "transactionVersion": 2, "stateVersion": 1,
                "apis": [["0xdf6acb689907609b", 5], ["0x37e397fc7c91f5e4", 1], ["0x17a6bc0d0062aeb3", 1]]}


def _reg(md):
    return tx.Runtime(md, VERSION_1002, MAINNET.genesis_hash).registry


@lru_cache(maxsize=1)
def _report():
    return rd.build_report(mainnet={"block_number": 2725953, "block_hash": "0x01", "code_hash": "0xaa"},
                           v3={"block_number": 118061, "block_hash": "0x02", "code_hash": "0xbb"},
                           mainnet_md=MAIN, mainnet_version=VERSION_1002, mainnet_reg=_reg(MAIN),
                           v3_md=V3MD, v3_version=VERSION_3002)


def _pallet(name):
    return next(p for p in _report()["pallets"] if p["pallet"] == name)


def test_report_names_real_call_changes_and_ignores_unused_parameter_underscores():
    staking, system = _pallet("Staking"), _pallet("System")
    bond = next(c for c in staking["calls"]["changed"] if c["call"] == "bond")
    assert bond["why"] == "arguments (controller, value, payee) → (value, payee)"
    assert "remark" in system["calls"]["same"]  # _remark (1002) and remark (V3): same position, same bytes
    assert "update_staking_limits" in staking["calls"]["only_mainnet"]


def test_report_constants_and_versions_are_read_from_the_runtimes():
    ed = next(k for k in _pallet("Balances")["constants"]["differ"] if k["constant"] == "ExistentialDeposit")
    assert (ed["mainnet"], ed["v3"]) == ("100,000,000,000,000", "10,000,000,000,000")
    v = _report()["versions"]
    assert v["apis_same_version"] == 1 and v["apis_other_version"] == 1 and v["apis_only_v3"] == 1
    assert {"field": "stateVersion", "mainnet": None, "v3": 1} in v["fields"]


def test_report_counts_add_up_and_no_type_is_left_opaque_in_a_call():
    r = _report()
    t = r["totals"]
    assert t["pallets_both"] + t["pallets_only_mainnet"] == len(rd.decode_metadata(MAIN)["V13"]["modules"])
    assert t["pallets_both"] + t["pallets_only_v3"] == len(rd.decode_metadata(V3MD)["V14"]["pallets"])
    both = [p for p in r["pallets"] if "only_on" not in p]
    assert t["calls_same"] + t["calls_changed"] == sum(len(p["calls"]["same"]) + len(p["calls"]["changed"]) for p in both)
    assert not [c for p in both for c in p["calls"]["changed"] if "opaque" in c["why"]]
    assert t["calls_index_moved"] > 0


def test_report_renders_the_same_bytes_twice():
    a, b = rd.render_markdown(_report()), rd.render_markdown(_report())
    assert a == b and rd.render_json(_report()) == rd.render_json(_report())
    assert "## Calls whose arguments changed" in a and "`Staking.bond`" in a


def test_the_fast_and_exact_runtimes_differ_in_twelve_time_scaled_constants_and_one_call():
    c = rd.constants_between(DEV, _reg(DEV), MAIN, _reg(MAIN))
    by = {k["constant"]: (k["fast"], k["exact"]) for k in c["constants_differ"]}
    assert len(by) == 12
    assert by["Timestamp.MinimumPeriod"] == ("3,000", "30,000")
    assert by["Babe.ExpectedBlockTime"] == ("6,000", "60,000")
    assert [x["call"] for x in c["calls_changed_list"]] == ["Staking.update_staking_limits"]
    assert "max_validator_bond" in c["calls_changed_list"][0]["why"]


def _run(label, ext, code, step, fee=0):
    return {"label": label, "extrinsic_id": ext, "tx_hash": f"0x{len(ext):02x}", "dispatch": {"ok": True, "error": None},
            "inner_events": [{"event": "Balances.Transfer", "attributes": [1, 2]}], "deltas": {"5W": -10, "5D": 10},
            "spec": ["portaldot", 1002], "impl_version": 0, "code_hash": code, "sudo_key_fee": fee,
            "dispatch_info": {"weight": 7}, "timestamp_step_ms": step}


def test_compare_rehearsals_marks_what_differs_and_says_why():
    rows = {r["field"]: r for r in rd.compare_rehearsals(_run("fork-1002-fast", "5-1", "0xdev", 6000),
                                                         _run("fork-1002-exact", "2725955-1", "0xmain", 60000))}
    assert rows["delta 5W"]["same"] and rows["inner_events"]["same"] and rows["dispatch_weight"]["same"]
    for f in ("code_hash", "timestamp_step_ms", "block_number", "tx_hash"):
        assert not rows[f]["same"] and rd.WHY[f]
    assert (rows["block_number"]["fast"], rows["block_number"]["exact"]) == (5, 2725955)


def test_runtime_call_arguments_are_one_leaf_and_constant_bytes_are_hex():
    assert rd.is_runtime_call("Box<<T as Config>::Call>") and rd.is_runtime_call("Box<<T as Config>::RuntimeCall>")
    assert not rd.is_runtime_call("T::Balance")
    assert rd._bytes_hex("0x2a00") == "0x2a00" and rd._bytes_hex("*\x00") == "0x2a00"
    assert rd.show_value("u32", "0x30750000") == "30,000"


def test_a_constant_with_the_same_number_in_another_width_names_both_types():
    row = next(k for k in _pallet("Multisig")["constants"]["differ"] if k["constant"] == "MaxSignatories")
    assert (row["mainnet"], row["v3"]) == ("100 (u16)", "100 (u32)")
