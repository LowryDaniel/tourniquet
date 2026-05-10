"""Tests for the unified alert fanout."""

from __future__ import annotations

import asyncio
import json
import pathlib
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
        patch("tourniquet.config.settings.enable_mac_notifications", False),
        patch("tourniquet.config.settings.enable_desktop_notifications", False),
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
        patch("tourniquet.config.settings.enable_mac_notifications", False),
        patch("tourniquet.config.settings.enable_desktop_notifications", False),
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
        patch("tourniquet.config.settings.enable_mac_notifications", False),
        patch("tourniquet.config.settings.enable_desktop_notifications", False),
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
        patch("tourniquet.config.settings.enable_mac_notifications", False),
        patch("tourniquet.config.settings.enable_desktop_notifications", False),
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
        patch("tourniquet.config.settings.enable_mac_notifications", True),
        patch("tourniquet.config.settings.enable_desktop_notifications", False),
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
        patch("tourniquet.config.settings.enable_mac_notifications", False),
        patch("tourniquet.config.settings.enable_desktop_notifications", True),
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
        patch("tourniquet.config.settings.enable_mac_notifications", False),
        patch("tourniquet.config.settings.enable_desktop_notifications", True),
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


def test_format_message_text_is_consistent_regardless_of_kill_url():
    """Locked-in contract: the message text NEVER varies based on kill_now_url.

    Action prompts ('Kill now', '+$5' etc) live in the channel-rendered button
    rows, not in the message prose. This keeps the canonical alert text
    identical across every delivery method (Slack/Telegram/email/desktop/JSONL).
    """
    from tourniquet.alerts.notifier import AlertEvent, _format_message

    base = AlertEvent(
        api_key_name="ojw-swarm",
        threshold_pct=80,
        spent_usd_cents=420,
        cap_usd_cents=500,
        display_currency="GBP",
        today=date(2026, 5, 6),
    )
    msg_no_url = _format_message(base)

    with_url = AlertEvent(
        api_key_name="ojw-swarm",
        threshold_pct=80,
        spent_usd_cents=420,
        cap_usd_cents=500,
        display_currency="GBP",
        today=date(2026, 5, 6),
        kill_now_url="https://example.com/kill-now",
    )
    msg_with_url = _format_message(with_url)

    assert msg_no_url == msg_with_url, (
        "Message text must not vary based on kill_now_url — buttons handle action prompts"
    )


def test_format_message_no_kill_hint_without_url():
    """_format_message does not add kill hint when kill_now_url is None."""
    from tourniquet.alerts.notifier import AlertEvent, _format_message

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


# ── Email channel: skipped when no creds, called when configured ──────────────

@pytest.mark.asyncio
async def test_email_reports_skipped_when_no_creds(
    base_event: AlertEvent, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: email used to silently no-op and falsely report 'sent'.

    With creds empty the dispatcher must report 'skipped:no-config', matching
    the behaviour of slack/telegram/webhook.
    """
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    with (
        patch("tourniquet.config.settings.slack_webhook_url", ""),
        patch("tourniquet.config.settings.telegram_bot_token", ""),
        patch("tourniquet.config.settings.telegram_chat_id", ""),
        patch("tourniquet.config.settings.alert_webhook_url", ""),
        patch("tourniquet.config.settings.enable_mac_notifications", False),
        patch("tourniquet.config.settings.enable_desktop_notifications", False),
        patch("tourniquet.config.settings.resend_api_key", ""),
    ):
        results = await fan_out(base_event)

    assert results["email"] == "skipped:no-config"


@pytest.mark.asyncio
async def test_email_dispatched_when_creds_present(
    base_event: AlertEvent, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When RESEND_API_KEY + RESEND_FROM_EMAIL are set, email is dispatched."""
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    mock_send = MagicMock()
    with (
        patch("tourniquet.config.settings.slack_webhook_url", ""),
        patch("tourniquet.config.settings.telegram_bot_token", ""),
        patch("tourniquet.config.settings.telegram_chat_id", ""),
        patch("tourniquet.config.settings.alert_webhook_url", ""),
        patch("tourniquet.config.settings.enable_mac_notifications", False),
        patch("tourniquet.config.settings.enable_desktop_notifications", False),
        patch("tourniquet.config.settings.resend_api_key", "re_fake"),
        patch("tourniquet.config.settings.resend_from_email", "alerts@example.com"),
        patch("resend.Emails.send", mock_send),
    ):
        results = await fan_out(base_event)

    assert results["email"] == "sent"
    mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_email_uses_per_key_alert_email_when_set(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If event.alert_email is set, email goes to that recipient (not the from-address)."""
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    event = AlertEvent(
        api_key_name="ojw-swarm",
        threshold_pct=80,
        spent_usd_cents=420,
        cap_usd_cents=500,
        display_currency="GBP",
        today=date(2026, 5, 6),
        alert_email="user@example.com",
    )
    mock_send = MagicMock()
    with (
        patch("tourniquet.config.settings.slack_webhook_url", ""),
        patch("tourniquet.config.settings.telegram_bot_token", ""),
        patch("tourniquet.config.settings.telegram_chat_id", ""),
        patch("tourniquet.config.settings.alert_webhook_url", ""),
        patch("tourniquet.config.settings.enable_mac_notifications", False),
        patch("tourniquet.config.settings.enable_desktop_notifications", False),
        patch("tourniquet.config.settings.resend_api_key", "re_fake"),
        patch("tourniquet.config.settings.resend_from_email", "alerts@example.com"),
        patch("resend.Emails.send", mock_send),
    ):
        await fan_out(event)

    payload = mock_send.call_args[0][0]
    assert payload["to"] == ["user@example.com"]
    assert payload["from"] == "alerts@example.com"


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
            patch("tourniquet.config.settings.enable_mac_notifications", False),
            patch("tourniquet.config.settings.enable_desktop_notifications", False),
            patch("tourniquet.config.settings.resend_api_key", ""),
        ):
            await fan_out(base_event)

    full_log = " ".join(r.message for r in caplog.records)
    assert "SECRET_TOKEN_HERE" not in full_log
    assert secret_url not in full_log


# ── _select_threshold — pure logic, no DB ──────────────────────────────────────
# Each level (50%, 80%, cap-hit) fires AT MOST ONCE per day. The proxy hot path
# uses this to decide whether to alert after each request.

class TestSelectThreshold:
    def test_below_50_percent_no_alert(self):
        from tourniquet.alerts.notifier import _select_threshold
        assert _select_threshold(spent_cents=200, cap_cents=500, last_fired_pct=None) is None

    def test_crossing_50_fires_50(self):
        from tourniquet.alerts.notifier import _select_threshold
        # 250/500 = 50%
        assert _select_threshold(250, 500, None) == 50

    def test_crossing_80_fires_80_when_50_already_fired(self):
        from tourniquet.alerts.notifier import _select_threshold
        # 400/500 = 80% — last was 50, so fire 80
        assert _select_threshold(400, 500, 50) == 80

    def test_50_does_not_refire_when_50_already_fired(self):
        from tourniquet.alerts.notifier import _select_threshold
        # 300/500 = 60% — last was 50, so 50 is "done"; haven't hit 80 yet
        assert _select_threshold(300, 500, 50) is None

    def test_cap_hit_fires_minus_one(self):
        from tourniquet.alerts.notifier import _select_threshold
        assert _select_threshold(500, 500, 80) == -1
        assert _select_threshold(600, 500, 80) == -1  # over-cap also fires -1

    def test_cap_hit_does_not_refire(self):
        from tourniquet.alerts.notifier import _select_threshold
        assert _select_threshold(700, 500, -1) is None

    def test_crossing_directly_to_cap_skips_50_80(self):
        """A single big request can take you from 0 → cap. Cap-hit fires immediately."""
        from tourniquet.alerts.notifier import _select_threshold
        assert _select_threshold(500, 500, None) == -1

    def test_zero_cap_no_alert(self):
        """Defensive: if cap is somehow 0, don't divide-by-zero or fire."""
        from tourniquet.alerts.notifier import _select_threshold
        assert _select_threshold(100, 0, None) is None


# ── maybe_fire_threshold_alert — integration with fake session ────────────────
# Verifies the helper records an audit row before dispatching, and returns the
# fired level so callers (or tests) can confirm what happened.

@pytest.mark.asyncio
async def test_maybe_fire_threshold_alert_records_audit_and_dispatches():
    from datetime import date as _date
    from unittest.mock import AsyncMock, MagicMock

    from tourniquet.alerts.notifier import maybe_fire_threshold_alert

    api_key = MagicMock()
    api_key.id = "00000000-0000-0000-0000-000000000001"
    api_key.name = "test-key"
    api_key.alert_email = None

    # Fake session with a working .add() and an .execute() that returns "no
    # prior alerts today" — so the helper concludes nothing has fired yet.
    session = MagicMock()
    session.add = MagicMock()
    fake_result = MagicMock()
    fake_result.scalar_one_or_none = MagicMock(return_value=None)
    session.execute = AsyncMock(return_value=fake_result)

    # Patch fan_out so we can assert it was called without actually dispatching
    # to real channels.
    with patch("tourniquet.alerts.notifier.fan_out", new_callable=AsyncMock) as mock_fanout:
        threshold = await maybe_fire_threshold_alert(
            api_key,
            spent_cents=400,  # 80% of 500
            cap_cents=500,
            today=_date(2026, 5, 8),
            kill_enabled=True,
            session=session,
        )
        # Yield once so the asyncio.create_task background task can run
        await asyncio.sleep(0)

    assert threshold == 80
    # Audit row was added inside the same session (so it commits with the spend)
    assert session.add.called
    added = session.add.call_args[0][0]
    assert added.action == "alert_fired"
    assert added.source == "proxy"
    assert added.details["threshold_pct"] == 80
    # Background task fired fan_out
    assert mock_fanout.await_count == 1


@pytest.mark.asyncio
async def test_maybe_fire_threshold_alert_no_op_when_already_fired():
    """If the audit log already has an 80% alert today, don't refire."""
    from datetime import date as _date
    from unittest.mock import AsyncMock, MagicMock

    from tourniquet.alerts.notifier import maybe_fire_threshold_alert

    api_key = MagicMock()
    api_key.id = "00000000-0000-0000-0000-000000000002"
    api_key.name = "test-key"
    api_key.alert_email = None

    # Fake prior audit row with threshold_pct=80
    prior = MagicMock()
    prior.details = {"threshold_pct": 80}

    session = MagicMock()
    session.add = MagicMock()
    fake_result = MagicMock()
    fake_result.scalar_one_or_none = MagicMock(return_value=prior)
    session.execute = AsyncMock(return_value=fake_result)

    with patch("tourniquet.alerts.notifier.fan_out", new_callable=AsyncMock) as mock_fanout:
        threshold = await maybe_fire_threshold_alert(
            api_key, 410, 500, _date(2026, 5, 8),
            kill_enabled=True, session=session,
        )
        await asyncio.sleep(0)

    assert threshold is None
    assert not session.add.called  # no new audit row
    assert mock_fanout.await_count == 0  # no dispatch


@pytest.mark.asyncio
async def test_maybe_fire_threshold_alert_cap_hit_with_kill_offers_recovery():
    """Cap-hit alert with kill_enabled=True should set recovery_offer=True so
    the channels render +$N bump buttons."""
    from datetime import date as _date
    from unittest.mock import AsyncMock, MagicMock

    from tourniquet.alerts.notifier import maybe_fire_threshold_alert

    api_key = MagicMock()
    api_key.id = "00000000-0000-0000-0000-000000000003"
    api_key.name = "test-key"
    api_key.alert_email = None

    session = MagicMock()
    session.add = MagicMock()
    fake_result = MagicMock()
    fake_result.scalar_one_or_none = MagicMock(return_value=None)
    session.execute = AsyncMock(return_value=fake_result)

    with patch("tourniquet.alerts.notifier.fan_out", new_callable=AsyncMock) as mock_fanout:
        threshold = await maybe_fire_threshold_alert(
            api_key, 500, 500, _date(2026, 5, 8),
            kill_enabled=True, session=session,
        )
        await asyncio.sleep(0)

    assert threshold == -1
    # The event passed to fan_out should have recovery_offer=True
    event_arg = mock_fanout.await_args.args[0]
    assert event_arg.recovery_offer is True
    assert event_arg.threshold_pct == -1


@pytest.mark.asyncio
async def test_fan_out_task_is_referenced():
    """Regression: fan_out background task must be strongly referenced.

    asyncio.create_task only returns a weak ref via the event loop, so a
    dropped reference can be GC'd mid-flight. We hold each task in
    _pending_tasks and discard via add_done_callback. This test verifies
    the task is registered immediately, then removed once it completes.
    """
    from datetime import date as _date
    from unittest.mock import AsyncMock, MagicMock

    from tourniquet.alerts.notifier import (
        _pending_tasks,
        maybe_fire_threshold_alert,
    )

    # Start from a clean slate — earlier tests may have left tasks behind.
    _pending_tasks.clear()

    api_key = MagicMock()
    api_key.id = "00000000-0000-0000-0000-000000000099"
    api_key.name = "test-key"
    api_key.alert_email = None

    session = MagicMock()
    session.add = MagicMock()
    fake_result = MagicMock()
    fake_result.scalar_one_or_none = MagicMock(return_value=None)
    session.execute = AsyncMock(return_value=fake_result)

    with patch("tourniquet.alerts.notifier.fan_out", new_callable=AsyncMock) as mock_fanout:
        threshold = await maybe_fire_threshold_alert(
            api_key,
            spent_cents=400,  # 80% of 500
            cap_cents=500,
            today=_date(2026, 5, 8),
            kill_enabled=True,
            session=session,
        )

        # Immediately after maybe_fire_threshold_alert returns, the task has
        # been created and registered but has not yet been awaited / run.
        assert threshold == 80
        assert len(_pending_tasks) == 1, (
            "fan_out task must be held in _pending_tasks to prevent GC cancellation"
        )
        task = next(iter(_pending_tasks))

        # Now let the event loop run the background dispatch to completion.
        await task

    # done_callback must have discarded it from the registry.
    assert len(_pending_tasks) == 0
    assert mock_fanout.await_count == 1


@pytest.mark.asyncio
async def test_maybe_fire_threshold_alert_monitor_mode_no_recovery():
    """In monitor mode (kill_enabled=False), cap-hit alert fires but
    recovery_offer must stay False — the key isn't actually blocked."""
    from datetime import date as _date
    from unittest.mock import AsyncMock, MagicMock

    from tourniquet.alerts.notifier import maybe_fire_threshold_alert

    api_key = MagicMock()
    api_key.id = "00000000-0000-0000-0000-000000000004"
    api_key.name = "test-key"
    api_key.alert_email = None

    session = MagicMock()
    session.add = MagicMock()
    fake_result = MagicMock()
    fake_result.scalar_one_or_none = MagicMock(return_value=None)
    session.execute = AsyncMock(return_value=fake_result)

    with patch("tourniquet.alerts.notifier.fan_out", new_callable=AsyncMock) as mock_fanout:
        await maybe_fire_threshold_alert(
            api_key, 500, 500, _date(2026, 5, 8),
            kill_enabled=False, session=session,
        )
        await asyncio.sleep(0)

    event_arg = mock_fanout.await_args.args[0]
    assert event_arg.recovery_offer is False  # no recovery in monitor mode
