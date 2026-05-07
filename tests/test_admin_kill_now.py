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

    async def _fake_apply_kill_now(kuid):
        killed["id"] = kuid
        killed["done"] = True
        return "my-key", 420

    with patch("tourniquet.routes.admin._apply_kill_now", _fake_apply_kill_now):
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


# ── _apply_kill_now sets kill_enabled=True and clamps cap ─────────────────────

@pytest.mark.asyncio
async def test_apply_kill_now_sets_fields():
    from tourniquet.routes.admin import _apply_kill_now

    key_id = uuid.uuid4()
    mock_key = MagicMock()
    mock_key.id = key_id
    mock_key.name = "test-key"
    mock_key.kill_enabled = False
    mock_key.daily_cap_usd_cents = 1000

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
    assert mock_key.daily_cap_usd_cents == 420  # clamped to today's spend
    assert new_cap == 420
    assert name == "test-key"


@pytest.mark.asyncio
async def test_apply_kill_now_zero_spend_clamps_to_one():
    """Spend of 0 must not set cap to 0 — clamp to 1."""
    from tourniquet.routes.admin import _apply_kill_now

    key_id = uuid.uuid4()
    mock_key = MagicMock()
    mock_key.id = key_id
    mock_key.name = "test-key"
    mock_key.kill_enabled = False
    mock_key.daily_cap_usd_cents = 1000

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
    assert mock_key.daily_cap_usd_cents == 1
