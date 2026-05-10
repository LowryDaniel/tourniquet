# Tourniquet

**A local Anthropic API proxy with a hard daily spend cap.**

You left an agent running overnight. You woke up to a bill. Tourniquet makes that impossible.

---

## Why this exists

Claude Code, custom agent scripts, LangChain chains — unattended agents are great until they aren't. A single runaway tool-call chain or a prompt that spawns sub-agents that spawn sub-agents can hit $50 before you check your phone. The Anthropic Console has spend alerts, but alerts are not caps: the tokens keep flowing after the email lands in your inbox.

Existing proxies (LiteLLM, Helicone) are built for teams with dashboards, API keys, billing admins, and budgets-per-project. They're overkill if you're one person who just wants to not get burned. Helicone is SaaS-first. LiteLLM's budget enforcement cuts the TCP connection mid-stream, which crashes your agent instead of letting it finish gracefully.

Tourniquet is for the solo dev who runs Claude all day and needs exactly one thing: a hard ceiling, locally enforced, with no third-party seeing your prompts or your key.

---

## What it does

**Caps**
- Hard daily cap in any currency (USD, GBP, EUR, JPY, CAD, AUD)
- Mid-stream kill via synthetic SSE `message_stop` — the agent loop sees a clean stop, not a crash (more below)
- Lift cap for today via dashboard, CLI, or Telegram — multiply cap by N, or raise to ceiling
- Auto-tune: suggests a sensible starting cap from your Anthropic usage history (admin-key fetch) or the last 7 days of your `usage_events`

**Alerts**
- Desktop notifications (macOS/Linux/Windows)
- Slack, Telegram (with inline lift + recovery buttons), email
- JSONL log for scripting
- One-click "killed, want to bump cap and continue?" recovery flow on every channel
- See [docs/alerts-setup.md](docs/alerts-setup.md) for per-channel walkthroughs

**Insights**
- Per-key daily sparkline, model breakdown, hourly heatmap
- By-caller and by `metadata.user_id` breakdown — see which agent is spending
- Per-key **action history** — every kill/lift/bump tagged with the source channel (Slack tap, Telegram tap, web, CLI). Proof of every action even when the resulting cap value didn't visibly change.
- All stored in SQLite on your machine

**Privacy**
- 100% local: SQLite, no telemetry, no Tourniquet account, no cloud dependency
- Anthropic key encrypted at rest; `tq_` proxy tokens hashed with bcrypt
- Dashboard at `http://127.0.0.1:8787/dashboard` — vanilla HTMX, no SPA, works offline

---

## Quick install

> **Note:** Tourniquet is not yet on PyPI. Use the `git clone` path below. `pip install tourniquet-dev` will work once the first release ships.

**macOS / Linux**

```bash
git clone https://github.com/LowryDaniel/tourniquet.git
cd tourniquet
pip install -e .
tourniquet
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/LowryDaniel/tourniquet.git
cd tourniquet
pip install -e .
tourniquet
```

See [docs/install.md](docs/install.md) for virtual-env setup, `pipx`, and Windows `cmd.exe` instructions. For 24/7 enforcement (Docker, Proxmox LXC, Raspberry Pi, cloud VM), see [docs/deploy.md](docs/deploy.md).

---

## First-run flow

1. Run `tourniquet` — the dashboard opens at `http://127.0.0.1:8787/dashboard`
2. Paste your `sk-ant-…` key and set a daily cap
3. Point your agent at `http://127.0.0.1:8787` with your `tq_…` proxy token

**Drop-in for Claude Code:**

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_API_KEY=tq_xxxxxxxxxxxx
```

**Drop-in for the Anthropic SDK:**

```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://127.0.0.1:8787",
    api_key="tq_xxxxxxxxxxxx",
)
```

---

## The kill mechanism

Most proxies enforce caps by dropping the TCP connection when the budget is exceeded. Your agent sees a network error, throws an exception, and either crashes or retries — which may spend more.

Tourniquet injects two synthetic SSE blocks into the in-flight stream:

```
event: message_stop
data: {"type":"message_stop","stop_reason":"end_turn"}

event: error
data: {"type":"error","error":{"type":"tourniquet_cap_hit","message":"Daily spend cap reached. Resets at midnight UTC.","cap_usd_cents":1000,"spent_usd_cents":1003,"resets_at":"2026-05-08T00:00:00Z"}}
```

The Anthropic SDK treats the first block as a normal `message_stop` with the
documented `end_turn` stop-reason — so strict Pydantic / Zod validators accept
it and your agent loop finishes cleanly. The second `event: error` block
carries the cap-hit metadata for clients that want to distinguish a cap hit
from a natural end-of-turn. Non-streaming or non-SSE-aware clients can also
look for the `X-Tourniquet-Cap-Hit: 1` HTTP response header.

Subsequent requests in the same day return `402 Payment Required` with the
same metadata. The cap resets at midnight UTC.

No other proxy does this. They cut the wire. Tourniquet closes the valve.

---

## Privacy & local-first guarantees

Your prompts, completions, and Anthropic key never leave your machine. Tourniquet proxies traffic through `localhost` — `api.anthropic.com` sees requests from your IP, exactly as if you called it directly.

Short version:
- No Tourniquet account required
- No analytics sent anywhere
- SQLite database is yours; delete it to wipe all history
- Key encrypted at rest with Fernet; proxy tokens hashed with bcrypt

Full details: [docs/data-residency.md](docs/data-residency.md) and the [`/trust`](http://127.0.0.1:8787/trust) page in the running dashboard.

Security model (deployment trust assumptions, threat boundaries, what's guarded vs. what isn't): [docs/security-model.md](docs/security-model.md).

---

## Configuration

Copy `.env.example` to `.env` and edit. The most important variables:

```
TOURNIQUET_CAP_USD=10.00
TOURNIQUET_CURRENCY=USD
TOURNIQUET_ALERT_PCT=80
```

CLI quick-reference:

```bash
tourniquet status          # current spend, cap, time to reset
tourniquet lift --x 2      # double today's cap
tourniquet lift --ceil      # remove cap for today
tourniquet keys            # list registered Anthropic keys
```

See `.env.example` for the full list of alert and integration variables.

---

## Architecture

```
  your agent / Claude Code / SDK
           │
           │  http://127.0.0.1:8787
           ▼
  ┌─────────────────────┐
  │    Tourniquet        │
  │  ┌───────────────┐  │     ┌──────────────┐
  │  │  Spend ledger │◄─┼────►│  SQLite DB   │
  │  └───────────────┘  │     └──────────────┘
  │  ┌───────────────┐  │     ┌──────────────┐
  │  │  SSE injector │  │     │  Dashboard   │
  │  └───────────────┘  │     │  :8787       │
  │  ┌───────────────┐  │     └──────────────┘
  │  │  Alert router │─►│─────► Slack/Telegram/
  │  └───────────────┘  │       Desktop/Email
  └──────────┬──────────┘
             │  proxied requests
             ▼
      api.anthropic.com
```

---

## Roadmap

- **v0.2** — macOS menu-bar app (tray icon shows today's spend)
- **v0.2** — MCP server so Claude Code can query its own remaining budget mid-run
- **v0.2** — Pre-built binaries (no Python required): `brew install tourniquet`, MSI, AppImage
- **v0.2** — Per-model sub-caps (e.g. cap Opus-4 at $5/day, Sonnet at $15/day)
- **v0.3** — OpenAI proxy (provider interface already stubbed)
- **v0.3** — Team mode: shared SQLite over LAN, per-user `tq_` tokens
- **Future** — Grafana datasource plugin for spend dashboards

---

## Contributing

Pull requests welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first — it covers the dev-server setup, the SQLite migration pattern, and the SSE injection test harness. Open an issue before starting large changes so we can agree on the approach.

---

## License

MIT — see [LICENSE](LICENSE).
