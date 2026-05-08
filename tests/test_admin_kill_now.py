"""Tests for the kill-now magic-link endpoints and helper."""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from tourniquet.config import settings
from tourniquet.routes.admin import build_kill_now_url, router


# ── Token signing / verification ───────────────────────────────────────────────

def test_build_kill_now_url_contains_key_id():
    key_id = str(uuid.uuid4())
    url = build_kill_now_url(key_id)
    assert key_id in url
    assert "/admin/kill-now/" in url
    assert "token=" in url


def test_kill_now_token_is_verifiable():
    key_id = str(uuid.uuid4())
    url = build_kill_now_url(key_id)
    token = url.split("token=")[1]

    s = URLSafeTimedSerializer(settings.secret_key, salt="kill-now")
    decoded = s.loads(token, max_age=24 * 60 * 60)
    assert decoded == key_id


def test_expired_token_rejected():
    """A token signed with a timestamp in the past is rejected as expired."""
    from itsdangerous import SignatureExpired
    import time

    key_id = str(uuid.uuid4())
    s = URLSafeTimedSerializer(settings.secret_key, salt="kill-now")

    # Dump with a deliberately old now via the undocumented but stable `now` param
    # Alternatively, sign normally then load with max_age=-1 (already-past threshold).
    token = s.dumps(key_id)

    # max_age=-1 means "expiry was 1 second ago at the moment of signing"
    with pytest.raises(SignatureExpired):
        s.loads(token, max_age=-1)


def test_wrong_salt_rejected():
    from itsdangerous import BadSignature

    key_id = str(uuid.uuid4())
    s_wrong = URLSafeTimedSerializer(settings.secret_key, salt="wrong-salt")
    token = s_wrong.dumps(key_id)

    s_correct = URLSafeTimedSerializer(settings.secret_key, salt="kill-now")
    with pytest.raises(BadSignature):
        s_correct.loads(token, max_age=24 * 60 * 60)


# ── GET /admin/kill-now/{key_id} — confirm page ────────────────────────────────

def _make_app():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture()
def client():
    return TestClient(_make_app(), raise_server_exceptions=True)


@pytest.fixture()
def valid_token_and_key():
    key_id = uuid.uuid4()
    url = build_kill_now_url(str(key_id))
    token = url.split("token=")[1]
    return key_id, token


def test_get_kill_now_confirm_renders(client, valid_token_and_key):
    key_id, token = valid_token_and_key
    mock_key = MagicMock()
    mock_key.name = "my-test-key"

    async def _fake_get(session_self, model, pk):
        return mock_key

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_session():
        session = AsyncMock()
        session.get = AsyncMock(return_value=mock_key)
        yield session

    with patch("tourniquet.routes.admin.get_session", _fake_session):
        resp = client.get(f"/admin/kill-now/{key_id}?token={token}")

    assert resp.status_code == 200
    assert "my-test-key" in resp.text
    assert "Confirm kill" in resp.text


def test_get_kill_now_bad_token_returns_400(client):
    key_id = uuid.uuid4()
    resp = client.get(f"/admin/kill-now/{key_id}?token=notavalidtoken")
    assert resp.status_code == 400


def test_get_kill_now_mismatched_key_returns_400(client):
    # Token is signed for key_id_a but URL uses key_id_b
    key_id_a = uuid.uuid4()
    key_id_b = uuid.uuid4()
    url_a = build_kill_now_url(str(key_id_a))
    token_a = url_a.split("token=")[1]

    resp = client.get(f"/admin/kill-now/{key_id_b}?token={token_a}")
    assert resp.status_code == 400


# ── POST /admin/kill-now/{key_id} — execute kill ──────────────────────────────

def test_post_kill_now_applies_kill(valid_token_and_key):
    key_id, token = valid_token_and_key
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=True)

    killed = {}

    async def _fake_apply_kill_now(kuid, **kwargs):
        killed["id"] = kuid
        killed["done"] = True
        return "my-key", 420

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_session():
        session = AsyncMock()
        # Replay check: no existing row → scalar_one_or_none returns None (token unused)
        execute_result = MagicMock()
        execute_result.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=execute_result)
        yield session

    with (
        patch("tourniquet.routes.admin._apply_kill_now", _fake_apply_kill_now),
        patch("tourniquet.routes.admin.get_session", _fake_session),
    ):
        resp = client.post(
            f"/admin/kill-now/{key_id}",
            data={"token": token},
        )

    assert resp.status_code == 200
    assert "Killed" in resp.text
    assert killed.get("done") is True
    assert killed["id"] == key_id


def test_post_kill_now_bad_token_returns_400():
    key_id = uuid.uuid4()
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(f"/admin/kill-now/{key_id}", data={"token": "badtoken"})
    assert resp.status_code == 400


# ── _apply_kill_now writes lifted_cap and PRESERVES daily_cap ─────────────────
# (Killing must not destroy the user's baseline daily cap — the kill is a
#  today-only override that auto-expires at midnight UTC.)

@pytest.mark.asyncio
async def test_apply_kill_now_sets_lifted_cap_and_preserves_daily_cap():
    from tourniquet.routes.admin import _apply_kill_now

    key_id = uuid.uuid4()
    mock_key = MagicMock()
    mock_key.id = key_id
    mock_key.name = "test-key"
    mock_key.kill_enabled = False
    mock_key.daily_cap_usd_cents = 1000  # $10 baseline
    mock_key.lifted_cap_usd_cents = None  # no active lift before the kill

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_session():
        session = AsyncMock()
        session.get = AsyncMock(return_value=mock_key)
        session.commit = AsyncMock()
        yield session

    with (
        patch("tourniquet.routes.admin.get_session", _fake_session),
        patch("tourniquet.routes.admin.get_today_spend", AsyncMock(return_value=420)),
    ):
        name, new_cap = await _apply_kill_now(key_id)

    assert mock_key.kill_enabled is True
    # Kill clamps the LIFTED cap to today's spend ($4.20)
    assert mock_key.lifted_cap_usd_cents == 420
    # The daily_cap baseline is intentionally untouched — survives until tomorrow
    assert mock_key.daily_cap_usd_cents == 1000
    # lift_expires_at is set so the kill auto-clears at midnight UTC
    assert mock_key.lift_expires_at is not None
    assert new_cap == 420
    assert name == "test-key"


@pytest.mark.asyncio
async def test_apply_kill_now_zero_spend_floors_lifted_at_one_cent():
    """Spend of 0 must floor lifted_cap at 1¢ (avoid div-by-zero downstream)."""
    from tourniquet.routes.admin import _apply_kill_now

    key_id = uuid.uuid4()
    mock_key = MagicMock()
    mock_key.id = key_id
    mock_key.name = "test-key"
    mock_key.kill_enabled = False
    mock_key.daily_cap_usd_cents = 1000
    mock_key.lifted_cap_usd_cents = None

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_session():
        session = AsyncMock()
        session.get = AsyncMock(return_value=mock_key)
        session.commit = AsyncMock()
        yield session

    with (
        patch("tourniquet.routes.admin.get_session", _fake_session),
        patch("tourniquet.routes.admin.get_today_spend", AsyncMock(return_value=0)),
    ):
        name, new_cap = await _apply_kill_now(key_id)

    assert mock_key.kill_enabled is True
    assert new_cap == 1
    assert mock_key.lifted_cap_usd_cents == 1
    # daily_cap untouched — restored automatically tomorrow
    assert mock_key.daily_cap_usd_cents == 1000
