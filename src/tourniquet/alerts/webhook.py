"""Generic JSON webhook channel."""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import httpx

from tourniquet.config import settings

log = logging.getLogger(__name__)


async def send_webhook(message: str, event: object) -> None:
    """POST a JSON payload to the configured webhook URL.

    The event dataclass is serialised via dataclasses.asdict; date fields are
    converted to ISO strings.
    No-op if ALERT_WEBHOOK_URL is not set.
    """
    if not settings.alert_webhook_url:
        return

    raw = dataclasses.asdict(event)  # type: ignore[call-overload]
    # Ensure date is JSON-serialisable
    if "today" in raw and hasattr(raw["today"], "isoformat"):
        raw["today"] = raw["today"].isoformat()

    body: dict[str, Any] = {"message": message, "event": raw}

    # When this is a recovery offer, embed the bump amounts + signed URLs so the
    # downstream automation (Zapier, n8n, HA) can present them as actionable buttons.
    if raw.get("recovery_offer"):
        from tourniquet.alerts.notifier import recovery_amounts_cents
        from tourniquet.routes.admin import build_lift_by_amount_url

        amounts = recovery_amounts_cents(raw.get("cap_usd_cents", 0) or 0)
        key_id = raw.get("api_key_id") or ""
        body["recovery_options"] = [
            {
                "amount_cents": c,
                "label": f"+${c // 100}" if c % 100 == 0 else f"+${c / 100:.2f}",
                "url": build_lift_by_amount_url(key_id, c),
            }
            for c in amounts
        ]

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(settings.alert_webhook_url, json=body)

    if resp.status_code not in range(200, 300):
        log.warning("Webhook returned status %d", resp.status_code)
