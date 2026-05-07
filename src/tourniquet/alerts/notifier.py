"""Unified alert fanout.

Usage::

    from tourniquet.alerts.notifier import AlertEvent, fan_out

    event = AlertEvent(
        api_key_name="ojw-swarm",
        threshold_pct=80,
        spent_usd_cents=420,
        cap_usd_cents=500,
        display_currency="GBP",
        today=date.today(),
    )
    results = await fan_out(event)
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import date

from tourniquet.billing.formatting import format_money
from tourniquet.config import settings

log = logging.getLogger(__name__)


@dataclasses.dataclass
class AlertEvent:
    api_key_name: str
    threshold_pct: int        # 50 / 80 / 100 / -1 for "cap-hit"
    spent_usd_cents: int
    cap_usd_cents: int
    display_currency: str     # for format_money
    today: date
    api_key_id: str = ""      # UUID string — used for Telegram lift buttons


def _format_message(event: AlertEvent) -> str:
    spent = format_money(event.spent_usd_cents, event.display_currency)
    cap = format_money(event.cap_usd_cents, event.display_currency)

    if event.threshold_pct == -1:
        return (
            f"\U0001f6d1 Tourniquet: {event.api_key_name} cap reached — "
            f"{spent}/{cap} today, requests now blocked"
        )
    return (
        f"⚠️ Tourniquet: {event.api_key_name} at {event.threshold_pct}% — "
        f"{spent} of {cap} today"
    )


async def fan_out(event: AlertEvent) -> dict[str, str]:
    """Send the alert to every configured channel concurrently.

    Returns a dict mapping channel name to one of:
      "sent" | "skipped:no-config" | "error:<message>"

    Never raises — channel failures are captured and returned.
    """
    from tourniquet.alerts.desktop import send_desktop_notification
    from tourniquet.alerts.email import send_email
    from tourniquet.alerts.jsonl_log import write_jsonl
    from tourniquet.alerts.slack import send_slack
    from tourniquet.alerts.telegram import send_telegram, send_telegram_with_lift_buttons
    from tourniquet.alerts.webhook import send_webhook

    message = _format_message(event)

    # Threshold >= 80 or cap-hit (-1) → send lift buttons on Telegram
    wants_lift_buttons = event.threshold_pct == -1 or event.threshold_pct >= 80

    # Build task list: (channel_name, coroutine | None)
    # None means "skip" — we know before dispatching it won't do anything.
    tasks: list[tuple[str, bool]] = []

    async def _run(name: str, coro: object) -> tuple[str, str]:
        try:
            await coro  # type: ignore[misc]
            return name, "sent"
        except Exception as exc:
            log.warning("Alert channel %r failed: %s", name, exc)
            return name, f"error:{exc}"

    coroutines = []

    # JSONL — always on
    coroutines.append(_run("jsonl", write_jsonl(event, message)))

    # Desktop notification (mac / windows / linux)
    desktop_enabled = (
        settings.enable_mac_notifications == "true"
        or getattr(settings, "enable_desktop_notifications", "") == "true"
    )
    if desktop_enabled:
        coroutines.append(_run("desktop", send_desktop_notification("Tourniquet", message, event)))
    else:
        tasks.append(("desktop", False))

    # Slack
    if settings.slack_webhook_url:
        coroutines.append(_run("slack", send_slack(message)))
    else:
        tasks.append(("slack", False))

    # Telegram — use lift buttons for >= 80% or cap-hit
    if settings.telegram_bot_token and settings.telegram_chat_id:
        if wants_lift_buttons and event.api_key_id:
            coroutines.append(_run("telegram", send_telegram_with_lift_buttons(message, event.api_key_id)))
        else:
            coroutines.append(_run("telegram", send_telegram(message)))
    else:
        tasks.append(("telegram", False))

    # Generic webhook
    if settings.alert_webhook_url:
        coroutines.append(_run("webhook", send_webhook(message, event)))
    else:
        tasks.append(("webhook", False))

    # Email — always attempt; send_email is a no-op if RESEND_API_KEY unset
    coroutines.append(_run("email", send_email(message, event)))

    results_list = await asyncio.gather(*coroutines, return_exceptions=True)

    results: dict[str, str] = {}

    # Skipped channels
    for name, _ in tasks:
        results[name] = "skipped:no-config"

    # Executed channels
    for item in results_list:
        if isinstance(item, BaseException):
            # Shouldn't happen (_run never raises) but be defensive
            log.error("Unexpected fanout exception: %s", item)
        else:
            name, status = item
            results[name] = status

    return results
