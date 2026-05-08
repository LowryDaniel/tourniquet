# Contributing

Pull requests are welcome. Tourniquet is small, opinionated, and local-first by design — the bar for "would this fit?" is "does it help a solo dev keep one Anthropic key from running away?". If you're not sure, open an issue first to talk it through.

The rest of this file documents the development workflow.

## Setup

See [docs/development.md](docs/development.md) for the full local setup guide.

Quick start:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in values
alembic upgrade head
uvicorn tourniquet.main:app --reload
```

## Code standards

- **Formatter / linter:** Ruff (`ruff check . && ruff format .`)
- **Type checker:** mypy strict (`mypy src/`)
- **Tests:** pytest (`pytest`)
- All three must pass before merge.

## Git workflow

- `main` — always deployable; protected branch
- `feat/<name>` — feature branches
- Squash-merge PRs; commit message = PR title

## Commit message format

```
<type>: <short description>

<optional body>
```

Types: `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `perf`

## Architecture decisions

Before changing a core invariant (cost in pence, provider directory pattern, triggers JSON column), check [docs/architecture.md](docs/architecture.md) — these decisions are load-bearing and intentional.
