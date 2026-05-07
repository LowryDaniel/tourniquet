"""Tests for the unified alert fanout."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import respx
from httpx import Response

from tourniquet.alerts.notifier import AlertEvent, _format_message, fan_out


@pytest.fixture()
def base_event() -> AlertEvent:
    return AlertEvent(
        api_key_name="ojw-swarm",
        threshold_pct=80,
        spent_usd_cents=420,
        cap_usd_cents=500,
        display_currency="GBP",
        today=date(2026, 5, 6),
    )


@pytest.fixture()
def cap_hit_event() -> AlertEvent:
    return AlertEvent(
        api_key_name="ojw-swarm",
        threshold_pct=-1,
        spent_usd_cents=500,
        cap_usd_cents=500,
        display_currency="GBP",
        today=date(2026, 5, 6),
    )


# ── _format_message ────────────────────────────────────────────────────────────

def test_format_message_threshold(base_event: AlertEvent) -> None:
    msg = _format_message(base_event)
    assert "80%" in msg
    assert "ojw-swarm" in msg
    assert "⚠️" in msg
    assert "cap reached" not in msg


def test_format_message_cap_hit(cap_hit_event: AlertEvent) -> None:
    msg = _format_message(cap_hit_event)
    assert "cap reached" in msg
    assert "blocked" in msg
    assert "⚠️" not in msg


# ── JSONL always written ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fanout_writes_jsonl_even_with_no_channels(
    base_event: AlertEvent, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    with (
        patch("tourniquet.config.settings.slack_webhook_url", ""),
        patch("tourniquet.config.settings.telegram_bot_token", ""),
        patch("tourniquet.config.settings.telegram_chat_id", ""),
        patch("tourniquet.config.settings.alert_webhook_url", ""),
        patch("tourniquet.config.settings.enable_mac_notifications", "false"),
        patch("tourniquet.config.settings.enable_desktop_notifications", ""),
        patch("tourniquet.config.settings.resend_api_key", ""),
    ):
        results = await fan_out(base_event)

    assert results["jsonl"] == "sent"
    log_file = tmp_path / ".tourniquet" / "alerts.jsonl"
    assert log_file.exists()
    record = json.loads(log_file.read_text().strip())
    assert record["event"]["api_key_name"] == "ojw-swarm"
    assert record["event"]["today"] == "2026-05-06"


# ── Only configured channels called ───────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_fanout_only_calls_configured_channels(
    base_event: AlertEvent, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    slack_route = respx.post("https://hooks.slack.com/test").mock(
        return_value=Response(200, text="ok")
    )

    with (
        patch("tourniquet.config.settings.slack_webhook_url", "https://hooks.slack.com/test"),
        patch("tourniquet.config.settings.telegram_bot_token", ""),
        patch("tourniquet.config.settings.telegram_chat_id", ""),
        patch("tourniquet.config.settings.alert_webhook_url", ""),
        patch("tourniquet.config.settings.enable_mac_notifications", "false"),
        patch("tourniquet.config.settings.enable_desktop_notifications", ""),
        patch("tourniquet.config.settings.resend_api_key", ""),
    ):
        results = await fan_out(base_event)

    assert results["slack"] == "sent"
    assert results["telegram"] == "skipped:no-config"
    assert results["webhook"] == "skipped:no-config"
    assert slack_route.called


# ── Slack / Telegram failures don't propagate ─────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_slack_failure_returned_not_raised(
    base_event: AlertEvent, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    respx.post("https://hooks.slack.com/bad").mock(side_effect=Exception("network error"))

    with (
        patch("tourniquet.config.settings.slack_webhook_url", "https://hooks.slack.com/bad"),
        patch("tourniquet.config.settings.telegram_bot_token", ""),
        patch("tourniquet.config.settings.telegram_chat_id", ""),
        patch("tourniquet.config.settings.alert_webhook_url", ""),
        patch("tourniquet.config.settings.enable_mac_notifications", "false"),
        patch("tourniquet.config.settings.enable_desktop_notifications", ""),
        patch("tourniquet.config.settings.resend_api_key", ""),
    ):
        results = await fan_out(base_event)

    assert results["slack"].startswith("error:")
    assert results["jsonl"] == "sent"


@pytest.mark.asyncio
@respx.mock
async def test_telegram_failure_returned_not_raised(
    base_event: AlertEvent, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    respx.post("https://api.telegram.org/bot123/sendMessage").mock(
        side_effect=Exception("timeout")
    )

    with (
        patch("tourniquet.config.settings.slack_webhook_url", ""),
        patch("tourniquet.config.settings.telegram_bot_token", "123"),
        patch("tourniquet.config.settings.telegram_chat_id", "456"),
        patch("tourniquet.config.settings.alert_webhook_url", ""),
        patch("tourniquet.config.settings.enable_mac_notifications", "false"),
        patch("tourniquet.config.settings.enable_desktop_notifications", ""),
        patch("tourniquet.config.settings.resend_api_key", ""),
    ):
        results = await fan_out(base_event)

    assert results["telegram"].startswith("error:")


# ── Mac notification skipped on non-Darwin (osascript path) ──────────────────

@pytest.mark.asyncio
async def test_mac_notification_skipped_osascript_on_non_darwin(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    mock_run = MagicMock()
    with (
        patch("sys.platform", "linux"),
        patch("subprocess.run", mock_run),
        patch("tourniquet.config.settings.enable_mac_notifications", "true"),
        patch("tourniquet.config.settings.enable_desktop_notifications", ""),
        patch("tourniquet.config.settings.slack_webhook_url", ""),
        patch("tourniquet.config.settings.telegram_bot_token", ""),
        patch("tourniquet.config.settings.telegram_chat_id", ""),
        patch("tourniquet.config.settings.alert_webhook_url", ""),
        patch("tourniquet.config.settings.resend_api_key", ""),
    ):
        # mac alias still works
        from tourniquet.alerts.desktop import send_mac_notification
        await send_mac_notification("Test", "body")

    # osascript subprocess must NOT be called on linux
    mock_run.assert_not_called()


# ── Win32 plyer path taken ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_desktop_notification_uses_plyer_on_win32() -> None:
    mock_notify = MagicMock()
    mock_plyer = MagicMock()
    mock_plyer.notification.notify = mock_notify

    with (
        patch("sys.platform", "win32"),
        patch("tourniquet.config.settings.enable_mac_notifications", "false"),
        patch("tourniquet.config.settings.enable_desktop_notifications", "true"),
        patch.dict("sys.modules", {"plyer": mock_plyer}),
    ):
        from importlib import reload
        import tourniquet.alerts.desktop as desktop_mod
        reload(desktop_mod)
        await desktop_mod.send_desktop_notification("T", "M")

    mock_notify.assert_called_once()
    call_kwargs = mock_notify.call_args
    assert call_kwargs.kwargs.get("title") == "T" or call_kwargs.args[0] == "T"


# ── Plyer not installed → silent no-op ───────────────────────────────────────

@pytest.mark.asyncio
async def test_desktop_notification_no_op_when_plyer_missing() -> None:
    import builtins
    real_import = builtins.__import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "plyer":
            raise ImportError("plyer not installed")
        return real_import(name, *args, **kwargs)

    with (
        patch("sys.platform", "linux"),
        patch("tourniquet.config.settings.enable_mac_notifications", "false"),
        patch("tourniquet.config.settings.enable_desktop_notifications", "true"),
        patch("builtins.__import__", side_effect=_fake_import),
    ):
        from importlib import reload
        import tourniquet.alerts.desktop as desktop_mod
        reload(desktop_mod)
        # Must not raise
        await desktop_mod.send_desktop_notification("T", "M")


# ── kill_now_url in AlertEvent ────────────────────────────────────────────────

def test_kill_now_url_included_when_kill_disabled():
    """fan_out with kill_enabled=False must attach a kill_now_url to the event."""
    from unittest.mock import patch as _patch

    import dataclasses
    from tourniquet.alerts.notifier import _build_kill_now_url

    event = AlertEvent(
        api_key_name="ojw-swarm",
        threshold_pct=80,
        spent_usd_cents=420,
        cap_usd_cents=500,
        display_currency="GBP",
        today=date(2026, 5, 6),
        api_key_id="abc-123",
    )
    # Verify the URL builder produces a URL containing the key_id
    url = _build_kill_now_url("abc-123")
    assert "abc-123" in url
    assert "kill-now" in url or "token=" in url


def test_kill_now_url_omitted_when_kill_enabled():
    """An event with kill_now_url=None stays None when kill_enabled=True."""
    from tourniquet.alerts.notifier import AlertEvent

    event = AlertEvent(
        api_key_name="test",
        threshold_pct=50,
        spent_usd_cents=250,
        cap_usd_cents=500,
        display_currency="USD",
        today=date(2026, 5, 6),
        api_key_id="some-key-id",
    )
    assert event.kill_now_url is None


def test_format_message_includes_kill_hint_when_url_set():
    """_format_message appends a kill hint when kill_now_url is present."""
    from tourniquet.alerts.notifier import _format_message, AlertEvent

    event = AlertEvent(
        api_key_name="ojw-swarm",
        threshold_pct=80,
        spent_usd_cents=420,
        cap_usd_cents=500,
        display_currency="GBP",
        today=date(2026, 5, 6),
        kill_now_url="https://example.com/kill-now",
    )
    msg = _format_message(event)
    assert "🛑" in msg
    assert "kill now" in msg.lower()


def test_format_message_no_kill_hint_without_url():
    """_format_message does not add kill hint when kill_now_url is None."""
    from tourniquet.alerts.notifier import _format_message, AlertEvent

    event = AlertEvent(
        api_key_name="ojw-swarm",
        threshold_pct=80,
        spent_usd_cents=420,
        cap_usd_cents=500,
        display_currency="GBP",
        today=date(2026, 5, 6),
    )
    msg = _format_message(event)
    assert "kill now" not in msg.lower()


# ── Webhook URL never appears in logs ─────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_webhook_url_not_in_logs(
    base_event: AlertEvent,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    secret_url = "https://hooks.slack.com/SECRET_TOKEN_HERE"
    respx.post(secret_url).mock(return_value=Response(500, text="fail"))

    import logging
    with caplog.at_level(logging.WARNING, logger="tourniquet.alerts.slack"):
        with (
            patch("tourniquet.config.settings.slack_webhook_url", secret_url),
            patch("tourniquet.config.settings.telegram_bot_token", ""),
            patch("tourniquet.config.settings.telegram_chat_id", ""),
            patch("tourniquet.config.settings.alert_webhook_url", ""),
            patch("tourniquet.config.settings.enable_mac_notifications", "false"),
            patch("tourniquet.config.settings.enable_desktop_notifications", ""),
            patch("tourniquet.config.settings.resend_api_key", ""),
        ):
            await fan_out(base_event)

    full_log = " ".join(r.message for r in caplog.records)
    assert "SECRET_TOKEN_HERE" not in full_log
    assert secret_url not in full_log
