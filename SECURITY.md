# Security Policy

## Supported versions

Only the `main` branch is supported (hackathon project, version 0.1.x).

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting
(Security tab → "Report a vulnerability") on this repository.

Forklift never signs against mainnet and refuses to submit extrinsics to a non-fork endpoint,
except `rehearse --send` and `receipt --target v3`, which the user runs explicitly with their own signer file and
which send only to the pinned V3 testnet. Signer files must be
mode 600 and are never printed. If you find a way around either guard, that is in scope.
