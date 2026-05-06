# BurnRate — Project Plan

**Date:** 2026-05-05
**Status:** Locked — ready to build
**One-liner:** A firebreak proxy for Claude. Set a daily £ cap on your Anthropic API spend. We kill the agent before it consumes the rent.

---

## North star

After the 1.67-billion-token Claude Code incident and the £47K LangChain loop, every developer running unattended Claude agents is one bad weekend away from a bill they can't pay. BurnRate is a free, drop-in proxy that catches that before it happens. Build small, ship fast, charge later.

---

## Audience

Sharpened 2026-05-06 after honest grill: non-devs aren't the buyer. They can't set env vars, can't price a cap. They were positioning fluff. Cut.

The real audience, in priority order:

1. **Solo developers running Claude Code unattended** — concrete pain (1.67B-token incident), 30-second install
2. **Indie founders / small teams where the founder commits code** — will pay £19/mo for "I sleep better"
3. **Platforms (Lovable / Flowise) bundling BurnRate** — B2B, not until 500+ users prove demand

Not in MVP audience: enterprises, OpenAI-only users, non-developers running hosted no-code platforms (they need the platform to integrate us — that's a B2B sale we don't pursue in v1).

## What we're actually selling

Be honest about the value: BurnRate is a thin layer over Anthropic's native API. The non-trivial parts are:

- **Mid-stream kill** (synthetic `message_stop`) — Anthropic has no equivalent
- **Atomic cap accounting under concurrency** — `INSERT … ON CONFLICT DO UPDATE` on `caps_today`, no race conditions
- **Multi-key isolation with revocation** — leaked dev key doesn't blow prod cap

Everything else (token counting, alert thresholds, profiles) is convenience around those three. Don't oversell it. The pitch is: *"a circuit-breaker for the failure mode Anthropic doesn't protect against."*

---

## Confirmed decisions (locked)

| Decision | Answer |
|---|---|
| Product name | **BurnRate** |
| Domain | `burnrate.ai` (preferred) / `burnrate.dev` (fallback) |
| Anomaly detection timing | **Week 4** — cold-start noise → false positives if enabled in W1 |
| LLM support scope | **Anthropic-only in v1**; OpenAI in v2 only if users ask (~6h with provider interface in place) |
| Multi-API keys | Multiple Anthropic API keys per user account (not multiple LLM providers) |
| Pricing | Free forever in v1; 2.5% of spend (capped £29/mo) in v3 once 500+ users |

---

## MVP scope — what ships in week 1

A single Fly.io app exposes:

1. **Anthropic-compatible proxy** at `/v1/messages` — transparent SSE streaming, non-streaming, all models
2. **Magic-link signup** (email only, no password)
3. **Multiple API keys per user** — register N Anthropic keys, name them ("prod", "dev", "claude-code-personal"), each with its own cap and profile
4. **Hard kill switch** — when today's £ spend > cap, terminate in-flight stream mid-token + 402 subsequent requests until midnight UTC
5. **Three pre-built profiles** (no custom rules editor in v1):
   - **Hobby** — generous cap, soft alerts, kill at 200% of cap
   - **Production** — alerts at 50/80/100%, hard kill at 100% (kill defaults OFF — user opts in)
   - **Demo day** — never kill silently, pause-and-ask at threshold
6. **Email alert at 80%** — one per day per key, idempotent (Resend)
7. **Dashboard** — today's spend per key, cap config, profile picker, last 50 usage events
8. **Anomaly evaluator scaffolded** — code paths in place, rule turned **off** until week 4

That is the entire MVP. Cut everything else.

---

## NOT in MVP (intentionally deferred)

| Feature | When | Why deferred |
|---|---|---|
| OpenAI support | v2, only if asked | ~6h with provider interface in place; Claude users don't need it |
| Gemini support | v3+ | Niche audience |
| Stripe / billing | 500+ users | No revenue infra until product proven |
| Slack / SMS / webhooks | v2 | Email covers MVP need |
| Custom rules editor | v2 | Profiles cover 90% of cases |
| Team accounts | v3 | Solo audience first |
| Per-session caps | v2 | Daily cap covers worst case |
| Auto-degrade to cheaper model | v3 | Nice-to-have, not load-bearing |

---

## Tech stack

| Layer | Choice | Rationale |
|---|---|---|
| Runtime | Python 3.12 + FastAPI + httpx | Mature, fast SSE, simple |
| DB | Fly Postgres (free tier 3GB) | Boring, ACID, free at this scale |
| Templates | Jinja2 | No SPA — keeps complexity flat |
| Auth | Magic-link via Resend | No password complexity in v1 |
| Email | Resend free tier (3K/mo) | Free, deliverability good |
| Errors | Sentry free tier | Free, mature |
| Hosting | Fly.io | One web app + one worker app, ~£8–10/mo combined |
| Domain | `burnrate.ai` | Dan to register |

---

## Anthropic-specific architecture

- **Endpoint shape**: pass-through to `https://api.anthropic.com/v1/messages`. Match Anthropic SSE event names (`message_start`, `content_block_delta`, `message_delta`, `message_stop`) — no normalisation in v1.
- **Auth**: user stores their `sk-ant-...` key in dashboard (encrypted at rest with `cryptography.Fernet`). Proxy injects it on each request as `x-api-key` header. We never log the key.
- **API version pinning**: default `anthropic-version: 2023-06-01`. If user's request has a different version header, pass it through.
- **Token counting**: read `message_start.usage` (input tokens) + accumulate `message_delta.usage` (output tokens) from streamed events. No `tiktoken`, no separate counting library.
- **Cost calculation**: per-model £ rates table in `pricing.py`. Update when Anthropic publishes price changes. Models supported in v1: Claude Sonnet 4.5, 4.6, 4.7, Claude Opus 4.5–4.7, Claude Haiku 4.5.
- **Streaming kill mechanic**: when cumulative cost crosses cap mid-stream, send a synthetic `message_stop` event to the client with `stop_reason: "burnrate_cap_hit"`, then close the connection. Client gets a clean termination, not a half-token corruption.
- **Drop-in for Claude Code**: user sets `ANTHROPIC_BASE_URL=https://burnrate.ai` + `ANTHROPIC_API_KEY=br_xxxxxxxxxxxx` (their BurnRate token, not their Anthropic key). BurnRate resolves the BurnRate token to the user's stored Anthropic key.

---

## Multiple API keys per user — clarified

Each BurnRate user can register N Anthropic API keys. Each registered key gets:

- Friendly name (e.g. "prod", "dev", "claude-code-experiments")
- A BurnRate token (`br_*`) to use in client code instead of the raw Anthropic key
- Its own daily £ cap
- Its own profile (Hobby / Production / Demo / Custom-coming-in-v2)
- Optional alert recipient email (defaults to account email)
- Kill switch on/off toggle

Use cases: separate "prod" cap from "personal experimentation" cap; revoke a leaked dev key without affecting prod; stricter rules for unattended cron jobs vs interactive use.

---

## Database schema (locked for v1)

```sql
users (id, email, magic_link_token, created_at, stripe_customer_id NULL)
api_keys (id, user_id, name, br_token_hash, anthropic_key_encrypted, profile, daily_cap_pence, kill_enabled, alert_email, created_at)
usage_events (id, api_key_id, request_id, model, input_tokens, output_tokens, cost_pence, cap_hit, created_at)
triggers (id, api_key_id, condition_json, actions_json, enabled, last_fired_at)  -- scaffolded, anomaly turns on W4
caps_today (api_key_id PK, date, total_pence)  -- denormalised for fast cap-check on hot path
```

`stripe_customer_id` and `cost_pence` (not pounds) are intentional — enables % billing later with zero migrations.

---

## 4-week sequence

| Week | Hours | Deliverable | Kill criterion |
|---|---|---|---|
| **1 — MVP build** | ~14h | Anthropic proxy + magic-link signup + multi-key dashboard + 3 profiles + email alerts + cap kill, deployed on Fly.io | Streaming kill doesn't actually stop Anthropic billing → architecture rethink |
| **2 — Self-test + soft launch** | ~7h | 7 days of Dan's own Claude Code traffic through BurnRate; landing page; Show HN; Anthropic Discord post; /r/ClaudeAI post; X thread anchored on the 1.67B-token incident | <30 free signups in week 1 of public exposure → reframe positioning before pushing harder |
| **3 — KeyHunt minimal** | ~6h | Cron-based Docker Hub Registry v2 + MCP config scanner, TruffleHog subprocess, findings table; Dan triages and submits 5 best findings to HackerOne manually | <2 accepted submissions → format wrong, fix before scaling |
| **4 — Anomaly + iterate** | ~7h | Anomaly detection rule turned on (3× rolling-7d-baseline trigger); iterate on user feedback; tutorial blog posts (BurnRate + Claude Code, BurnRate + Lovable, BurnRate + n8n) | <100 active users by end of week 4 → reframe positioning |

**Total active dev time: ~34 hours over 4 weeks.** Realistic calendar: one focused weekend + light evening work.

---

## Hosting & ops (~£10/month)

| Item | Cost | Notes |
|---|---|---|
| Fly.io web app (proxy + dashboard) | ~£5 | shared-cpu-1x, 256MB |
| Fly.io worker app (midnight reset cron + alerts) | ~£2 | shared-cpu-1x |
| Fly Postgres | £0 | Free tier 3GB shared between both apps |
| Domain `burnrate.ai` | ~£1 | ~£10/year amortised |
| Resend (transactional email) | £0 | Free tier 3K/mo |
| Sentry | £0 | Free tier 5K events/mo |
| Better Stack uptime | £0 | Free tier 10 monitors |
| **Total** | **~£8–10/mo** | Sustainable at zero customers |

---

## Distribution plan

### Phase A — alpha (week 2): 5 hand-picked users
- Dan + 4 contacts who actively run Claude Code or Anthropic agents in production
- Each runs BurnRate for 7 days
- Goal: confirm zero false positives, zero missed kills, no perceptible latency

### Phase B — soft launch (weeks 3–4): aim for 100 signups
1. **Show HN.** Headline: *"BurnRate — a kill switch for Claude, after the 1.67B-token incident."* Tuesday-Thursday morning UK time. Single sharp narrative. Free, no signup-wall.
2. **Anthropic developer Discord** — find #showcase or equivalent, one post with screenshot of "saved £127 today" from Dan's own usage.
3. **/r/ClaudeAI + /r/LocalLLaMA + /r/LangChain** — anchored on the incident, not a launch announcement.
4. **X/Twitter thread** — 5 tweets: incident hook → screenshots → free tool link in last tweet. No "launching" tweet first.

Cut from the original plan: Lovable Discord. Their audience builds apps without writing code; they can't install a proxy. Pursue them only if/when bundling becomes the path (Phase D, 500+ users).

### Phase C — organic flywheel (weeks 5+)
- **Tutorials** (one each): *"Use BurnRate with Claude Code in 30 seconds"*, *"...with Lovable"*, *"...with n8n"*. SEO + community-shareable. Submit to dev.to and Hashnode.
- **User screenshots** — every "BurnRate saved me £X" message becomes a testimonial post.
- **Awesome lists** — submit to Awesome-LLM, Awesome-Claude, Awesome-AI-Tools.

### Phase D — partnership (week 8+, only if 500+ users)
- Approach **Anthropic** about being listed in their developer-ecosystem tools (low-probability ask, free to make).
- Approach **Lovable / Flowise** about default-bundling.

### Where NOT to spend time
- Cold email to enterprises (wrong audience)
- Paid ads (no conversion path with no paid tier)
- Conference talks (ROI terrible at this scale)
- LinkedIn (audience mismatch)

---

## KeyHunt — minimal scope (week 3 only)

KeyHunt is a worker, not a co-equal product. Single cron job:

1. Pull top-100 most-pulled Docker Hub images each hour (Registry v2 API)
2. Pull GitHub Code Search results for `mcp.json` and `claude_desktop_config.json` containing `sk-ant-` patterns
3. Pipe through TruffleHog (subprocess)
4. Insert verified findings into `keyhunt_findings` table
5. Dan reviews top 10 findings weekly, manually drafts and submits the highest-impact ones to HackerOne

No UI. No automation of submission. No customer support. Just a cron and a findings table. If it generates >£500/month in bounties by week 12, expand. If not, retire.

---

## Future hooks (built into v1, not exposed)

- `usage_events.cost_pence` from day one → enables % billing later via one Stripe Metered Billing call, no migration
- `users.stripe_customer_id NULL` from day one → no schema change when billing arrives
- Provider-agnostic interface (`providers/anthropic.py` is the only one in v1; `providers/openai.py` slot in later) → adds OpenAI in ~6h when demand emerges
- `triggers` JSON column on `api_keys` from day one → custom rules editor (v2) and anomaly detection (week 4) plug in without schema changes
- KeyHunt findings table separate from proxy data → can spin out as standalone product if it proves out

---

## Risk register (top 5)

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Proxy adds latency that drives Claude Code users away | Med | High | Benchmark p95 latency; target <50ms overhead vs raw Anthropic; warn on dashboard if exceeded; auto-fail-open after 30s connection timeout |
| R2 | Cloudflare AI Gateway ships £-denominated caps in <12 months | Med | High | Differentiate on "Claude-first + non-coder dashboard + KeyHunt"; if commoditised, the Anthropic proxy and KeyHunt are both still useful, so partial-pivot |
| R3 | False-positive kills break a user's prod app at the worst time | Med | Critical | Default kill OFF on Production profile; user must opt in; soft-alert by default |
| R4 | Anthropic ToS changes restrict third-party proxies | Low | Critical | Read ToS before launch; document our use; respond to any takedown promptly |
| R5 | KeyHunt account flagged as low-quality submitter on HackerOne | Med | Med | Dan reviews every submission for first 6 weeks; never submit auto-drafted reports without human pass |

---

## Kill criteria (project-wide)

- **Week 8:** if <100 free signups, run a 1-hour debrief; reframe positioning, pivot, or stop
- **Week 12:** if <500 active users AND KeyHunt < £500/mo, stop
- **Any time:** legal threat letter (CMA / CFAA / GDPR / Anthropic) → stop, lawyer up

---

## GitHub repository

Private repo: https://github.com/LowryDaniel/burnrate

Project directory: `/Users/danlowry/Desktop/AI/burnrate/`

---

## Immediate next 5 actions (this week)

1. **(Dan, 5 min)** Domain check — register `burnrate.ai` (and `burnrate.dev` fallback) on Namecheap or Cloudflare.
2. **(Dan, 5 min)** UKIPO + USPTO TESS quick trademark search on "BurnRate" in software class.
3. **(Sonnet, 30 min)** Build the SQLAlchemy models (`src/burnrate/models.py`), run `alembic revision --autogenerate`, apply initial migration.
4. **(Sonnet, 1h)** Build the Anthropic forwarding proxy: non-streaming `/v1/messages` → `api.anthropic.com/v1/messages`. Smoke test with `curl`.
5. **(Sonnet, 2h)** Add SSE streaming pass-through + cumulative token counting from `message_start.usage` and `message_delta.usage`. Write 3 integration tests (under cap, over cap mid-stream, multi-key isolation).

---

## Architectural commitments that pay off later

If you remember nothing else from this plan, build week 1 with these four invariants:

1. **Anthropic format pass-through** — don't normalise. Anthropic's SSE events go straight to the client. Saves 6h now and avoids a class of bugs forever.
2. **`cost_pence` everywhere** — never store cost in pounds, never store cost in dollars. Pence integer = no float-rounding pain when % billing arrives.
3. **`triggers` JSON column** — anomaly detection, custom rules, and per-session caps all slot into the same column without schema migration.
4. **Provider directory pattern** — `providers/anthropic.py` is the only file in v1, but the directory exists. OpenAI/Gemini drop in as new files when demand emerges.

These four decisions cost ~30 minutes total in week 1 and save weeks of rework later.
