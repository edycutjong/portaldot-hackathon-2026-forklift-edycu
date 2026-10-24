# Contributing

Forklift is a hackathon project; small fixes and issues are welcome.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pip install ruff
```

## Before you open a PR

```bash
ruff check .
pytest -q          # offline tests; `pytest -m live` needs network access and, for V3, a funded signer
```

CI runs the same two commands and never needs a seed or a live write. Never commit signer files, mnemonics,
generated chain specs or raw lift dumps. Keep commits small.
