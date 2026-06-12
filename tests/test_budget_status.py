"""Tests for GET /v1/budget-status.

Hermetic: no database, no network, no env secrets beyond what conftest
already satisfies. The DB session and key-resolution logic are mocked.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tourniquet.proxy.router import router


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _make_key(
    daily_cap: int = 1000,
    lifted_cap: int | None = None,
    lift_expires_at: datetime | None = None,
) -> MagicMock:
    key = MagicMock()
    key.id = uuid.uuid4()
    key.tq_token_sha256 = _sha256("tq_testtoken")
    key.tq_token_hash = b"$2b$12$fake"
    key.daily_cap_usd_cents = daily_cap
    key.lifted_cap_usd_cents = lifted_cap
    key.lift_expires_at = lift_expires_at
    return key


@pytest.fixture()
def client() -> TestClient:
    return TestClient(_make_app(), raise_server_exceptions=True)


def _patch_db(key: MagicMock, spent: int):
    """Context manager: stubs _resolve_api_key + get_today_spend."""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    return [
        patch("tourniquet.proxy.router._resolve_api_key", new=AsyncMock(return_value=key)),
        patch("tourniquet.proxy.router.get_today_spend", new=AsyncMock(return_value=spent)),
        patch("tourniquet.proxy.router.get_session", return_value=mock_session),
    ]


class TestBudgetStatus:
    def test_no_auth_returns_401(self, client: TestClient):
        resp = client.get("/v1/budget-status")
        assert resp.status_code == 401

    def test_basic_response_shape(self, client: TestClient):
        key = _make_key(daily_cap=1000)
        patches = _patch_db(key, spent=300)
        with patches[0], patches[1], patches[2]:
            resp = client.get(
                "/v1/budget-status",
                headers={"authorization": "Bearer tq_testtoken"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["spent_usd_cents"] == 300
        assert body["cap_usd_cents"] == 1000
        assert body["remaining_usd_cents"] == 700
        assert body["percent_used"] == 30.0
        assert body["throttle_advised"] is False

    def test_throttle_advised_above_85_percent(self, client: TestClient):
        key = _make_key(daily_cap=1000)
        patches = _patch_db(key, spent=900)
        with patches[0], patches[1], patches[2]:
            resp = client.get(
                "/v1/budget-status",
                headers={"authorization": "Bearer tq_testtoken"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["throttle_advised"] is True
        assert body["percent_used"] == 90.0
        assert body["remaining_usd_cents"] == 100

    def test_throttle_not_advised_at_exactly_85_percent(self, client: TestClient):
        key = _make_key(daily_cap=1000)
        patches = _patch_db(key, spent=850)
        with patches[0], patches[1], patches[2]:
            resp = client.get(
                "/v1/budget-status",
                headers={"authorization": "Bearer tq_testtoken"},
            )
        body = resp.json()
        assert body["throttle_advised"] is False  # > 85, not >=

    def test_over_cap_remaining_is_zero(self, client: TestClient):
        """remaining_usd_cents is floored at 0 — never goes negative."""
        key = _make_key(daily_cap=500)
        patches = _patch_db(key, spent=600)
        with patches[0], patches[1], patches[2]:
            resp = client.get(
                "/v1/budget-status",
                headers={"authorization": "Bearer tq_testtoken"},
            )
        body = resp.json()
        assert body["remaining_usd_cents"] == 0
        assert body["percent_used"] == 120.0
        assert body["throttle_advised"] is True

    def test_active_lift_reported_in_cap(self, client: TestClient):
        """When a cap lift is active the lifted cap is reported, not daily_cap."""
        future = datetime.now(UTC) + timedelta(hours=2)
        key = _make_key(daily_cap=500, lifted_cap=1000, lift_expires_at=future)
        patches = _patch_db(key, spent=200)
        with patches[0], patches[1], patches[2]:
            resp = client.get(
                "/v1/budget-status",
                headers={"authorization": "Bearer tq_testtoken"},
            )
        body = resp.json()
        assert body["cap_usd_cents"] == 1000
        assert body["remaining_usd_cents"] == 800
        assert body["percent_used"] == 20.0
