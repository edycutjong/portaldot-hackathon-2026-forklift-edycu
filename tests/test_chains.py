import pytest

from forklift.chains import MAINNET, V3, UnknownChain, for_spec, identify, units
from forklift.rpc import MAINNET_GENESIS


def test_v3_is_identified_by_genesis_and_code_hash():
    assert identify(V3.genesis_hash, V3.code_hash) is V3
    assert identify(V3.genesis_hash.upper().replace("0X", "0x"), V3.code_hash.upper().replace("0X", "0x")) is V3


def test_a_v3_runtime_upgrade_is_refused_until_re_pinned():
    with pytest.raises(UnknownChain, match="re-pin"):
        identify(V3.genesis_hash, "0x" + "00" * 32)


def test_unknown_genesis_is_refused():
    with pytest.raises(UnknownChain):
        identify("0x" + "11" * 32, None)


def test_mainnet_profile_matches_the_write_guard_and_is_never_writable():
    assert MAINNET.genesis_hash == MAINNET_GENESIS
    assert identify(MAINNET_GENESIS, "0x" + "22" * 32) is MAINNET  # mainnet's code hash is not pinned
    assert not MAINNET.writable and V3.writable


def test_v3_live_identity_values():
    assert V3.spec == ("portaldot-v3", 3002)
    assert (V3.symbol, V3.decimals, V3.ss58) == ("tPOT", 14, 42)
    assert V3.endpoint == "wss://test-node-v3.portaldot.dev"
    assert for_spec("portaldot-v3") is V3 and for_spec("portaldot") is MAINNET and for_spec("x") is None


def test_explorer_link_is_node_console_with_the_v3_rpc():
    url = V3.explorer_block("0xabc")
    assert url == ("https://node-console.portaldot.dev/?rpc=wss%3A%2F%2Ftest-node-v3.portaldot.dev"
                   "#/explorer/query/0xabc")


def test_units():
    assert units(V3, 10**14) == "1 tPOT"
    assert units(V3, 1_170_476_213_578) == "0.01170476213578 tPOT"
    assert units(V3, -15 * 10**13) == "-1.5 tPOT"
