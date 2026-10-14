import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from forklift import cli
from forklift.lift import (MMR_JUMP, MMR_PREFIX_HEX, EXIT_LIFT_FAILED, LiftError, lift, mmr_nodes_for_leaves,
                           page_keys)
from forklift.rpc import RpcClient
from tests.fakes import FakeChain, fake_connect

MMR = MMR_PREFIX_HEX
NODES = MMR + "519dfc7fdad21b84f64a5310fa178ef2"


def store():
    s = {
        "0x0101": "0x01",
        "0x0102": "0x02",
        "0x26aa01": "0x03",
        "0x26aa02": "0x04",
        "0x3a636f6465": "0x0061736d",  # :code
    }
    for i in range(10):  # an MMR range bigger than one page
        s[NODES + f"{i:016x}"] = "0x" + "ee" * 32
    s["0x9900"] = "0x05"
    s["0xff00"] = "0x06"
    return s


def client_for(chain, **kw):
    kw.setdefault("sleep", lambda s: None)
    return RpcClient("wss://mainnet.portaldot.io", connect=fake_connect(chain), **kw)


def test_page_keys_jumps_past_mmr_and_never_fetches_it():
    chain = FakeChain(store())
    c = client_for(chain)
    res = lift(c, "head", page_size=3)
    assert not any(k.startswith(MMR) for k in res.keys)
    assert res.keys == sorted(k for k in store() if not k.startswith(MMR))
    starts = [p[2] for m, p in chain.calls if m == "state_getKeysPaged"]
    assert MMR_JUMP in starts
    # Only the page that first reached the range touched it: one jump, not ten keys of paging.
    assert sum(1 for s in starts if s and s.startswith(MMR) and s != MMR_JUMP) <= 1
    fetched = [k for m, p in chain.calls if m == "state_queryStorageAt" for k in p[0]]
    assert not any(k.startswith(MMR) for k in fetched)
    assert res.values["0xff00"] == "0x06"


def test_every_read_is_pinned_to_one_hash():
    chain = FakeChain(store(), block_hash="0x" + "cd" * 32)
    lift(client_for(chain), "head", page_size=4)
    pinned = [p[-1] for m, p in chain.calls if m in ("state_getKeysPaged", "state_queryStorageAt")]
    assert pinned and set(pinned) == {"0x" + "cd" * 32}


def test_resolve_pin_by_number():
    chain = FakeChain(store(), number=2725639)
    res = lift(client_for(chain), 2725639, page_size=5)
    assert res.pin.number == 2725639
    with pytest.raises(LiftError):
        lift(client_for(FakeChain(store(), number=1)), 7)


class Flaky:
    """Serves the chain until the n-th key page, then the node goes away."""

    def __init__(self, chain, fail_on_page):
        self.chain, self.fail_on_page, self.pages = chain, fail_on_page, 0
        self.down = False

    def __call__(self, method, params):
        if method == "state_getKeysPaged":
            self.pages += 1
            if self.pages >= self.fail_on_page:
                self.down = True
        if self.down:
            raise ConnectionResetError("node gone")
        return self.chain(method, params)


def test_failure_mid_paging_keeps_checkpoint_and_writes_no_complete_lift(tmp_path):
    chain = FakeChain(store(), number=42)
    flaky = Flaky(chain, fail_on_page=3)
    with pytest.raises(LiftError) as err:
        lift(client_for(flaky, retries=2), "head", out_root=tmp_path, page_size=2)
    assert err.value.phase == "keys"
    assert err.value.cursor == "0x26aa02"  # last key of the second (last good) page
    d = tmp_path / "42"
    assert not (d / "state.json").exists()
    assert json.loads((d / "checkpoint.json").read_text())["cursor"] == "0x26aa02"

    # Resume from the checkpoint: same result as a clean lift.
    resumed = lift(client_for(chain), "head", out_root=tmp_path, page_size=2)
    clean = lift(client_for(FakeChain(store(), number=42)), "head", page_size=2)
    assert resumed.resumed_from == "0x26aa02"
    assert resumed.keys == clean.keys and resumed.values == clean.values
    assert (d / "state.json").exists() and not (d / "checkpoint.json").exists()


def test_key_without_value_fails_the_lift():
    s = store()

    class Hole(FakeChain):
        def __call__(self, method, params):
            out = super().__call__(method, params)
            if method == "state_queryStorageAt":
                for change in out[0]["changes"]:
                    if change[0] == "0x9900":
                        change[1] = None
            return out

    with pytest.raises(LiftError) as err:
        lift(client_for(Hole(s)), "head", page_size=4)
    assert err.value.phase == "values"


def test_cli_exits_4_with_last_cursor(monkeypatch, tmp_path):
    flaky = Flaky(FakeChain(store(), number=42), fail_on_page=1)
    monkeypatch.setattr(cli, "RpcClient",
                        lambda endpoint: RpcClient(endpoint, connect=fake_connect(flaky), retries=1,
                                                   sleep=lambda s: None))
    result = CliRunner().invoke(cli.main, ["lift", "--at", "head", "--out", str(tmp_path), "--json"])
    assert result.exit_code == EXIT_LIFT_FAILED
    err = json.loads(result.output)
    assert err["error"] == "lift_failed" and err["phase"] == "keys"
    assert not list(Path(tmp_path).rglob("state.json"))


@pytest.mark.parametrize("leaves,nodes", [(1, 1), (2, 3), (3, 4), (4, 7), (2725639, 5451267)])
def test_mmr_node_count(leaves, nodes):
    assert mmr_nodes_for_leaves(leaves) == nodes


def test_page_keys_stops_on_short_page():
    chain = FakeChain({"0x01": "0x", "0x02": "0x"})
    res = page_keys(client_for(chain), chain.block_hash, page_size=10)
    assert res.keys == ["0x01", "0x02"] and res.pages == 1


def test_usage_errors_exit_64_not_2(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["forklift", "lift", "--at", "not-a-block"])
    with pytest.raises(SystemExit) as exc:
        cli.run()
    assert exc.value.code == cli.EXIT_USAGE == 64
