# Forklift

Lift Portaldot chain state at a pinned block onto a local fork you can write to, check the fork against the chain,
and rehearse a transaction on it before you send it. Built for the Portaldot Hacker House 2026
(Node Operations & Tooling).

- **V3 testnet:** `forklift up --target v3` forks the live V3 testnet at a pinned block with
  [Chopsticks](https://github.com/AcalaNetwork/chopsticks) (V3's own runtime), `forklift verify` checks the fork against
  the live chain, `forklift rehearse <Pallet.call>` predicts one extrinsic on the fork and, with `--send`, sends the
  same signed bytes to V3 and compares the outcomes field by field.
- **Mainnet (spec 1002):** `forklift lift` reads every top-level storage key at one pinned block, `forklift up` splices
  it into a local `portaldot_dev` node (a dev runtime, not V3), `forklift replay <block>:<index>` re-runs a mainnet
  extrinsic on a fork of its parent block. Mainnet is only ever read.

## Install

```sh
pip install portaldot-forklift     # Python 3.11+
forklift --help
```

The V3 path runs Chopsticks through `npx`, so it needs Node.js 18+. The mainnet `--runtime fast` path downloads the
official `portaldot_dev` node binary and checks its sha256 first.

## Use it in CI

```yaml
- uses: edycutjong/portaldot-hackathon-2026-forklift-edycu@v0.6.0
  with:
    command: verify        # or: rehearse (with call, args, signer-file)
```

The job fails with Forklift's own exit code. Exit codes:
[docs/EXIT-CODES.md](https://github.com/edycutjong/portaldot-hackathon-2026-forklift-edycu/blob/main/docs/EXIT-CODES.md).

## More

- Source, README and evidence: https://github.com/edycutjong/portaldot-hackathon-2026-forklift-edycu
- Measured results: [docs/PROOF.md](https://github.com/edycutjong/portaldot-hackathon-2026-forklift-edycu/blob/main/docs/PROOF.md)
- Landing page: https://forklift.edycu.dev/

MIT licensed.
