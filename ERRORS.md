# Errors & fixes

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
