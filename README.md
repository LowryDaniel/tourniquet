# BurnRate

**Stop the next £47K token burn before it costs you the rent.**

BurnRate is a free, drop-in Anthropic API proxy that enforces a hard daily £ spend cap per API key. When the cap is hit, in-flight streams are killed cleanly and subsequent requests return `402` until midnight UTC.

## Why

- The 1.67-billion-token Claude Code incident. The £47K LangChain loop. Every developer running unattended agents is one bad weekend away from a bill they can't pay.
- Anthropic has no native per-key spending caps.
- BurnRate is a transparent proxy — zero code changes beyond two environment variables.

## Drop-in for Claude Code

```bash
export ANTHROPIC_BASE_URL=https://burnrate.ai
export ANTHROPIC_API_KEY=br_xxxxxxxxxxxx   # your BurnRate token, not your Anthropic key
```

That's it. Claude Code routes through BurnRate. Your Anthropic key never leaves your account.

## Drop-in for any Anthropic SDK

```python
import anthropic

client = anthropic.Anthropic(
    base_url="https://burnrate.ai",
    api_key="br_xxxxxxxxxxxx",
)
```

## Features (v1)

- **Hard kill switch** — when today's £ spend > cap, terminate in-flight stream mid-token + 402 subsequent requests until midnight UTC
- **Multiple API keys per account** — register N Anthropic keys, name them ("prod", "dev", "claude-code-experiments"), each with its own cap and profile
- **Three pre-built profiles** — Hobby / Production / Demo — preset alert thresholds and kill behaviour
- **Email alerts at 80%** — one per day per key via Resend, idempotent
- **Dashboard** — today's spend per key, cap config, profile picker, last 50 usage events
- **Magic-link auth** — no passwords

## Architecture overview

See [docs/architecture.md](docs/architecture.md).

## Self-hosting

See [docs/deployment.md](docs/deployment.md). Runs on Fly.io for ~£10/month. Free tier covers the first N users.

## Development

See [docs/development.md](docs/development.md).

## Security

See [SECURITY.md](SECURITY.md). API keys are encrypted at rest with Fernet. BurnRate tokens are hashed with bcrypt. We never log raw keys.

## Roadmap

| Version | Target | Scope |
|---|---|---|
| v1 | Week 1 | Anthropic proxy + multi-key dashboard + profiles + kill switch |
| v2 | On user request | OpenAI support (~6h with provider interface already in place) |
| v3 | 500+ users | Stripe % billing (2.5% of pass-through spend, capped at £29/mo) |

## Status

Pre-launch. Building week of 2026-05-05.
