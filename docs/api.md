# API Reference

BurnRate exposes two surfaces:
1. **Proxy API** — Anthropic-compatible, used by Claude Code and Anthropic SDKs
2. **Dashboard API** — internal HTMX/Jinja2 endpoints; not a public REST API

---

## Proxy API

Base URL: `https://burnrate.ai`

Authentication: `Authorization: Bearer br_xxxxxxxxxxxx` (BurnRate token)

### POST /v1/messages

Transparent pass-through to `api.anthropic.com/v1/messages`. Accepts and returns identical request/response shapes.

**Request headers forwarded:**
- `content-type`
- `anthropic-version` (default: `2023-06-01` if not provided)
- `anthropic-beta` (if present)

**Headers injected by BurnRate:**
- `x-api-key: sk-ant-...` (decrypted from user's stored key)
- `authorization` header is stripped before forwarding

**Non-streaming response:**
```json
{
  "id": "msg_01...",
  "type": "message",
  "role": "assistant",
  "content": [{"type": "text", "text": "..."}],
  "model": "claude-opus-4-7",
  "stop_reason": "end_turn",
  "usage": {"input_tokens": 100, "output_tokens": 50}
}
```

**Streaming response (SSE):**
Standard Anthropic SSE event sequence. BurnRate does not reformat events.

**Cap-hit response (mid-stream):**
```
data: {"type":"message_stop","stop_reason":"burnrate_cap_hit"}

```
Connection closes after this event.

**Cap-hit response (pre-flight — request arrives after cap hit):**
```
HTTP/1.1 402 Payment Required
Content-Type: application/json

{
  "error": {
    "type": "burnrate_cap_hit",
    "message": "Daily spend cap reached. Resets at midnight UTC.",
    "resets_at": "2026-05-06T00:00:00Z",
    "cap_pence": 500,
    "spent_pence": 512
  }
}
```

### GET /health

Returns 200 OK if the service is up. No auth required.

```json
{"status": "ok", "version": "0.1.0"}
```

---

## Error codes

| HTTP | `error.type` | Meaning |
|---|---|---|
| 401 | `invalid_token` | `br_*` token not found or malformed |
| 402 | `burnrate_cap_hit` | Daily cap reached; try again after midnight UTC |
| 429 | `rate_limited` | Too many requests (forwarded from Anthropic or BurnRate) |
| 502 | `upstream_error` | Anthropic returned an error; body forwarded as-is |
| 504 | `upstream_timeout` | Anthropic connection timed out (30s) |

---

## Dashboard routes (HTMX, not a public API)

These routes are HTML/HTMX — not for programmatic use.

| Route | Description |
|---|---|
| `GET /` | Landing page |
| `GET /login` | Magic-link request form |
| `POST /auth/magic-link` | Send magic link email |
| `GET /auth/verify?token=...` | Verify magic link, set session cookie |
| `GET /dashboard` | Main dashboard (requires session) |
| `GET /dashboard/keys` | API key list |
| `POST /dashboard/keys` | Register a new Anthropic key |
| `DELETE /dashboard/keys/{id}` | Delete a key |
| `PATCH /dashboard/keys/{id}` | Update cap / profile / kill toggle |
| `GET /dashboard/keys/{id}/usage` | Last 50 usage events for a key |
| `GET /dashboard/keys/{id}/token` | Show br_* token (once only) |
