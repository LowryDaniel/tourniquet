"""Tests for the Telegram long-polling client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tourniquet.alerts.telegram_poller import TelegramPoller


def _mock_telegram_client() -> AsyncMock:
    """Build a Mock for httpx.AsyncClient that won't leak unawaited coroutines.

    httpx.Response.json() is sync — without this scaffold, AsyncMock would
    return an unawaited coroutine from .json() and TelegramPoller._call would
    leak it on every dispatch (visible as 'coroutine was never awaited'
    RuntimeWarning).
    """
    client = AsyncMock()
    resp = MagicMock()
    resp.json = MagicMock(return_value={"ok": True, "result": []})
    client.post = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_poller_skips_start_when_token_missing():
    """No bot token → poller is a no-op (doesn't crash, doesn't spin a task)."""
    p = TelegramPoller()
    with patch("tourniquet.config.settings.telegram_bot_token", ""):
        await p.start()
    assert p._task is None


@pytest.mark.asyncio
async def test_dispatch_routes_lift_by_amount():
    """A 'lift_by_amount|<id>|<cents>' callback dispatches to the right handler."""
    p = TelegramPoller()
    p._client = _mock_telegram_client()  # used by _answer_callback_query / _edit_message_text
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb-id",
            "data": "lift_by_amount|11111111-1111-1111-1111-111111111111|500",
            "message": {"message_id": 42, "chat": {"id": 999}},
        },
    }
    with (
        patch("tourniquet.alerts.telegram_callbacks._apply_lift_by_amount_from_callback", new_callable=AsyncMock) as mock_bump,
        patch("tourniquet.alerts.telegram_poller._summary_after_bump", new_callable=AsyncMock, return_value="✓ Bumped"),
    ):
        await p._dispatch(update)
    mock_bump.assert_awaited_once_with("11111111-1111-1111-1111-111111111111", 500)


@pytest.mark.asyncio
async def test_dispatch_routes_kill_now_then_fires_recovery():
    """A 'kill_now|<id>' callback applies the kill AND fires the recovery alert."""
    p = TelegramPoller()
    p._client = _mock_telegram_client()
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb-id",
            "data": "kill_now|11111111-1111-1111-1111-111111111111",
            "message": {"message_id": 42, "chat": {"id": 999}},
        },
    }
    with (
        patch("tourniquet.alerts.telegram_callbacks._apply_kill_now_from_callback", new_callable=AsyncMock) as mock_kill,
        patch("tourniquet.alerts.telegram_callbacks._fire_recovery_alert_for", new_callable=AsyncMock) as mock_recovery,
    ):
        await p._dispatch(update)
    mock_kill.assert_awaited_once()
    mock_recovery.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_lift_by_amount_zero_means_leave_it():
    """cents=0 maps to 'Leave it' — no-op handler call but still acks."""
    p = TelegramPoller()
    p._client = _mock_telegram_client()
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb-id",
            "data": "lift_by_amount|22222222-2222-2222-2222-222222222222|0",
            "message": {"message_id": 5, "chat": {"id": 1}},
        },
    }
    with patch(
        "tourniquet.alerts.telegram_callbacks._apply_lift_by_amount_from_callback",
        new_callable=AsyncMock,
    ) as mock_bump:
        await p._dispatch(update)
    # Handler is called with cents=0 and no-ops internally
    mock_bump.assert_awaited_once_with("22222222-2222-2222-2222-222222222222", 0)


@pytest.mark.asyncio
async def test_dispatch_unknown_callback_type_is_silent():
    """Unrecognised callback_data is acked but nothing else happens — defensive."""
    p = TelegramPoller()
    p._client = _mock_telegram_client()
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb-id",
            "data": "unrelated|payload|here",
            "message": {"message_id": 1, "chat": {"id": 1}},
        },
    }
    with patch(
        "tourniquet.alerts.telegram_callbacks._apply_lift_by_amount_from_callback",
        new_callable=AsyncMock,
    ) as mock_bump:
        await p._dispatch(update)
    mock_bump.assert_not_called()
