# Security model

Tourniquet's threat model is shaped by its primary deployment posture: a
single-user, localhost-bound proxy with an Anthropic API key inside it. That
posture defines a relatively narrow attack surface — but the same code can be
deployed in three quite different topologies, each with its own trust
assumptions. This document spells out what Tourniquet defends against, what it
explicitly does not, and what the operator has to do at the edge.

## 1. Deployment scenarios with trust assumptions

**Localhost-only (default — single-user developer machine).** This is the
posture the README and `docs/install.md` describe. The proxy binds to
`127.0.0.1:8787`, no external network exposure, one human at the keyboard.
The threat model collapses to **local-process attackers** (anything else on
the same machine that can read your home directory or open a TCP connection
to loopback) and **accidental misconfiguration** (committing `.env`,
forgetting to lock down a port). Tourniquet does not — and cannot — protect
the database file, encryption key, or proxy tokens from a process running
under the same UID.

**Tailscale Funnel / cloud VM.** `docs/deploy.md` describes Proxmox LXC,
Raspberry Pi, and cloud-VM deployments. As soon as the listener is reachable
from anywhere except loopback, **cross-origin browser contexts** become
relevant: a malicious page in another tab can shape requests against the
admin or proxy surface. Public-internet-reachable surfaces also draw
unauthenticated probing, body-size DoS attempts, and TLS downgrade pressure.
**TLS termination at the edge (Caddy, nginx, Tailscale Funnel TLS) is
REQUIRED** in this scenario — Tourniquet itself does not terminate TLS, and
bare HTTP exposes both `tq_*` proxy tokens and any Anthropic responses to
network observers.

**Multi-user shared LAN.** Multiple humans share a network segment (an office
subnet, a co-living homelab). LAN devices are **semi-trusted at best**: any
machine on the same broadcast domain can attempt to hit the listener once
`--host 0.0.0.0` is set, and ARP/DNS spoofing is in scope. Tourniquet has no
multi-user authorisation model in v0.1 — the v0.2 roadmap calls out per-user
`tq_*` tokens. Until then, treat shared-LAN deployment as "all LAN devices
share the same blast radius as the operator".

## 2. What Tourniquet guards against

- **Token authentication on the proxy.** `Authorization: Bearer tq_*` tokens
  are looked up via SHA-256 against a unique-indexed column. See
  `_resolve_api_key` and `_legacy_bcrypt_scan` in
  `src/tourniquet/proxy/router.py:114` and `:89`. Tokens minted before
  migration `0003` fall through a one-shot bcrypt scan and are upgraded
  in-place.
- **Atomic cap enforcement under concurrency.** `reserve_or_reject` in
  `src/tourniquet/billing/caps.py:49` performs the cap check and the
  reservation in a single `INSERT ... ON CONFLICT DO UPDATE WHERE`
  statement. Concurrent requests for the same key cannot all squeeze past a
  stale `spent_cents` read.
- **Stored XSS in admin pages.** Every admin HTML response is rendered
  through Jinja templates with autoescape on (`src/tourniquet/templates/admin/`).
  A key named `<script>...</script>` renders as escaped text in kill-now and
  lift confirmation pages — including links arriving from email, Slack, or
  Telegram.
- **Body-size DoS.** `max_request_body_bytes` (default 10 MiB) lives in
  `src/tourniquet/config.py:71` and is enforced chunk-by-chunk in
  `proxy_messages` (`src/tourniquet/proxy/router.py:215-222`). Oversized
  POSTs return 413 before the body is fully buffered.
- **Action-link replay.** Migration `0003` adds the partial unique index
  `ix_api_key_actions_unique_token` — the same kill-now or lift token can
  not be redeemed twice, even under concurrent click-throughs from
  different channels (email + Telegram).
- **Sleep-prevention attestation.** `_sleep_protection_status` in
  `src/tourniquet/dashboard/routes.py:253` best-effort detects an active
  wake-lock on macOS (`pmset -g assertions`), Linux (`systemd-inhibit`),
  and Windows (`powercfg /requests`). On platforms or installs where the
  probe can't read the state, the helper returns "active=False, owner="
  rather than the previous misleading "always-on" string.
- **Startup-time key validation.** `fernet_key` and `secret_key` are
  validated as Pydantic field validators (`src/tourniquet/config.py:36-55`).
  A short `secret_key` or a non-Fernet `fernet_key` fails fast at boot, not
  silently at first request.

## 3. What Tourniquet does NOT guard against

- **In-process / file-system attackers on the host.** Anyone who can read
  `~/.tourniquet/tourniquet.db` and the configured `FERNET_KEY` can decrypt
  every vaulted Anthropic key. Anyone with the DB file alone gets every
  `tq_token_sha256` and every legacy `tq_token_hash`. Use file-system
  permissions and a per-user account; do not run as root.
- **Anthropic-side abuse signals.** The proxy does not currently translate
  Anthropic's `anthropic-ratelimit-*` headers into operator alerts. If your
  upstream account is being throttled or flagged, you'll see it in
  individual response codes but not in a dashboard surface.
- **Network-level MITM in absence of TLS.** Bare HTTP exposes `tq_*` bearer
  tokens to any network observer between the agent and the listener. TLS
  is the operator's responsibility — see "Recommended operator practices"
  below.
- **Side-channel timing on the bcrypt legacy fallback.** The legacy bcrypt
  scan in `_legacy_bcrypt_scan` is `O(legacy keys)`. The window is small
  (only keys created before migration `0003` that haven't been used
  since), but for a brand-new install with hundreds of legacy keys, the
  first request per key is observably slower than a fast-path lookup. Once
  hit, each legacy key upgrades itself.
- **Compromised cap storage.** `caps_today` lives in the same SQLite file
  as everything else. An attacker who can write to the DB defeats the cap;
  the cap mechanism trusts its own storage. Treat the DB file as a secret.

## 4. Recommended operator practices

- **Front the listener with TLS** (Caddy, nginx, or Tailscale Funnel TLS)
  whenever it's reachable from anywhere except loopback. See
  `docs/deploy.md` for VPN-first guidance.
- **Rotate `FERNET_KEY` periodically.** A rotation procedure should
  re-encrypt vaulted keys with the new Fernet key in a single atomic
  migration; document it in `docs/deploy.md` if you maintain a fork.
- **Use a 32+ byte `SECRET_KEY`.** The `m2` field validator now enforces
  this at startup — a too-short key refuses to boot rather than silently
  weakening session signing.
- **Monitor `caps_today` for anomalies.** Sustained high water-mark across
  multiple keys typically means an agent loop is hot — surface it via the
  dashboard sparkline or alert via Slack/Telegram.
- **Apply schema migrations promptly.** The token-auth fast path requires
  migration `0003`. Older installs continue to work via the bcrypt
  fallback, but each request pays a linear-scan cost until the migration
  lands.
