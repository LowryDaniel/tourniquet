# Errors & fixes

## 2026-06-12 — CI lint red on main: unpinned ruff version drift flagged never-linted backfill files

**What failed:** First push since the 2026-06-11 backfill turned CI red — `ruff check` reported 14 errors, all in files untouched by the push (`tests/test_migrations_sqlite.py`, `tests/test_budget_status.py`, `migrations/versions/0001_initial_schema.py`). The previous run on main had passed.

**Root cause:** Two compounding gaps: (1) `ruff>=0.5` was unpinned in pyproject dev deps, so CI silently picked up ruff 0.15's new/stricter rules between runs; (2) the 2026-06-11 backfill commits were pushed without a CI run, so those files had never been linted at all. The failure surfaced on the next unrelated push.

**Fix:** Pinned `ruff>=0.15,<0.16` in pyproject.toml; applied `ruff check --fix` plus manual line-wraps and removed a `UTC = UTC` self-assignment the auto-fixer left behind; `ruff format` reconciled `0001_initial_schema.py` and `proxy/router.py`. Verified locally: `ruff check` + `ruff format --check` clean, `pytest` 234 passed / 3 skipped. Committed with the /health GIT_SHA work (see HANDOFF.md).

## 2026-06-11 — Stale index.lock blocked all commits since Jun 9; stranded work finally committed

**What failed:** Every `git commit` failed with `Unable to create .git/index.lock: File exists`. The lock dated Jun 9 20:18 — a crashed commit attempt — meaning the migration fix the entry below describes could not have been committed even when a session tried.

**Root cause:** Crashed git process left `.git/index.lock` behind; no session since noticed because none attempted a commit (the aspirational-shipped pattern hid it).

**Fix:** Verified no live git process, removed the stale lock, committed everything in `32c39df` (migrate.py, tests, kit files). Resolves the 2026-06-09 entry below: the fix is now committed. Still unverified: deployment to tourniquet.dev (see HANDOFF.md).

## 2026-06-09 — ERRORS.md documents unshipped migration fix

**What failed:** The 2026-05-12 ERRORS.md entry claims a long-term fix was shipped: `src/tourniquet/migrate.py` created, `create_all` replaced with programmatic `alembic upgrade head` in `main.py`, `cli.py`, `scripts/init.py`, `scripts/bootstrap_local.py`, and `tests/test_migrations_sqlite.py` added. OJW review on 2026-06-09 found none of this exists in the working tree or git log. The entry was aspirational — written as if the fix were complete, but never committed.

**Root cause:** The long-term fix section of the entry was written prospectively during the incident post-mortem and not flagged as "TODO". No commit in `git log` references `migrate.py`, `upgrade_to_head`, or `test_migrations_sqlite`. The five call sites (`main.py:33`, `cli.py:199`, `scripts/init.py:83`, `scripts/bootstrap_local.py:50`, `scripts/insights.py:167`) all still use `Base.metadata.create_all`.

**Fix:** Honest note — the entry is aspirational. Shipping it requires:
1. Create `src/tourniquet/migrate.py` with an `upgrade_to_head(database_url: str)` helper that converts async URLs to sync form and calls `alembic upgrade head` programmatically.
2. Replace `Base.metadata.create_all` with `upgrade_to_head(str(settings.database_url))` in `main.py::lifespan`, `cli.py::cmd_add_key`, `scripts/init.py`, `scripts/bootstrap_local.py`. (Keep `create_all` in `scripts/insights.py` and tests — those are intentionally ephemeral.)
3. Add `tests/test_migrations_sqlite.py` covering fresh-SQLite upgrade + the pre-0003 schema failure mode.
4. Ensure migrations 0001/0002 gate `pgcrypto`/`gen_random_uuid()` behind `op.get_bind().dialect.name == "postgresql"` so SQLite can run `alembic upgrade head` without errors.
See 2026-05-12 entry below for full context.

**Status (2026-06-09): shipped.** All four steps above implemented and verified:
- `src/tourniquet/migrate.py` created (`upgrade_to_head`).
- `migrations/versions/0001_initial_schema.py` and `0002_api_key_actions.py` made dialect-aware (UUID/JSONB/boolean/timestamp server_defaults branch on `op.get_bind().dialect.name`).
- `migrations/versions/0004_fix_profile_default.py` skips on SQLite.
- `migrations/env.py` honours programmatic URL override and uses `disable_existing_loggers=False`.
- `create_all` replaced with `upgrade_to_head` in `main.py`, `cli.py`, `scripts/init.py`, `scripts/bootstrap_local.py`. `scripts/insights.py` intentionally left with `create_all` (ephemeral analytics).
- `tests/test_migrations_sqlite.py` — 3 tests (fresh upgrade, idempotency, pre-0003 column catch-up); all pass.
- `GET /v1/budget-status` endpoint added to `src/tourniquet/proxy/router.py`; `tests/test_budget_status.py` covers 6 cases.

## 2026-05-12 — Dashboard 500: `no such column: api_keys.tq_token_sha256` on existing SQLite dev DBs

**What failed:** `GET /dashboard` returned HTTP 500 with `sqlite3.OperationalError: no such column: api_keys.tq_token_sha256`. Server itself started fine; only routes that queried `api_keys` crashed.

**Root cause:** Schema drift on existing SQLite dev DBs. `tourniquet init` and the auto-bootstrap on `start` call `Base.metadata.create_all()`, which only creates missing **tables** — it never adds columns to existing tables. Migration `0003_token_sha256_and_action_uniqueness.py` is what adds `tq_token_sha256`, but `alembic upgrade head` is non-viable on SQLite because `0001_initial_schema.py` opens with `CREATE EXTENSION IF NOT EXISTS "pgcrypto"` (Postgres-only). `migrations/env.py` even documents this: the SQLite fallback only makes the alembic CLI itself runnable, not upgrades. So any dev DB created before 0003 has no in-band way to catch up.

**Immediate fix:** Applied the column + unique index directly with SQL on the active DB as the unblock:

```sql
ALTER TABLE api_keys ADD COLUMN tq_token_sha256 VARCHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS ix_api_keys_tq_token_sha256 ON api_keys(tq_token_sha256);
```

**Long-term fix:** dialect-aware migrations + replaced `create_all` with a programmatic `alembic upgrade head` runner.
- `migrations/versions/0001_initial_schema.py` and `0002_api_key_actions.py` now use the portable `UUID`/`JSONB` types from `tourniquet.models` and gate `pgcrypto` / `gen_random_uuid()` behind `op.get_bind().dialect.name`. Boolean and timestamp defaults switched to `sa.text("true|false")` and `sa.func.now()` so they compile cleanly on both Postgres and modern SQLite.
- `migrations/versions/0004_fix_profile_default.py` skips on SQLite — the SQLite path always used the model-side default, so there's nothing to fix and no need to drag in `op.batch_alter_table`.
- `migrations/env.py` honours a programmatically-set `sqlalchemy.url` (so the runtime helper and tests can pass their own URL through) and runs `fileConfig` with `disable_existing_loggers=False` so it doesn't trample pytest's `caplog` handler when invoked mid-suite.
- New `src/tourniquet/migrate.py` — `upgrade_to_head(database_url)` runs `alembic upgrade head` programmatically, converting async URLs to their sync form. Replaces `Base.metadata.create_all()` in `main.py::lifespan`, `cli.py::cmd_add_key`, `scripts/init.py`, and `scripts/bootstrap_local.py`.
- Test coverage in `tests/test_migrations_sqlite.py` pins fresh-SQLite upgrade, the exact pre-0003 → head failure mode from this incident, and the runtime helper's idempotency.

Net: every `tourniquet` launch now runs `alembic upgrade head`. SQLite dev DBs catch up on the next launch after a release; no manual `ALTER TABLE` ever again.

## 2026-05-08 — LAUNCH BLOCKER: proxy never invoked fan_out, so real cap-hits never alerted

**What failed:** A `git grep fan_out` showed `fan_out` was only called from `cli.py::cmd_test_alerts` (the synthetic smoke-test command). The production proxy hot path (`/v1/messages` in `proxy/router.py`) wrote `usage_events` and called `add_spend()` but never fired alerts when spend crossed 50%/80%/cap. End users would have got desktop banners and Telegram/Slack pings *only* if they ran `tourniquet test-alerts` by hand. Real spend going past their cap silently went past — which defeats the entire product.

**Root cause:** Earlier work built the alert pipeline (`fan_out`, channel renderers, recovery flow) bottom-up but never wired it into the proxy's write path. The plumbing was in place; nobody pulled the lever.

**Fix:** New `notifier.maybe_fire_threshold_alert()` helper called from BOTH proxy write sites (non-streaming JSON path and streaming SSE path) right after `add_spend()` and inside the same session as the spend write. Helper:
- queries the audit log (`api_key_actions` where `action='alert_fired'` AND `created_at >= today_start_utc`) for the highest threshold already fired today
- decides via pure `_select_threshold()` whether to fire (50/80/-1, or no-op)
- records an `alert_fired` audit row in the same session (so it commits atomically with the spend — proves idempotency)
- spawns `asyncio.create_task(fan_out(...))` so the proxy response isn't held up by Slack/Telegram round-trips
- swallows ALL exceptions: alert-path failures must NEVER break the proxy

Recovery offer (post-cap-hit "+$N to continue?" prompt) is set on the `AlertEvent` only when `threshold == -1 AND kill_enabled=True` — monitor-mode users who hit cap don't see recovery options because their requests aren't actually blocked.

**Files touched:** `src/tourniquet/alerts/notifier.py` (new helper + `_select_threshold` pure function + `_last_fired_threshold_today`), `src/tourniquet/proxy/router.py` (two new call sites), `tests/test_notifier.py` (12 new tests covering the threshold-selection logic + 4 integration tests for the helper).

**Verification:** 175 passed, 3 skipped. Targeted: `TestSelectThreshold` covers 8 cases including direct cap-hit jumps from 0%, zero-cap defence, and idempotency of each level.

## 2026-05-08 — Postgres deployments would 500 on `api_key_actions` queries (no migration)

**What failed:** `ApiKeyAction` was added as a model and `Base.metadata.create_all()` auto-created it on SQLite local dev — so Dan's machine was fine. But Postgres production deployments use alembic migrations as the schema source of truth, and there was no migration for the new table. Any Postgres user running `alembic upgrade head` after pulling would not get the table, and the dashboard's `/dashboard/key/<id>/history` plus the proxy's threshold-alert helper would 500 on first query.

**Fix:** New migration `migrations/versions/0002_api_key_actions.py` with `down_revision="0001"`. Creates the table with two indexes — `ix_api_key_actions_api_key_id` and `ix_api_key_actions_created_at` — matching the access pattern of the dashboard route (filter by key, order by ts desc).

**Files touched:** `migrations/versions/0002_api_key_actions.py` (new).

**Verification:** Migration file syntactically valid (alembic loads it; revision graph 0001 → 0002 is linear). Postgres-side dry-run is left to the user — `ABSOLUTE_CEILING_USD_CENTS=…` only impacts SQLite locally, the production Postgres path won't be exercised until someone deploys.

## 2026-05-08 — Kill-now permanently destroyed the user's configured daily_cap

**What failed:** Tapping "🛑 Kill now" from any channel (Slack/Telegram/web) clamped `daily_cap_usd_cents` to today's spend (or 1¢ when spend was 0). The clamp was permanent — `daily_cap` is the persistent baseline, so tomorrow's quota was also destroyed. Dan flagged it after his Test_1 key got stuck at $0.01: he'd configured $1, killed it during testing, and couldn't understand why it had become 1¢ until I traced the kill chain.

**Root cause:** `_apply_kill_now` mutated the wrong field. `lifted_cap_usd_cents` exists exactly for "today-only override that auto-expires at midnight UTC" (see `proxy/router.py::_effective_cap`). The kill should write the lift, not the baseline. The original implementation got this backwards.

**Fix:** `_apply_kill_now` now writes `lifted_cap_usd_cents = max(today_spend, 1)` and sets `lift_expires_at` to next midnight UTC. `daily_cap_usd_cents` is intentionally untouched. The proxy's effective-cap function honours the lift while it's active, so today's requests are blocked. Tomorrow at 00:00 UTC the lift expires and `daily_cap` resumes — no manual intervention needed to "restart" the key.

**Files touched:** `src/tourniquet/routes/admin.py` (rewrote `_apply_kill_now`), `tests/test_admin_kill_now.py` (renamed both test cases + updated assertions to expect lifted-cap behaviour and an untouched daily_cap).

**Side note:** Audit log row for the kill now describes both fields explicitly: "lifted_cap clamped to $4.20 until midnight UTC; daily_cap preserved at $10.00" — so even if a future regression happens, the audit row makes it obvious.

## 2026-05-08 — Slack `chat.postMessage` returns `invalid_blocks` on Block Kit actions block

**What failed:** Bot-post mode was wired up correctly (xoxb token + channel ID present, payload reaching `chat.postMessage`), but Slack rejected every alert with `invalid_blocks`. CLI dispatcher reported `✅ slack delivered` (false positive — see follow-up).

**Root cause:** Two buttons in the standard alert (`💸 Lift 2× today` and `🚀 To ceiling`) both used `action_id: "lift"`. Slack's Block Kit spec requires action_ids to be unique within an `actions` block; duplicates trigger `invalid_blocks`. Recovery alerts had the same shape with three `lift_by_amount` buttons.

**Fix:** Gave each button a unique `action_id` (`lift_2x`, `lift_ceiling`, `lift_by_amount_<cents>`) and switched the Socket Mode dispatcher to prefix-match (`startswith("lift_by_amount")`, `startswith("lift_")`). Routing payload still travels in `value` so the dispatch logic itself didn't change.

**Files touched:** `src/tourniquet/alerts/slack.py` (`_build_action_payload`), `src/tourniquet/alerts/slack_socket.py` (`_handle_interactive`), `tests/test_slack_socket.py` (3 cases updated to assert prefix + uniqueness).

**Follow-up (also fixed in same commit):** `_send_via_bot()` previously only logged a warning when Slack returned `ok: false` — the CLI dispatcher reported `✅ delivered` for failures. Now raises `RuntimeError("slack chat.postMessage failed: <reason>")` so failures surface as `❌ slack <reason>` to the operator.

## 2026-05-07 — Slack incoming webhook returns 400 on Block Kit `actions` blocks

**What failed:** When `SLACK_APP_TOKEN` was set, `slack.py` started rendering payloads with Block Kit `actions` blocks (button rows with `action_id` + `value`) so Socket Mode could deliver taps. The webhook `POST` returned `400` and no message was delivered.

**Root cause:** Slack's incoming webhooks only accept `text` and a subset of Block Kit (sections, dividers, headers, context, image). They explicitly reject `actions` blocks because interactive components need to be posted by a Bot User via `chat.postMessage`. The xapp-token (app-level) authorises Socket Mode receive only — it doesn't authorise sending bot messages.

**Fix:** Gate the Block Kit code path on three things being true: `SLACK_APP_TOKEN` AND `SLACK_BOT_TOKEN` AND `SLACK_CHANNEL_ID`. When only the first is set, the renderer falls back to mrkdwn link payloads (which webhooks accept) and the Socket Mode client stays dormant ("Slack Socket Mode dormant — SLACK_APP_TOKEN set but SLACK_BOT_TOKEN / SLACK_CHANNEL_ID missing").

**Files touched:** `src/tourniquet/alerts/slack.py` (gate), `src/tourniquet/alerts/slack_socket.py` (dormant gate), `src/tourniquet/config.py` (new `slack_bot_token`, `slack_channel_id`).

**Follow-up (v0.2):** Implement `chat.postMessage` send path so when bot token + channel ID are provided, alerts post via the bot and Block Kit action buttons drive in-app one-tap via the existing Socket Mode handler. Setup guide section in `docs/alerts-setup.md` already lists the four Slack-side actions the user takes.

## 2026-05-07 — Slack Socket Mode WebSocket fails with CERTIFICATE_VERIFY_FAILED on Python.org Python

**What failed:** With `SLACK_APP_TOKEN=xapp-...` set, the Socket Mode client successfully fetched the wss URL via `apps.connections.open` but raised `[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1002)` when calling `websockets.connect(ws_url)`. Reconnect loop spammed the log every 2/4/8/16/32s.

**Root cause:** Python.org's Python builds for macOS ship without bootstrapping the system CA store; you'd normally run the `Install Certificates.command` shipped with the installer. `httpx` works around this by using `certifi.where()` as the default cafile — but `websockets` uses the standard library's empty default SSL context.

**Fix:** Build an explicit SSL context backed by certifi and pass it to `websockets.connect`:
```python
import ssl, certifi
ssl_context = ssl.create_default_context(cafile=certifi.where())
async with websockets.connect(ws_url, ssl=ssl_context, ...) as ws:
```

**Files touched:** `src/tourniquet/alerts/slack_socket.py` — added `_ssl_context()` helper, threaded through.

**Follow-up:** Will affect any other WebSocket / TLS code we add. Helper is reusable. Worth a startup check that prints "Run `Install Certificates.command` if Python.org Python on macOS" if certifi import fails.

## 2026-05-07 — Alerts subsystem never invoked from production code path 🚨 LAUNCH BLOCKER

**What failed:** During AFK alert-channel testing I discovered `fan_out` is only called from the new `tourniquet test-alerts` CLI subcommand. Search across the codebase for callers:
```
$ grep -rn "fan_out(" src/tourniquet --include="*.py" | grep -v "notifier.py:"
src/tourniquet/cli.py:404:    results = asyncio.run(fan_out(event, kill_enabled=not args.monitor))
```
That's it. The proxy records spend (`add_spend(...)` in `proxy/router.py`) but never crosses thresholds → alerts → fan_out. Every alert channel works correctly when invoked, but in production no alerts ever fire.

**Root cause:** Threshold-detection logic was never wired into the `/v1/messages` post-processing path. `triggers/evaluator.py` defines `spend_threshold_pct` as a condition type but no caller exists.

**Fix (required before launch):**
1. After `add_spend(...)` in `proxy/router.py` (both streaming `_generate` epilogue and non-streaming branch), compute `today_spent_pct = spent_after / cap * 100` (use `lifted_cap_usd_cents` if active, else `daily_cap_usd_cents`).
2. Determine which thresholds (50, 80, 100) the request just crossed. Need state to avoid re-firing — simplest: add `alerts_fired_today: list[int]` JSON column on `daily_spend` table OR last-fired-pct integer on `api_keys` row reset at midnight UTC.
3. For each newly-crossed threshold, build `AlertEvent` and `await fan_out(event, kill_enabled=key.kill_enabled)`. Pass `key.alert_email` into the event.
4. For cap-hit specifically: fire when the cap-injection actually triggers (streaming path's `cap_was_hit=True` branch + non-streaming pre-flight 402 branch).

Estimated effort: 30-60min. This is the single highest-priority fix before any public launch — the entire alerts-channel feature is dead code without it.

**Files to touch:** `src/tourniquet/proxy/router.py` (call site), `src/tourniquet/models.py` (idempotency state column), and an integration test that asserts `fan_out` is called when 80% is crossed.

## 2026-05-07 — Email channel falsely reported "sent" with no creds

**What failed:** `tourniquet test-alerts` reported `email: ✅ delivered` with `RESEND_API_KEY=` empty. Should have reported `skipped:no-config`.

**Root cause:** `alerts/email.py:send_email` returned silently (`if not settings.resend_api_key: return`) without raising. The dispatcher treated a non-raising coroutine as success → "sent". Pattern was inconsistent with slack/telegram/webhook which were credential-checked in the dispatcher upstream.

**Fix:** Move the credential check into `notifier.py:fan_out`. Email dispatch now matches the slack/telegram/webhook pattern:
```python
if settings.resend_api_key and settings.resend_from_email:
    coroutines.append(_run("email", send_email(message, event)))
else:
    tasks.append(("email", False))
```
Plus: replaced placeholder recipient (`[settings.resend_from_email]`) with `getattr(event, "alert_email", None) or settings.resend_from_email`. New `alert_email` field on `AlertEvent`. Three new tests in `tests/test_notifier.py`. 17/17 notifier tests pass; 147/150 full-suite (3 unrelated skips).

**Files touched:** `src/tourniquet/alerts/notifier.py`, `src/tourniquet/alerts/email.py`, `tests/test_notifier.py`.

## 2026-05-07 — Non-streaming `/v1/messages` requests bypassed accounting

**What failed:** First real-traffic smoke test through the proxy. Anthropic returned the response (so passthrough worked), but Tourniquet recorded `model=unknown, input=0, output=0, cost=0¢`. Multiple requests would silently bypass the cap entirely.

**Root cause:** `providers/anthropic.py:stream_request` used `aiter_lines()` and only parsed `event:`/`data:` SSE lines. For non-streaming requests (no `"stream": true` in body), Anthropic returns a single JSON blob — no SSE events — so `UsageAccumulator` saw zero events and recorded zero tokens.

**Fix:** `proxy/router.py` now detects `stream` field in the request body and dispatches:
- streaming → existing SSE path with mid-stream cap kill
- non-streaming → `httpx.AsyncClient.post`, parse `usage` from response JSON, persist, return as `Response` (correct content-type)

Mid-stream kill is impossible for non-streaming responses (no stream to inject into). Cap is still enforced pre-flight, so the next request hits the 402. Single-request bound by `max_tokens` so blast radius is bounded regardless.

**Files touched:** `src/tourniquet/proxy/router.py` (new non-streaming branch), import `httpx` and `Response` directly.

**Test gap:** existing tests only covered the SSE path. v0.1.1 should add a non-streaming test against a mock httpx response.

## 2026-05-06 — `ANTHROPIC_BASE_URL` clobbered by shell env

**What failed:** During E2E PoC test, Tourniquet forwarded requests to the real `api.anthropic.com` despite `.env` having `ANTHROPIC_BASE_URL=http://127.0.0.1:9999` (pointing at a fake upstream). First test request returned a real Anthropic auth error.

**Root cause:** Claude Desktop sets `ANTHROPIC_BASE_URL=https://api.anthropic.com` in the shell environment. `pydantic-settings` prioritises shell env vars over `.env` file values, so the `.env` override was silently ignored.

**Fix (immediate):** Pass the override on the uvicorn command line:
```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:9999 python -m uvicorn tourniquet.main:app ...
```

**Fix (proper, v0.1.1):** Rename the setting to `TOURNIQUET_UPSTREAM_URL` to avoid the namespace collision. Ship a startup check that warns if `ANTHROPIC_BASE_URL` is set in the shell env when running the proxy, since users will hit this constantly.

**Files touched in repro:** `src/tourniquet/config.py` (field name), `src/tourniquet/providers/anthropic.py` (URL ref), `.env.example` (rename).
