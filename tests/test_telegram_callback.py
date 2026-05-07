"""Tests for Telegram bot callback handling.

Covers:
  - Valid callback_data parses and dispatches to lift logic
  - Missing/wrong webhook secret returns 401
  - Unrecognised callback_data is silently ignored
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from tourniquet.main import app
    return TestClient(app, raise_server_exceptions=True)


def _telegram_update(callback_data: str) -> dict:
    return {
        "update_id": 123456,
        "callback_query": {
            "id": "abc",
            "from": {"id": 99, "first_name": "Test"},
            "data": callback_data,
        },
    }


def test_telegram_callback_valid_lift(client):
    """Valid lift|<key>|2x callback dispatches to _apply_lift_from_callback."""
    key_id = str(uuid.uuid4())

    with patch(
        "tourniquet.alerts.telegram_callbacks._apply_lift_from_callback",
        new_callable=AsyncMock,
    ) as mock_lift, patch(
        "tourniquet.config.settings.telegram_webhook_secret", ""
    ):
        resp = client.post(
            "/telegram/callback",
            json=_telegram_update(f"lift|{key_id}|2x"),
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    mock_lift.assert_awaited_once_with(key_id, "2x")


def test_telegram_callback_ceiling_mode(client):
    """lift|<key>|ceiling mode is passed through correctly."""
    key_id = str(uuid.uuid4())

    with patch(
        "tourniquet.alerts.telegram_callbacks._apply_lift_from_callback",
        new_callable=AsyncMock,
    ) as mock_lift, patch(
        "tourniquet.config.settings.telegram_webhook_secret", ""
    ):
        resp = client.post(
            "/telegram/callback",
            json=_telegram_update(f"lift|{key_id}|ceiling"),
        )

    assert resp.status_code == 200
    mock_lift.assert_awaited_once_with(key_id, "ceiling")


def test_telegram_callback_ignore_mode(client):
    """lift|<key>|ignore is passed to _apply_lift_from_callback (which no-ops)."""
    key_id = str(uuid.uuid4())

    with patch(
        "tourniquet.alerts.telegram_callbacks._apply_lift_from_callback",
        new_callable=AsyncMock,
    ) as mock_lift, patch(
        "tourniquet.config.settings.telegram_webhook_secret", ""
    ):
        resp = client.post(
            "/telegram/callback",
            json=_telegram_update(f"lift|{key_id}|ignore"),
        )

    assert resp.status_code == 200
    mock_lift.assert_awaited_once_with(key_id, "ignore")


def test_telegram_callback_wrong_secret(client):
    """Wrong X-Telegram-Bot-Api-Secret-Token returns 401."""
    with patch("tourniquet.alerts.telegram_callbacks.settings") as mock_settings:
        mock_settings.telegram_webhook_secret = "correct-secret"
        resp = client.post(
            "/telegram/callback",
            json=_telegram_update("lift|abc|2x"),
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
        )

    assert resp.status_code == 401


def test_telegram_callback_missing_secret_when_required(client):
    """Missing secret header when secret is configured returns 401."""
    with patch("tourniquet.alerts.telegram_callbacks.settings") as mock_settings:
        mock_settings.telegram_webhook_secret = "required-secret"
        resp = client.post(
            "/telegram/callback",
            json=_telegram_update("lift|abc|2x"),
            # No X-Telegram-Bot-Api-Secret-Token header
        )

    assert resp.status_code == 401


def test_telegram_callback_correct_secret(client):
    """Correct secret header passes authentication."""
    key_id = str(uuid.uuid4())

    with patch("tourniquet.alerts.telegram_callbacks.settings") as mock_settings, patch(
        "tourniquet.alerts.telegram_callbacks._apply_lift_from_callback",
        new_callable=AsyncMock,
    ):
        mock_settings.telegram_webhook_secret = "my-secret"
        resp = client.post(
            "/telegram/callback",
            json=_telegram_update(f"lift|{key_id}|2x"),
            headers={"X-Telegram-Bot-Api-Secret-Token": "my-secret"},
        )

    assert resp.status_code == 200


def test_telegram_callback_non_lift_data(client):
    """Non-lift callback_data is silently ignored (other bots/handlers)."""
    with patch(
        "tourniquet.alerts.telegram_callbacks._apply_lift_from_callback",
        new_callable=AsyncMock,
    ) as mock_lift, patch(
        "tourniquet.config.settings.telegram_webhook_secret", ""
    ):
        resp = client.post(
            "/telegram/callback",
            json=_telegram_update("some_other_action"),
        )

    assert resp.status_code == 200
    mock_lift.assert_not_awaited()
