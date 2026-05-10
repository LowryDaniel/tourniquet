"""Slack Socket Mode client.

Receives interactive button events from Slack WITHOUT requiring a public HTTPS
callback URL. Tourniquet opens a WebSocket TO Slack (rather than Slack
reaching in to it) — works behind NAT, on any network.

Started automatically on app startup when SLACK_APP_TOKEN is set.

Setup (one-time, in your Slack app config):
  1. Settings → Socket Mode → Enable
  2. Settings → Basic Information → App-Level Tokens → Generate
     scope: connections:write
     copy the xapp-... token → SLACK_APP_TOKEN in .env
  3. Features → Interactivity & Shortcuts → Enable (no Request URL needed)
  4. Restart `tourniquet start`
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
import uuid as _uuid_mod
from typing import Any

import certifi
import httpx
import websockets

from tourniquet.config import settings


def _ssl_context() -> ssl.SSLContext:
    """SSL context backed by certifi's CA bundle.

    Python.org's Python distribution on macOS ships without system CA
    certificates. When `websockets` connects to Slack's wss:// endpoint to
    open the Socket Mode channel, it checks the server certificate against the
    system store and fails with CERTIFICATE_VERIFY_FAILED. We explicitly pass
    certifi's bundled CA store to SSL context creation to work around this.
    """
    return ssl.create_default_context(cafile=certifi.where())

log = logging.getLogger(__name__)


_BACKOFF_INITIAL_SECONDS = 2
_BACKOFF_MAX_SECONDS = 60
_RECONNECT_GRACEFUL_SECONDS = 5  # Slack rotates connections; we get a 'disconnect'


class SlackSocketClient:
    """Open a Socket Mode WebSocket to Slack and dispatch interactive events."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if not settings.slack_app_token:
            log.info("Slack Socket Mode disabled — SLACK_APP_TOKEN not set")
            return
        if not settings.slack_app_token.startswith("xapp-"):
            log.warning("SLACK_APP_TOKEN must start with 'xapp-' (app-level token, not bot)")
            return
        # Block Kit action buttons require bot-posted messages (chat.postMessage),
        # which need slack_bot_token + slack_channel_id. Without those, the
        # Socket Mode WebSocket would idle without ever receiving an interaction
        # — pointless. Stay dormant until full config is present.
        if not (getattr(settings, "slack_bot_token", "") and getattr(settings, "slack_channel_id", "")):
            log.info(
                "Slack Socket Mode dormant — SLACK_APP_TOKEN set but SLACK_BOT_TOKEN / "
                "SLACK_CHANNEL_ID missing. Webhook + mrkdwn link fallback in use."
            )
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._http = httpx.AsyncClient(timeout=30)
        self._task = asyncio.create_task(self._run())
        log.info("Slack Socket Mode started")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _run(self) -> None:
        backoff = _BACKOFF_INITIAL_SECONDS
        while not self._stop_event.is_set():
            try:
                ws_url = await self._open_connection()
                if not ws_url:
                    raise RuntimeError("apps.connections.open returned no URL")

                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=15,
                    ssl=_ssl_context(),
                ) as ws:
                    backoff = _BACKOFF_INITIAL_SECONDS
                    log.info("Slack Socket connected")
                    await self._handle_socket(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Slack Socket error (%s) — reconnecting in %ds", exc, backoff)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                    return
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)

    async def _open_connection(self) -> str | None:
        assert self._http is not None
        r = await self._http.post(
            "https://slack.com/api/apps.connections.open",
            headers={"Authorization": f"Bearer {settings.slack_app_token}"},
        )
        data = r.json()
        if not data.get("ok"):
            log.warning("apps.connections.open failed: %s", data)
            return None
        return data.get("url")

    async def _handle_socket(self, ws: Any) -> None:
        async for raw in ws:
            if self._stop_event.is_set():
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "hello":
                continue
            if mtype == "disconnect":
                # Slack rotates connections — break out so the outer loop reconnects
                break
            envelope_id = msg.get("envelope_id")
            try:
                if mtype == "interactive":
                    await self._handle_interactive(msg.get("payload") or {})
            except Exception:
                log.exception("Slack Socket dispatch error for %s", mtype)
            # Always ack the envelope, even if dispatch fails
            if envelope_id:
                with contextlib.suppress(Exception):
                    await ws.send(json.dumps({"envelope_id": envelope_id}))

    async def _handle_interactive(self, payload: dict[str, Any]) -> None:
        """Process a block_actions payload from a Slack button tap."""
        actions = payload.get("actions") or []
        if not actions:
            return
        action = actions[0]
        action_id: str = action.get("action_id", "") or ""
        value: str = action.get("value", "") or ""
        # Slack message context for the in-app update
        message = payload.get("message") or {}
        channel = (payload.get("channel") or {}).get("id")
        message_ts = message.get("ts")
        response_url = payload.get("response_url")

        # Use the apply helpers directly so we can stamp source="slack_socket"
        # on every audit row (rather than the generic source the Telegram-side
        # wrappers use).
        import uuid as _uuid_mod_inner

        from tourniquet.alerts.telegram_callbacks import _fire_recovery_alert_for
        from tourniquet.routes.admin import (
            _apply_kill_now,
            _apply_lift,
            _apply_lift_by_amount,
        )

        confirmation: str | None = None
        try:
            # Match by prefix — Slack requires unique action_ids per button,
            # so siblings (lift_2x / lift_ceiling, lift_by_amount_500 / _1000…)
            # share routing via the `value` field which carries `key_id|payload`.
            # Order matters: check `lift_by_amount` before the generic `lift_`.
            if action_id.startswith("lift_by_amount"):
                # value: "<key_id>|<cents>"
                key_id, cents_str = value.split("|", 1)
                cents = int(cents_str)
                if cents == 0:
                    confirmation = "Left alone."
                else:
                    await _apply_lift_by_amount(
                        _uuid_mod_inner.UUID(key_id), cents, source="slack_socket",
                    )
                    confirmation = await _summary_after_bump(key_id, cents)
            elif action_id == "kill_now":
                key_id = value.strip()
                await _apply_kill_now(_uuid_mod_inner.UUID(key_id), source="slack_socket")
                await _fire_recovery_alert_for(key_id)
                confirmation = "🛑 Killed. Cap clamped to today's spend. Recovery alert sent."
            elif action_id.startswith("lift_"):  # lift_2x, lift_ceiling
                # value: "<key_id>|<mode>"
                key_id, mode = value.split("|", 1)
                await _apply_lift(
                    _uuid_mod_inner.UUID(key_id), mode, source="slack_socket",
                )
                confirmation = await _summary_after_lift(key_id, mode)
        except Exception as exc:
            log.exception("Slack interactive dispatch failed for action %s", action_id)
            confirmation = f"⚠️ Action failed: {exc}"

        if confirmation:
            await self._update_message(response_url, confirmation, channel, message_ts)

    async def _update_message(
        self,
        response_url: str | None,
        text: str,
        channel: str | None,
        ts: str | None,
    ) -> None:
        """Replace the Slack message body with a confirmation, removing buttons.

        Prefer the response_url (signed, no auth needed); fall back to chat.update
        with a bot token if we have one.
        """
        assert self._http is not None
        body = {"text": text, "replace_original": True}
        if response_url:
            try:
                await self._http.post(response_url, json=body)
                return
            except Exception as exc:
                log.warning("response_url update failed: %s", exc)
        # No bot token in v0.1; just log if response_url failed
        log.warning("Could not update Slack message (no response_url and no bot token)")


async def _summary_after_lift(key_id: str, mode: str) -> str:
    if mode == "ignore":
        return "Ignored."
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
        return f"✓ Lifted. *{k.name}* cap is now ${cap:.2f} until midnight UTC."


async def _summary_after_bump(key_id: str, cents: int) -> str:
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
        return f"✓ Bumped {amt}. *{k.name}* cap is now ${cap:.2f} until midnight UTC."


# Module-level singleton — managed via lifespan in main.py
socket_client = SlackSocketClient()
