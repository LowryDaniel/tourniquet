# Alert channels — setup guide

Tourniquet sends one alert event ("at 80%", "cap reached", "killed — bump?") to **every channel you have configured**. The text is the same everywhere; only the rendering and click targets differ.

This guide walks through the channels worth setting up in priority order. After each one, run `tourniquet test-alerts` to verify it lights up green before moving on.

## Requirements at a glance

Before you start, here's exactly what each method needs from you. Pick the ones you actually want — you don't need them all.

| Method | Cost | Account needed | Setup time | Notes |
|---|---|---|---|---|
| **Dashboard** | Free | None | 0s — auto-on | `http://127.0.0.1:8787/dashboard` |
| **JSONL log** | Free | None | 0s — auto-on | Writes to `~/.tourniquet/alerts.jsonl` |
| **Mac desktop banner** | Free | None | 30s | macOS only. Allow notifications for the *Script Editor* app |
| **Slack** (browser-confirm) | Free | A Slack workspace where you can install apps | ~3 min | Tap → browser → confirm. Works with any URL scheme |
| **Slack in-app one-tap** (Socket Mode) | Free | Same workspace + ability to add a Bot User scope and install/reinstall the app | ~8 min | Adds `chat:write` scope, bot token, channel ID. Tap = applied, no browser hop |
| **Telegram** | Free | Telegram account on phone or desktop | ~5 min | In-app one-tap automatically (long-poll) — no public URL |
| **WhatsApp** | Paid (~$0.005/msg, $15 free credit) | Twilio + WhatsApp sender registration | ~30+ min | Plain text only — interactive buttons need Meta-approved templates. **Recommended: use Telegram or webhook+Zapier instead** |
| **Email (Resend)** | Free tier 3k/mo | Resend account + a domain you own | ~15 min | Domain DNS verification (SPF/DKIM). Skip unless you already own a domain |
| **Generic webhook** | Free with most targets | Zapier / n8n / Home Assistant / custom | ~5 min | Tourniquet POSTs JSON; downstream renders |
| **Off-network access** | Free with Cloudflare Tunnel | Cloudflare account (free) | ~5 min | Lets phones / other machines reach your local Tourniquet via a public URL |

You'll also need **`~/.tourniquet/.env`** to live somewhere editable — Tourniquet creates it on first run. Each method's section below tells you which lines to fill in.

After every `.env` edit, restart `tourniquet start` so the new config loads. `tourniquet test-alerts` reads `.env` fresh each time and is the fastest way to verify a channel works.

## Already working out of the box

You don't need to do anything for these — they're on by default:

| Channel | Where the alert lands |
|---|---|
| **Dashboard** | `http://127.0.0.1:8787/dashboard` — live spend bar, alert log, control panel |
| **JSONL log** | `~/.tourniquet/alerts.jsonl` — one JSON line per alert, grep-friendly |

Run `tourniquet test-alerts` to see both fire.

---

## Mac desktop banners — 30 seconds

Most useful for working at your laptop without leaving Tourniquet open in a tab.

**Requirements:** macOS. No accounts, no credentials.

### 1. Edit `~/.tourniquet/.env`

```
ENABLE_MAC_NOTIFICATIONS=true
```

### 2. Allow notifications for "Script Editor"

This is non-obvious: macOS attributes `osascript` notifications to the **Script Editor** app, not to Tourniquet. So:

1. **System Settings → Notifications**
2. Scroll to **Script Editor**
3. **Allow Notifications: ON**
4. **Banner style: Banners** (auto-dismiss in ~5s) or **Alerts** (stay until dismissed)
5. Optional: **Show in Notification Centre** ON, **Show on Lock Screen** ON

If banners don't show, check **Focus** mode in the menu bar — moon/star icon means notifications are muted.

### 3. Verify

```
tourniquet test-alerts
```

Banner should pop top-right within 1 second. After ~5s it slides into Notification Centre (click the clock to see history).

---

## Slack — 2-3 minutes (browser-confirm flow)

Free. Works from desktop and mobile. Best when you already have a Slack workspace open.

**Requirements:**
- A Slack workspace where you can create and install apps (your own workspace, or one you're an admin of)
- Permission to enable Incoming Webhooks
- Decide which channel/DM the alerts should land in

What you get: tap a button in Slack → browser opens to confirm page → confirm → applied. Same UX as most other Slack apps.

### 1. Create a Slack app

1. Go to <https://api.slack.com/apps> and click **Create New App** → **From scratch**
2. App name: `Tourniquet`
3. Workspace: pick the one where you want alerts to land
4. Click **Create App**

### 2. Enable incoming webhooks

1. Sidebar → **Incoming Webhooks**
2. Toggle **Activate Incoming Webhooks** → **On**
3. Scroll down → **Add New Webhook to Workspace**
4. Pick a channel:
   - **DM yourself** by searching your name (best for solo use)
   - Or `#tourniquet-alerts` if you want a dedicated channel
5. Click **Allow**
6. Slack returns you to the webhook page — copy the URL. It looks like:
   ```
   https://hooks.slack.com/services/T01ABC.../B02DEF.../xxxxxxxxxxxx
   ```

### 3. Add to `.env`

```
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T01.../B02.../xxx
```

### 4. Verify

```
tourniquet test-alerts
```

Slack should DM/message the channel within 1-2 seconds. Status should show `✅ slack delivered`.

### Gotchas

- The webhook URL is a **bearer credential** — anyone with the string can post to your Slack. Don't paste it in screenshots, public diffs, or chat with anyone you don't trust.
- The webhook is bound to **one channel**. To alert a different channel, repeat steps 3-7 (Tourniquet only reads one URL — pick the channel you'll actually look at).
- If you ever need to revoke: <https://api.slack.com/apps> → your app → Incoming Webhooks → trash → recreate.

### Optional: Slack in-app one-tap (Socket Mode + bot post) — ~8 min

By default, tapping a Slack button opens your browser to a confirmation page. To make taps apply **in-app** without a browser hop, you need three additional Slack-side credentials. Tourniquet then sends via `chat.postMessage` (so the buttons can carry interactive `action_id`) and opens a Socket Mode WebSocket *to* Slack to receive taps. No public URL anywhere.

**Requirements:**
- Everything in the Slack section above (workspace, app, webhook URL is optional once this is set up)
- App-level token (`xapp-...`) with `connections:write` scope
- Bot User OAuth Token (`xoxb-...`) with `chat:write` scope
- Channel ID (or DM ID) where alerts should land
- Bot must be invited to that channel/DM (Slack: `/invite @YourBot`)

What you get: tap a button → message rewrites in place to *"✓ Bumped $5. test cap is now $7.04 until midnight UTC."* No browser. No second confirm.

#### Step 1 — Enable Socket Mode + generate the app-level token

1. <https://api.slack.com/apps> → your Tourniquet app → **Settings → Socket Mode** → toggle **Enable Socket Mode** ON
2. Slack prompts for an app-level token. Click **Generate**:
   - Token name: `tourniquet-socket`
   - Scopes: add `connections:write`
   - Generate, **copy the `xapp-...` token immediately** (Slack shows it once)

#### Step 2 — Add the bot user scope and install

1. **Features → OAuth & Permissions** → **Scopes → Bot Token Scopes** → **Add an OAuth Scope** → `chat:write`
2. Top of the same page → **Install to Workspace** (or **Reinstall to Workspace** if it's already installed). Approve.
3. Copy the **Bot User OAuth Token** — it starts with `xoxb-...`

#### Step 3 — Invite the bot to your channel + grab the channel ID

1. In Slack, open the channel/DM you want alerts in
2. Type `/invite @Tourniquet` (or whatever you named the bot) and send
3. Get the channel ID:
   - In Slack desktop: right-click the channel name → **Copy link**
   - The link looks like `https://yourworkspace.slack.com/archives/C123ABC456`
   - The `C123ABC456` part is your channel ID. For DMs the prefix is `D` instead of `C` — same idea.

#### Step 4 — Wire up `.env`

Add all three lines to `~/.tourniquet/.env`:

```
SLACK_APP_TOKEN=xapp-1-...
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL_ID=C123ABC456
```

#### Step 5 — Enable Interactivity (this just turns the feature on; no URL needed)

1. **Features → Interactivity & Shortcuts** → toggle **Interactivity** ON
2. **Leave the Request URL empty** — Socket Mode delivers interactions over the WebSocket
3. Save Changes

#### Step 6 — Restart and verify

```
# in the terminal running `tourniquet start`:
Ctrl+C
tourniquet start
```

Watch for `INFO:     Slack Socket connected`. Then `tourniquet test-alerts --recovery` — Slack should show a message with three real action buttons (`+$1` `+$5` `+$10`). Tap one, watch the message update in place.

**Note:** Once bot-post is fully configured, Tourniquet stops using `SLACK_WEBHOOK_URL` (it would duplicate alerts). The bot posts every alert directly to your `SLACK_CHANNEL_ID` instead. You can leave `SLACK_WEBHOOK_URL` set or remove it — your call.

---

## Telegram — 3-5 minutes

Free. Works from phone and desktop. Best for "buzz me on my phone if my agents go feral."

**Requirements:**
- A Telegram account (phone number + the app on phone or desktop)
- Ability to chat with `@BotFather` (no payment, no business verification)
- ~2 minutes to copy a bot token and a chat ID into `.env`

What you get: in-app one-tap automatically. Tourniquet runs a long-poll client to Telegram, so taps on phone or desktop apply directly — message rewrites itself to *"✓ Bumped $5. test cap is now $7.04 until midnight UTC."* No browser, no public URL needed.

### 1. Create a bot via @BotFather

1. Open Telegram on phone or desktop
2. Search for `@BotFather` — make sure it has the **blue verified tick**
3. Tap Start, then send `/newbot`
4. BotFather: *"Alright, a new bot. How are we going to call it?"*
   → Reply with display name, e.g. `Tourniquet Alerts`
5. BotFather: *"Now let's choose a username..."* — must end in `bot`
   → Reply with something globally unique, e.g. `dan_tourniquet_bot`
6. BotFather replies with the API token — looks like:
   ```
   123456789:AAEhBPzLhfg-xxxxxxxxxxxxxxxxxxxxxxxxx
   ```
   **Copy that.** This is your `TELEGRAM_BOT_TOKEN`.

### 2. Get your chat ID

The classic way (`@userinfobot` — search and tap Start) often works. If it doesn't, use the direct method:

1. **Message your own bot first** — search the username you picked, tap Start, send `hi`. Telegram blocks bots from initiating; you must message them once before they can DM you.
2. Open this URL in a browser (replace `<TOKEN>` with the one BotFather gave you):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. Find `"chat":{"id":` in the JSON response. The number is your chat ID.

If `getUpdates` returns `{"ok":true,"result":[]}`, you haven't messaged your bot yet. Go back to step 1.

### 3. Add to `.env`

```
TELEGRAM_BOT_TOKEN=123456789:AAEhBPzLhfg-xxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=987654321
```

⚠️ Common mistake: don't paste `Chat ID: 987654321` (the label words). Just the number.

### 4. Verify

```
tourniquet test-alerts
tourniquet test-alerts --threshold cap-hit
tourniquet test-alerts --recovery
```

Phone should buzz three times — each message comes with inline buttons (`💸 Lift 2× today`, `🚀 To ceiling`, `🛑 Kill now`, or `+$1`/`+$5`/`+$10`).

### Gotchas

- **Bot tokens are bearer credentials.** If you ever paste one publicly, revoke via BotFather (`/revoke`) immediately and update your `.env`.
- **In-app one-tap is on by default** — Tourniquet runs a long-polling client to Telegram, so tapping `+$5` / `🛑 Kill now` updates the original message in-place ("✓ Bumped $5. cap is now $X.XX until midnight UTC.") with no browser hop, no public URL. Polling auto-starts when `TELEGRAM_BOT_TOKEN` is set.
- **Phone vs Mac doesn't matter** — taps go to Telegram's servers, Telegram delivers them to your local Tourniquet. Works the same from any device.

---

## WhatsApp — paid + complex; consider the alternatives

**Requirements (whichever path):**
- A phone number registered for WhatsApp (your personal number works for sandbox testing only — production needs a separate WhatsApp Business number)
- A Twilio account (or Meta Cloud API approval) — significant onboarding either way
- A credit card for paid messaging (~$0.005/msg after free trial credit)
- For inline buttons: Meta-approved message templates (24-48h approval cycle)

For a solo-dev tool, this is heavyweight. Stronger recommendation: **use Telegram for phone alerts** — same use case, free, 5-minute setup. If you specifically need WhatsApp, the lowest-effort path is Tourniquet's webhook → Zapier → WhatsApp action.



Native WhatsApp support isn't built in v0.1. Three paths if you really want it:

### A. Webhook → Zapier → WhatsApp (~5 min, free for low volume)

The lowest-effort path:

1. Set up the **generic webhook** below pointing at a Zapier "Catch Hook" trigger
2. In Zapier, add a "Send WhatsApp message" action via [WhatsApp by Zapier](https://zapier.com/apps/whatsapp-business) or Twilio → WhatsApp
3. Done

### B. Native Twilio WhatsApp (planned for v0.2)

Stable, paid (~$0.005/msg, ~$15 free trial credit), production-ready. Requires Twilio account + WhatsApp sender registration.

### C. Meta Cloud API (direct)

Free tier 1,000 conversations/month. Setup is brutal — business verification, phone-number provisioning, app review. Only worth it for high-volume use.

For a solo-dev tool, **Telegram is the better default.** Same use case, 10× simpler.

---

## Generic webhook — 1 minute

Most useful for Zapier / n8n / Home Assistant / custom automation.

**Requirements:**
- A target endpoint that accepts POST + JSON (Zapier "Catch Hook", n8n Webhook node, Home Assistant webhook automation, or your own server)
- A URL to paste

What you get: every alert is POSTed as JSON. Your downstream system decides how to render it (Pushcut on iPhone, SMS via Twilio, Discord, anything).

### 1. Get a target URL

- **Zapier:** new Zap → trigger "Webhooks by Zapier" → "Catch Hook" → copy URL
- **n8n:** new workflow → Webhook node → copy production URL
- **Home Assistant:** automation with Webhook trigger → URL is `https://your-ha.url/api/webhook/<id>`

### 2. Add to `.env`

```
ALERT_WEBHOOK_URL=https://hooks.zapier.com/hooks/catch/.../...
```

### 3. Payload shape (so you can build mappings)

```json
{
  "message": "⚠️ Tourniquet: ojw-swarm — at 80%. $4.00/$5.00 today.",
  "event": {
    "api_key_name": "ojw-swarm",
    "threshold_pct": 80,
    "spent_usd_cents": 400,
    "cap_usd_cents": 500,
    "display_currency": "USD",
    "today": "2026-05-07",
    "api_key_id": "uuid-here",
    "kill_now_url": "https://...",
    "recovery_offer": false
  },
  "recovery_options": [
    {"amount_cents": 100, "label": "+$1", "url": "https://..."},
    {"amount_cents": 500, "label": "+$5", "url": "https://..."},
    {"amount_cents": 1000, "label": "+$10", "url": "https://..."}
  ]
}
```

`recovery_options` is only present when `event.recovery_offer == true`.

### 4. Verify

```
tourniquet test-alerts
```

Check your Zap/n8n/HA history — should show one POST.

---

## Email via Resend — only if you own a domain

**Requirements:**
- A domain you own (your personal `.com`, your `.dev`, etc — Gmail addresses won't work, you can't send "from" Gmail at scale)
- Access to that domain's DNS records (Cloudflare, Route 53, your registrar's DNS panel)
- A Resend.com account (free tier: 3,000 emails/month, 100/day)
- ~10-15 minutes for DNS records to propagate and verify

Setup is the longest of any channel because email infrastructure requires DNS verification. Skip this unless you specifically want email alerts.

If you do want it:

1. Sign up at <https://resend.com> (free tier: 3,000/month)
2. **Domains → Add Domain** → enter a domain you own
3. Add the DNS records Resend shows you (TXT for SPF + DKIM, optionally MX). In Cloudflare → DNS → Records → DNS only (grey cloud, NOT proxied)
4. Wait 1-5 min for verification
5. **API Keys → Create** → name `tourniquet`, permission Sending access
6. `.env`:
   ```
   RESEND_API_KEY=re_xxxxxxxxxxxxxxxx
   RESEND_FROM_EMAIL=alerts@yourdomain.com
   ```
7. In the dashboard, set each key's **alert_email** field to where you want delivery
8. `tourniquet test-alerts` — check the recipient inbox (also spam folder; new domains often start there)

For most solo devs, **Slack or Telegram is the better choice** — instant, no DNS pain.

---

## Verifying everything works

After setting up each channel, the gold-standard test is:

```
tourniquet test-alerts                          # 80% threshold
tourniquet test-alerts --threshold cap-hit      # cap reached
tourniquet test-alerts --recovery               # killed, want to bump?
```

You should see something like:

```
🧪 Tourniquet test-alerts
   Threshold:  80%
   Mode:       standard (kill_enabled=True)

  Channel      Status
  ✅  jsonl     delivered
  ✅  desktop   delivered
  ✅  slack     delivered
  ✅  telegram  delivered
  —  email     not configured
  —  webhook   not configured
```

The `—` rows are skipped (not configured) — that's fine. The `✅` rows actually fired.

If a row says `❌ <error>`, the channel is configured but something failed. Most common causes:
- Wrong credential format (e.g. `Chat ID: 12345` instead of `12345`)
- Webhook URL revoked or deleted
- Resend domain not yet verified

Run `grep '^<CHANNEL>' ~/.tourniquet/.env` to check the exact value Tourniquet is reading.

---

## After every `.env` edit

If you have `tourniquet start` running, **restart it** to pick up the new config:

```
# In the terminal running tourniquet start:
Ctrl+C
tourniquet start
```

The `tourniquet test-alerts` command always reads `.env` fresh (separate process), so it doesn't need a restart.

---

## Off-network access (out of v0.1 scope)

By default the dashboard runs at `http://127.0.0.1:8787` — only your laptop can reach it. So buttons in alerts that point at this URL won't work from your **phone** (different machine).

**Requirements (any one of these):**
- A Cloudflare account (free) and `cloudflared` installed (`brew install cloudflared`)
- Or a Tailscale account (free) with the device on your tailnet
- Or an `ngrok` account (free with caveats)
- Or your own server / port-forwarding setup

For phone-side recovery to work, you'd need a public tunnel:

```bash
# Cloudflare Tunnel — free
cloudflared tunnel --url http://127.0.0.1:8787

# Or Tailscale Funnel — free
tailscale funnel 8787

# Or ngrok — free with caveats
ngrok http 8787
```

Then set `APP_BASE_URL=https://your-tunnel.trycloudflare.com` in `.env` and restart. All alert URLs will use the public tunnel.

This is on the v0.2 roadmap as a built-in flag (`tourniquet start --tunnel`) — not in v0.1.
