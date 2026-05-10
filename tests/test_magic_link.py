"""Tests for magic-link single-use enforcement."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from tourniquet.auth.magic_link import _make_token, _token_hash


def _make_mock_user(email: str, token: str | None = None) -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = email
    user.magic_link_token = _token_hash(token) if token else None
    return user


def _make_session_cm(user: MagicMock):
    """Session mock that returns `user` for both execute+scalar and session.get."""

    @asynccontextmanager
    async def _session():
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=user)
        session.execute = AsyncMock(return_value=result)
        session.get = AsyncMock(return_value=user)
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.commit = AsyncMock()
        yield session

    return _session


# ── Test 1: fresh token verifies, creates session, NULLs token ────────────────


def test_verify_fresh_token_creates_session():
    from fastapi.testclient import TestClient

    from tourniquet.main import app

    client = TestClient(app, raise_server_exceptions=True)
    email = "test@example.com"
    token = _make_token(email)
    user = _make_mock_user(email, token)

    with patch("tourniquet.auth.magic_link.get_session", _make_session_cm(user)):
        resp = client.get(f"/auth/verify?token={token}", follow_redirects=False)

    # Should redirect to /dashboard on success
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/dashboard")
    # Token must have been consumed (set to None)
    assert user.magic_link_token is None


# ── Test 2: replaying the same token returns 400 ─────────────────────────────


def test_verify_replayed_token_returns_400():
    from fastapi.testclient import TestClient

    from tourniquet.main import app

    client = TestClient(app, raise_server_exceptions=True)
    email = "replay@example.com"
    token = _make_token(email)

    # Simulate token already consumed (magic_link_token = None)
    consumed_user = _make_mock_user(email, None)

    with patch("tourniquet.auth.magic_link.get_session", _make_session_cm(consumed_user)):
        resp = client.get(f"/auth/verify?token={token}", follow_redirects=False)

    assert resp.status_code == 400


# ── Test 3: tampered token returns 400 ───────────────────────────────────────


def test_verify_tampered_token_returns_400():
    from fastapi.testclient import TestClient

    from tourniquet.main import app

    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/auth/verify?token=notavalidtoken.aaa.bbb", follow_redirects=False)
    assert resp.status_code == 400


# ── Test 4: valid token with mismatched stored hash returns 400 ───────────────


def test_verify_hash_mismatch_returns_400():
    from fastapi.testclient import TestClient

    from tourniquet.main import app

    client = TestClient(app, raise_server_exceptions=True)
    email = "mismatch@example.com"
    token = _make_token(email)

    # User row has a completely different hash (simulates a superseded token)
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = email
    user.magic_link_token = "a" * 64  # valid-length sha256 but won't match token

    with patch("tourniquet.auth.magic_link.get_session", _make_session_cm(user)):
        resp = client.get(f"/auth/verify?token={token}", follow_redirects=False)

    assert resp.status_code == 400
