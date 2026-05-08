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
    kill_now_url: str | None = None   # signed magic-link; set when kill_enabled=False
    alert_email: str | None = None    # per-key recipient for email channel; falls back to RESEND_FROM_EMAIL
    recovery_offer: bool = False      # True when this alert is a "killed, want to bump?" recovery prompt


def _build_kill_now_url(key_id: str) -> str:
    """Return a signed 24h-expiry kill-now URL."""
    from itsdangerous import URLSafeTimedSerializer
    s = URLSafeTimedSerializer(settings.secret_key, salt="kill-now")
    token = s.dumps(key_id)
    return f"{settings.app_base_url}/admin/kill-now/{key_id}?token={token}"


def _build_lift_by_amount_url(key_id: str, amount_cents: int) -> str:
    """Return a signed 24h-expiry lift-by-amount URL.

    Token encodes (key_id, amount_cents) so it can't be replayed for a different
    amount. Each amount option = different signed link.
    """
    from itsdangerous import URLSafeTimedSerializer
    s = URLSafeTimedSerializer(settings.secret_key, salt="lift-by-amount")
    token = s.dumps([key_id, amount_cents])
    return f"{settings.app_base_url}/admin/lift-by-amount/{key_id}?token={token}&amount={amount_cents}"


def recovery_amounts_cents(cap_cents: int) -> list[int]:
    """Return 3 sensible recovery-bump amounts (in cents) given today's cap.

    Scales with cap magnitude — a $5 cap offers +$1/+$5/+$10; a $1000 cap
    offers +$25/+$100/+$500. Same scaling logic as the dashboard nudge buttons.
    """
    if cap_cents <= 1000:        # ≤ $10
        return [100, 500, 1000]    # +$1   +$5   +$10
    if cap_cents <= 10000:       # ≤ $100
        return [500, 2500, 10000]  # +$5   +$25  +$100
    if cap_cents <= 100000:      # ≤ $1000
        return [2500, 10000, 50000]
    return [10000, 50000, 100000]


def _format_money_cents(cents: int) -> str:
    """Compact dollar formatting for button labels — no decimals on round amounts."""
    if cents % 100 == 0:
        return f"${cents // 100}"
    return f"${cents / 100:.2f}"


def _format_message(event: AlertEvent) -> str:
    """Single canonical alert template — same shape for every threshold and channel.

    Pattern:    {icon} Tourniquet: {name} — {state}. {spent}/{cap} today.
    Action verbs live in the buttons, not the prose. Don't tweak the wording —
    consistency across alerts is more valuable than clever phrasing.
    """
    spent = format_money(event.spent_usd_cents, event.display_currency)
    cap = format_money(event.cap_usd_cents, event.display_currency)

    if event.recovery_offer:
        return (
            f"🛑 Tourniquet: {event.api_key_name} — killed. "
            f"{spent}/{cap} today. Bump cap to continue?"
        )

    if event.threshold_pct == -1:
        return (
            f"🛑 Tourniquet: {event.api_key_name} — cap reached. "
            f"{spent}/{cap} today. Requests blocked."
        )

    return (
        f"⚠️ Tourniquet: {event.api_key_name} — at {event.threshold_pct}%. "
        f"{spent}/{cap} today."
    )


async def fan_out(event: AlertEvent, *, kill_enabled: bool = True) -> dict[str, str]:
    """Send the alert to every configured channel concurrently.

    Pass kill_enabled=False when the key is in monitor mode — this causes a
    signed kill-now URL to be embedded in the event and surfaced in all channels
    that support it.

    Returns a dict mapping channel name to one of:
      "sent" | "skipped:no-config" | "error:<message>"

    Never raises — channel failures are captured and returned.
    """
    from tourniquet.alerts.desktop import send_desktop_notification
    from tourniquet.alerts.email import send_email
    from tourniquet.alerts.jsonl_log import write_jsonl
    from tourniquet.alerts.slack import send_slack
    from tourniquet.alerts.telegram import (
        send_telegram,
        send_telegram_recovery_offer,
        send_telegram_with_lift_buttons,
    )
    from tourniquet.alerts.webhook import send_webhook

    # Always attach kill-now URL — every alert should have a one-click escape.
    # When kill_enabled=True the proxy will block at 100% anyway, but the user
    # might want to slam the brake early (50/80%) or lock down post-hit.
    # When kill_enabled=False (monitor) it's the only enforcement mechanism.
    if event.api_key_id and event.kill_now_url is None:
        event = dataclasses.replace(event, kill_now_url=_build_kill_now_url(event.api_key_id))

    message = _format_message(event)

    # Send Telegram with inline buttons whenever the kill-now URL is available
    # (which is always, post-fix). User wants kill option on every alert.
    wants_lift_buttons = event.kill_now_url is not None or event.api_key_id != ""

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
        coroutines.append(_run("slack", send_slack(message, event)))
    else:
        tasks.append(("slack", False))

    # Telegram — recovery_offer takes precedence (one-tap bump buttons),
    # else lift buttons for >= 80% or cap-hit, else plain text.
    if settings.telegram_bot_token and settings.telegram_chat_id:
        if event.recovery_offer and event.api_key_id:
            amounts = recovery_amounts_cents(event.cap_usd_cents)
            coroutines.append(_run("telegram", send_telegram_recovery_offer(message, event.api_key_id, amounts)))
        elif wants_lift_buttons and event.api_key_id:
            coroutines.append(_run("telegram", send_telegram_with_lift_buttons(message, event.api_key_id, event.kill_now_url)))
        else:
            coroutines.append(_run("telegram", send_telegram(message)))
    else:
        tasks.append(("telegram", False))

    # Generic webhook
    if settings.alert_webhook_url:
        coroutines.append(_run("webhook", send_webhook(message, event)))
    else:
        tasks.append(("webhook", False))

    # Email — only dispatch when creds are configured. Reports skipped:no-config
    # otherwise (was previously silently no-op'ing inside send_email and
    # falsely reporting "sent" to the dispatcher).
    if settings.resend_api_key and settings.resend_from_email:
        coroutines.append(_run("email", send_email(message, event)))
    else:
        tasks.append(("email", False))

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
