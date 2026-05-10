"""Telegram bot callback handler.

Receives Telegram Update objects when the user taps an inline button on a
cap-hit notification. Parses callback_data and dispatches to the lift or
kill_now logic.

callback_data formats:
  "lift|<key_id>|<mode>"             — mode: "2x", "ceiling", "ignore"
  "kill_now|<key_id>"                — immediately kill the key (kill_enabled=True, cap clamped)
  "lift_by_amount|<key_id>|<cents>"  — bump cap by the given USD cent amount; 0 = leave it

Auth: X-Telegram-Bot-Api-Secret-Token header — must match settings.telegram_webhook_secret.
Register the webhook with:
  curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
    -d "url=https://your-host/telegram/callback" \
    -d "secret_token=<your-secret>"
"""

from __future__ import annotations

import logging
import uuid as _uuid_mod
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from tourniquet.config import settings
from tourniquet.db import get_session
from tourniquet.models import ApiKey

log = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram")


async def _apply_lift_from_callback(key_id: str, mode: str) -> None:
    """Apply a cap lift from a Telegram inline button tap.

    Resolves the key (UUID, prefix, or name) and delegates to the shared
    `_apply_lift` helper in admin routes — which records the audit row.
    """
    from tourniquet.routes.admin import _apply_lift

    async with get_session() as session:
        # Accept UUID prefix (8+ chars) or exact name
        result = await session.execute(select(ApiKey))
        keys = result.scalars().all()

        target: ApiKey | None = None
        for k in keys:
            if k.name == key_id or (len(key_id) >= 8 and str(k.id).startswith(key_id)):
                target = k
                break
        if target is None:
            for k in keys:
                if str(k.id) == key_id:
                    target = k
                    break
        if target is None:
            log.warning("Telegram callback: key %r not found — ignoring", key_id)
            return

    name = await _apply_lift(target.id, mode, source="telegram_poll")
    if name:
        log.info("Telegram callback: lift %s for key %s applied", mode, name)


async def _apply_lift_by_amount_from_callback(key_id: str, cents: int) -> None:
    """Apply a one-tap +$N recovery lift from a Telegram inline button tap.

    Delegates to the shared `_apply_lift_by_amount` helper in admin routes.
    `cents == 0` is treated as "leave it" — no-op.
    """
    if cents == 0:
        log.info("Telegram lift_by_amount: user chose 'Leave it' for key %s", key_id)
        return

    from tourniquet.routes.admin import _apply_lift_by_amount

    try:
        key_uuid = _uuid_mod.UUID(key_id)
    except ValueError:
        log.warning("Telegram lift_by_amount: invalid UUID %r — ignoring", key_id)
        return

    try:
        name, new_lifted, ceiling_clamped = await _apply_lift_by_amount(
            key_uuid, cents, source="telegram_poll",
        )
        log.info(
            "Telegram lift_by_amount: bumped %s by %d cents → cap %d cents%s",
            name, cents, new_lifted, " (ceiling-clamped)" if ceiling_clamped else "",
        )
    except Exception as exc:
        log.warning("Telegram lift_by_amount callback failed for key %r: %s", key_id, exc)


async def _fire_recovery_alert_for(key_id: str) -> None:
    """Fire a recovery offer alert (best-effort) after a Telegram-initiated kill."""
    try:
        key_uuid = _uuid_mod.UUID(key_id)
    except ValueError:
        return
    from tourniquet.routes.admin import _fire_recovery_alert
    try:
        async with get_session() as session:
            key = await session.get(ApiKey, key_uuid)
            if key is None:
                return
            await _fire_recovery_alert(key.id, key.name, key.daily_cap_usd_cents)
    except Exception as exc:
        log.warning("Recovery alert dispatch failed for %s: %s", key_id, exc)


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
        name, new_cap = await _apply_kill_now(key_uuid, source="telegram_poll")
        log.info(
            "Telegram kill_now: killed key %s (%s), cap clamped to %d cents",
            key_id,
            name,
            new_cap,
        )
    except Exception as exc:
        log.warning("Telegram kill_now callback failed for key %r: %s", key_id, exc)


@router.post("/callback")
async def telegram_callback(request: Request) -> dict[str, Any]:
    """Telegram sends an Update object when a user taps an inline button.

    Verify it's from our bot via the X-Telegram-Bot-Api-Secret-Token header,
    parse callback_data, dispatch to the lift logic.
    """
    secret = settings.telegram_webhook_secret
    if secret:
        incoming = request.headers.get("x-telegram-bot-api-secret-token", "")
        if incoming != secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    update: dict[str, Any] = await request.json()

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
        # After a kill via Telegram, fire a recovery offer so the user can
        # one-tap bump if they actually need to keep going.
        await _fire_recovery_alert_for(key_id.strip())

    elif data.startswith("lift_by_amount|"):
        parts = data.split("|")
        if len(parts) != 3:
            log.warning("Malformed lift_by_amount callback_data: %r", data)
            return {"ok": True}
        _, key_id, cents_str = parts
        try:
            cents = int(cents_str)
        except ValueError:
            log.warning("Non-integer cents in lift_by_amount callback_data: %r", data)
            return {"ok": True}
        await _apply_lift_by_amount_from_callback(key_id.strip(), cents)

    else:
        # Not our callback (could be another handler); acknowledge and ignore
        pass

    return {"ok": True}
