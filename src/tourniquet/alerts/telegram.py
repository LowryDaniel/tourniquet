"""Telegram bot channel."""

from __future__ import annotations

import logging

import httpx

from tourniquet.config import settings

log = logging.getLogger(__name__)


async def send_telegram(message: str) -> None:
    """Send message via Telegram bot API.

    Requires both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to be set.
    Both are treated as secrets and never logged.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": int(settings.telegram_chat_id),
        "text": message,
        "parse_mode": "HTML",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)

    if resp.status_code != 200:
        log.warning("Telegram API returned status %d", resp.status_code)


async def send_telegram_with_lift_buttons(
    message: str,
    key_id: str,
    kill_now_url: str | None = None,
) -> None:
    """Send a Telegram message with inline lift buttons.

    Used for cap-hit and >= 80% threshold alerts so the user can raise the cap
    directly from the notification without opening a terminal.

    Buttons:
      💸 Lift 2× today   → callback_data: lift|<key_id>|2x
      🚀 To ceiling       → callback_data: lift|<key_id>|ceiling
      🛑 Kill now         → callback_data: kill_now|<key_id>  (only when kill_now_url set)
      Ignore              → callback_data: lift|<key_id>|ignore
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"

    row = [
        {"text": "💸 Lift 2× today", "callback_data": f"lift|{key_id}|2x"},
        {"text": "🚀 To ceiling", "callback_data": f"lift|{key_id}|ceiling"},
        {"text": "Ignore", "callback_data": f"lift|{key_id}|ignore"},
    ]
    if kill_now_url:
        row.append({"text": "🛑 Kill now", "callback_data": f"kill_now|{key_id}"})

    payload = {
        "chat_id": int(settings.telegram_chat_id),
        "text": message,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [row]},
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)

    if resp.status_code != 200:
        log.warning("Telegram API returned status %d", resp.status_code)
