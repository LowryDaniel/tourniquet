# OJW Review — 2026-06-09

## State

burnrate (tourniquet) is a working Claude API proxy with cap enforcement, threshold alerts, and a web dashboard. Core proxy, admin routes, alert channels (Slack/Telegram/email/webhook), and Alembic migration scaffolding are all present. The codebase has CI (GitHub Actions), a Fly.io deploy path, and active git history. The primary open risk is that a documented schema-migration fix was never shipped: every startup still calls `Base.metadata.create_all` instead of running Alembic migrations, meaning Postgres production deployments are still vulnerable to the schema-drift class of bug the 2026-05-12 incident documented.

## Fixed today

- **ERRORS.md** — prepended a `2026-06-09` entry documenting that the 2026-05-12 long-term migration fix was aspirational and was never committed, with exact steps to ship it.
- **.gitignore** — added `logs/` and `*.log` entries (the only coverage gap; `.env`, `.venv`, `__pycache__`, `*.py[cod]`, `*.db`/`*.sqlite`/`*.sqlite3` were already covered).
- **.github/** — no action needed. All four files (`ci.yml`, `PULL_REQUEST_TEMPLATE.md`, and two issue templates) are present and tracked. The prior agent's "3 deleted-but-tracked" finding was incorrect.

## Built 2026-06-09

- **Migration fix shipped** — `src/tourniquet/migrate.py` (`upgrade_to_head`); 0001/0002/0004 made dialect-aware; `migrations/env.py` supports programmatic URL injection + `disable_existing_loggers=False`; `create_all` removed from `main.py`, `cli.py`, `scripts/init.py`, `scripts/bootstrap_local.py`.
- **`tests/test_migrations_sqlite.py`** — 3 hermetic SQLite tests (fresh upgrade, idempotency, pre-0003 catch-up); all pass.
- **`GET /v1/budget-status`** — read-only JSON endpoint in `src/tourniquet/proxy/router.py` (same Bearer-token auth as `/v1/messages`); returns `{spent_usd_cents, cap_usd_cents, remaining_usd_cents, percent_used, throttle_advised}`.
- **`tests/test_budget_status.py`** — 6 hermetic cases (no-auth 401, shape, throttle thresholds, over-cap, active lift).

## Needs Dan

1. **Migration fix decision** — ship or explicitly defer the `migrate.py` / `upgrade_to_head` work described in the new ERRORS.md entry. Every `tourniquet` launch on a real Postgres DB skips migrations today. Estimated effort: 60–90 min. Steps are spelled out in ERRORS.md.
2. **SQLite migration compatibility** — `migrations/versions/0001_initial_schema.py` opens with `CREATE EXTENSION IF NOT EXISTS "pgcrypto"` (Postgres-only). Before `alembic upgrade head` can run on SQLite, this and any `gen_random_uuid()` calls must be gated on `op.get_bind().dialect.name == "postgresql"`.
3. **`scripts/insights.py` create_all** — this standalone script also calls `create_all`. Decide whether it should use the migrate helper or keep `create_all` (acceptable for a read-only analytics script that always runs against an already-initialised DB).

## Top 5 improvements I could build unattended

1. **(Sonnet) Pre-cap alerting hardening** — The `notifier.maybe_fire_threshold_alert` helper in `proxy/router.py` fires alerts after spend writes but has no persistent idempotency guard across restarts. A lightweight `alerts_fired_bitmask` integer column on `api_keys` (reset at midnight UTC) would make threshold-firing crash-safe and idempotent even if the process restarts mid-day. Directly prevents duplicate or missing alerts on server restarts.

2. **(Sonnet) Per-project / per-label budgets** — `api_keys` has a single `daily_cap_usd_cents`. Adding an optional `project_tag` field + a `project_budgets` table would let Dan allocate e.g. "$5/day to swarm agents, $2/day to ad-hoc chat" and see per-project burn in the dashboard. Highest leverage for the OJW-swarm integration.

3. **(Sonnet) OJW-swarm budget integration** — A read-only `/v1/budget-status` endpoint (authenticated by the same API key) returning `{spent_today_cents, cap_cents, pct, threshold_next}` as JSON. The OJW swarm agent could poll this before spawning expensive sub-agents to avoid surprise cap hits. Trivial to build; high operational value.

4. **(Haiku) Usage dashboard sparklines** — The dashboard shows a table of spend events but no time-series chart. A small Chart.js or SVG sparkline per key (last 7 days of daily spend) would make burn-rate trends immediately visible without querying the raw table. Pure frontend addition, no schema changes.

5. **(Sonnet) Automated daily spend digest** — A scheduled task (or cron-triggered CLI command) that emails/Slacks a "yesterday's spend by key" digest each morning. Builds on the existing `fan_out` / alert-channel infrastructure. Useful for passive monitoring without needing to open the dashboard.

## Kickoff prompt

```
You are picking up burnrate (tourniquet) — an async Python proxy for the Anthropic API
with per-key spend caps, threshold alerts, and a web dashboard.

Repo: /Users/danlowry/Desktop/AI/burnrate
Stack: FastAPI + SQLAlchemy (async) + Alembic, SQLite (dev) / Postgres (prod), Fly.io deploy.
Key source tree:
  src/tourniquet/main.py          — FastAPI app + lifespan (DB init here)
  src/tourniquet/cli.py           — CLI entry point (also calls create_all)
  src/tourniquet/proxy/router.py  — hot path: /v1/messages, spend accounting, cap kill
  src/tourniquet/alerts/          — fan_out, Slack/Telegram/email/webhook channels
  src/tourniquet/routes/admin.py  — dashboard + kill-now + lift actions
  migrations/versions/            — Alembic migrations 0001–0004
  scripts/                        — standalone init/bootstrap/insights helpers

OPEN ISSUES (highest priority first):
1. migrate.py was never created. main.py:33 and cli.py:199 still call
   Base.metadata.create_all instead of alembic upgrade head. See ERRORS.md
   entry 2026-06-09 for exact steps to ship the fix.
2. migrations/0001 uses CREATE EXTENSION IF NOT EXISTS "pgcrypto" which breaks
   SQLite; must be gated on dialect.name == "postgresql" before SQLite upgrade
   is viable.

Context files to read first: ERRORS.md, FEATURE_REQUESTS.md (if present), README.md.
No commits or pushes without explicit approval. Never print secret values.
```
