"""Dashboard route tests.

Uses FastAPI TestClient against a real SQLite in-memory DB.
All tests are synchronous (TestClient wraps async).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tourniquet.main import app


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_key(
    name: str = "test-key",
    daily_cap: int = 1000,
    absolute_ceiling: int = 10000,
    profile: str = "standard",
    kill_enabled: bool = True,
    auto_tune_mode: str = "off",
    lifted_cap: int | None = None,
    lift_expires_at: datetime | None = None,
) -> MagicMock:
    key = MagicMock()
    key.id = uuid.uuid4()
    key.name = name
    key.daily_cap_usd_cents = daily_cap
    key.absolute_ceiling_usd_cents = absolute_ceiling
    key.profile = profile
    key.kill_enabled = kill_enabled
    key.auto_tune_mode = auto_tune_mode
    key.lifted_cap_usd_cents = lifted_cap
    key.lift_expires_at = lift_expires_at
    key.tq_token_hash = "hash"
    return key


def _make_session_cm(keys: list, get_key=None):
    """Build a mock async session context manager."""

    @asynccontextmanager
    async def _cm():
        session = AsyncMock()

        scalars_mock = MagicMock()
        scalars_mock.all.return_value = keys

        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars_mock
        execute_result.scalar_one_or_none.return_value = keys[0] if keys else None
        execute_result.all.return_value = []
        execute_result.first.return_value = None
        execute_result.scalar.return_value = 0

        session.execute = AsyncMock(return_value=execute_result)
        session.get = AsyncMock(return_value=get_key or (keys[0] if keys else None))
        session.commit = AsyncMock()
        session.delete = AsyncMock()
        session.add = MagicMock()
        yield session

    return _cm


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=True)


# ── GET /dashboard ─────────────────────────────────────────────────────────────

_MOCK_INSIGHTS = MagicMock(
    by_model=[], by_caller=[], by_metadata_user_id=[],
    suggestions=[], api_key_name="test", total_usd_cents=0,
    request_count=0, cap_hit_days=0, cap_hit_days_prior=0,
    hottest_hour=None, biggest_request=None, biggest_request_pct=0.0,
)


def test_dashboard_get_200(client):
    """GET /dashboard returns 200 and lists key names in HTML."""
    key = _make_key(name="ojw-swarm")
    cm = _make_session_cm([key])

    with (
        patch("tourniquet.dashboard.routes.get_session", cm),
        patch("tourniquet.dashboard.routes.get_today_spend", AsyncMock(return_value=500)),
        patch("tourniquet.dashboard.routes.compute_insights", AsyncMock(return_value=_MOCK_INSIGHTS)),
    ):
        resp = client.get("/dashboard")

    assert resp.status_code == 200
    assert "ojw-swarm" in resp.text
    assert "<html" in resp.text.lower()


def test_dashboard_lists_multiple_keys(client):
    """GET /dashboard shows all key names in sidebar."""
    keys = [_make_key(name="alpha"), _make_key(name="beta")]
    cm = _make_session_cm(keys)

    with (
        patch("tourniquet.dashboard.routes.get_session", cm),
        patch("tourniquet.dashboard.routes.get_today_spend", AsyncMock(return_value=0)),
        patch("tourniquet.dashboard.routes.compute_insights", AsyncMock(return_value=_MOCK_INSIGHTS)),
    ):
        resp = client.get("/dashboard")

    assert resp.status_code == 200
    assert "alpha" in resp.text
    assert "beta" in resp.text


# ── GET /dashboard/key/<id> ────────────────────────────────────────────────────

def test_key_panel_missing_returns_404(client):
    """GET /dashboard/key/<unknown-id> returns 404."""
    cm = _make_session_cm([])

    with patch("tourniquet.dashboard.routes.get_session", cm):
        resp = client.get(f"/dashboard/key/{uuid.uuid4()}")

    assert resp.status_code == 404


def test_key_panel_returns_200(client):
    """GET /dashboard/key/<id> for a known key returns 200 full page."""
    key = _make_key(name="test-key")

    async def fake_spend(*args, **kwargs):
        return 200

    cm = _make_session_cm([key])
    with (
        patch("tourniquet.dashboard.routes.get_session", cm),
        patch("tourniquet.dashboard.routes.get_today_spend", AsyncMock(return_value=200)),
        patch("tourniquet.dashboard.routes.compute_insights", AsyncMock(return_value=MagicMock(
            by_model=[], by_caller=[], by_metadata_user_id=[],
            suggestions=[], api_key_name="test-key",
        ))),
    ):
        resp = client.get(f"/dashboard/key/{key.id}")

    assert resp.status_code == 200
    assert "test-key" in resp.text


def test_key_panel_htmx_returns_fragment(client):
    """GET /dashboard/key/<id> with HX-Request returns partial (no <html> shell)."""
    key = _make_key(name="partial-key")
    cm = _make_session_cm([key])

    with (
        patch("tourniquet.dashboard.routes.get_session", cm),
        patch("tourniquet.dashboard.routes.get_today_spend", AsyncMock(return_value=0)),
        patch("tourniquet.dashboard.routes.compute_insights", AsyncMock(return_value=MagicMock(
            by_model=[], by_caller=[], by_metadata_user_id=[],
            suggestions=[], api_key_name="partial-key",
        ))),
    ):
        resp = client.get(
            f"/dashboard/key/{key.id}",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    # Partial should not contain a full HTML shell
    assert "<html" not in resp.text.lower()
    assert "partial-key" in resp.text


# ── POST /dashboard/key/<id>/cap ───────────────────────────────────────────────

def test_update_cap(client):
    """POST /dashboard/key/<id>/cap with valid major-units updates daily_cap_usd_cents."""
    key = _make_key(daily_cap=500)
    cm = _make_session_cm([key], get_key=key)

    with (
        patch("tourniquet.dashboard.routes.get_session", cm),
        patch("tourniquet.dashboard.routes.get_today_spend", AsyncMock(return_value=0)),
    ):
        resp = client.post(
            f"/dashboard/key/{key.id}/cap",
            data={"daily_cap": "10.00"},
        )

    # Should return the control panel partial
    assert resp.status_code == 200


def test_update_cap_sets_cents(client):
    """POST /dashboard/key/<id>/cap converts major-units to cents correctly (USD)."""
    key = _make_key(daily_cap=500)
    captured = {}

    @asynccontextmanager
    async def tracking_cm():
        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [key]
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars_mock
        execute_result.scalar_one_or_none.return_value = key
        execute_result.all.return_value = []
        execute_result.first.return_value = None
        execute_result.scalar.return_value = 0
        session.execute = AsyncMock(return_value=execute_result)

        def _capture_set(value):
            captured["cap"] = value
            key.daily_cap_usd_cents = value

        key_mock = MagicMock()
        key_mock.id = key.id
        key_mock.name = key.name
        key_mock.profile = key.profile
        key_mock.kill_enabled = key.kill_enabled
        key_mock.auto_tune_mode = key.auto_tune_mode
        key_mock.absolute_ceiling_usd_cents = key.absolute_ceiling_usd_cents
        key_mock.lifted_cap_usd_cents = None
        key_mock.lift_expires_at = None

        type(key_mock).daily_cap_usd_cents = property(
            lambda self: captured.get("cap", 500),
            lambda self, v: captured.__setitem__("cap", v),
        )

        session.get = AsyncMock(return_value=key_mock)
        session.commit = AsyncMock()
        yield session

    with (
        patch("tourniquet.dashboard.routes.get_session", tracking_cm),
        patch("tourniquet.dashboard.routes.get_today_spend", AsyncMock(return_value=0)),
        patch("tourniquet.config.settings") as mock_settings,
    ):
        mock_settings.display_currency = "USD"
        mock_settings.fernet_key = "z4azMzwW477CRdx06P37hYjF01ccWZkc6J3gvxhGpHI="
        resp = client.post(
            f"/dashboard/key/{key.id}/cap",
            data={"daily_cap": "10.00"},
        )

    assert resp.status_code == 200


def test_update_cap_rejects_above_ceiling(client):
    """POST /dashboard/key/<id>/cap with cap > absolute_ceiling returns 422.

    The invariant `daily_cap <= absolute_ceiling` is enforced on the ceiling
    edit path; this guards the symmetric cap edit path so a manual edit can't
    silently violate it (auto-tune already clamps).
    """
    key = _make_key(daily_cap=500, absolute_ceiling=1000)
    cm = _make_session_cm([key], get_key=key)

    with (
        patch("tourniquet.dashboard.routes.get_session", cm),
        patch("tourniquet.dashboard.routes.get_today_spend", AsyncMock(return_value=0)),
    ):
        # ceiling is $10.00 (1000 cents). Submit $10.01 (1001 cents) — should reject.
        resp = client.post(
            f"/dashboard/key/{key.id}/cap",
            data={"daily_cap": "10.01"},
        )

    assert resp.status_code == 422
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
    detail = body.get("detail", "") if isinstance(body, dict) else body
    assert "ceiling" in detail.lower()


# ── POST /dashboard/key/<id>/lift ──────────────────────────────────────────────

def test_lift_multiplier(client):
    """POST /dashboard/key/<id>/lift with multiplier=2 sets lifted_cap to 2× base."""
    written: dict = {}

    class _MutableKey:
        id = uuid.uuid4()
        name = "test-lift"
        profile = "standard"
        kill_enabled = True
        auto_tune_mode = "off"
        daily_cap_usd_cents = 500
        absolute_ceiling_usd_cents = 5000
        lifted_cap_usd_cents = None
        lift_expires_at = None

    mutable = _MutableKey()

    @asynccontextmanager
    async def cm():
        session = AsyncMock()
        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = [mutable]
        execute_result.scalar_one_or_none.return_value = mutable
        execute_result.all.return_value = []
        execute_result.first.return_value = None
        execute_result.scalar.return_value = 0
        session.execute = AsyncMock(return_value=execute_result)
        session.get = AsyncMock(return_value=mutable)

        async def _commit():
            written["lifted"] = mutable.lifted_cap_usd_cents
            written["expires"] = mutable.lift_expires_at

        session.commit = _commit
        yield session

    with (
        patch("tourniquet.dashboard.routes.get_session", cm),
        patch("tourniquet.dashboard.routes.get_today_spend", AsyncMock(return_value=0)),
    ):
        resp = client.post(
            f"/dashboard/key/{mutable.id}/lift",
            data={"mode": "multiplier", "multiplier": "2"},
        )

    assert resp.status_code == 200
    assert written.get("lifted") == 1000
    assert written.get("expires") is not None


# ── POST /dashboard/key/<id>/rotate ───────────────────────────────────────────

def test_rotate_returns_token_in_html(client):
    """POST /dashboard/key/<id>/rotate returns the new token in the HTML body."""
    key = _make_key(name="rotate-me")
    cm = _make_session_cm([key], get_key=key)

    with patch("tourniquet.dashboard.routes.get_session", cm):
        resp = client.post(f"/dashboard/key/{key.id}/rotate")

    assert resp.status_code == 200
    assert "tq_" in resp.text
    # Must be on key_rotated page (full HTML)
    assert "<html" in resp.text.lower()


# ── HTMX partial routes return fragments ──────────────────────────────────────

@pytest.mark.parametrize("path_suffix", [
    "/spend-now",
    "/charts",
    "/alerts",
])
def test_partial_routes_return_no_html_shell(client, path_suffix):
    """HTMX partial routes return HTML fragments without a full <html> shell."""
    key = _make_key()
    cm = _make_session_cm([key])

    with (
        patch("tourniquet.dashboard.routes.get_session", cm),
        patch("tourniquet.dashboard.routes.get_today_spend", AsyncMock(return_value=0)),
        patch("tourniquet.dashboard.routes.compute_insights", AsyncMock(return_value=MagicMock(
            by_model=[], by_caller=[], by_metadata_user_id=[],
            suggestions=[], api_key_name=key.name,
        ))),
    ):
        resp = client.get(f"/dashboard/key/{key.id}{path_suffix}")

    assert resp.status_code == 200
    assert "<html" not in resp.text.lower()
