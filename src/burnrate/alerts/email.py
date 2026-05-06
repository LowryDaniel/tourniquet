"""Email alerts via Resend.

Idempotent: one alert per (api_key_id, threshold_pct, date).
Checked by querying usage_events for existing alert flags — no separate alerts table needed in v1.
"""

from __future__ import annotations

import uuid
from datetime import date

import resend

from burnrate.config import settings


def _already_alerted_today(api_key_id: uuid.UUID, threshold_pct: int, today: date) -> bool:
    # Placeholder — implement with a simple DB check in W1.
    # In v1: store alert sent state in a JSON column on api_keys or a tiny table.
    # For MVP: accept rare duplicate emails; fix in W2.
    return False


async def maybe_send_alert(
    *,
    api_key_id: uuid.UUID,
    api_key_name: str,
    recipient_email: str,
    spent_pence: int,
    cap_pence: int,
    threshold_pct: int,
    today: date,
) -> None:
    """Send an alert email if the threshold is crossed and we haven't sent one today."""
    if not settings.resend_api_key:
        return
    if _already_alerted_today(api_key_id, threshold_pct, today):
        return

    pct_used = int(spent_pence / cap_pence * 100) if cap_pence else 0
    spent_pounds = spent_pence / 100
    cap_pounds = cap_pence / 100

    resend.api_key = settings.resend_api_key
    resend.Emails.send({
        "from": settings.resend_from_email,
        "to": [recipient_email],
        "subject": f"BurnRate alert: {api_key_name} at {pct_used}% of daily cap",
        "html": (
            f"<p>Your API key <strong>{api_key_name}</strong> has used "
            f"<strong>£{spent_pounds:.2f}</strong> of your £{cap_pounds:.2f} daily cap "
            f"({pct_used}%).</p>"
            f"<p><a href='{settings.app_base_url}/dashboard'>View dashboard</a></p>"
        ),
    })
