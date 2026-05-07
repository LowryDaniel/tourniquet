# Feature requests

Newest at top. **Status: done** entries link to the relevant module/commit when known.

## 2026-05-07 — Profile redesign + one-click kill from alerts
**Want:** The original `hobby/production/demo` profiles didn't add value (`kill_at_pct` and `kill_silently` fields were unused; `production`'s default-OFF kill was footgun-shaped; `demo` had no real use case). Replace with three profiles where the differences are *real*. Plus: when a key is in observe-mode (kill off), every alert must include a one-click way to enforce the cap.
**Why:** Fake configuration is worse than no configuration. Disabling the kill switch is a deliberate trade-off — when the user makes that choice, they need a fast escape hatch when reality bites.
**Status: done** —
- New profiles: `standard` (kill on, alerts at 50/80/100%), `strict` (kill on, alert at 100% only), `monitor` (kill OFF, alerts at 50/80/100% with one-click kill links).
- Dropped vestigial fields `kill_at_pct` and `kill_silently`.
- Heavy confirm dialog when toggling kill OFF in the dashboard or unchecking it on the new-key form (enabling stays friction-free).
- New `/admin/kill-now/{key_id}?token=<...>` magic-link endpoint with 24h `itsdangerous`-signed token + GET confirmation page + POST that sets `kill_enabled=True` and clamps `daily_cap` to current spend.
- Telegram inline keyboard adds 🛑 Kill now button alongside lift buttons.
- Email, Slack, JSONL log, generic webhook, and desktop notification all surface the kill-now URL when applicable.
- `tests/test_admin_kill_now.py` (new) + `tests/test_notifier.py` extended. 147 tests pass.

## 2026-05-07 — Tolerant pre-flight max-cost guard
**Want:** Reject a request pre-flight (HTTP 402) if its worst-case cost would push today's spend over cap by more than a tolerance.
**Why:** Without it, a single oversized streaming request can produce most of its output before Anthropic reveals output tokens at end-of-stream — leaving the user billed for ~18 KB of essay before the cap-hit injection fires. The mid-stream kill is post-hoc unless input cost alone exceeds the cap.
**Status: done** — `proxy/router.py` now estimates worst-case cost (input chars / 4 × 1.25 + max_tokens) and rejects pre-flight if overage > `max(MAX_OVERAGE_ABS_CENTS, cap × MAX_OVERAGE_PCT%)`. Defaults: 50¢ / 10%. Both env-configurable.

## 2026-05-07 — Cap-edit UX: chips, nudges, auto-save, dynamic scaling
**Want:** Quick-set cap to common values; +/- nudge buttons; no Save button; nudge amounts that scale with the cap magnitude (so $1k cap doesn't nudge by $1).
**Why:** Original `<input type="number" step="0.01">` with a Save button was tedious for any cap above $5.
**Status: done** — Six preset chips ($1/$5/$10/$25/$50/$100), four nudge buttons (`−big −small +small +big`) that recompute labels live based on cap value (1/5 → 5/25 → 25/100 → 100/500 → 500/2500 across cap tiers), auto-save on chip click / blur / Enter / nudge with a non-shifting "✓ saved" pulse on the autosave hint. Native browser spinbox hidden via CSS so only our scaled buttons drive increments.

## 2026-05-07 — Smart suggestions onboarding panel
**Want:** When a user creates their first key, immediately offer to fetch their last 14 days of Anthropic usage and suggest a cap. If they don't have an admin key, explain how to get one or fall back to "monitor over time."
**Why:** Cold-start cap selection is guesswork. A cap recommendation grounded in the user's actual history is the killer onboarding moment.
**Status: done** — Three branches on the post-creation page: (a) paste admin key → one-shot fetch from Anthropic Admin API → P50/P95/max + sparkline + numbered reasoning steps + recommended profile + "Apply both" button; (b) instructions for generating an admin key; (c) `auto_tune_mode = "suggest"` for learn-from-usage. Admin key zeroed in `finally` block, never persisted.

## 2026-05-07 — Visual reasoning + profile recommendation
**Want:** Show users WHY we suggested a particular cap. Show the math. Recommend a profile (hobby/production/demo) based on actual usage patterns.
**Why:** Trust comes from showing your work. A bare number is suspicious; a sparkline + step-by-step math + plain-English profile reason is convincing.
**Status: done** — `billing/suggestions.py:recommend_profile()` uses coefficient of variation + average to recommend production for steady ≥$20/day workloads, hobby otherwise. Reasoning rendered as a numbered green-bordered step list. Sparkline in inline SVG with the P95 day highlighted in orange. "Apply both" button sets cap + profile in one click.

## 2026-05-07 — Cross-platform CLI + auto-browser-open
**Want:** `pip install tourniquet-dev && tourniquet` works identically on macOS, Linux, and Windows; browser opens automatically at the dashboard.
**Why:** Two-command install is the difference between "I'll try it" and "looks complicated."
**Status: done** — `tourniquet/cli.py` argparse with subcommands `start | init | add-key | status | lift | register-url-handler | handle-url`. Resolves config dir to `~/.tourniquet/` cross-platform. Force-UTF8 stdout on Windows. Auto-browser via `webbrowser.open()`. Dashboard shell snippets show platform-aware tabs (bash / PowerShell / cmd) keyed off User-Agent.

## 2026-05-07 — Trust messaging at every touchpoint
**Want:** Make it visible at six surfaces in the product that nothing leaves the user's machine.
**Why:** Helicone got acquired by Mintlify; trust in hosted LLM-cost tools is shakier than it was. "Local-first" is a real moat — must be obvious in-product, not just in docs.
**Status: done** — Persistent green nav badge ("🔒 Local only · nothing leaves this {device}"), trust panel above sk-ant- field on new-key form, "bcrypt-only" note on token-shown page, alert-channels note in control panel, dashboard footer, dedicated `/trust` page with three colour-coded cards + verify-it-yourself shell commands. Device label dynamic — Mac/PC/machine/device based on User-Agent.

## 2026-05-07 — Local web dashboard with analytics + control
**Want:** A local dashboard at `http://127.0.0.1:8787/dashboard` showing per-key analytics + control actions in one page. No auth, no SaaS.
**Why:** CLI-only management is fine for power users but doesn't sell the product. A browser dashboard is the moment users go "oh, I get it."
**Status: done** — Sidebar of keys, per-key main panel with live spend bar (HTMX 5s polling), 14-day daily-spend sparkline, by-model bar chart, hourly heatmap (7×24 grid), by-caller / by-metadata.user_id breakdown, suggestion card, control panel, alert log tail. Pure HTMX + vanilla CSS, HTMX vendored locally.

## 2026-05-07 — Currency-agnostic display
**Want:** Allow users to view caps and spend in their local currency (USD/GBP/EUR/JPY/CAD/AUD).
**Why:** Anthropic prices in USD but a UK solo dev thinks in GBP. Forcing pence-only made the schema feel hostile.
**Status: done** — Canonical storage is now USD cents (matching Anthropic's source of truth). `billing/formatting.py` provides `format_money()` and `from_major_units()` with a static FX table. Per-deployment `DISPLAY_CURRENCY` env var. JPY (no fractional unit) handled correctly.

## 2026-05-07 — Lift today's cap (multi-surface)
**Want:** Easy way to bump today's cap when the alerts fire, accessible from anywhere (terminal, dashboard, phone).
**Why:** "Cap hit, demo in 5 minutes, panicking" is a real scenario that shouldn't require restarting the proxy.
**Status: done** — Three surfaces: dashboard buttons (`💸 2× / 🚀 ceiling`), CLI (`tourniquet lift <key> --multiplier 2`), Telegram inline buttons on cap-hit notifications. Always honours `absolute_ceiling_usd_cents`. Lifts expire at midnight UTC by default; `--for-hours N` and `--to-time HH:MM` also supported.

## 2026-05-07 — Anomaly insights (local-only)
**Want:** Tell me what burned my tokens last week. Locally — never on a remote server.
**Why:** When you get hit by a runaway, the next question is "which agent did it?" — without a private breakdown, the user has no actionable signal.
**Status: done** — `analytics/insights.py:compute_insights()`. Five suggestion rules: caller dominance, hottest hour z-score, biggest single request, cap-hit rate trending, model-mix opus heavy. Statically asserted to import no network library (`tests/test_insights.py::test_no_network_imports`). CLI: `python scripts/insights.py <key>`.

## 2026-05-07 — Notification fanout (Mac/Win/Linux toast + Slack + Telegram + email + JSONL + webhook)
**Want:** Multiple alert channels, opt-in per channel, all silent if not configured.
**Why:** A single channel will always be wrong for some user — Slack-on-team, Telegram-when-mobile, JSONL-for-grep.
**Status: done** — `alerts/desktop.py` (osascript on Mac, plyer on Win/Linux), `alerts/slack.py`, `alerts/telegram.py` (with inline buttons for lift), `alerts/jsonl_log.py` (always-on), `alerts/webhook.py` (generic Zapier/n8n target), `alerts/email.py` (Resend). All run concurrently via `asyncio.gather` with per-channel error isolation.

## 2026-05-06 — Multiple Anthropic keys per user with shared management
**Want:** Solo devs typically have one or two `sk-ant-` keys; treat them as separate cap units.
**Why:** Workspace separation. Different agents = different blast radius.
**Status: done** — Dashboard sidebar lists all keys; each has independent cap, profile, kill_enabled, lift state, alert_email. CLI `tourniquet status` lists all.

---

# v0.2 roadmap (not yet built — priority order)

| # | Feature | Effort | Why |
|---|---|---|---|
| 1 | **`GET /v1/status`** endpoint returning `{spent, cap, pct, lift_active}` | ★ ~1h | Unblocks SwiftBar + MCP server + agent self-throttle from one shared API |
| 2 | **SwiftBar plugin** (`tourniquet.30s.sh`) | ★ ~1h | Menu-bar live spend widget — biggest desktop UX upgrade for ~zero native-app work |
| 3 | **`X-Tourniquet-Tag` header** capture for per-agent cost slicing | ★ ~30m | Unique vs all competitors. Solo devs running multiple agents see exactly which one ate the budget |
| 4 | **MCP server** exposing `tourniquet.budget_status()` and `tourniquet.recommend_model_for_budget()` | ★★ ~3h | Native Claude Code integration; agents query before doing expensive ops, can self-downgrade Opus → Haiku |
| 5 | **Pre-flight token guard** (warn/reject if request body > N tokens) | ★ ~30m | Catches "accidentally pasted whole codebase" pattern that no streaming protection can stop |
| 6 | **Stripe-Radar-style "this rule would have caught"** preview on suggestions | ★★ ~1.5h | Shows concrete impact of accepting a suggestion vs averages |
| 7 | **Anthropic Admin API nightly reconciliation** | ★★ ~2h | Self-correcting ledger: surfaces drift between Tourniquet's estimate and Anthropic's billed total |
| 8 | **Token-count limit** alongside USD limit | ★ ~1h | USD caps drift when Anthropic changes prices; token caps are stable |
| 9 | **Tiered alerts at 70% / 90%** before 100% | ★ ~30m | Closes "it just blocked with no warning" complaint pattern |
| 10 | **Name-edit field in control panel** | ★ ~5m | Currently delete + recreate is the only rename path |
| 11 | **Custom domain in `/admin/lift` for off-network access** | ★★★ ~3h | Lift cap from your phone via your own tunnel/Tailscale; document the threat model |
| 12 | **Comparison table** in README (Tourniquet vs LiteLLM vs Helicone) | ★ ~30m | Strongest hook in the research report — make it visible |

## Distribution & launch (separate from product code)

| Step | Status |
|---|---|
| MIT LICENSE in repo root | ✅ done |
| `tourniquet-dev` reserved on PyPI | ⏳ user action — `python -m twine upload dist/*` |
| Homebrew tap (`LowryDaniel/homebrew-tourniquet`) | ⏳ user action after PyPI |
| Scoop bucket (`LowryDaniel/scoop-tourniquet`) | ⏳ user action after PyPI |
| Cloudflare Pages deploy of `landing/` | ✅ live at `tourniquet.pages.dev` |
| DNS records for `tourniquet.dev` apex + www | ⏳ user action in Cloudflare dash |
| GitHub repo public | ⏳ deferred |
| Show HN / Reddit launch | ⏳ deferred |
