"""Tests for action-link replay protection on admin POST endpoints."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from tourniquet.routes.admin import build_lift_by_amount_url, router


def _make_app():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return app


def _make_no_replay_session():
    """Session where no existing audit row is found (token unused)."""

    @asynccontextmanager
    async def _session():
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=result)
        yield session

    return _session


def _make_has_replay_session():
    """Session where an existing audit row IS found (token already used)."""

    @asynccontextmanager
    async def _session():
        session = AsyncMock()
        result = MagicMock()
        # Return a non-None row — signals token already consumed
        result.scalar_one_or_none = MagicMock(return_value=MagicMock())
        session.execute = AsyncMock(return_value=result)
        yield session

    return _session


# ── lift-by-amount replay protection ─────────────────────────────────────────

def test_lift_by_amount_second_use_returns_400():
    """Using a lift-by-amount token twice: first use succeeds, second returns 400
    and the cap is bumped only once."""

    key_id = uuid.uuid4()
    amount = 500  # $5.00
    url = build_lift_by_amount_url(str(key_id), amount)
    token = url.split("token=")[1].split("&")[0]

    app = _make_app()
    client = TestClient(app, raise_server_exceptions=True)

    bumped = {"count": 0}

    async def _fake_apply(kid, amt, **kwargs):
        bumped["count"] += 1
        return "test-key", 1000, False

    # First call: no replay row → succeeds
    with (
        patch("tourniquet.routes.admin.get_session", _make_no_replay_session()),
        patch("tourniquet.routes.admin._apply_lift_by_amount", _fake_apply),
    ):
        resp1 = client.post(
            f"/admin/lift-by-amount/{key_id}",
            data={"token": token, "amount": str(amount)},
        )

    assert resp1.status_code == 200
    assert bumped["count"] == 1

    # Second call: replay row found → rejected
    with (
        patch("tourniquet.routes.admin.get_session", _make_has_replay_session()),
        patch("tourniquet.routes.admin._apply_lift_by_amount", _fake_apply),
    ):
        resp2 = client.post(
            f"/admin/lift-by-amount/{key_id}",
            data={"token": token, "amount": str(amount)},
        )

    assert resp2.status_code == 400
    # _apply_lift_by_amount was never called a second time — cap bumped exactly once
    assert bumped["count"] == 1
