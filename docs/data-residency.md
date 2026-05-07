# Data Residency

Tourniquet is designed as a local-first proxy. This document explains exactly what data stays on your machine, what doesn't, and who can see what.

## What Tourniquet stores (locally)

| Data | Where | Notes |
|------|-------|-------|
| Usage tokens & cost | SQLite / Postgres | input_tokens, output_tokens, cost_usd_cents per request |
| Timestamps | SQLite / Postgres | created_at per request, UTC |
| Model name | SQLite / Postgres | e.g. `claude-opus-4-7` |
| User-agent | SQLite / Postgres | HTTP User-Agent header from your client |
| metadata.user_id | SQLite / Postgres | Value of `X-User-ID` / Anthropic metadata field, if sent |
| cap_hit flag | SQLite / Postgres | Boolean — whether this request triggered your daily cap |
| Encrypted Anthropic key | SQLite / Postgres | AES-256 (Fernet); decrypted only in memory for forwarding |
| bcrypt'd tq_ tokens | SQLite / Postgres | One-way hash; plaintext never stored |
| Daily spend totals | SQLite / Postgres | Denormalised cap check table |

## What Tourniquet does NOT store

- **Prompt content** — the request body is forwarded directly; Tourniquet never persists message text
- **Response content** — the Anthropic response is streamed to your client; no content is written to disk
- **IP addresses** — client IPs are not logged or stored
- **Authentication credentials** — only bcrypt hashes of your tq_ tokens; the Anthropic key is stored encrypted and the plaintext is `del`'d from memory after forwarding

## Outbound network traffic

Tourniquet makes **exactly two categories** of outbound connection:

1. **Anthropic API** (`api.anthropic.com`) — your requests, proxied. Anthropic sees the same traffic it would if you called them directly.
2. **Your configured alert channels** — only if you have set `SLACK_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`, `RESEND_API_KEY`, or `ALERT_WEBHOOK_URL`. Alerts contain spend totals and key names only — no prompt content.

No telemetry, analytics, or usage data is ever sent to Tourniquet's servers or any third party.

## Admin key handling

If you use the one-shot history bootstrap (`scripts/bootstrap_local.py`), your Anthropic admin key is:

- Accepted as a CLI argument or environment variable
- Held in process memory only for the duration of the API calls
- `del`'d immediately after use
- **Never written to disk, the database, or any log**

## Threat model

| Threat | Mitigation |
|--------|-----------|
| Prompt leakage via Tourniquet | Tourniquet never reads or stores message bodies — it streams them opaquely |
| Anthropic key theft via DB dump | Key is AES-256 encrypted; attacker needs both the DB and `FERNET_KEY` |
| tq_ token forgery | Tokens are bcrypt'd (cost 12); timing-safe comparison in auth |
| Outbound data exfiltration | All outbound is explicit and user-configured; no third-party telemetry |
| Insights report leakage | `compute_insights` is a pure local function; no import of any network library |

## Local analytics guarantee

The `tourniquet.analytics.insights` module is statically verified to import no network-capable libraries (`httpx`, `requests`, `urllib`, `socket`, `aiohttp`). This is asserted in the test suite (`tests/test_insights.py::test_no_network_imports`).
