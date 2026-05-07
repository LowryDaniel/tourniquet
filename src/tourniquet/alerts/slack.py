"""Slack incoming-webhook channel."""

from __future__ import annotations

import logging

import httpx

from tourniquet.config import settings

log = logging.getLogger(__name__)


async def send_slack(message: str) -> None:
    """POST message to the configured Slack webhook URL.

    No-op if SLACK_WEBHOOK_URL is not set.
    The webhook URL is treated as a secret and never logged.
    """
    if not settings.slack_webhook_url:
        return

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(settings.slack_webhook_url, json={"text": message})

    if resp.status_code != 200:
        log.warning("Slack webhook returned status %d", resp.status_code)
