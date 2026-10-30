"""Fork Receipt v2 (Revive contract on V3): ABI encoding, the deployed bytecode, ReviveApi decoding (offline)."""

import re
import shutil
import subprocess
from importlib import resources

import pytest
from click.testing import CliRunner

from forklift import revive_receipt as rr
from forklift.cli import main
from forklift.rpc import is_read_method, is_write_method

BH = "0xec57106c405af29ab9a5a75f9281050bd1f2755c4592ba3b55e0d19d4603d8ac"  # V3 #118,061
ROOT = "0xfe4962de728b8a17601f3e97dc884a4c21be38a981b8c36cef47f02248c430a4"
MANIFEST = {"profile": "v3-read (spec 3002)", "manifest_root": ROOT, "load": {"keys_lifted": 5508},
            "source": {"block_hash": BH, "block_number": 118061}}
ATTESTER = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"

# ReviveApi_instantiate replies from V3 (2026-10-31, funder origin, read-only state_call):
# the deployed init code (Ok, 18.882 tPOT deposit) and a 991-byte solc build (Err: Revive.ContractReverted,
# the funder cannot cover its 88.22 tPOT deposit).
DRY_OK = bytes.fromhex(
    "ca9d280000ca9d2800000100d043d24eb5060000000000000000000100d043d24eb506000000000000000000d566a1eb958e0200000000"
    "0000000000000000000029016004355f3560e01c80637f86bc5314602c5763198a27aa14601e575f80fd5b80545f52195460205260405f"
    "f35b5060243560443590808355818319555f526020523360405260605fa10052f0edd92af9ff410ca63ccc83735552e1b2abc7")
DRY_ERR = bytes.fromhex(
    "02005b000002005b000001000000000000000000000000000000000100000000000000000000000000000000674602000000000000000000"
    "0000000001035017000000")


def _yul() -> str:
    return resources.files("forklift.contracts").joinpath("ForkReceipt.yul").read_text()


def test_selectors_in_the_yul_are_the_abi_selectors():
    cases = dict((sig, sel) for sel, sig in re.findall(r"case 0x([0-9a-f]{8}) \{ // (\S+)", _yul()))
    assert cases == {rr.ATTEST: rr.selector(rr.ATTEST).hex(), rr.RECEIPT: rr.selector(rr.RECEIPT).hex()}
    assert rr.selector(rr.ATTEST).hex() == "7f86bc53"
    assert rr.selector(rr.RECEIPT).hex() == "198a27aa"


def test_interface_declares_the_same_functions_and_an_anonymous_event():
    sol = resources.files("forklift.contracts").joinpath("IForkReceipt.sol").read_text()
    assert "function attest(bytes32 blockHash, bytes32 manifestRoot, uint64 keysLifted) external;" in sol
    assert "function receipt(bytes32 blockHash) external view returns (bytes32 manifestRoot, uint64 keysLifted);" in sol
    assert re.search(r"event Attested\(bytes32 indexed blockHash, bytes32 manifestRoot, uint64 keysLifted, "
                     r"address attester\)\s+anonymous;", sol)


def test_init_code_is_the_committed_build_and_carries_both_selectors():
    code = rr.init_code()
    assert len(code) == 84
    runtime = code[10:]  # the 10-byte constructor copies and returns the rest
    assert code[:10].hex() == "604a80600a5f395ff3fe" and len(runtime) == 0x4a
    assert rr.selector(rr.ATTEST) in runtime and rr.selector(rr.RECEIPT) in runtime
    assert bytes([0xa1]) in runtime  # LOG1: the anonymous event, block hash as its only topic


def test_init_code_is_the_solc_build_of_the_yul():
    solc = shutil.which("solc")
    if solc is None:
        pytest.skip("solc not installed (0.8.30: https://binaries.soliditylang.org)")
    version = subprocess.run([solc, "--version"], capture_output=True, text=True, check=True).stdout
    if "0.8.30" not in version:
        pytest.skip("the committed build is solc 0.8.30's")
    src = resources.files("forklift.contracts").joinpath("ForkReceipt.yul")
    out = subprocess.run([solc, "--strict-assembly", "--optimize", "--evm-version", "cancun", "--bin", str(src)],
                         capture_output=True, text=True, check=True).stdout
    assert bytes.fromhex(out.strip().splitlines()[-1]) == rr.init_code()


def test_attest_data_is_abi_encoded():
    data = rr.attest_data(BH, ROOT, 5508)
    assert len(data) == 4 + 3 * 32
    assert data[:4].hex() == "7f86bc53"
    assert data[4:36].hex() == BH[2:] and data[36:68].hex() == ROOT[2:]
    assert int.from_bytes(data[68:], "big") == 5508


@pytest.mark.parametrize("keys", [-1, 2**64])
def test_attest_data_refuses_keys_outside_uint64(keys):
    with pytest.raises(ValueError):
        rr.attest_data(BH, ROOT, keys)


def test_attest_data_refuses_short_hashes():
    with pytest.raises(ValueError):
        rr.attest_data("0x1234", ROOT, 1)


def test_receipt_query_and_return():
    assert rr.receipt_query(BH) == bytes.fromhex("198a27aa" + BH[2:])
    ret = bytes.fromhex(ROOT[2:]) + (5508).to_bytes(32, "big")
    assert rr.decode_receipt(ret) == {"manifest_root": ROOT, "keys_lifted": 5508}
    assert rr.decode_receipt(bytes(64)) == {"manifest_root": "0x" + "00" * 32, "keys_lifted": 0}
    with pytest.raises(ValueError):
        rr.decode_receipt(bytes(63))


def test_decode_attested_matches_the_log_layout():
    # The log an EVM run of the same bytecode emitted (anvil, 2026-10-31): topic = block hash,
    # data = root ‖ keys ‖ attester (left-padded).
    data = bytes.fromhex(ROOT[2:]) + (5508).to_bytes(32, "big") + bytes(12) + bytes.fromhex(ATTESTER[2:])
    got = rr.decode_attested([BH], data)
    assert got == {"block_hash": BH, "manifest_root": ROOT, "keys_lifted": 5508, "attester": ATTESTER}
    assert rr.differs(got, MANIFEST) == []
    with pytest.raises(ValueError):
        rr.decode_attested([BH, BH], data)


def test_differs_names_each_wrong_field():
    assert rr.differs({"manifest_root": ROOT.upper().replace("0X", "0x"), "keys_lifted": 5508}, MANIFEST) == []
    assert rr.differs({"manifest_root": "0x" + "00" * 32, "keys_lifted": 5507}, MANIFEST) == [
        "manifest_root", "keys_lifted"]


def test_decode_contract_result_ok_instantiate():
    d = rr.decode_contract_result(DRY_OK, instantiate=True)
    assert d.success and not d.reverted
    assert d.storage_deposit == 18_882 * 10**11  # 18.882 tPOT (14 decimals)
    assert d.weight_required == {"ref_time": 665_458, "proof_size": 0}
    assert d.data == rr.init_code()[10:]  # instantiate returns the runtime code it stored
    assert d.address == "0x52f0edd92af9ff410ca63ccc83735552e1b2abc7"


def test_decode_contract_result_dispatch_error():
    d = rr.decode_contract_result(DRY_ERR, instantiate=True)
    assert not d.ok and not d.success
    assert d.error == "0x035017000000"  # DispatchError::Module { index 80 (Revive), error 23 (ContractReverted) }


def test_limits_have_floors_and_a_margin():
    assert rr.weight_limit({"ref_time": 665_458, "proof_size": 0}) == {"ref_time": 10**10, "proof_size": 2_000_000}
    assert rr.weight_limit({"ref_time": 10**10, "proof_size": 10**6})["ref_time"] == 2 * 10**11
    assert rr.deposit_limit(1000) == 1100 and rr.deposit_limit(-5) == 0


def test_emitted_filters_by_contract_and_decodes():
    contract = "0x52f0edd92af9ff410ca63ccc83735552e1b2abc7"
    data = "0x" + ROOT[2:] + (5508).to_bytes(32, "big").hex() + "00" * 12 + ATTESTER[2:]
    events = [
        {"pallet": "Revive", "event": "ContractEmitted", "attributes": {"contract": contract.upper().replace("0X", "0x"),
                                                                        "data": data, "topics": [BH]}},
        {"pallet": "Revive", "event": "ContractEmitted", "attributes": {"contract": "0x" + "11" * 20, "data": data,
                                                                        "topics": [BH]}},
        {"pallet": "System", "event": "ExtrinsicSuccess", "attributes": {}},
    ]
    got = rr.emitted(events, contract)
    assert len(got) == 1 and got[0]["decoded"]["keys_lifted"] == 5508
    bad = rr.emitted([{**events[0], "attributes": {**events[0]["attributes"], "topics": []}}], contract)
    assert bad[0]["decoded"] is None and "1 topic" in bad[0]["error"]


def test_instantiated_reads_the_contract_field():
    ev = [{"pallet": "Revive", "event": "Instantiated",
           "attributes": {"deployer": "0x2c85e86717368d596e379155a3ec4ad1e46ebe7a", "contract": "0xABCDEF" + "00" * 17}}]
    assert rr.instantiated(ev) == "0xabcdef" + "00" * 17
    assert rr.instantiated([]) is None


def test_state_call_is_a_read():
    assert is_read_method("state_call") and not is_write_method("state_call")


def test_receipt_v3_needs_a_signer():
    r = CliRunner().invoke(main, ["receipt", "--target", "v3", "--manifest", "/nonexistent.json"])
    assert r.exit_code != 0 and "--signer" in r.output


def test_receipt_fork_refuses_v3_only_options(tmp_path):
    r = CliRunner().invoke(main, ["receipt", "--signer", str(tmp_path / "s")])
    assert r.exit_code != 0 and "--target v3" in r.output


def test_receipt_v3_refuses_fork_only_options(tmp_path):
    r = CliRunner().invoke(main, ["receipt", "--target", "v3", "--signer", str(tmp_path / "s"), "--no-top-up"])
    assert r.exit_code != 0 and "--target fork" in r.output


def test_receipt_v3_refuses_a_substrate_address(tmp_path):
    r = CliRunner().invoke(main, ["receipt", "--target", "v3", "--signer", str(tmp_path / "s"),
                                  "--address", "5EnCf9PauY6RtW5jCTxCXQFCMEo5U2kP3pLexZahX1HCejvg"])
    assert r.exit_code != 0 and "H160" in r.output
