"""Tests for the cap lift mechanism.

Covers:
  - _effective_cap logic (no lift, active lift, expired lift)
  - HTTP /admin/lift endpoint with multiplier mode
  - Ceiling clamp: requesting > absolute_ceiling clamps and reports it
  - /admin/unlift clears the columns
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tourniquet.proxy.router import _effective_cap

# ── Model stubs ────────────────────────────────────────────────────────────────


def _make_key(
    daily_cap: int = 500,
    absolute_ceiling: int = 2000,
    lifted_cap: int | None = None,
    lift_expires_at: datetime | None = None,
) -> MagicMock:
    key = MagicMock()
    key.daily_cap_usd_cents = daily_cap
    key.absolute_ceiling_usd_cents = absolute_ceiling
    key.lifted_cap_usd_cents = lifted_cap
    key.lift_expires_at = lift_expires_at
    return key


# ── _effective_cap unit tests ──────────────────────────────────────────────────


def test_effective_cap_no_lift():
    """Returns base cap when no lift is set."""
    key = _make_key(daily_cap=500)
    now = datetime.now(UTC)
    assert _effective_cap(key, now) == 500


def test_effective_cap_active_lift():
    """Returns lifted cap when lift_expires_at is in the future."""
    future = datetime.now(UTC) + timedelta(hours=4)
    key = _make_key(daily_cap=500, lifted_cap=1000, lift_expires_at=future)
    now = datetime.now(UTC)
    assert _effective_cap(key, now) == 1000


def test_effective_cap_expired_lift():
    """Returns base cap when lift_expires_at is in the past."""
    past = datetime.now(UTC) - timedelta(hours=1)
    key = _make_key(daily_cap=500, lifted_cap=1000, lift_expires_at=past)
    now = datetime.now(UTC)
    assert _effective_cap(key, now) == 500


def test_effective_cap_lift_but_no_expiry():
    """Returns base cap when lifted_cap is set but lift_expires_at is None."""
    key = _make_key(daily_cap=500, lifted_cap=1000, lift_expires_at=None)
    now = datetime.now(UTC)
    assert _effective_cap(key, now) == 500


def test_effective_cap_expiry_but_no_amount():
    """Returns base cap when lift_expires_at is set but lifted_cap is None."""
    future = datetime.now(UTC) + timedelta(hours=4)
    key = _make_key(daily_cap=500, lifted_cap=None, lift_expires_at=future)
    now = datetime.now(UTC)
    assert _effective_cap(key, now) == 500


# ── HTTP endpoint tests ────────────────────────────────────────────────────────


def _build_fake_db_key(
    daily_cap: int = 500,
    absolute_ceiling: int = 2000,
    tq_token: str = "tq_testtoken",
) -> MagicMock:
    import hashlib

    import bcrypt

    token_hash = bcrypt.hashpw(tq_token.encode(), bcrypt.gensalt()).decode()
    key = MagicMock()
    key.id = uuid.uuid4()
    key.name = "test-key"
    key.daily_cap_usd_cents = daily_cap
    key.absolute_ceiling_usd_cents = absolute_ceiling
    key.lifted_cap_usd_cents = None
    key.lift_expires_at = None
    key.tq_token_hash = token_hash
    # C3: post-migration keys carry the indexed sha256 column. The admin
    # auth path prefers it over bcrypt for verification.
    key.tq_token_sha256 = hashlib.sha256(tq_token.encode()).hexdigest()
    return key


def _make_session_cm(fake_key: MagicMock):
    """Build an asynccontextmanager-compatible mock for get_session."""

    @asynccontextmanager
    async def _session_cm():
        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [fake_key]
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=execute_result)

        mutable = MagicMock()
        mutable.id = fake_key.id
        mutable.name = fake_key.name
        mutable.daily_cap_usd_cents = fake_key.daily_cap_usd_cents
        mutable.absolute_ceiling_usd_cents = fake_key.absolute_ceiling_usd_cents
        mutable.tq_token_hash = fake_key.tq_token_hash
        mutable.tq_token_sha256 = fake_key.tq_token_sha256
        session.get = AsyncMock(return_value=mutable)
        session.commit = AsyncMock()
        yield session

    return _session_cm


@pytest.fixture()
def client():
    from tourniquet.main import app

    return TestClient(app, raise_server_exceptions=True)


def test_lift_endpoint_multiplier(client):
    """POST /admin/lift with multiplier=2 doubles the base cap."""
    fake_key = _build_fake_db_key(daily_cap=500, absolute_ceiling=2000)

    with patch("tourniquet.routes.admin.get_session", _make_session_cm(fake_key)):
        resp = client.post(
            "/admin/lift",
            json={"key_id": fake_key.name, "mode": "multiplier", "multiplier": 2.0},
            headers={"Authorization": "Bearer tq_testtoken"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["lifted_cap_usd_cents"] == 1000
    assert data["ceiling_clamped"] is False


def test_lift_endpoint_ceiling_clamp(client):
    """Lift requesting more than absolute_ceiling is clamped."""
    fake_key = _build_fake_db_key(daily_cap=1500, absolute_ceiling=2000)

    with patch("tourniquet.routes.admin.get_session", _make_session_cm(fake_key)):
        # 3x of 1500 = 4500, ceiling = 2000 → should clamp
        resp = client.post(
            "/admin/lift",
            json={"key_id": fake_key.name, "mode": "multiplier", "multiplier": 3.0},
            headers={"Authorization": "Bearer tq_testtoken"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["lifted_cap_usd_cents"] == 2000  # clamped
    assert data["ceiling_clamped"] is True


def test_unlift_clears_columns(client):
    """POST /admin/unlift clears lifted_cap_usd_cents and lift_expires_at."""
    future = datetime.now(UTC) + timedelta(hours=4)
    fake_key = _build_fake_db_key(daily_cap=500, absolute_ceiling=2000)
    fake_key.lifted_cap_usd_cents = 1000
    fake_key.lift_expires_at = future

    with patch("tourniquet.routes.admin.get_session", _make_session_cm(fake_key)):
        resp = client.post(
            "/admin/unlift",
            json={"key_id": fake_key.name},
            headers={"Authorization": "Bearer tq_testtoken"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["restored_cap_usd_cents"] == 500
