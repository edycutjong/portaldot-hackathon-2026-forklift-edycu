"""The chains Forklift knows, pinned by genesis hash and runtime ``:code`` hash.

A chain is identified by what it serves, not by the URL it was reached at: the
genesis hash says which chain, the ``:code`` hash says which runtime. A known
genesis with an unknown code hash means the runtime was upgraded since the pin
and Forklift refuses to label it until the profile is re-pinned.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class Chain:
    id: str
    label: str
    genesis_hash: str
    code_hash: str | None  # None: not pinned (mainnet is read at a fixed block, never written)
    spec: tuple[str, int]
    symbol: str
    decimals: int
    ss58: int
    endpoint: str
    writable: bool  # Forklift may submit to the chain itself (a public testnet), never to mainnet

    def explorer_block(self, block_hash: str) -> str:
        return (f"https://node-console.portaldot.dev/?rpc={quote(self.endpoint, safe='')}"
                f"#/explorer/query/{block_hash}")


MAINNET = Chain(
    id="mainnet",
    label="Portaldot mainnet (read-only)",
    genesis_hash="0x83b5212c69f85f3996f8696bb35bf913af51b176a5f53d0a74fd3a83dc0b8a54",
    code_hash=None,
    spec=("portaldot", 1002),
    symbol="POT",
    decimals=14,
    ss58=42,
    endpoint="wss://mainnet.portaldot.io",
    writable=False,
)

# Pinned from the live chain on 2026-10-20 at #117,696 (chain_getBlockHash(0), blake2_256 of :code,
# state_getRuntimeVersion, system_properties). The pre-launch docs said portaldotV3/3000/tPOTv3 at
# testnetv3-node.feso-apps.xyz; the chain that launched on 2026-10-19 differs on all four.
V3 = Chain(
    id="v3",
    label="Portaldot V3 testnet",
    genesis_hash="0xd4f9acbcf7f52388d8b095abe3c198d19fc510e0343e643e87cfbccb155283bd",
    code_hash="0xedc03c9dc81d39fea5c02893cc34cf2ec898d55d7ee8f8e89eb27597a724c7c2",
    spec=("portaldot-v3", 3002),
    symbol="tPOT",
    decimals=14,
    ss58=42,
    endpoint="wss://test-node-v3.portaldot.dev",
    writable=True,
)

CHAINS: dict[str, Chain] = {c.id: c for c in (MAINNET, V3)}
BY_GENESIS: dict[str, Chain] = {c.genesis_hash: c for c in CHAINS.values()}


class UnknownChain(Exception):
    """The endpoint serves a genesis or runtime no profile pins."""


def identify(genesis_hash: str, code_hash: str | None) -> Chain:
    """The profile for a (genesis, ``:code``) pair. Raises :class:`UnknownChain` otherwise."""
    chain = BY_GENESIS.get((genesis_hash or "").lower())
    if chain is None:
        raise UnknownChain(f"genesis {genesis_hash} is not a chain Forklift pins")
    if chain.code_hash is not None and (code_hash or "").lower() != chain.code_hash:
        raise UnknownChain(f"{chain.label}: runtime :code hash {code_hash} is not the pinned "
                           f"{chain.code_hash} (runtime upgraded? re-pin the profile)")
    return chain


def for_spec(spec_name: str) -> Chain | None:
    return next((c for c in CHAINS.values() if c.spec[0] == spec_name), None)


def units(chain: Chain, planck: int) -> str:
    whole, frac = divmod(abs(planck), 10 ** chain.decimals)
    sign = "-" if planck < 0 else ""
    return f"{sign}{whole}.{frac:0{chain.decimals}d}".rstrip("0").rstrip(".") + f" {chain.symbol}"
