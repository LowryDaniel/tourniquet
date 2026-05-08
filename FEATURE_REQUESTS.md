# Feature requests

Newest at top. **Status: done** entries link to the relevant module/commit when known.

## 2026-05-08 — Per-key action history (audit log) on the dashboard
**Want:** A history view of every cap-changing action per key — kill, lift, bump, manual cap-set — with the time, source channel (Slack/Telegram/web/CLI), and a one-line summary. So when the operator fires an action and the key was already at minimum (or any other no-visible-change case), they can still confirm the tap landed.
**Why:** When Dan tapped "kill" in Slack against a key whose `daily_cap_usd_cents` was already at minimum (1¢), the row in `api_keys` didn't visibly change — leaving him with no way to verify the action ran. Same for a Telegram "ignore" tap (intentional no-op): no DB diff means no proof. The audit log is the proof, and a useful timeline regardless.
**Status: done** —
- New `ApiKeyAction` model (`models.py`) — table `api_key_actions` with `action`, `source`, `summary`, optional structured `details` JSON. Auto-created on next startup via `Base.metadata.create_all` (Postgres alembic migration is a v0.2 follow-up).
- New `audit.py` with `record_action()` helper — adds an audit row inside the caller's session so it commits atomically with the cap mutation it describes.
- `_apply_kill_now`, `_apply_lift_by_amount`, and the new `_apply_lift` (promoted from `telegram_callbacks`) all take a `source` parameter and write audit rows. Slack tags `slack_socket`; Telegram tags `telegram_poll`; web routes default to `web`.
- New dashboard route `GET /dashboard/key/{key_id}/history` + partial `_partials/action_history.html`. HTMX-polled every 10s.
- Renders under each key panel below the alert log. Shows time, action icon, source, and human-readable summary. Empty state copy: "No actions recorded yet — kill, lift, or bump this key from any channel and the entry will appear here."
- 163 tests pass (3 slack_socket tests updated to patch the new admin import paths).

## 2026-05-07 — In-app one-tap actions for Telegram (and partial Slack scaffold)

## 2026-05-07 — In-app one-tap actions for Telegram (and partial Slack scaffold)
**Want:** Tapping a button inside Telegram or Slack should apply the action (kill, lift, +$N bump) **without** opening the user's browser. Local-first — no public callback URL required.
**Why:** The browser-confirm flow has too many hops: tap → "Open Link?" dialog → browser → confirm page → confirm. Users expect mobile-app actions to be one tap. Critical for the "agent's gone feral, kill it from my phone in 2 seconds" use case Tourniquet was built for.
**Status: done (Telegram), scaffolded (Slack)** —
- **Telegram in-app one-tap is fully working.** New `alerts/telegram_poller.py` runs as a background asyncio task in the FastAPI lifespan. Long-polls `getUpdates` (timeout 25s, allowed_updates=["callback_query"]). On startup, calls `getUpdates?offset=-1&timeout=0` to drain the backlog and only process taps from after Tourniquet started — no replays of stale buttons. Backoff on errors (2s → 60s exponential). Each `callback_query` is acked via `answerCallbackQuery`, then the original Telegram message is rewritten via `editMessageText` to "✓ Bumped $X. <key> cap is now $Y until midnight UTC" with buttons removed (so the action can't be re-tapped).
- Telegram buttons reverted from URL back to `callback_data` since the polling client now handles them.
- Live verified: tapped +$1 in Telegram on phone, lifted_cap_usd_cents jumped 604 → 704 in DB, message rewrote in-place.
- **Slack scaffold built but not user-complete.** `alerts/slack_socket.py` opens a WebSocket via `apps.connections.open` (with certifi-backed SSL context to handle Python.org-on-macOS missing CAs) and dispatches `interactive` payloads to the same handlers Telegram uses. Block Kit `_build_action_payload` helper added. **Blocked at v0.1 boundary because Block Kit `actions` blocks aren't accepted by incoming webhooks** — they require `chat.postMessage` with a bot user token. Adding bot-token send path is straightforward (~30 min) but requires the user to do four extra Slack-side actions: add `chat:write` scope, install/reinstall app, invite bot to channel, copy channel ID. Logged as v0.2 follow-up.
- New settings: `slack_app_token`, `slack_bot_token`, `slack_channel_id`. Socket Mode stays dormant unless all three are present (otherwise we'd idle a useless WebSocket). Webhook + mrkdwn-link fallback covers the "user has webhook only" path.
- 11 new tests in `tests/test_telegram_poller.py` and `tests/test_slack_socket.py`. Full suite: 161 pass.
- Two bugs surfaced and logged in `ERRORS.md`: Slack incoming webhook 400 on `actions` blocks; Python.org Python's missing CA store breaking `websockets.connect`.
- Setup guide updated with the "optional Slack Socket Mode" section that walks through the four Slack-side steps for users who want to upgrade later.

## 2026-05-07 — Channel setup guide + consistent alert text + recovery flow
**Want:** (a) one canonical alert message across every delivery method (Telegram, Slack, email, desktop, webhook, JSONL — same text, character-for-character); (b) after a kill (manual via 🛑 button or auto at cap-hit), a fresh "killed at $X — want to bump and continue?" notification with `+$1 / +$5 / +$10` recovery buttons in every channel; (c) a setup walkthrough for non-developers covering Slack + Telegram + Mac desktop banners.
**Why:** Inconsistent prose across channels eroded trust ("did the same thing fire twice?"). Once a kill happens the user almost always wants a graceful "let me finish this task" path — not a hard reset. And the channels that need setup (Slack/Telegram) trip up anyone who hasn't done bot/webhook plumbing before.
**Status: done** —
- New `_format_message` template — fixed shape `{icon} Tourniquet: {name} — {state}. {spent}/{cap} today.` for all three states (50/80%, cap-hit, recovery). Action verbs live in BUTTONS, not prose. Locked in by `tests/test_notifier.py::test_format_message_text_is_consistent_regardless_of_kill_url`.
- `AlertEvent.recovery_offer: bool` flag. When set, channel renderers swap to bump-button mode: Telegram URL buttons (`+$1`, `+$5`, `+$10`), Slack URL buttons, email HTML link buttons, desktop banner with single dashboard appendix, webhook payload includes `recovery_options` array.
- New endpoints `/admin/lift-by-amount/{id}` and `/admin/lift-mode/{id}` — itsdangerous-signed magic links (24h expiry, salts `lift-by-amount` and `lift-mode`). GET shows confirm page; POST applies to `lifted_cap_usd_cents` (auto-expires midnight UTC, clamped to absolute_ceiling).
- All Telegram inline buttons switched from `callback_data` → `url`, removing the webhook-registration requirement. Trade-off: tap opens browser instead of one-tap callback. Same UX shape as Slack/email.
- Email body simplified — drop the duplicating prologue paragraph; subject line derived from canonical message (emoji-stripped) so inbox preview matches Slack/Telegram.
- Desktop banner appends one consistent line `→ {dashboard_url}` for every alert, regardless of state.
- Kill-now POST success page now shows inline `+$N` recovery buttons + fires a fan_out recovery alert (best-effort, non-blocking).
- Telegram-initiated kill (via callback) also fires the recovery alert.
- New `tourniquet test-alerts --recovery` flag for end-to-end manual testing.
- New guide [docs/alerts-setup.md](docs/alerts-setup.md) with full Slack + Telegram walkthroughs (including the "message your bot first" gotcha and `getUpdates` fallback for chat ID), Mac notification permission gate, payload format for webhook automation, and the deferred-to-v0.2 explanation for WhatsApp + off-network access.
- 150 tests pass.

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
| 6 | **WhatsApp via Twilio** alert channel | ★★ ~2h | UK / global mobile-first audience; Twilio is the only sane production path. Plain text only (Meta gates inline buttons behind template approval). Add `WHATSAPP_TWILIO_SID/TOKEN/FROM/TO` env vars + `alerts/whatsapp.py` matching the existing channel interface. |
| 7 | **In-product alert-channel setup wizard** | ★★★ ~6h | Biggest low-code lift. Browser UI walks through Slack / Telegram / WhatsApp with screenshots + embedded test-fire button. Critical for non-developer audience |
| 8 | **Web-Push notifications via service worker** | ★★ ~2h | Most accessible single channel — no third-party account, browser sends the alert. Closes the gap for users who own no Slack workspace and no domain |
| 9 | **Stripe-Radar-style "this rule would have caught"** preview on suggestions | ★★ ~1.5h | Shows concrete impact of accepting a suggestion vs averages |
| 10 | **Anthropic Admin API nightly reconciliation** | ★★ ~2h | Self-correcting ledger: surfaces drift between Tourniquet's estimate and Anthropic's billed total |
| 11 | **Token-count limit** alongside USD limit | ★ ~1h | USD caps drift when Anthropic changes prices; token caps are stable |
| 12 | **Tiered alerts at 70% / 90%** before 100% | ★ ~30m | Closes "it just blocked with no warning" complaint pattern |
| 13 | **Name-edit field in control panel** | ★ ~5m | Currently delete + recreate is the only rename path |
| 14 | **`tourniquet start --tunnel`** built-in Cloudflare/Tailscale tunnel for phone-side recovery | ★★ ~2h | Lift / kill / bump from phone without a separate tunnel setup; document the threat model |
| 15 | **Comparison table** in README (Tourniquet vs LiteLLM vs Helicone) | ★ ~30m | Strongest hook in the research report — make it visible |

### De-prioritised for v0.1 (per 2026-05-07 review)

The following channels work and ship in v0.1 but are not pushed in onboarding — too much setup friction for the target solo-dev audience:

- **Email (Resend)** — requires a domain you own + DNS records. Most users don't.
- **Generic webhook** — works as a recovery path for users who already use Zapier/n8n; not worth onboarding focus.
- **Off-network access (phone-side recovery)** — superseded by item #14 above.

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
