"""Telegram bot callback handler.

Receives Telegram Update objects when the user taps an inline button on a
cap-hit notification. Parses callback_data and dispatches to the lift or
kill_now logic.

callback_data formats:
  "lift|<key_id>|<mode>"    — mode: "2x", "ceiling", "ignore"
  "kill_now|<key_id>"       — immediately kill the key (kill_enabled=True, cap clamped)

Auth: X-Telegram-Bot-Api-Secret-Token header — must match settings.telegram_webhook_secret.
Register the webhook with:
  curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
    -d "url=https://your-host/telegram/callback" \
    -d "secret_token=<your-secret>"
"""

from __future__ import annotations

import logging
import uuid as _uuid_mod

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from tourniquet.config import settings
from tourniquet.db import get_session
from tourniquet.models import ApiKey

log = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram")


async def _apply_lift_from_callback(key_id: str, mode: str) -> None:
    """Apply a cap lift directly from a Telegram inline button tap."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)

    async with get_session() as session:
        # Accept UUID prefix (8+ chars) or exact name
        result = await session.execute(select(ApiKey))
        keys = result.scalars().all()

        target: ApiKey | None = None
        for k in keys:
            if k.name == key_id or (len(key_id) >= 8 and str(k.id).startswith(key_id)):
                target = k
                break
        # Also try exact UUID match
        if target is None:
            for k in keys:
                if str(k.id) == key_id:
                    target = k
                    break

        if target is None:
            log.warning("Telegram callback: key %r not found — ignoring", key_id)
            return

        if mode == "ignore":
            log.info("Telegram callback: user chose ignore for key %s", key_id)
            return

        db_key = await session.get(ApiKey, target.id)

        if mode == "2x":
            lifted = int(target.daily_cap_usd_cents * 2)
        elif mode == "ceiling":
            lifted = target.absolute_ceiling_usd_cents
        else:
            log.warning("Unknown Telegram callback mode %r — ignoring", mode)
            return

        # Clamp to ceiling
        lifted = min(lifted, target.absolute_ceiling_usd_cents)

        # Expire at next midnight UTC (coterminous with the daily spend period)
        tomorrow = (now.date() + timedelta(days=1))
        expires_at = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)

        db_key.lifted_cap_usd_cents = lifted
        db_key.lift_expires_at = expires_at
        await session.commit()

        log.info(
            "Telegram callback: lifted cap for %s → %d cents (expires %s)",
            target.name, lifted, expires_at.isoformat(),
        )


async def _apply_kill_now_from_callback(key_id: str) -> None:
    """Apply kill-now directly from a Telegram inline button tap.

    Delegates to the shared _apply_kill_now helper in admin routes.
    Accepts UUID string; silently ignores if key not found.
    """
    from tourniquet.routes.admin import _apply_kill_now

    try:
        key_uuid = _uuid_mod.UUID(key_id)
    except ValueError:
        log.warning("Telegram kill_now callback: invalid UUID %r — ignoring", key_id)
        return

    try:
        name, new_cap = await _apply_kill_now(key_uuid)
        log.info("Telegram kill_now: killed key %s (%s), cap clamped to %d cents", key_id, name, new_cap)
    except Exception as exc:
        log.warning("Telegram kill_now callback failed for key %r: %s", key_id, exc)


@router.post("/callback")
async def telegram_callback(request: Request) -> dict:
    """Telegram sends an Update object when a user taps an inline button.

    Verify it's from our bot via the X-Telegram-Bot-Api-Secret-Token header,
    parse callback_data, dispatch to the lift logic.
    """
    secret = settings.telegram_webhook_secret
    if secret:
        incoming = request.headers.get("x-telegram-bot-api-secret-token", "")
        if incoming != secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    update: dict = await request.json()

    callback_query = update.get("callback_query") or {}
    data: str = callback_query.get("data", "")

    if data.startswith("lift|"):
        parts = data.split("|")
        if len(parts) != 3:
            log.warning("Malformed lift callback_data: %r", data)
            return {"ok": True}
        _, key_id, mode = parts
        await _apply_lift_from_callback(key_id.strip(), mode.strip())

    elif data.startswith("kill_now|"):
        parts = data.split("|")
        if len(parts) != 2:
            log.warning("Malformed kill_now callback_data: %r", data)
            return {"ok": True}
        _, key_id = parts
        await _apply_kill_now_from_callback(key_id.strip())

    else:
        # Not our callback (could be another handler); acknowledge and ignore
        pass

    return {"ok": True}
