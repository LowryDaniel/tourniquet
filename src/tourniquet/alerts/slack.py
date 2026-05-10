"""Slack channel — two routing modes.

**Bot-post mode** (SLACK_APP_TOKEN + SLACK_BOT_TOKEN + SLACK_CHANNEL_ID all set):
Tourniquet sends via `chat.postMessage` with Block Kit action buttons. Taps
arrive via Socket Mode WebSocket (alerts/slack_socket.py) and apply in-app —
the original message rewrites itself to "✓ Bumped … cap is now …" with no
browser hop. SLACK_WEBHOOK_URL is not used in this mode (would duplicate alerts).

**Webhook mode** (only SLACK_WEBHOOK_URL set): renders the same message as
plain text + mrkdwn-link tail. Taps open the user's browser → confirm page →
applied. Works with any URL scheme including http://127.0.0.1.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import httpx

from tourniquet.config import settings

log = logging.getLogger(__name__)


def _build_action_payload(
    message: str,
    event: Any,
    recovery_offer: bool,
    kill_now_url: str | None,
    key_id: str,
) -> dict[str, Any]:
    """Build a Block Kit payload with real action buttons (Socket Mode path).

    action_id values map to handlers in slack_socket.py via prefix match:
      "lift_2x" / "lift_ceiling"  → lift handler   (mode parsed from value)
      "lift_by_amount_<cents>"    → bump handler   (cents parsed from value)
      "kill_now"                  → kill handler

    Slack rejects duplicate action_ids in the same actions block — so each
    button gets a unique id even when siblings share routing. The `value`
    field still carries `key_id|payload` for the dispatcher to parse.
    """
    elements: list[dict[str, Any]] = []
    if recovery_offer:
        from tourniquet.alerts.notifier import recovery_amounts_cents

        amounts = recovery_amounts_cents(getattr(event, "cap_usd_cents", 0) or 0)
        for c in amounts:
            label = f"+${c // 100}" if c % 100 == 0 else f"+${c / 100:.2f}"
            elements.append(
                {
                    "type": "button",
                    "action_id": f"lift_by_amount_{c}",
                    "text": {"type": "plain_text", "text": label},
                    "value": f"{key_id}|{c}",
                    "style": "primary",
                }
            )
    else:
        elements.extend(
            [
                {
                    "type": "button",
                    "action_id": "lift_2x",
                    "text": {"type": "plain_text", "text": "💸 Lift 2× today"},
                    "value": f"{key_id}|2x",
                },
                {
                    "type": "button",
                    "action_id": "lift_ceiling",
                    "text": {"type": "plain_text", "text": "🚀 To ceiling"},
                    "value": f"{key_id}|ceiling",
                },
            ]
        )
        if kill_now_url:
            elements.append(
                {
                    "type": "button",
                    "action_id": "kill_now",
                    "text": {"type": "plain_text", "text": "🛑 Kill now"},
                    "value": key_id,
                    "style": "danger",
                }
            )

    return {
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": message}},
            {"type": "actions", "elements": elements},
        ]
    }


def _bot_post_fully_configured() -> bool:
    """Bot-post mode requires the full Slack-side trio so messages can carry
    Block Kit action buttons and taps can land via Socket Mode."""
    return bool(
        settings.slack_app_token
        and getattr(settings, "slack_bot_token", "")
        and getattr(settings, "slack_channel_id", "")
    )


async def _send_via_bot(payload: dict[str, Any], fallback_text: str) -> None:
    """POST to chat.postMessage with the bot user OAuth token."""
    payload = dict(payload)  # don't mutate caller's dict
    payload["channel"] = settings.slack_channel_id
    # `text` is the fallback shown in notifications + accessibility readers
    # when the blocks can't render. Keep it identical to the canonical message.
    payload.setdefault("text", fallback_text)
    headers = {
        "Authorization": f"Bearer {settings.slack_bot_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            json=payload,
            headers=headers,
        )
    data: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        data = resp.json()
    if not data.get("ok"):
        # Common errors: not_in_channel (bot needs invite), invalid_auth (bad token),
        # channel_not_found (wrong ID), invalid_blocks (duplicate action_id, > limits).
        # Surface these without leaking the token, and RAISE so the CLI dispatcher
        # reports "❌ slack <error>" instead of a false-positive "delivered".
        err = data.get("error") or f"http_{resp.status_code}"
        log.warning("Slack chat.postMessage failed: %s", err)
        raise RuntimeError(f"slack chat.postMessage failed: {err}")


async def send_slack(message: str, event: object = None) -> None:
    """Send an alert to Slack via either bot-post or webhook.

    Bot-post mode wins when fully configured — webhook is unused in that case
    so we don't duplicate alerts. No-op if neither path is configured.
    """
    kill_now_url: str | None = getattr(event, "kill_now_url", None) if event is not None else None
    recovery_offer: bool = (
        bool(getattr(event, "recovery_offer", False)) if event is not None else False
    )
    key_id: str = getattr(event, "api_key_id", "") if event is not None else ""

    # ── Bot-post mode (full Socket Mode) ──────────────────────────────────────
    if _bot_post_fully_configured() and event is not None and key_id:
        payload = _build_action_payload(message, event, recovery_offer, kill_now_url, key_id)
        await _send_via_bot(payload, fallback_text=message)
        return

    # ── Webhook mode (mrkdwn-link fallback) ───────────────────────────────────
    if not settings.slack_webhook_url:
        return

    if recovery_offer and event is not None:
        from tourniquet.alerts.notifier import recovery_amounts_cents
        from tourniquet.routes.admin import build_lift_by_amount_url

        amounts = recovery_amounts_cents(getattr(event, "cap_usd_cents", 0) or 0)
        link_parts = []
        for c in amounts:
            label = f"+${c // 100}" if c % 100 == 0 else f"+${c / 100:.2f}"
            url = build_lift_by_amount_url(key_id, c)
            link_parts.append(f"<{url}|{label}>")
        payload = {"text": f"{message}\nBump: {' · '.join(link_parts)}"}
    elif kill_now_url:
        payload = {"text": f"{message}\n<{kill_now_url}|🛑 Kill now>"}
    else:
        payload = {"text": message}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(settings.slack_webhook_url, json=payload)

    if resp.status_code != 200:
        log.warning("Slack webhook returned status %d", resp.status_code)
