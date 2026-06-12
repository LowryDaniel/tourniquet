# Handoff — 2026-06-12

## Task
Added the /ship skill (`.claude/skills/ship/SKILL.md`) and made deploys verifiable: `/health` now reports the git commit SHA baked in at image build time (Dockerfile `ARG GIT_SHA` → env; CI and manual deploy commands pass `--build-arg`).

## Current state
- **Deploy state VERIFIED (closes 2026-06-11 next action #1): the Fly app has never been deployed.** `tourniquet.dev` is a static landing page (`/health` → 404); `tourniquet-web.fly.dev` unreachable; `ENABLE_FLY_DEPLOY=false`; no `FLY_API_TOKEN` repo secret; no local `flyctl`. "Live at tourniquet.dev" currently means the landing page, not the app.
- Test suite run fresh this session: `234 passed, 3 skipped` (closes 2026-06-11 next action #2).
- /ship gate therefore ends at commit+push until a deploy path is enabled (noted, dated, inside the skill itself).
- Repo renamed on GitHub: canonical is `LowryDaniel/tourniquet`; local remote URL still says `burnrate` (redirects fine).

## Next actions
1. Dan decides: enable the Fly deploy path (set `FLY_API_TOKEN` secret + flip `ENABLE_FLY_DEPLOY=true`, or install flyctl locally) — or formally keep the app local-first and leave /ship Steps 4–5 dormant. (Decision, then Sonnet)
2. Create `Dockerfile.worker` (chip already spawned) — blocks the worker deploy whenever deploys go live. (Sonnet)

## Key files
- `.claude/skills/ship/SKILL.md` — the shipping gate; contains the dated deploy-state note.
- `src/tourniquet/main.py`, `Dockerfile`, `.github/workflows/ci.yml` — GIT_SHA injection chain.

## Open questions / blockers
- Whether the hosted Fly app is wanted at all for v0.1, or deferred to the v0.2 roadmap.
