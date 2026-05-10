"""C2 — XSS regression tests for admin HTML pages.

Each admin endpoint renders user-controlled fields (key_name, mode, amount,
new_cap, etc.) into HTML. These tests assert that a key named with raw HTML
markup is rendered with HTML entities — i.e. autoescape is on and a malicious
key name cannot inject <script>.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tourniquet.routes.admin import (
    build_kill_now_url,
    build_lift_by_amount_url,
    build_lift_mode_url,
    router,
)

_XSS_NAME = "<script>alert(1)</script>"
_ESCAPED_PREFIX = "&lt;script&gt;"


def _make_app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(_make_app(), raise_server_exceptions=True)


def _session_returning(key) -> object:
    """Build a get_session() asynccontextmanager that yields a session whose
    ``session.get(...)`` returns ``key`` and whose ``session.execute(...)``
    returns no replay row.
    """

    @asynccontextmanager
    async def _session():
        session = AsyncMock()
        session.get = AsyncMock(return_value=key)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=result)
        session.commit = AsyncMock()
        session.add = MagicMock()
        session.rollback = AsyncMock()
        yield session

    return _session


def _xss_key(daily_cap: int = 1000, lifted: int | None = None, ceiling: int = 5000) -> MagicMock:
    k = MagicMock()
    k.id = uuid.uuid4()
    k.name = _XSS_NAME
    k.daily_cap_usd_cents = daily_cap
    k.lifted_cap_usd_cents = lifted
    k.absolute_ceiling_usd_cents = ceiling
    return k


def _assert_escaped(body: str) -> None:
    assert _XSS_NAME not in body, "raw <script> leaked through autoescape"
    assert _ESCAPED_PREFIX in body, "expected &lt;script&gt; in escaped output"


# ── kill-now ──────────────────────────────────────────────────────────────────


def test_kill_now_confirm_escapes_key_name(client):
    key = _xss_key()
    key_id = key.id
    url = build_kill_now_url(str(key_id))
    token = url.split("token=")[1]

    with patch("tourniquet.routes.admin.get_session", _session_returning(key)):
        resp = client.get(f"/admin/kill-now/{key_id}?token={token}")

    assert resp.status_code == 200
    _assert_escaped(resp.text)


def test_kill_now_applied_escapes_key_name(client):
    key_id = uuid.uuid4()
    url = build_kill_now_url(str(key_id))
    token = url.split("token=")[1]

    async def _fake_apply(kuid, **kwargs):
        return _XSS_NAME, 420

    async def _fake_alert(*args, **kwargs):
        return None

    with (
        patch("tourniquet.routes.admin._apply_kill_now", _fake_apply),
        patch("tourniquet.routes.admin._fire_recovery_alert", _fake_alert),
        patch("tourniquet.routes.admin.get_session", _session_returning(MagicMock())),
        patch(
            "tourniquet.alerts.notifier.recovery_amounts_cents",
            return_value=[100, 500],
        ),
    ):
        resp = client.post(f"/admin/kill-now/{key_id}", data={"token": token})

    assert resp.status_code == 200
    _assert_escaped(resp.text)


# ── lift-mode ─────────────────────────────────────────────────────────────────


def test_lift_mode_confirm_escapes_key_name(client):
    key = _xss_key()
    key_id = key.id
    url = build_lift_mode_url(str(key_id), "2x")
    token = url.split("token=")[1].split("&")[0]

    with patch("tourniquet.routes.admin.get_session", _session_returning(key)):
        resp = client.get(f"/admin/lift-mode/{key_id}?token={token}&mode=2x")

    assert resp.status_code == 200
    _assert_escaped(resp.text)


def test_lift_mode_applied_escapes_key_name(client):
    key = _xss_key()
    key_id = key.id
    url = build_lift_mode_url(str(key_id), "2x")
    token = url.split("token=")[1].split("&")[0]

    with patch("tourniquet.routes.admin.get_session", _session_returning(key)):
        resp = client.post(
            f"/admin/lift-mode/{key_id}",
            data={"token": token, "mode": "2x"},
        )

    assert resp.status_code == 200
    _assert_escaped(resp.text)


# ── lift-by-amount ────────────────────────────────────────────────────────────


def test_lift_by_amount_confirm_escapes_key_name(client):
    key = _xss_key()
    key_id = key.id
    amount = 500
    url = build_lift_by_amount_url(str(key_id), amount)
    token = url.split("token=")[1].split("&")[0]

    with patch("tourniquet.routes.admin.get_session", _session_returning(key)):
        resp = client.get(f"/admin/lift-by-amount/{key_id}?token={token}&amount={amount}")

    assert resp.status_code == 200
    _assert_escaped(resp.text)


def test_lift_by_amount_applied_escapes_key_name(client):
    key_id = uuid.uuid4()
    amount = 500
    url = build_lift_by_amount_url(str(key_id), amount)
    token = url.split("token=")[1].split("&")[0]

    async def _fake_apply(kid, amt, **kwargs):
        return _XSS_NAME, 1500, False

    with (
        patch("tourniquet.routes.admin._apply_lift_by_amount", _fake_apply),
        patch("tourniquet.routes.admin.get_session", _session_returning(MagicMock())),
    ):
        resp = client.post(
            f"/admin/lift-by-amount/{key_id}",
            data={"token": token, "amount": str(amount)},
        )

    assert resp.status_code == 200
    _assert_escaped(resp.text)
