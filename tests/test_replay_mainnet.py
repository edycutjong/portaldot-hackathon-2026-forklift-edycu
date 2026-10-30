"""``forklift replay <block>:<index>``: the pure parts (id parsing, comparison, naming accounts, drop reason)."""

import copy

import pytest

from forklift.rehearse import RehearseError
from forklift.replay import (_dropped_reason, _named_accounts, _parse_mainnet_id, compare_1002, replay)

ALICE = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
BOB = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


def outcome(where, **over):
    o = {"where": where, "tx_hash": "0xaa", "success": True, "error": None, "block_number": 11, "fee": 5,
         "events": [{"pallet": "Balances", "event": "Transfer",
                     "attributes": [{"type": "AccountId", "value": ALICE}, {"type": "Balance", "value": 7}]},
                    {"pallet": "Treasury", "event": "Deposit", "attributes": [{"type": "Balance", "value": 5}]}],
         "deltas": {ALICE: {"free": -12, "nonce": 1}}}
    o.update(over)
    return o


def test_ids_with_a_colon_are_mainnet():
    assert _parse_mainnet_id("2700013:1") == (2700013, 1)
    for bad in ("2700013-1", "x:1", "0:1", "5:-1", "5"):
        with pytest.raises(RehearseError):
            _parse_mainnet_id(bad)


def test_replay_rejects_a_malformed_mainnet_id_before_any_network():
    with pytest.raises(RehearseError):
        replay("12:x", ws_port=59944)


def test_equal_outcomes_match_field_by_field():
    r = compare_1002(outcome("fork"), outcome("mainnet"), {ALICE: True}, True)
    assert r["match"] and r["differs"] == [] and r["fields"] == len(r["rows"])
    assert {"tx_hash", "success", "error", "event_sequence", "inclusion_height", "fee (Treasury.Deposit)"} <= \
        {x["field"] for x in r["rows"]}


def test_a_different_fee_or_delta_is_reported_never_smoothed_over():
    fork = outcome("fork", fee=6)
    fork["deltas"] = {ALICE: {"free": -13, "nonce": 1}}
    r = compare_1002(fork, outcome("mainnet"), {ALICE: True}, True)
    assert not r["match"]
    assert r["differs"] == ["fee (Treasury.Deposit)", f"delta {ALICE} free"]


def test_different_parent_state_or_runtime_fails_the_comparison():
    r = compare_1002(outcome("fork"), outcome("mainnet"), {ALICE: False}, False)
    assert r["differs"] == ["runtime_code_hash_at_parent", f"parent System.Account {ALICE}"]


def test_the_block_author_account_is_not_compared_but_its_amount_is():
    dep = lambda who, amt: {"pallet": "Balances", "event": "Deposit",  # noqa: E731
                            "attributes": [{"type": "AccountId", "value": who}, {"type": "Balance", "value": amt}]}
    a, b = outcome("fork"), outcome("mainnet")
    a["events"].append(dep(ALICE, 3))
    b["events"].append(dep(BOB, 3))
    assert compare_1002(a, b, {}, True)["match"]
    b2 = copy.deepcopy(b)
    b2["events"][-1] = dep(BOB, 4)
    assert compare_1002(a, b2, {}, True)["differs"] == ["event[2] Balances.Deposit"]


def test_named_accounts_come_from_typed_args_and_events_only():
    pub = "0x" + "d4" * 32  # not an account-typed value
    args = [{"name": "dest", "type": "LookupSource", "value": BOB}, {"name": "value", "type": "Compact<Balance>",
            "value": 5}, {"name": "memo", "type": "Bytes", "value": pub}]
    events = [{"pallet": "Balances", "event": "Transfer",
               "attributes": [{"type": "AccountId", "value": ALICE}, {"type": "AccountId", "value": BOB}]}]
    assert _named_accounts(args, events) == [BOB, ALICE]


def test_dropped_reason_quotes_chopsticks_own_message(tmp_path):
    log = tmp_path / "chopsticks.log"
    log.write_text('[03:11:32.210] INFO (block-builder): Failed to apply extrinsic\n    app: "chopsticks"\n'
                   '    err: {\n      "type": "Error",\n      "message": "createType(ApplyExtrinsicResult):: boom",\n')
    assert "createType(ApplyExtrinsicResult):: boom" in _dropped_reason(str(log))
