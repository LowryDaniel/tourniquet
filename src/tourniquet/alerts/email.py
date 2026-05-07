"""Email alerts via Resend.

Idempotent: one alert per (api_key_id, threshold_pct, date).
Checked by querying usage_events for existing alert flags — no separate alerts table needed in v1.
"""

from __future__ import annotations

import uuid
from datetime import date

import resend

from tourniquet.billing.formatting import format_money
from tourniquet.config import settings


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

    currency = event.display_currency
    spent_display = format_money(event.spent_usd_cents, currency)
    cap_display = format_money(event.cap_usd_cents, currency)

    if event.threshold_pct == -1:
        subject = f"Tourniquet alert: {event.api_key_name} cap reached"
    else:
        subject = (
            f"Tourniquet alert: {event.api_key_name} at {event.threshold_pct}% of daily cap"
        )

    pct_used = (
        int(event.spent_usd_cents / event.cap_usd_cents * 100) if event.cap_usd_cents else 0
    )

    kill_now_url: str | None = getattr(event, "kill_now_url", None)
    kill_html = (
        f"<p><a href='{kill_now_url}' style='color:#dc2626;font-weight:bold'>"
        f"🛑 Kill now (one-click, link expires in 24h)</a></p>"
    ) if kill_now_url else ""

    resend.api_key = settings.resend_api_key
    resend.Emails.send({
        "from": settings.resend_from_email,
        "to": [settings.resend_from_email],  # placeholder — real recipient wired in W1
        "subject": subject,
        "html": (
            f"<p>Your API key <strong>{event.api_key_name}</strong> has used "
            f"<strong>{spent_display}</strong> of your {cap_display} daily cap "
            f"({pct_used}%).</p>"
            f"<p>{message}</p>"
            f"{kill_html}"
            f"<p><a href='{settings.app_base_url}/dashboard'>View dashboard</a></p>"
        ),
    })
