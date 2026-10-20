# Exit codes

Every `forklift` command exits with one of these codes, so a script or CI job can tell
"the fork disagrees with mainnet" from "the node did not start" from "the network is down".
With `--json`, a command that could not run (exit 1, 3, 4, and `receipt`'s failed transaction) prints
one JSON object with an `error` field; exits 2, 5 and 6 report through result fields (last column);
usage errors (64) are always prose on stderr.

| Code | Meaning | Commands | `--json` error |
|---|---|---|---|
| `0` | Success: the lift is complete, the fork is up, nothing is unexplained, the call succeeded, the Receipt matches and pays no rent | all | — |
| `1` | Could not run: endpoint unreachable, transaction rejected by the pool or never included, unreadable chain spec, or the **I3 write-guard** refused a non-fork endpoint | `verify`, `fund`, `as`, `receipt`, `tx`, `rehearse` | `verify_failed`, `fund_failed`, `as_failed`, `receipt_failed`, `tx_failed`, `rehearse_failed`, `write_refused` |
| `2` | Mismatch: `verify` found a sampled key or a whole-fork check (key set, issuance, overrides, code hash) that nothing explains; `receipt` did not see exactly one Receipt event whose 72-byte payload equals the fork manifest (missing, malformed, duplicated or different) | `verify`, `receipt` | — (`verify`: `ok: false`; `receipt`: `attest.matches_manifest: false` + `mismatched_fields`) |
| `3` | Node failed: the binary could not be found, downloaded or verified, a port was busy, the splice failed, or the node did not start, did not author a block within 2 slots (I5), did not finalize, had peers, or exited; the log tail and a known-cause hint are printed | `up`, `down` | `node_failed` (`up`; `down` has no `--json`) |
| `4` | Lift failed: pinning, key paging, value reads, the manifest or the write step failed; the last key cursor is printed (`up` prints it with `--json` only) and kept as a checkpoint, and nothing is written as a complete lift | `lift`, `up` | `lift_failed` |
| `5` | Dispatch failed: the extrinsic landed on the fork, but the call it carried returned an error (`Sudo.Sudid` / `Sudo.SudoAsDone` = `Err`, or `ExtrinsicFailed`, including a failed Receipt instantiate or rent top-up); the decoded error name is printed | `fund`, `as`, `receipt` | `fund` / `as`: `dispatch.ok: false`; `receipt`: `dispatch_failed`, or `steps[].extrinsic.success: false` |
| `5` (rehearse) | Predicted failure: on the V3 fork the call fails (`ExtrinsicFailed`) or the fork's transaction pool rejects it (e.g. `Invalid: Payment` for an unfunded signer); nothing is sent to the chain | `rehearse` | `predicted.success: false` (+ `predicted.pool_rejected`) |
| `6` | Receipt not rent-safe: the payload matched, but after the top-up the contract still pays rent, or rent was charged across the attest (`rent.proof.no_rent_charged: false`); not enforced with `--no-top-up` | `receipt` | — (`rent.rent_safe: false`) |
| `7` | Prediction differs: the same signed bytes were sent to the live V3 testnet, and its tx hash, success, event sequence or attributes, inclusion height, fee or a named account's balance delta differ from the fork's prediction; every differing field is listed | `rehearse` | `compare.match: false` + `compare.differs` |
| `64` | Usage error: bad arguments, an unknown `Pallet.call`, call arguments that do not encode, a bad SS58 address or amount, no running fork of that name, `--evidence` without the fork's manifest (checked before anything is sent) | all | — |

Click's own usage code (`2`) is remapped to `64` so that `2` always means a mismatch.

## The write-guard (I3)

`fund`, `as` and `receipt` sign with the fork's dev sudo key and submit, so they only ever
open a *fork client*: its endpoint must be loopback (`127.0.0.1`, `localhost`, `::1`) and never
a known mainnet host, and before the first write the client reads the chain's genesis hash and
refuses if it is Portaldot mainnet's (`0x83b5…8a54`); the check runs again on every new
connection. When the command acts on a fork started by `forklift up`, the chain's genesis must
also equal the genesis in that fork's manifest, so a different chain on the same port is never
written to or recorded as this fork. A refusal exits `1` with `"error": "write_refused"` before
anything is sent. `lift`, `verify` (both sides) and `tx` use a
read-only client that can only send an allowlist of read methods.

On the V3 path, a Chopsticks fork serves V3's own genesis hash, so a loopback endpoint serving
V3's genesis is accepted as a fork only if it also answers Chopsticks' `dev_newBlock` and
`dev_setStorage` (the live chain does not). `rehearse --send` is the one command that writes to
a real chain: it opens a *testnet client*, which can send only `author_submitExtrinsic`, only
after reading a genesis pinned as writable (the V3 testnet), never mainnet, and never on an
endpoint that is also a fork.

## Evidence is never allowed to hide a result

With `--evidence DIR`, a transaction that already landed but cannot be written to
`DIR/txs-<block>.json` (unwritable directory, corrupt file) prints
`warning: … landed as extrinsic <block>-<index> (tx <hash>) but was NOT recorded …` on stderr and
keeps the command's real exit code; the JSON output then has `"evidence": null`. `verify --evidence`
also warns (`verify finished (exit N) but was NOT recorded …`) and keeps its exit code; its JSON has no
`evidence` field. Each landed `receipt` step (instantiate, rent top-up, attest) is recorded
as soon as it lands, so a later failure cannot lose an earlier transaction.
