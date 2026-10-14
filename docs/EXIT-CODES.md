# Exit codes

Every `forklift` command exits with one of these codes, so a script or CI job can tell
"the fork disagrees with mainnet" from "the node did not start" from "the network is down".
With `--json`, an error is printed as one JSON object with an `error` field instead of prose.

| Code | Meaning | Commands | `--json` error |
|---|---|---|---|
| `0` | Success: the lift is complete, the fork is up, nothing is unexplained, the call succeeded, the Receipt matches | all | — |
| `1` | Could not run: endpoint unreachable, transaction rejected by the pool or never included, unreadable chain spec, or the **I3 write-guard** refused a non-fork endpoint | `verify`, `fund`, `as`, `receipt`, `tx` | `verify_failed`, `fund_failed`, `as_failed`, `receipt_failed`, `tx_failed`, `write_refused` |
| `2` | Mismatch: `verify` found a sampled key or a whole-fork check (key set, issuance, overrides, code hash) that nothing explains; `receipt` saw an emitted payload that is not the fork manifest's (or no Receipt event) | `verify`, `receipt` | — (the report has `ok: false`) |
| `3` | Node failed: the binary did not start, did not author a block within 2 slots (I5), did not finalize, had peers, or exited; the log tail and a known-cause hint are printed | `up`, `down` | `node_failed` |
| `4` | Lift failed: pinning, key paging, value reads, the manifest or the write step failed; the last key cursor is printed and kept as a checkpoint, and nothing is written as a complete lift | `lift`, `up` | `lift_failed` |
| `5` | Dispatch failed: the extrinsic landed on the fork, but the call it carried returned an error (`Sudo.Sudid` / `Sudo.SudoAsDone` = `Err`, or `ExtrinsicFailed`); the decoded error name is printed | `fund`, `as`, `receipt` | — (the result has `dispatch.ok: false`) |
| `64` | Usage error: bad arguments, an unknown `Pallet.call`, a bad SS58 address or amount, no running fork of that name | all | — |

Click's own usage code (`2`) is remapped to `64` so that `2` always means a mismatch.

## The write-guard (I3)

`fund`, `as` and `receipt` sign with the fork's dev sudo key and submit, so they only ever
open a *fork client*: its endpoint must be loopback (`127.0.0.1`, `localhost`, `::1`) and never
a known mainnet host, and before the first write the client reads the chain's genesis hash and
refuses if it is Portaldot mainnet's (`0x83b5…8a54`). A refusal exits `1` with
`"error": "write_refused"` before anything is sent. `lift`, `verify` (mainnet side) and `tx` use a
read-only client that can only send an allowlist of read methods.
