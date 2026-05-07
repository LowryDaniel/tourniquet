"""Generic JSON webhook channel."""

from __future__ import annotations

import dataclasses
import logging

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

    raw = dataclasses.asdict(event)  # type: ignore[arg-type]
    # Ensure date is JSON-serialisable
    if "today" in raw and hasattr(raw["today"], "isoformat"):
        raw["today"] = raw["today"].isoformat()

    body = {"message": message, "event": raw}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(settings.alert_webhook_url, json=body)

    if resp.status_code not in range(200, 300):
        log.warning("Webhook returned status %d", resp.status_code)
