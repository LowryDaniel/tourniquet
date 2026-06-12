---
name: ship
description: Enforce the Tourniquet shipping gate — run tests, verify migrations, commit, deploy to Fly.io, and confirm tourniquet.dev version matches before writing any log entry. Trigger when Dan says "ship it", "ship this", "push to prod", "deploy tourniquet", "release", "mark as shipped", or invokes /ship.
---

# Ship — Tourniquet production gate

Purpose: a fix is **shipped** when it is **committed AND live on tourniquet.dev**. This gate was added after ERRORS.md twice recorded fixes as "shipped" while the code sat uncommitted (2026-06-09, 2026-06-11). Every step is mandatory. None may be skipped.

---

## Pre-flight: what you need

- `flyctl` installed and authenticated (`flyctl auth whoami` confirms it)
- `FLY_API_TOKEN` set in the environment — load from `.env` or the Fly.io dashboard (Settings → Tokens); **never hardcode it here**
- The GitHub Actions CI deploy path (`vars.ENABLE_FLY_DEPLOY=true` + `secrets.FLY_API_TOKEN` set in repo Settings) is the automated path; this skill covers the **manual** fallback when CI is not configured or a manual push is needed

---

## Gate sequence — run in order, none skippable

### Step 1 — Run pytest THIS session (Haiku)

A test run must happen in the current session. A passing run from a previous session does not count.

```zsh
# Mac zsh — from /Users/danlowry/Desktop/AI/burnrate
cd /Users/danlowry/Desktop/AI/burnrate && pytest --tb=short -q 2>&1 | tail -5
```

**Hard gate:** if any tests fail, stop. Fix or explicitly accept the failure with Dan's confirmation before continuing. Paste the summary line (e.g. `175 passed, 3 skipped`) into the ERRORS.md entry at the end.

---

### Step 2 — Schema migration check (Sonnet)

Run this check every time. If any new migration file was added since the last deploy, you must verify both database paths.

**Detect new migrations:**

```zsh
# Mac zsh — from /Users/danlowry/Desktop/AI/burnrate
git diff HEAD~1..HEAD --name-only -- 'migrations/versions/*.py'
```

If that outputs migration filenames (or if you are uncertain whether migrations changed), run Step 2a and 2b. If output is empty and no migration files are dirty/staged, skip to Step 3.

**Step 2a — SQLite path:**

```zsh
# Mac zsh — from /Users/danlowry/Desktop/AI/burnrate
python -c "from tourniquet.migrate import upgrade_to_head; upgrade_to_head('sqlite+aiosqlite:///./test_migrate_gate.db'); print('SQLite: OK')" && rm -f test_migrate_gate.db
```

**Step 2b — Postgres path (requires a live Postgres connection):**

```zsh
# Mac zsh — from /Users/danlowry/Desktop/AI/burnrate
# DATABASE_URL must point to a real Postgres instance (dev or CI)
DATABASE_URL=postgresql+psycopg://tourniquet:test@localhost:5432/tourniquet_test python -c "from tourniquet.migrate import upgrade_to_head; upgrade_to_head('postgresql+psycopg://tourniquet:test@localhost:5432/tourniquet_test'); print('Postgres: OK')"
```

**Hard gate:** both paths must print OK. If SQLite fails, do not proceed. If a local Postgres instance is unavailable, note this explicitly and accept the risk with Dan's confirmation — the CI job (`ci.yml` `Run migrations` step) covers the Postgres gate on push.

---

### Step 3 — Commit (Sonnet)

No fix may be called "shipped" before this step.

```zsh
# Mac zsh — from /Users/danlowry/Desktop/AI/burnrate
git status
git add <specific files — never git add -A blindly>
git commit -m "<message>"
```

Rules:
- Never commit `.env` (gitignored; contains live Anthropic keys and Fernet key)
- Commit message should reference the ERRORS.md entry being closed if applicable
- Verify `git status` shows a clean tree after commit

---

### Step 4 — Deploy to Fly.io (Sonnet)

**Pre-flight blocker:** `Dockerfile.worker` must exist before deploying the worker app.

```zsh
# Mac zsh — from /Users/danlowry/Desktop/AI/burnrate
ls /Users/danlowry/Desktop/AI/burnrate/Dockerfile.worker
```

If that file is missing, the worker deploy (`flyctl deploy --app tourniquet-worker --config fly.worker.toml --remote-only`) will fail immediately — the build context does not exist. **Do not proceed with the worker deploy until `Dockerfile.worker` is created, and get explicit confirmation from Dan if skipping the worker deploy is acceptable.** Option A (CI) will also fail on its Deploy worker step without this file.

Two apps must be deployed: `tourniquet-web` (serves tourniquet.dev) and `tourniquet-worker` (background jobs).

**Option A — push to `main` and let CI deploy** (preferred when `ENABLE_FLY_DEPLOY=true` is set in repo vars):

```zsh
# Mac zsh
git push origin main
```

Then watch the GitHub Actions `deploy` job at: https://github.com/LowryDaniel/tourniquet/actions

CI will run `flyctl deploy --app tourniquet-web --config fly.toml --remote-only` then `flyctl deploy --app tourniquet-worker --config fly.worker.toml --remote-only`.

**Option B — manual flyctl deploy** (fallback when CI deploy is not configured):

```zsh
# Mac zsh — from /Users/danlowry/Desktop/AI/burnrate
# FLY_API_TOKEN must be set — load from .env or fly dashboard, never hardcode
export FLY_API_TOKEN=<load from .env or flyctl auth token>
flyctl deploy --app tourniquet-web --config fly.toml --remote-only
flyctl deploy --app tourniquet-worker --config fly.worker.toml --remote-only
```

**Hard gate:** `flyctl deploy` must exit 0 for both apps. A non-zero exit means the deploy failed — do not proceed to Step 5, do not write any log entry claiming "shipped".

---

### Step 5 — Verify tourniquet.dev version matches the commit (Haiku)

The word "shipped" may only be used after this step passes.

```zsh
# Mac zsh
curl -s https://tourniquet.dev/health | python3 -m json.tool
```

Expected response shape (from `src/tourniquet/main.py:68-70`):
```json
{"status": "ok", "version": "0.1.0"}
```

Compare the `version` field against `src/tourniquet/__init__.py:__version__`. If the versions match and `status` is `"ok"`, the deploy is confirmed live.

**Hard gate:** if the `/health` response is unreachable, returns an error, or shows a stale version, do NOT write "shipped" anywhere. Investigate — the deploy may have failed silently or the machine may not have restarted.

Note: version `"0.1.0"` is currently hardcoded in both `main.py` and `__init__.py`. When the project bumps its version number, this check will catch stale deploys automatically. Because the version string alone cannot distinguish two different commits at the same version, also verify one observable behaviour changed by the fix — for example: a specific endpoint response reflecting the new logic, a log line emitted by the new code, or a migration-applied indicator in the database. Confirming `status: ok` at the same version proves the app is running; verifying the changed behaviour proves the correct commit is live.

---

### Step 6 — Write the log entry (Haiku)

Only after Step 5 passes: write the ERRORS.md or HANDOFF.md entry. The entry must include:

- The pytest summary line from Step 1
- The git commit SHA (from `git log --oneline -1`)
- The `/health` response confirming the live version
- The word "shipped" may now be used

ERRORS.md entry format:

```markdown
## YYYY-MM-DD — <title>

**What failed:** <description>
**Root cause:** <root cause>
**Fix:** <what was done> — committed `<SHA>`, deployed to tourniquet.dev (`/health` → `{"status":"ok","version":"0.1.0"}`). Tests: <pytest summary line>.
```

---

## Never

- Never write "shipped" in any log or summary before Step 5 passes
- Never skip the pytest run (Step 1) on the grounds that "nothing test-related changed"
- Never `git add -A` — always stage specific files to avoid committing `.env`
- Never invent a deploy command — use `flyctl` with the config files that actually exist: `fly.toml` (web) and `fly.worker.toml` (worker)
- Never skip the worker deploy — `tourniquet-worker` handles midnight cap resets and email queues; shipping only the web app leaves these stale
