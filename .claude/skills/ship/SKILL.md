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

**Deploy state as of 2026-06-12 (verified live):** the Fly app has never been deployed. `tourniquet.dev` is a static landing page (its `/health` returns 404); `tourniquet-web.fly.dev` is unreachable; `ENABLE_FLY_DEPLOY=false` and no `FLY_API_TOKEN` secret exists on the repo; `flyctl` is not installed on the Mac. Until Dan enables a deploy path, this gate ends at Step 4: "shipped" means **committed AND pushed to `main`**, and Steps 4–5 are dormant. Re-verify this note (curl the two URLs, `gh variable list`) before trusting it — delete it once the first real deploy lands.

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

CI will run `flyctl deploy --app tourniquet-web --config fly.toml --remote-only --build-arg GIT_SHA=${{ github.sha }}` then `flyctl deploy --app tourniquet-worker --config fly.worker.toml --remote-only`. The `GIT_SHA` build-arg is what makes Step 5's commit check work — it is baked into the image and reported by `/health`. The worker deploy deliberately omits it: the worker serves no `/health` endpoint, so there is nothing to report. If the worker ever gains one, add the same build-arg to its deploy commands.

**Option B — manual flyctl deploy** (fallback when CI deploy is not configured):

```zsh
# Mac zsh — from /Users/danlowry/Desktop/AI/burnrate
# FLY_API_TOKEN must be set — load from .env or fly dashboard, never hardcode
export FLY_API_TOKEN=<load from .env or flyctl auth token>
flyctl deploy --app tourniquet-web --config fly.toml --remote-only --build-arg GIT_SHA=$(git rev-parse HEAD)
flyctl deploy --app tourniquet-worker --config fly.worker.toml --remote-only
```

**Hard gate:** `flyctl deploy` must exit 0 for both apps. A non-zero exit means the deploy failed — do not proceed to Step 5, do not write any log entry claiming "shipped".

---

### Step 5 — Verify tourniquet.dev version matches the commit (Haiku)

The word "shipped" may only be used after this step passes.

```zsh
# Mac zsh — from /Users/danlowry/Desktop/AI/burnrate
curl -s https://tourniquet.dev/health | python3 -m json.tool
git rev-parse HEAD
```

Expected response shape (from `src/tourniquet/main.py`):
```json
{"status": "ok", "version": "0.1.0", "commit": "<full git SHA>"}
```

**Hard gate:** the `commit` field must exactly equal the local `git rev-parse HEAD` output. That equality proves the running code IS the commit you just made — not a stale build at the same version number. If `/health` is unreachable, errors, or the SHAs differ, do NOT write "shipped" anywhere; investigate the deploy.

If `commit` is `"unknown"`, the image was built without the `GIT_SHA` build-arg — redeploy using the exact Step 4 commands (both the CI path and the manual path pass it).

---

### Step 6 — Write the log entry (Haiku)

Only after Step 5 passes: write the ERRORS.md or HANDOFF.md entry. The entry must include:

- The pytest summary line from Step 1
- The git commit SHA (from `git log --oneline -1`)
- The `/health` response confirming the live `commit` SHA matches
- The word "shipped" may now be used

ERRORS.md entry format:

```markdown
## YYYY-MM-DD — <title>

**What failed:** <description>
**Root cause:** <root cause>
**Fix:** <what was done> — committed `<SHA>`, deployed to tourniquet.dev (`/health` `commit` matches `<SHA>`). Tests: <pytest summary line>.
```

---

## Never

- Never write "shipped" in any log or summary before Step 5 passes
- Never skip the pytest run (Step 1) on the grounds that "nothing test-related changed"
- Never `git add -A` — always stage specific files to avoid committing `.env`
- Never invent a deploy command — use `flyctl` with the config files that actually exist: `fly.toml` (web) and `fly.worker.toml` (worker)
- Never skip the worker deploy — `tourniquet-worker` handles midnight cap resets and email queues; shipping only the web app leaves these stale
