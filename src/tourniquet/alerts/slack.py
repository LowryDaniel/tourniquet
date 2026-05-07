"""Slack incoming-webhook channel."""

from __future__ import annotations

import logging

import httpx

from tourniquet.config import settings

log = logging.getLogger(__name__)


async def send_slack(message: str, event: object = None) -> None:
    """POST message to the configured Slack webhook URL.

    No-op if SLACK_WEBHOOK_URL is not set.
    Appends a 🛑 Kill now button block when event.kill_now_url is set.
    """
    if not settings.slack_webhook_url:
        return

    kill_now_url: str | None = getattr(event, "kill_now_url", None) if event is not None else None

    if kill_now_url:
        payload = {
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": message},
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "🛑 Kill now"},
                            "url": kill_now_url,
                            "style": "danger",
                        }
                    ],
                },
            ]
        }
    else:
        payload = {"text": message}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(settings.slack_webhook_url, json=payload)

    if resp.status_code != 200:
        log.warning("Slack webhook returned status %d", resp.status_code)
