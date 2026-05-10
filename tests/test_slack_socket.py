"""Tests for the Slack Socket Mode client."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tourniquet.alerts.slack_socket import SlackSocketClient


@pytest.mark.asyncio
async def test_socket_skips_start_when_token_missing():
    """No SLACK_APP_TOKEN → socket client is a no-op."""
    c = SlackSocketClient()
    with patch("tourniquet.config.settings.slack_app_token", ""):
        await c.start()
    assert c._task is None


@pytest.mark.asyncio
async def test_socket_rejects_non_xapp_token():
    """A SLACK_APP_TOKEN that isn't an xapp- prefix is rejected with a warning."""
    c = SlackSocketClient()
    with patch("tourniquet.config.settings.slack_app_token", "xoxb-not-app-level"):
        await c.start()
    assert c._task is None


@pytest.mark.asyncio
async def test_handle_interactive_routes_lift_by_amount():
    """An lift_by_amount block_actions payload dispatches to the +$N handler."""
    c = SlackSocketClient()
    c._http = AsyncMock()
    payload = {
        "actions": [
            {
                "action_id": "lift_by_amount_500",
                "value": "11111111-1111-1111-1111-111111111111|500",
            }
        ],
        "channel": {"id": "C123"},
        "message": {"ts": "1234.5"},
        "response_url": "https://hooks.slack.com/actions/...",
    }
    with (
        patch(
            "tourniquet.routes.admin._apply_lift_by_amount",
            new_callable=AsyncMock,
        ) as mock_bump,
        patch(
            "tourniquet.alerts.slack_socket._summary_after_bump",
            new_callable=AsyncMock,
            return_value="✓ Bumped",
        ),
    ):
        await c._handle_interactive(payload)
    # Slack handler now calls admin._apply_lift_by_amount directly with source="slack_socket"
    # so the audit log records who triggered the action.
    import uuid as _uuid
    mock_bump.assert_awaited_once_with(
        _uuid.UUID("11111111-1111-1111-1111-111111111111"),
        500,
        source="slack_socket",
    )


@pytest.mark.asyncio
async def test_handle_interactive_routes_kill_then_fires_recovery():
    """A kill_now action runs kill + fires recovery alert."""
    c = SlackSocketClient()
    c._http = AsyncMock()
    payload = {
        "actions": [
            {"action_id": "kill_now", "value": "11111111-1111-1111-1111-111111111111"}
        ],
        "channel": {"id": "C123"},
        "message": {"ts": "1234.5"},
        "response_url": "https://hooks.slack.com/actions/...",
    }
    with (
        patch(
            "tourniquet.routes.admin._apply_kill_now",
            new_callable=AsyncMock,
        ) as mock_kill,
        patch(
            "tourniquet.alerts.telegram_callbacks._fire_recovery_alert_for",
            new_callable=AsyncMock,
        ) as mock_recovery,
    ):
        await c._handle_interactive(payload)
    mock_kill.assert_awaited_once()
    # First positional arg = key UUID, source kwarg = "slack_socket"
    assert mock_kill.call_args.kwargs.get("source") == "slack_socket"
    mock_recovery.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_interactive_routes_lift_mode():
    """A 'lift' action with mode=2x dispatches to the lift handler."""
    c = SlackSocketClient()
    c._http = AsyncMock()
    payload = {
        "actions": [
            {
                "action_id": "lift_2x",
                "value": "11111111-1111-1111-1111-111111111111|2x",
            }
        ],
        "channel": {"id": "C123"},
        "message": {"ts": "1234.5"},
        "response_url": "https://hooks.slack.com/actions/...",
    }
    with (
        patch(
            "tourniquet.routes.admin._apply_lift",
            new_callable=AsyncMock,
        ) as mock_lift,
        patch(
            "tourniquet.alerts.slack_socket._summary_after_lift",
            new_callable=AsyncMock,
            return_value="✓ Lifted",
        ),
    ):
        await c._handle_interactive(payload)
    import uuid as _uuid
    mock_lift.assert_awaited_once_with(
        _uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "2x",
        source="slack_socket",
    )


@pytest.mark.asyncio
async def test_send_slack_uses_bot_post_when_fully_configured(monkeypatch):
    """When SLACK_APP_TOKEN + SLACK_BOT_TOKEN + SLACK_CHANNEL_ID all set,
    chat.postMessage is called with Block Kit and the webhook is NOT touched.
    """
    from datetime import date

    import respx
    from httpx import Response

    from tourniquet.alerts.notifier import AlertEvent
    from tourniquet.alerts.slack import send_slack

    event = AlertEvent(
        api_key_name="test", threshold_pct=80,
        spent_usd_cents=400, cap_usd_cents=500,
        display_currency="USD", today=date(2026, 5, 7),
        api_key_id="abcd-1234",
    )
    with respx.mock(assert_all_called=False) as mock:
        bot_route = mock.post("https://slack.com/api/chat.postMessage").mock(
            return_value=Response(200, json={"ok": True, "channel": "C1", "ts": "1.1"})
        )
        webhook_route = mock.post("https://hooks.slack.com/webhook").mock(
            return_value=Response(200)
        )
        with (
            patch("tourniquet.config.settings.slack_app_token", "xapp-1-foo"),
            patch("tourniquet.config.settings.slack_bot_token", "xoxb-bar"),
            patch("tourniquet.config.settings.slack_channel_id", "C1"),
            patch("tourniquet.config.settings.slack_webhook_url", "https://hooks.slack.com/webhook"),
        ):
            await send_slack("hello", event)

        assert bot_route.called, "chat.postMessage should be called in bot-post mode"
        assert not webhook_route.called, (
            "Webhook must NOT be called when bot-post mode is active (avoids duplicates)"
        )
        # Inspect the bot-post payload — must have Block Kit + channel + bearer auth
        request = bot_route.calls[0].request
        assert request.headers.get("Authorization", "").startswith("Bearer xoxb-")
        body = request.read().decode()
        assert "C1" in body  # channel
        assert "action_id" in body  # block kit actions present
        assert "blocks" in body


@pytest.mark.asyncio
async def test_send_slack_falls_back_to_webhook_when_bot_partially_configured(monkeypatch):
    """If only SLACK_APP_TOKEN is set without bot+channel, route to webhook
    fallback (mrkdwn links). Avoids 400-from-Block-Kit on webhook path."""
    from datetime import date

    import respx
    from httpx import Response

    from tourniquet.alerts.notifier import AlertEvent
    from tourniquet.alerts.slack import send_slack

    event = AlertEvent(
        api_key_name="test", threshold_pct=80,
        spent_usd_cents=400, cap_usd_cents=500,
        display_currency="USD", today=date(2026, 5, 7),
        api_key_id="abcd-1234",
    )
    with respx.mock(assert_all_called=False) as mock:
        bot_route = mock.post("https://slack.com/api/chat.postMessage").mock(
            return_value=Response(200, json={"ok": True})
        )
        webhook_route = mock.post("https://hooks.slack.com/webhook").mock(return_value=Response(200))
        with (
            patch("tourniquet.config.settings.slack_app_token", "xapp-1-foo"),
            patch("tourniquet.config.settings.slack_bot_token", ""),  # missing!
            patch("tourniquet.config.settings.slack_channel_id", ""),  # missing!
            patch("tourniquet.config.settings.slack_webhook_url", "https://hooks.slack.com/webhook"),
        ):
            await send_slack("hello", event)

        assert webhook_route.called, "Webhook must be used when bot-post not fully configured"
        assert not bot_route.called, "Bot post must not run without bot_token + channel_id"


def test_build_action_payload_recovery_uses_block_kit_buttons():
    """Recovery offer renders 3 primary buttons with action_id=lift_by_amount."""
    from datetime import date

    from tourniquet.alerts.notifier import AlertEvent
    from tourniquet.alerts.slack import _build_action_payload

    event = AlertEvent(
        api_key_name="test",
        threshold_pct=-1,
        spent_usd_cents=400,
        cap_usd_cents=400,
        display_currency="USD",
        today=date(2026, 5, 7),
        api_key_id="abcd",
        recovery_offer=True,
    )
    payload = _build_action_payload(
        "msg", event, recovery_offer=True, kill_now_url=None, key_id="abcd"
    )
    elements = payload["blocks"][1]["elements"]
    assert len(elements) == 3
    # Each button needs a UNIQUE action_id (Slack rejects duplicates as invalid_blocks)
    # but they all share the lift_by_amount routing prefix.
    action_ids = [e["action_id"] for e in elements]
    assert len(set(action_ids)) == 3, f"action_ids must be unique, got {action_ids}"
    for e in elements:
        assert e["action_id"].startswith("lift_by_amount"), e["action_id"]
        assert e["value"].startswith("abcd|")
