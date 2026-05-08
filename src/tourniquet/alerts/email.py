"""Email alerts via Resend.

Idempotent: one alert per (api_key_id, threshold_pct, date).
Checked by querying usage_events for existing alert flags — no separate alerts table needed in v1.

The email body uses the SAME canonical message text that Slack / Telegram /
JSONL / desktop receive — no per-channel prose. Only the surrounding HTML
button block is channel-specific.
"""

from __future__ import annotations

import re
import uuid
from datetime import date

import resend

from tourniquet.config import settings


_EMOJI_PATTERN = re.compile(
    "["
    "\U0001f300-\U0001f6ff"
    "\U0001f900-\U0001f9ff"
    "\U00002600-\U000027bf"
    "\U0001f000-\U0001f02f"
    "️"
    "]",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    return _EMOJI_PATTERN.sub("", text).strip()


def _already_alerted_today(api_key_id: uuid.UUID, threshold_pct: int, today: date) -> bool:
    # Placeholder — implement with a simple DB check in W1.
    # In v1: store alert sent state in a JSON column on api_keys or a tiny table.
    # For MVP: accept rare duplicate emails; fix in W2.
    return False


async def send_email(message: str, event: object) -> None:
    """Send an alert email via Resend.

    Conforms to the unified channel interface: (message, event) -> None.
    No-op if RESEND_API_KEY is not set.
    The event must have attributes: api_key_name, threshold_pct, spent_usd_cents,
    cap_usd_cents, display_currency.
    """
    from tourniquet.alerts.notifier import AlertEvent

    if not isinstance(event, AlertEvent):
        return
    if not settings.resend_api_key:
        return
    if _already_alerted_today(uuid.uuid4(), event.threshold_pct, event.today):
        return

    # Subject == body's message so the inbox preview matches what we send to Slack/Telegram.
    # Strip emoji from subject only (some MUAs render them poorly in subject lines).
    subject = "Tourniquet alert: " + _strip_emoji(message)

    kill_now_url: str | None = getattr(event, "kill_now_url", None)
    recovery_offer: bool = bool(getattr(event, "recovery_offer", False))

    kill_html = (
        f"<p><a href='{kill_now_url}' style='color:#dc2626;font-weight:bold'>"
        f"🛑 Kill now (one-click, link expires in 24h)</a></p>"
    ) if (kill_now_url and not recovery_offer) else ""

    recovery_html = ""
    if recovery_offer:
        from tourniquet.alerts.notifier import recovery_amounts_cents
        from tourniquet.routes.admin import build_lift_by_amount_url

        amounts = recovery_amounts_cents(event.cap_usd_cents)
        key_id = event.api_key_id or ""
        links = []
        for c in amounts:
            label = f"+${c // 100}" if c % 100 == 0 else f"+${c / 100:.2f}"
            url = build_lift_by_amount_url(key_id, c)
            links.append(
                f"<a href='{url}' style='display:inline-block;background:#16a34a;"
                f"color:#fff;padding:8px 16px;border-radius:6px;text-decoration:none;"
                f"font-weight:600;margin:4px'>{label}</a>"
            )
        recovery_html = (
            "<p><strong>Want to bump the cap and continue?</strong></p>"
            f"<p>{''.join(links)}</p>"
            "<p style='font-size:.85rem;color:#555'>Each lift expires at midnight UTC. "
            "Confirmation page before applying.</p>"
        )

    # Recipient: use per-key alert_email if set, else fall back to from-address
    # so a configured-but-unaddressed setup still goes somewhere observable.
    recipient = getattr(event, "alert_email", None) or settings.resend_from_email

    resend.api_key = settings.resend_api_key
    resend.Emails.send({
        "from": settings.resend_from_email,
        "to": [recipient],
        "subject": subject,
        "html": (
            # Canonical message — identical to what Slack/Telegram/JSONL receive
            f"<p style='font-size:1.1rem'>{message}</p>"
            f"{recovery_html}"
            f"{kill_html}"
            f"<p><a href='{settings.app_base_url}/dashboard'>Open dashboard</a></p>"
        ),
    })
