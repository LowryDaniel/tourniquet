# Architecture

## System overview

```
Client (Claude Code / SDK)
       │ ANTHROPIC_BASE_URL=https://tourniquet.ai
       │ ANTHROPIC_API_KEY=tq_xxxxxxxxxxxx
       ▼
┌─────────────────────────────┐
│         Tourniquet            │
│  FastAPI + httpx + Jinja2   │
│                             │
│  ① Auth: verify tq_* token  │
│  ② Cap check: caps_today    │
│  ③ Proxy: stream to Anth.   │
│  ④ Count: tokens in flight  │
│  ⑤ Kill: if cap crossed     │
│  ⑥ Persist: usage_events    │
└────────────┬────────────────┘
             │ x-api-key: sk-ant-...  (decrypted on hot path)
             ▼
      api.anthropic.com
```

Tourniquet is a **transparent pass-through proxy**. It never normalises Anthropic's SSE event format — events go straight to the client. The only mutations are:
1. Replacing the `x-api-key` header with the user's stored Anthropic key
2. Injecting a synthetic `message_stop` event (with `stop_reason: "tourniquet_cap_hit"`) when the cap is crossed mid-stream

## Provider directory pattern

```
src/tourniquet/providers/
    anthropic.py   ← v1 only
    openai.py      ← slot in for v2 (~6h) — different endpoint, SSE format, auth header
    gemini.py      ← slot in for v3
```

Each provider implements a thin interface (`stream_request`, `count_tokens`, `cost_pence`). The proxy router picks the provider from the stored api_key config. Adding a new provider requires no router changes.

## Streaming kill mechanic

When cumulative spend crosses the daily cap mid-stream:

1. Tourniquet stops forwarding bytes from the Anthropic connection
2. Sends a synthetic `data: {"type":"message_stop","stop_reason":"tourniquet_cap_hit"}\n\n` SSE event
3. Closes the client connection cleanly
4. Records `cap_hit=true` on the usage event
5. All subsequent requests return `402 Payment Required` with body `{"error": "tourniquet_cap_hit", "resets_at": "<midnight-UTC-iso>"}`

The client sees a clean termination, not a half-token corruption. Claude Code handles `message_stop` gracefully.

## Token counting (Anthropic)

No tiktoken. No separate counting call. Read from the stream:

- `message_start` event → `usage.input_tokens` (fixed at request time)
- `message_delta` events → accumulate `usage.output_tokens` (incremental)

Final cost = `(input_tokens × input_rate_pence) + (output_tokens × output_rate_pence)`

Rates live in `src/tourniquet/billing/pricing.py` — update when Anthropic publishes price changes.

## Database schema

```sql
-- Users — one row per email address
users (
    id              UUID PK DEFAULT gen_random_uuid(),
    email           TEXT UNIQUE NOT NULL,
    magic_link_token TEXT,          -- one-time, expires 15min, NULL after use
    created_at      TIMESTAMPTZ DEFAULT now(),
    stripe_customer_id TEXT         -- NULL until billing enabled (v3)
)

-- API keys — N per user
api_keys (
    id                      UUID PK DEFAULT gen_random_uuid(),
    user_id                 UUID REFERENCES users(id) ON DELETE CASCADE,
    name                    TEXT NOT NULL,              -- "prod", "dev", etc.
    tq_token_hash           TEXT NOT NULL,              -- bcrypt hash of tq_* token
    anthropic_key_encrypted TEXT NOT NULL,              -- Fernet(sk-ant-...)
    profile                 TEXT NOT NULL DEFAULT 'hobby', -- hobby|production|demo
    daily_cap_pence         INTEGER NOT NULL DEFAULT 500,  -- 500 = £5
    kill_enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    alert_email             TEXT,                       -- NULL = account email
    created_at              TIMESTAMPTZ DEFAULT now()
)

-- Usage events — one row per request (or per cap-kill event)
usage_events (
    id              UUID PK DEFAULT gen_random_uuid(),
    api_key_id      UUID REFERENCES api_keys(id) ON DELETE CASCADE,
    request_id      TEXT,                   -- Anthropic request ID from response header
    model           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_pence      INTEGER NOT NULL DEFAULT 0,  -- always pence, never pounds
    cap_hit         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT now()
)

-- Triggers — scaffolded W1, anomaly rule turns on W4
triggers (
    id              UUID PK DEFAULT gen_random_uuid(),
    api_key_id      UUID REFERENCES api_keys(id) ON DELETE CASCADE,
    condition_json  JSONB NOT NULL,         -- {"type": "spend_3x_baseline"} etc.
    actions_json    JSONB NOT NULL,         -- {"alert": true, "kill": false} etc.
    enabled         BOOLEAN NOT NULL DEFAULT FALSE,  -- off until W4
    last_fired_at   TIMESTAMPTZ
)

-- Denormalised cap tracker — one row per key per day, fast cap-check on hot path
caps_today (
    api_key_id  UUID REFERENCES api_keys(id) ON DELETE CASCADE,
    date        DATE NOT NULL,
    total_pence INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (api_key_id, date)
)
```

## Cost invariants

- **All costs stored in pence (integer)** — never pounds, never dollars
- Pence integer avoids float-rounding errors across millions of events
- Enables future % billing (`total_pence * 0.025`) with no migration
- `stripe_customer_id NULL` from day one → no schema change when billing arrives

## Profiles (v1)

Pre-built profiles stored as named constants in `src/tourniquet/billing/profiles.py`. Profiles set:
- `alert_thresholds`: list of percentages where email alert fires (e.g. [80])
- `kill_at_pct`: percentage of cap where kill triggers (e.g. 100, 200, or None)
- `kill_silently`: if False, send pause-and-ask instead of hard kill

| Profile | Alert thresholds | Kill at | Notes |
|---|---|---|---|
| Hobby | [80] | 200% | Safety net, not a hard wall |
| Production | [50, 80, 100] | 100% | Hard kill; kill defaults OFF — must opt in |
| Demo day | [80] | — | Never kill silently; pause-and-ask at 100% |

## Request flow (sequence)

```
1. Client sends POST /v1/messages with Authorization: Bearer tq_xxxxxxxxxxxx
2. Auth middleware: hash token → lookup api_keys.tq_token_hash
3. Load api_key row (includes profile, daily_cap_pence, kill_enabled)
4. Check caps_today for today's total_pence; if >= cap and kill_enabled → 402 immediately
5. Decrypt anthropic_key_encrypted with FERNET_KEY → sk-ant-...
6. Open httpx streaming request to api.anthropic.com/v1/messages
7. Stream SSE events to client, accumulating token counts
8. On message_stop OR cap crossed: finalise cost_pence, write usage_events, update caps_today
9. If cap crossed mid-stream: inject synthetic message_stop, close connection
10. If alert threshold crossed: enqueue email (idempotent — check if already sent today)
```

## Two-app Fly.io deployment

| App | Role |
|---|---|
| `tourniquet-web` | FastAPI: proxy + dashboard + magic-link auth |
| `tourniquet-worker` | Celery/APScheduler: midnight cap reset cron, alert email queue, (W4) anomaly evaluator |

Both apps share the same Fly Postgres cluster.
