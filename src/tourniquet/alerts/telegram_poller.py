"""Telegram long-polling client.

Receives callback_query updates from Telegram WITHOUT requiring a webhook /
public URL. Standard self-hosted bot pattern: open a long-poll connection to
Telegram's servers and pull updates every few seconds.

Trade-offs vs webhook:
  - 1-3s latency on action vs sub-second for webhook
  - Constant outbound HTTPS connection while running
  - Auto-reconnects on network blips
  + Zero infra requirement — works behind NAT, on any network

Started automatically on app startup when TELEGRAM_BOT_TOKEN is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid as _uuid_mod
from typing import Any

import httpx

from tourniquet.config import settings

log = logging.getLogger(__name__)


_POLL_TIMEOUT_SECONDS = 25  # Telegram caps at 50; 25 is the polite default
_BACKOFF_INITIAL_SECONDS = 2
_BACKOFF_MAX_SECONDS = 60


class TelegramPoller:
    """Long-poll getUpdates and dispatch callback_query events.

    Tracks the offset to ack updates after dispatch.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._client: httpx.AsyncClient | None = None
        self._offset = 0  # next update_id to fetch

    async def start(self) -> None:
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            log.info("Telegram polling disabled — TELEGRAM_BOT_TOKEN/CHAT_ID not set")
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._client = httpx.AsyncClient(timeout=_POLL_TIMEOUT_SECONDS + 10)
        self._task = asyncio.create_task(self._run())
        log.info("Telegram polling started (chat_id=%s)", settings.telegram_chat_id)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        backoff = _BACKOFF_INITIAL_SECONDS
        # On first start, drop any pending updates older than the bot's startup —
        # don't replay tap events the user did days ago.
        await self._drain_initial()

        while not self._stop_event.is_set():
            try:
                updates = await self._fetch_updates()
                backoff = _BACKOFF_INITIAL_SECONDS  # reset on success
                for update in updates:
                    await self._dispatch(update)
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Telegram poll error (%s) — retrying in %ds", exc, backoff)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                    return
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)

    async def _drain_initial(self) -> None:
        """On startup, skip any backlog of old updates so we only see future taps.

        Without this, if Tourniquet restarted and the Telegram bot received taps
        while it was down, we'd replay those old taps when we come back online
        — potentially re-triggering stale cap lifts. By advancing the offset past
        all existing updates on startup (offset=-1, timeout=0 gets the latest),
        we ensure we only dispatch tap events that arrive AFTER the poller starts.
        """
        try:
            resp = await self._call("getUpdates", offset=-1, timeout=0)
            if not resp.get("ok"):
                return
            results = resp.get("result", [])
            if results:
                self._offset = int(results[-1]["update_id"]) + 1
        except Exception:
            pass

    async def _fetch_updates(self) -> list[dict[str, Any]]:
        resp = await self._call(
            "getUpdates",
            offset=self._offset,
            timeout=_POLL_TIMEOUT_SECONDS,
            allowed_updates=["callback_query"],
        )
        if not resp.get("ok"):
            return []
        return resp.get("result", []) or []

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        assert self._client is not None
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
        r = await self._client.post(url, json=kwargs)
        return r.json()

    async def _answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        """Dismiss the loading spinner on the user's button after we've processed the tap."""
        try:
            await self._call("answerCallbackQuery", callback_query_id=callback_query_id, text=(text or "")[:200])
        except Exception as exc:
            log.warning("answerCallbackQuery failed: %s", exc)

    async def _edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        """Replace the original alert with a confirmation, removing buttons."""
        try:
            await self._call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                reply_markup={"inline_keyboard": []},
            )
        except Exception as exc:
            log.warning("editMessageText failed: %s", exc)

    async def _dispatch(self, update: dict[str, Any]) -> None:
        """Route one Telegram update to the appropriate action handler."""
        cq = update.get("callback_query") or {}
        if not cq:
            return  # We only registered for callback_query; defensive.
        data: str = cq.get("data", "")
        cq_id: str = cq.get("id", "")
        message = cq.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")

        if not data:
            await self._answer_callback_query(cq_id)
            return

        # Lazy imports — avoids circular import on module load
        from tourniquet.alerts.telegram_callbacks import (
            _apply_kill_now_from_callback,
            _apply_lift_by_amount_from_callback,
            _apply_lift_from_callback,
            _fire_recovery_alert_for,
        )

        confirmation: str | None = None
        try:
            if data.startswith("lift|"):
                _, key_id, mode = data.split("|", 2)
                await _apply_lift_from_callback(key_id.strip(), mode.strip())
                confirmation = await _summary_after_lift(key_id.strip(), mode.strip())

            elif data.startswith("kill_now|"):
                _, key_id = data.split("|", 1)
                await _apply_kill_now_from_callback(key_id.strip())
                await _fire_recovery_alert_for(key_id.strip())
                confirmation = "🛑 Killed. Cap clamped to today's spend. Recovery alert sent."

            elif data.startswith("lift_by_amount|"):
                _, key_id, cents_str = data.split("|", 2)
                cents = int(cents_str)
                await _apply_lift_by_amount_from_callback(key_id.strip(), cents)
                if cents == 0:
                    confirmation = "Left alone. Tourniquet will alert you again at the next threshold."
                else:
                    confirmation = await _summary_after_bump(key_id.strip(), cents)

            else:
                # Not our callback — silently ack
                pass
        except Exception as exc:
            log.exception("Callback dispatch failed for %r", data)
            confirmation = f"⚠️ Action failed: {exc}"

        # Always ack the callback so the user's button stops spinning.
        await self._answer_callback_query(cq_id, "✓" if confirmation else None)
        if confirmation and chat_id and message_id:
            await self._edit_message_text(chat_id, message_id, confirmation)


async def _summary_after_lift(key_id: str, mode: str) -> str:
    """Look up the key's current state and produce a one-line confirmation."""
    if mode == "ignore":
        return "Ignored. Tourniquet will alert you again at the next threshold."
    try:
        key_uuid = _uuid_mod.UUID(key_id)
    except ValueError:
        return "✓ Lifted."
    from sqlalchemy import select

    from tourniquet.db import get_session
    from tourniquet.models import ApiKey
    async with get_session() as s:
        k = (await s.execute(select(ApiKey).where(ApiKey.id == key_uuid))).scalar_one_or_none()
        if not k:
            return "✓ Lifted."
        cap = (k.lifted_cap_usd_cents or k.daily_cap_usd_cents) / 100
        return f"✓ Lifted. <b>{k.name}</b> cap is now ${cap:.2f} until midnight UTC."


async def _summary_after_bump(key_id: str, cents: int) -> str:
    """Build a confirmation message describing the new cap after a +$N bump."""
    try:
        key_uuid = _uuid_mod.UUID(key_id)
    except ValueError:
        return f"✓ Bumped by ${cents / 100:.2f}."
    from sqlalchemy import select

    from tourniquet.db import get_session
    from tourniquet.models import ApiKey
    async with get_session() as s:
        k = (await s.execute(select(ApiKey).where(ApiKey.id == key_uuid))).scalar_one_or_none()
        if not k:
            return f"✓ Bumped by ${cents / 100:.2f}."
        cap = (k.lifted_cap_usd_cents or k.daily_cap_usd_cents) / 100
        amt = f"${cents // 100}" if cents % 100 == 0 else f"${cents / 100:.2f}"
        return f"✓ Bumped {amt}. <b>{k.name}</b> cap is now ${cap:.2f} until midnight UTC."


# Module-level singleton — managed via lifespan in main.py
poller = TelegramPoller()
