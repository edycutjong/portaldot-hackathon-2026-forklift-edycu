from pathlib import Path

import pytest

from forklift import tx
from forklift.hashing import blake2_256

FIX = Path(__file__).parent / "fixtures"
META_HEX = "0x" + (FIX / "metadata-1002.scale").read_bytes().hex()
VERSION = {"specName": "portaldot", "specVersion": 1002, "transactionVersion": 2}
GENESIS = "0x" + "11" * 32
WHALE = "5DvRSgerqB5KHPqWGGnCtRJgJokj91XyGLm3aZ5DKH1rMndf"


@pytest.fixture(scope="module")
def rt():
    return tx.Runtime(META_HEX, VERSION, GENESIS)


@pytest.mark.parametrize("n,encoded", [
    (0, "00"), (1, "04"), (63, "fc"), (64, "0101"), (16383, "fdff"), (16384, "02000100"),
    (2**30 - 1, "feffffff"), (2**30, "0300000040"), (10**14, "0b00407a10f35a"),
])
def test_compact_encoding_matches_scale(n, encoded):
    assert tx.encode_compact(n).hex() == encoded


def test_compact_refuses_negative():
    with pytest.raises(ValueError):
        tx.encode_compact(-1)


def test_pot_to_planck_is_exact():
    assert tx.pot_to_planck("1") == 10**14
    assert tx.pot_to_planck("1,000,000") == 10**20
    assert tx.pot_to_planck("0.00000000000001") == 1
    for bad in ("0.000000000000001", "-1", "abc", "nan", "inf"):
        with pytest.raises(ValueError):
            tx.pot_to_planck(bad)


def test_fmt_pot():
    assert tx.fmt_pot(2 * 10**22) == "200,000,000 POT"
    assert tx.fmt_pot(15 * 10**13) == "1.5 POT"


def test_ss58_round_trip():
    pub = tx.ss58_to_public(WHALE)
    assert len(pub) == 32 and tx.public_to_ss58(pub) == WHALE
    with pytest.raises(ValueError):
        tx.ss58_to_public("not-an-address")


def test_dev_keypair_refuses_anything_but_a_dev_uri():
    assert tx.dev_keypair().ss58_address == "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    with pytest.raises(ValueError):
        tx.dev_keypair("bottom drive obey lake curtain smoke basket hold race lonely fit walk")


def test_runtime_refuses_unknown_signed_extensions(rt, monkeypatch):
    monkeypatch.setattr(tx, "SIGNED_EXTENSIONS", tx.SIGNED_EXTENSIONS[:-1])
    with pytest.raises(tx.TxError, match="unsupported signed extensions"):
        tx.Runtime(META_HEX, VERSION, GENESIS)


def test_compose_sudo_set_balance_uses_multiaddress_and_call_indices(rt):
    inner = rt.compose("Balances", "set_balance", {"who": WHALE, "new_free": 10**14, "new_reserved": 0})
    data = bytes(inner.data.data)
    # Balances is pallet 6, set_balance call 1; MultiAddress::Id = 0x00 ‖ 32-byte account
    assert data[:3] == bytes([6, 1, 0]) and data[3:35] == tx.ss58_to_public(WHALE)
    assert data[35:] == tx.encode_compact(10**14) + tx.encode_compact(0)
    sudo = rt.compose("Sudo", "sudo", {"call": inner.value})
    assert bytes(sudo.data.data) == bytes([14, 0]) + data


def test_compose_unknown_call_is_a_readable_error(rt):
    with pytest.raises(KeyError, match="no call"):
        rt.compose("Balances", "mint", {})
    with pytest.raises(KeyError, match="no pallet"):
        rt.compose("Nope", "x", {})


def test_signing_payload_layout(rt):
    call = b"\x00\x01"
    p = tx.signing_payload(rt, call, nonce=5, tip=0)
    genesis = bytes.fromhex(GENESIS[2:])
    assert p == call + b"\x00" + b"\x14" + b"\x00" + (1002).to_bytes(4, "little") + (2).to_bytes(4, "little") \
        + genesis + genesis


def test_signing_payload_is_hashed_above_256_bytes(rt):
    call = b"\x07" * 300
    raw = call + b"\x00\x00\x00" + (1002).to_bytes(4, "little") + (2).to_bytes(4, "little") \
        + bytes.fromhex(GENESIS[2:]) * 2
    assert tx.signing_payload(rt, call, nonce=0) == blake2_256(raw)


def test_signed_extrinsic_layout_and_signature(rt):
    alice = tx.dev_keypair()
    call = rt.compose("Balances", "transfer", {"dest": WHALE, "value": 10**14})
    call_bytes = bytes(call.data.data)
    xt = tx.signed_extrinsic(rt, call_bytes, alice, nonce=3)
    body = xt[2:]  # this extrinsic is 64..16383 bytes: a 2-byte compact length prefix
    assert xt[:2] == tx.encode_compact(len(body))
    assert body[0] == 0x84 and body[1] == 0x00 and body[2:34] == alice.public_key and body[34] == 0x01
    sig = body[35:99]
    assert body[99:102] == b"\x00\x0c\x00"  # immortal era, nonce 3, tip 0
    assert body[102:] == call_bytes
    assert alice.verify(tx.signing_payload(rt, call_bytes, 3), sig)
    decoded = rt.decode("Extrinsic", "0x" + xt.hex())
    assert decoded["call"]["call_module"] == "Balances" and decoded["call"]["call_function"] == "transfer"
    assert decoded["nonce"] == 3


def test_storage_key_for_a_blake2_128concat_map(rt):
    pub = tx.ss58_to_public(WHALE)
    key = rt.storage_key("System", "Account", pub)
    from forklift.hashing import blake2_128, twox128

    assert key == "0x" + (twox128("System") + twox128("Account") + blake2_128(pub) + pub).hex()
    with pytest.raises(ValueError):
        rt.storage_key("System", "Account")


def test_decode_whale_account_with_legacy_preset(rt):
    raw = ("0x0000000000000000010000000000000000008064" "7593c1333c04" + "00" * 54)  # the 80 bytes read at mainnet #2,725,639
    v = rt.decode_storage("System", "Account", raw)
    assert v["providers"] == 1 and v["data"]["free"] == 2 * 10**22


def test_constants_read_as_little_endian(rt):
    assert rt.constant_int("Balances", "ExistentialDeposit") == 10**14


def test_error_names_come_from_metadata(rt):
    assert rt.error_name(6, 2) == "Balances.InsufficientBalance"
    assert tx.result_of(rt, {"Err": {"Module": {"index": 6, "error": 2}}}) == (False, "Balances.InsufficientBalance")
    assert tx.result_of(rt, {"Ok": ()}) == (True, None)
    assert tx.result_of(rt, "Ok") == (True, None)
    assert tx.result_of(rt, {"Err": {"BadOrigin": None}}) == (False, "BadOrigin")
