"""Compose, sign and submit extrinsics on a local fork, and decode what they did.

Every byte goes through :class:`forklift.rpc.RpcClient`, so the I3 write-guard
applies here too: a client that is not a fork client cannot submit.

The SDK (``scalecodec``, via ``substrate-interface``) is used for the parts that
need the runtime's type registry: composing calls from metadata, decoding
events and storage values. The extrinsic envelope and its signing payload are
built here, explicitly, because spec 1002's signed-extension list is fixed and
short, and a mismatch must fail loudly instead of producing a bad signature.

Type preset: ``legacy``. It decodes the pinned whale's ``System.Account`` at
mainnet #2,725,639 to the byte (80 bytes, free 2*10^22 planck); the
``polkadot`` preset has no decoder for spec 1002's ``AccountInfo<Index, AccountData>``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from forklift.hashing import blake2_128, blake2_256, twox128
from forklift.rpc import RpcClient

TYPE_PRESET = "legacy"

# 1 POT = 10^14 planck: Balances.ExistentialDeposit is 1 POT = 100,000,000,000,000
# planck on spec 1002, and the pinned whale's 2*10^22 planck is its 200,000,000 POT.
# Spec 1002's system_properties is empty, so this is a convention, not a chain read:
# every command that takes POT also takes --planck for an exact raw amount.
PLANCK_PER_POT = 10**14

# The signed extensions this module knows how to sign for, in order (spec 1002 metadata V13).
SIGNED_EXTENSIONS = ["CheckSpecVersion", "CheckTxVersion", "CheckGenesis", "CheckMortality",
                     "CheckNonce", "CheckWeight", "ChargeTransactionPayment"]

EXTRINSIC_V4_SIGNED = 0x84
MULTIADDRESS_ID = 0x00
MULTISIG_SR25519 = 0x01
IMMORTAL_ERA = b"\x00"


class TxError(Exception):
    """The extrinsic could not be built, was rejected by the pool, or never landed."""


def encode_compact(n: int) -> bytes:
    """SCALE compact encoding of a non-negative integer."""
    if n < 0:
        raise ValueError("compact integers are unsigned")
    if n < 1 << 6:
        return bytes([n << 2])
    if n < 1 << 14:
        return ((n << 2) | 1).to_bytes(2, "little")
    if n < 1 << 30:
        return ((n << 2) | 2).to_bytes(4, "little")
    raw = n.to_bytes((n.bit_length() + 7) // 8, "little")
    if len(raw) > 67:
        raise ValueError("integer too large for compact encoding")
    return bytes([((len(raw) - 4) << 2) | 3]) + raw


def pot_to_planck(amount: str) -> int:
    """'1000000' or '0.5' POT → planck; refuses fractions finer than one planck and negatives."""
    try:
        value = Decimal(amount.replace(",", "").replace("_", ""))
    except InvalidOperation:
        raise ValueError(f"not a POT amount: {amount!r}") from None
    if not value.is_finite() or value < 0:
        raise ValueError(f"not a POT amount: {amount!r}")
    planck = value * PLANCK_PER_POT
    if planck != planck.to_integral_value():
        raise ValueError(f"{amount} POT is finer than one planck (10^-14 POT)")
    return int(planck)


def fmt_pot(planck: int) -> str:
    whole, frac = divmod(int(planck), PLANCK_PER_POT)
    return f"{whole:,}" + (f".{frac:014d}".rstrip("0") if frac else "") + " POT"


def ss58_to_public(address: str) -> bytes:
    from substrateinterface.utils.ss58 import ss58_decode

    try:
        return bytes.fromhex(ss58_decode(address))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"not an SS58 address: {address!r} ({exc})") from None


def public_to_ss58(public: bytes, ss58_format: int = 42) -> str:
    from substrateinterface.utils.ss58 import ss58_encode

    return ss58_encode(public, ss58_format=ss58_format)


def dev_keypair(uri: str = "//Alice"):
    """A well-known dev key (sr25519). Local forks only: its seed is public."""
    from substrateinterface import Keypair

    if not uri.startswith("//"):
        raise ValueError("only dev derivation URIs like //Alice are accepted: never pass a real seed")
    return Keypair.create_from_uri(uri)


class Runtime:
    """The fork's runtime as the SDK sees it: metadata, type registry, versions, genesis."""

    def __init__(self, metadata_hex: str, runtime_version: dict, genesis_hash: str):
        from scalecodec.base import RuntimeConfigurationObject, ScaleBytes
        from scalecodec.type_registry import load_type_registry_preset

        self.registry = RuntimeConfigurationObject()
        self.registry.update_type_registry(load_type_registry_preset("core"))
        self.registry.update_type_registry(load_type_registry_preset(TYPE_PRESET))
        self.metadata = self.registry.create_scale_object("MetadataVersioned", data=ScaleBytes(metadata_hex))
        self.metadata.decode()
        self.spec_name = runtime_version["specName"]
        self.spec_version = int(runtime_version["specVersion"])
        self.tx_version = int(runtime_version["transactionVersion"])
        self.registry.set_active_spec_version_id(self.spec_version)
        self.genesis_hash = genesis_hash
        exts = self._raw()["extrinsic"]["signed_extensions"]
        if list(exts) != SIGNED_EXTENSIONS:
            raise TxError(f"unsupported signed extensions {exts}: this signer knows only {SIGNED_EXTENSIONS}")

    @classmethod
    def load(cls, client: RpcClient, block_hash: str | None = None) -> "Runtime":
        params = [block_hash] if block_hash else []
        return cls(client.call("state_getMetadata", params), client.call("state_getRuntimeVersion", params),
                   client.call("chain_getBlockHash", [0]))

    def _raw(self) -> dict:
        value = self.metadata.value[1]
        if "V13" not in value:
            raise TxError(f"metadata {list(value)} is not V13 (spec 1002)")
        return value["V13"]

    def pallet(self, name: str):
        for p in self.metadata.pallets:
            if p.name == name:
                return p
        raise KeyError(f"no pallet {name} in this runtime")

    def call_names(self) -> dict[str, list[str]]:
        return {m["name"]: [c["name"] for c in (m["calls"] or [])] for m in self._raw()["modules"]}

    def call_args(self, pallet: str, function: str) -> list[dict]:
        for m in self._raw()["modules"]:
            if m["name"] == pallet:
                for c in m["calls"] or []:
                    if c["name"] == function:
                        return c["args"]
                raise KeyError(f"{pallet} has no call {function}")
        raise KeyError(f"no pallet {pallet} in this runtime")

    def compose(self, pallet: str, function: str, args: dict[str, Any]):
        """A ``Call`` scale object (``.data`` = encoded bytes, ``.value`` = nestable dict)."""
        self.call_args(pallet, function)  # a readable KeyError before the SDK's
        call = self.registry.create_scale_object("Call", metadata=self.metadata)
        call.encode({"call_module": pallet, "call_function": function, "call_args": args})
        return call

    def constant(self, pallet: str, name: str) -> bytes:
        for c in self.pallet(pallet).constants:
            if c.name == name:
                return bytes(c.constant_value)
        raise KeyError(f"no constant {pallet}.{name}")

    def constant_int(self, pallet: str, name: str) -> int:
        return int.from_bytes(self.constant(pallet, name), "little")

    def storage_entry(self, pallet: str, item: str) -> dict:
        for m in self._raw()["modules"]:
            if m["name"] == pallet and m["storage"]:
                for e in m["storage"]["entries"]:
                    if e["name"] == item:
                        return e
        raise KeyError(f"no storage item {pallet}.{item}")

    def storage_key(self, pallet: str, item: str, *keys: bytes) -> str:
        """Key of a plain value or a single map entry (``keys`` already SCALE-encoded)."""
        entry = self.storage_entry(pallet, item)
        prefix = twox128(pallet) + twox128(item)
        kind, spec = next(iter(entry["type"].items()))
        if kind == "Plain":
            if keys:
                raise ValueError(f"{pallet}.{item} is a plain value: no key expected")
            return "0x" + prefix.hex()
        hashers = [spec["hasher"]] if kind == "Map" else [spec["key1_hasher"], spec["key2_hasher"]] \
            if kind == "DoubleMap" else None
        if hashers is None or len(keys) != len(hashers):
            raise ValueError(f"{pallet}.{item} ({kind}) needs {len(hashers or [])} key(s)")
        return "0x" + (prefix + b"".join(hash_key(h, k) for h, k in zip(hashers, keys))).hex()

    def decode(self, type_string: str, raw_hex: str) -> Any:
        from scalecodec.base import ScaleBytes

        obj = self.registry.create_scale_object(type_string, data=ScaleBytes(raw_hex), metadata=self.metadata)
        return obj.decode()

    def decode_storage(self, pallet: str, item: str, raw_hex: str | None) -> Any:
        entry = self.storage_entry(pallet, item)
        kind, spec = next(iter(entry["type"].items()))
        value_type = spec if kind == "Plain" else spec["value"]
        if raw_hex is None:
            if entry["modifier"] != "Default":
                return None  # Optional item, absent
            raw_hex = _bytes_hex(entry.get("default"))  # what the runtime reads for an absent key
        return self.decode(value_type, raw_hex)

    def error_name(self, module_index: int, error_index: int) -> str:
        for m in self._raw()["modules"]:
            if int(m["index"]) == module_index:
                errors = m["errors"] or []
                if error_index < len(errors):
                    return f"{m['name']}.{errors[error_index]['name']}"
                return f"{m['name']}.<error {error_index}>"
        return f"<module {module_index}>.<error {error_index}>"


def _bytes_hex(value: Any) -> str:
    """The SDK hands metadata byte fields back as hex or as a latin-1 string: normalise to hex."""
    if value is None:
        return "0x"
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    if isinstance(value, str) and value.startswith("0x"):
        return value
    return "0x" + str(value).encode("latin-1").hex()


def hash_key(hasher: str, key: bytes) -> bytes:
    if hasher == "Blake2_128Concat":
        return blake2_128(key) + key
    if hasher == "Twox64Concat":
        import xxhash

        return xxhash.xxh64_intdigest(key, seed=0).to_bytes(8, "little") + key
    if hasher == "Identity":
        return key
    if hasher == "Blake2_256":
        return blake2_256(key)
    if hasher == "Twox128":
        return twox128(key)
    raise ValueError(f"unsupported hasher {hasher}")


def signing_payload(rt: Runtime, call: bytes, nonce: int, tip: int = 0) -> bytes:
    """What an sr25519 key signs for spec 1002: call ‖ extra ‖ additional (hashed if > 256 bytes).

    extra = era (immortal) ‖ compact nonce ‖ compact tip; additional = spec version ‖
    tx version ‖ genesis hash ‖ era birth hash (= genesis for an immortal era).
    """
    genesis = bytes.fromhex(rt.genesis_hash[2:])
    payload = (call + IMMORTAL_ERA + encode_compact(nonce) + encode_compact(tip)
               + rt.spec_version.to_bytes(4, "little") + rt.tx_version.to_bytes(4, "little")
               + genesis + genesis)
    return blake2_256(payload) if len(payload) > 256 else payload


def signed_extrinsic(rt: Runtime, call: bytes, keypair, nonce: int, tip: int = 0) -> bytes:
    """A complete, length-prefixed v4 signed extrinsic (MultiAddress::Id, MultiSignature::Sr25519)."""
    signature = keypair.sign(signing_payload(rt, call, nonce, tip))
    if len(signature) != 64:
        raise TxError("expected a 64-byte sr25519 signature")
    body = (bytes([EXTRINSIC_V4_SIGNED, MULTIADDRESS_ID]) + keypair.public_key
            + bytes([MULTISIG_SR25519]) + signature
            + IMMORTAL_ERA + encode_compact(nonce) + encode_compact(tip) + call)
    return encode_compact(len(body)) + body


@dataclass
class Event:
    index: int  # position in the block's event list
    phase: str  # ApplyExtrinsic | Finalization | Initialization
    extrinsic_index: int | None
    pallet: str
    name: str
    attributes: Any

    @property
    def full_name(self) -> str:
        return f"{self.pallet}.{self.name}"

    def as_json(self) -> dict:
        return {"event": self.full_name, "attributes": readable_attributes(self.attributes)}


def _is_account_type(type_string: str) -> bool:
    return "AccountId" in type_string or "LookupSource" in type_string or "Lookup as StaticLookup" in type_string


def as_ss58(value: Any) -> Any:
    """A 32-byte account id as hex → SS58 (format 42); anything else unchanged."""
    if isinstance(value, str) and value.startswith("0x") and len(value) == 66:
        return public_to_ss58(bytes.fromhex(value[2:]))
    return value


def readable_attributes(attributes: Any) -> Any:
    """Event attributes with account ids shown as SS58 (by their declared type, never by length alone)."""
    if not isinstance(attributes, list):
        return attributes
    out = []
    for a in attributes:
        if isinstance(a, dict) and "type" in a and _is_account_type(str(a["type"])):
            a = {**a, "value": as_ss58(a.get("value"))}
        out.append(a)
    return out


def events_at(client: RpcClient, rt: Runtime, block_hash: str) -> list[Event]:
    raw = client.call("state_getStorage", ["0x" + (twox128("System") + twox128("Events")).hex(), block_hash])
    if raw is None:
        return []
    out = []
    for i, rec in enumerate(rt.decode("Vec<EventRecord<Event, Hash>>", raw)):
        out.append(Event(index=i, phase=rec.get("phase"), extrinsic_index=rec.get("extrinsic_idx"),
                         pallet=rec.get("module_id") or rec["event"].get("module_id"),
                         name=rec.get("event_id") or rec["event"].get("event_id"),
                         attributes=rec.get("attributes", rec["event"].get("attributes"))))
    return out


def dispatch_error_name(rt: Runtime, err: Any) -> str:
    """Readable name of a decoded ``DispatchError`` (``Pallet.Error`` for module errors)."""
    if isinstance(err, dict):
        if "Module" in err:
            mod = err["Module"]
            if isinstance(mod, dict):
                return rt.error_name(int(mod["index"]), int(mod["error"]))
            return rt.error_name(int(mod[0]), int(mod[1]))
        key = next(iter(err))
        return f"{key}" if err[key] in (None, ()) else f"{key}({err[key]})"
    return str(err)


def result_of(rt: Runtime, value: Any) -> tuple[bool, str | None]:
    """A decoded ``DispatchResult`` → (ok, error name)."""
    if isinstance(value, dict) and set(value) & {"Ok", "Err"}:
        if "Ok" in value:
            return True, None
        return False, dispatch_error_name(rt, value["Err"])
    if value == "Ok":
        return True, None
    if isinstance(value, bool):  # older pallets report the dispatch outcome as a bool
        return (True, None) if value else (False, "dispatch returned false")
    if value is None:
        return False, "no dispatch result decoded"
    return False, dispatch_error_name(rt, value)


@dataclass
class Inclusion:
    tx_hash: str
    block_number: int
    block_hash: str
    index: int
    events: list[Event]
    success: bool
    error: str | None
    seconds: float
    finalized: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def extrinsic_id(self) -> str:
        return f"{self.block_number}-{self.index}"

    def events_named(self, full_name: str) -> list[Event]:
        return [e for e in self.events if e.full_name == full_name]

    def as_json(self) -> dict:
        return {"extrinsic_id": self.extrinsic_id, "block_number": self.block_number,
                "block_hash": self.block_hash, "tx_hash": self.tx_hash, "success": self.success,
                "error": self.error, "finalized": self.finalized, "seconds": self.seconds,
                "events": [e.as_json() for e in self.events]}


def _outcome(rt: Runtime, events: list[Event]) -> tuple[bool, str | None]:
    for e in events:
        if e.full_name == "System.ExtrinsicSuccess":
            return True, None
        if e.full_name == "System.ExtrinsicFailed":
            attrs = e.attributes
            err = attrs[0]["value"] if isinstance(attrs, list) and attrs and isinstance(attrs[0], dict) \
                and "value" in attrs[0] else attrs
            return False, dispatch_error_name(rt, err)
    raise TxError("the extrinsic has neither ExtrinsicSuccess nor ExtrinsicFailed")


def _header_number(client: RpcClient, block_hash: str | None = None) -> int:
    header = client.call("chain_getHeader", [block_hash] if block_hash else [])
    return int(header["number"], 16)


def find_in_block(client: RpcClient, block_hash: str, xt_hex: str) -> int | None:
    block = client.call("chain_getBlock", [block_hash])
    for i, x in enumerate(block["block"]["extrinsics"]):
        if x == xt_hex:
            return i
    return None


# Pool answers that mean "this exact extrinsic already reached the pool": seen when the first
# author_submitExtrinsic was accepted but its answer was lost and the client re-sent it.
POOL_ALREADY_IMPORTED = 1013
POOL_TEMPORARILY_BANNED = 1012  # recently included or dropped; inclusion is then checked, not assumed
# After a re-send these also mean "it may already be in a block" (stale nonce / ancient birth);
# on a first send they are real rejections.
POOL_INVALID = 1010
POOL_ANCIENT_BIRTH = 1016


def _send(client: RpcClient, xt_hex: str, expected: str) -> str:
    from forklift.rpc import RpcError

    resent_before = getattr(client, "resent_writes", 0)
    try:
        tx_hash = client.call("author_submitExtrinsic", [xt_hex])
    except RpcError as exc:
        resent = getattr(client, "resent_writes", 0) > resent_before  # an earlier attempt reached the node
        if exc.code in (POOL_ALREADY_IMPORTED, POOL_TEMPORARILY_BANNED) or (
                resent and exc.code in (POOL_INVALID, POOL_ANCIENT_BIRTH)):
            return expected  # same bytes, same hash; whether it landed is decided by the block scan
        raise
    if tx_hash != expected:
        raise TxError(f"node returned tx hash {tx_hash}, expected {expected}")
    return tx_hash


def submit(client: RpcClient, rt: Runtime, xt: bytes, *, timeout: float = 90.0, finalized: bool = True,
           poll: float = 0.5, sleep=time.sleep, clock=time.monotonic) -> Inclusion:
    """Submit, wait for the block that includes it (and, by default, its finalization), decode the result.

    ``author_submitExtrinsic`` is a write: ``client`` must be a fork client (I3). A re-sent
    extrinsic that the pool already holds is not an error (see ``_send``). If the block that
    included it is not the one that finalized at its height, the scan resumes from there.
    """
    xt_hex = "0x" + xt.hex()
    expected = "0x" + blake2_256(xt).hex()
    scanned = _header_number(client)
    tip = client.call("chain_getBlockHash", [scanned])
    t0 = clock()
    tx_hash = _send(client, xt_hex, expected)
    while True:
        found: tuple[int, str, int] | None = None
        while found is None:
            if clock() - t0 > timeout:
                raise TxError(f"{tx_hash} not included within {timeout:.0f} s (scanned to #{scanned})")
            best = _header_number(client)
            if best < scanned or client.call("chain_getBlockHash", [scanned]) != tip:
                scanned = max(0, min(best, scanned) - 1)  # re-org: the blocks we scanned changed, look again
            while scanned < best and found is None:
                scanned += 1
                bh = client.call("chain_getBlockHash", [scanned])
                tip = bh
                idx = find_in_block(client, bh, xt_hex)
                if idx is not None:
                    found = (scanned, bh, idx)
            if found is None:
                sleep(poll)
        number, block_hash, index = found
        if not finalized:
            break
        while True:
            fin = _header_number(client, client.call("chain_getFinalizedHead", []))
            if fin >= number:
                break
            if clock() - t0 > timeout:
                raise TxError(f"{tx_hash} included in #{number} but not finalized within {timeout:.0f} s")
            sleep(poll)
        canonical = client.call("chain_getBlockHash", [number])
        if canonical == block_hash:
            break
        idx = find_in_block(client, canonical, xt_hex)
        if idx is not None:
            block_hash, index = canonical, idx
            break
        scanned = number  # re-org below finality: the extrinsic may be in a later block
        tip = canonical
    seconds = round(clock() - t0, 1)
    mine = [e for e in events_at(client, rt, block_hash)
            if e.phase == "ApplyExtrinsic" and e.extrinsic_index == index]
    ok, err = _outcome(rt, mine)
    return Inclusion(tx_hash=tx_hash, block_number=number, block_hash=block_hash, index=index, events=mine,
                     success=ok, error=err, seconds=seconds, finalized=finalized)


WRAPPER_RESULT_EVENTS = ("Sudo.Sudid", "Sudo.SudoAsDone")


def wrapper_result(rt: Runtime, success: bool, error: str | None, events: list) -> dict | None:
    """The result of the call a Sudo wrapper carried, from its ``Sudid`` / ``SudoAsDone`` event.

    ``events`` are Event objects or their ``as_json()`` dicts. None if the extrinsic has no wrapper
    event (not a sudo call); ``ok`` is False if the extrinsic itself failed.
    """
    for e in events:
        name = e.full_name if isinstance(e, Event) else e.get("event")
        if name in WRAPPER_RESULT_EVENTS:
            attrs = e.attributes if isinstance(e, Event) else e.get("attributes")
            value = attrs[0]["value"] if isinstance(attrs, list) and attrs and isinstance(attrs[0], dict) else attrs
            ok, err = result_of(rt, value) if success else (False, error)
            return {"event": name, "ok": ok, "error": err}
    return None


def next_nonce(client: RpcClient, address: str) -> int:
    return int(client.call("system_accountNextIndex", [address]))


def sign_and_submit(client: RpcClient, rt: Runtime, call, keypair, **kw) -> Inclusion:
    xt = signed_extrinsic(rt, bytes(call.data.data), keypair, next_nonce(client, keypair.ss58_address))
    return submit(client, rt, xt, **kw)


def fetch_extrinsic(client: RpcClient, rt: Runtime, extrinsic_id: str) -> dict:
    """Re-read an extrinsic by ``<block>-<index>``: its hash, decoded call, events and outcome."""
    try:
        number_s, index_s = extrinsic_id.split("-")
        number, index = int(number_s), int(index_s)
    except ValueError:
        raise ValueError(f"extrinsic id must look like 123-1, got {extrinsic_id!r}") from None
    block_hash = client.call("chain_getBlockHash", [number])
    if block_hash is None:
        raise TxError(f"no block #{number} on this chain")
    block = client.call("chain_getBlock", [block_hash])
    extrinsics = block["block"]["extrinsics"]
    if index >= len(extrinsics):
        raise TxError(f"block #{number} has {len(extrinsics)} extrinsics, no index {index}")
    xt_hex = extrinsics[index]
    xt = rt.decode("Extrinsic", xt_hex)
    mine = [e for e in events_at(client, rt, block_hash)
            if e.phase == "ApplyExtrinsic" and e.extrinsic_index == index]
    ok, err = _outcome(rt, mine)
    return {"extrinsic_id": extrinsic_id, "block_number": number, "block_hash": block_hash,
            "tx_hash": "0x" + blake2_256(bytes.fromhex(xt_hex[2:])).hex(),
            "signer": as_ss58(xt.get("address") or xt.get("account_id")),
            "call": {"pallet": xt.get("call", {}).get("call_module"),
                     "function": xt.get("call", {}).get("call_function")},
            "success": ok, "error": err, "dispatch": wrapper_result(rt, ok, err, mine),
            "events": [e.as_json() for e in mine]}
