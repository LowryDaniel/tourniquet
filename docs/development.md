# Development guide

## Prerequisites

- Python 3.12+
- PostgreSQL 16 (local or Docker)
- `gh` CLI (for GitHub operations)
- Fly CLI for deployment

## Local setup

```bash
# Clone
git clone git@github.com:LowryDaniel/burnrate.git
cd burnrate

# Virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Environment
cp .env.example .env
# Edit .env — minimum required:
#   DATABASE_URL  (local Postgres)
#   FERNET_KEY    (generate with the command in .env.example)
#   SECRET_KEY    (generate with the command in .env.example)

# Database
createdb burnrate
alembic upgrade head

# Run
uvicorn burnrate.main:app --reload --port 8000
```

Open http://localhost:8000.

## Running with Docker (optional)

```bash
docker compose up
```

This starts both the web app and a local Postgres instance. No additional config needed.

## Code quality

```bash
# All checks (run before committing)
ruff check . && ruff format --check . && mypy src/ && pytest

# Auto-fix formatting
ruff format . && ruff check --fix .
```

## Tests

```bash
# All tests
pytest

# With coverage
pytest --cov=burnrate --cov-report=term-missing

# Specific test file
pytest tests/test_proxy.py -v
```

Tests use `respx` to mock Anthropic's API. No real API calls in the test suite.

## Database migrations

```bash
# Create a new migration after changing models
alembic revision --autogenerate -m "describe the change"

# Apply migrations
alembic upgrade head

# Rollback one step
alembic downgrade -1
```

## Generating secrets

```bash
# FERNET_KEY
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# SECRET_KEY
python3 -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

## Project structure

```
src/burnrate/
    main.py             # FastAPI app factory, lifespan, middleware
    config.py           # Pydantic Settings — all env vars typed
    models.py           # SQLAlchemy ORM models
    providers/
        anthropic.py    # Anthropic streaming proxy + token counting
    proxy/
        router.py       # /v1/messages route — auth → cap check → stream
    auth/
        magic_link.py   # Magic-link generation, verification, session
    billing/
        pricing.py      # Per-model £ rate table
        caps.py         # Cap check, cap update, midnight reset
        profiles.py     # Hobby / Production / Demo profile definitions
    dashboard/
        routes.py       # HTMX dashboard routes
    alerts/
        email.py        # Resend integration, idempotency check
    triggers/
        evaluator.py    # Trigger evaluation engine (scaffolded; anomaly rule off until W4)
migrations/
    versions/           # Alembic migration files
    env.py
tests/
    conftest.py         # Fixtures: test DB, mock Anthropic, test client
    test_proxy.py       # Proxy: under cap, over cap mid-stream, multi-key isolation
    test_billing.py     # Cost calculation, pence rounding, cap persistence
    test_auth.py        # Magic-link: generation, expiry, second-use rejection
templates/              # Jinja2 HTML templates
static/                 # CSS (minimal)
```
