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
from datetime import UTC, date, datetime
from typing import Any

from tourniquet.billing.formatting import format_money
from tourniquet.config import settings

log = logging.getLogger(__name__)

# Strong references to background fan_out tasks. asyncio.create_task only
# returns a weak-referenced task; without holding it here the GC can cancel
# the dispatch mid-flight. Tasks discard themselves via add_done_callback.
_pending_tasks: set[asyncio.Task] = set()


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


def _select_threshold(spent_cents: int, cap_cents: int, last_fired_pct: int | None) -> int | None:
    """Decide which threshold (50/80/-1) to fire now, or None if nothing to do.

    Each level fires AT MOST ONCE per key per day. The proxy hot path passes
    `last_fired_pct` from the audit log; we only return a level that's strictly
    higher than what's already fired today.

    Returns:
        -1   when spent ≥ cap and a cap-hit alert hasn't fired yet today
        80   when spent ≥ 80% of cap and the 80 alert hasn't fired
        50   when spent ≥ 50% of cap and the 50 alert hasn't fired
        None otherwise
    """
    if cap_cents <= 0:
        return None
    # Cap hit takes priority — only fire once per day
    if spent_cents >= cap_cents:
        return -1 if last_fired_pct != -1 else None
    # `last_fired_pct == -1` means cap-hit already fired (somehow spend dropped?
    # impossible during normal incrementing, but be defensive). Don't downgrade.
    if last_fired_pct == -1:
        return None
    pct = (spent_cents * 100) // cap_cents
    for level in (80, 50):
        if pct >= level and (last_fired_pct is None or last_fired_pct < level):
            return level
    return None


async def _last_fired_threshold_today(api_key_id: Any, today: date, session: Any) -> int | None:
    """Look at the audit log for the most recent threshold fired today.

    Returns the `threshold_pct` from the latest `alert_fired` action recorded
    today, or None if no alert has fired yet. Used by `_maybe_fire_threshold_alert`
    to avoid re-firing the same level twice.
    """
    from sqlalchemy import desc, select

    from tourniquet.models import ApiKeyAction

    today_start = datetime(today.year, today.month, today.day, tzinfo=UTC)
    result = await session.execute(
        select(ApiKeyAction)
        .where(
            ApiKeyAction.api_key_id == api_key_id,
            ApiKeyAction.action == "alert_fired",
            ApiKeyAction.created_at >= today_start,
        )
        .order_by(desc(ApiKeyAction.created_at))
        .limit(1)
    )
    last = result.scalar_one_or_none()
    if last is None:
        return None
    details = last.details or {}
    return details.get("threshold_pct")


async def maybe_fire_threshold_alert(
    api_key: Any,
    spent_cents: int,
    cap_cents: int,
    today: date,
    *,
    kill_enabled: bool,
    session: Any,
) -> int | None:
    """Fire a threshold alert if spend has crossed a new level today.

    Called from the proxy hot path right after add_spend(). Designed to be:
      • idempotent — records the audit row before dispatching, so a retry
        won't double-alert even if fan_out is interrupted
      • non-blocking — alert delivery runs as a background asyncio task so
        the proxy response isn't held up by Slack/Telegram round-trips
      • silent on failure — any error (including audit failure) is logged
        and swallowed; the proxy must not break because alerts misbehaved

    Returns the threshold level fired (-1 / 80 / 50) or None if no alert
    fired. Returned value is mostly for tests.
    """
    try:
        last_fired = await _last_fired_threshold_today(api_key.id, today, session)
        threshold = _select_threshold(spent_cents, cap_cents, last_fired)
        if threshold is None:
            return None

        # Build the event. Recovery offer (post-kill +$N buttons) only makes
        # sense when kill_enabled=True AND we just hit the cap — otherwise
        # the key isn't actually blocked, so "want to bump to continue?" is
        # the wrong prompt.
        event = AlertEvent(
            api_key_name=api_key.name,
            threshold_pct=threshold,
            spent_usd_cents=spent_cents,
            cap_usd_cents=cap_cents,
            display_currency=settings.display_currency,
            today=today,
            api_key_id=str(api_key.id),
            alert_email=getattr(api_key, "alert_email", None),
            recovery_offer=(threshold == -1 and kill_enabled),
        )

        # Record FIRST so a future retry sees the level and doesn't re-fire.
        # The proxy commits this audit row alongside the spend write, so they
        # land atomically.
        from tourniquet.audit import ACTION_ALERT_FIRED, SOURCE_PROXY, record_action
        await record_action(
            session, api_key.id, ACTION_ALERT_FIRED, SOURCE_PROXY,
            f"Alert fired at {'cap-hit' if threshold == -1 else f'{threshold}%'} "
            f"(spent ${spent_cents / 100:.2f} / ${cap_cents / 100:.2f})",
            details={
                "threshold_pct": threshold,
                "spent_cents": spent_cents,
                "cap_cents": cap_cents,
                "kill_enabled": kill_enabled,
            },
        )

        # Background dispatch — proxy returns to the user without waiting.
        # `fan_out` itself returns per-channel statuses but we don't need them
        # here; logs from each channel will surface failures.
        async def _dispatch():
            try:
                await fan_out(event, kill_enabled=kill_enabled)
            except Exception:
                log.exception("Background alert fan_out failed for key %s", api_key.id)

        t = asyncio.create_task(_dispatch())
        _pending_tasks.add(t)
        t.add_done_callback(_pending_tasks.discard)
        return threshold
    except Exception:
        # Never let an alert-path failure break the proxy. The cap-check has
        # already happened — the request either passed or got 402'd before we
        # got here.
        log.exception("maybe_fire_threshold_alert failed for key %s", getattr(api_key, "id", "?"))
        return None


async def fan_out(event: AlertEvent, *, kill_enabled: bool = True) -> dict[str, str]:
    """Send the alert to every configured channel concurrently (fan-out pattern).

    Each channel runs as its own asyncio task and reports delivery status
    independently. A failure in one channel (e.g., Slack is down) does NOT
    block the others (e.g., email still goes out). This resilience is why we
    fan out instead of bailing on first error.

    Pass kill_enabled=False when the key is in monitor mode — this embeds a
    signed kill-now URL in the event, surfaced in all channels that support
    interactive buttons (Slack, Telegram, etc.).

    Returns a dict mapping channel name to one of:
      "sent" | "skipped:no-config" | "error:<message>"

    Never raises — all exceptions are caught, logged, and returned as errors.
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
    desktop_enabled = bool(
        settings.enable_mac_notifications
        or getattr(settings, "enable_desktop_notifications", False)
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
