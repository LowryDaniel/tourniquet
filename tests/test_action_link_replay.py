"""Tests for action-link replay protection on admin POST endpoints."""

from __future__ import annotations

import threading
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

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


# ── m7: concurrent same-token race ───────────────────────────────────────────


def test_lift_by_amount_concurrent_replay_one_winner_one_loser():
    """m7: two concurrent same-token POSTs — exactly one succeeds.

    Simulates the production race that the partial unique index
    ``ix_api_key_actions_unique_token`` resolves: both requests pass
    ``_assert_token_unused`` (no row yet), both proceed to the apply, but only
    the first commit lands — the second commit raises IntegrityError, which
    the handler translates to a 400 ``_REPLAY_DETAIL`` response.

    We simulate the race by having the second invocation of
    ``_apply_lift_by_amount`` raise ``IntegrityError`` referencing the unique
    index name, the same way SQLAlchemy reports it for both Postgres and
    SQLite.
    """

    key_id = uuid.uuid4()
    amount = 500
    url = build_lift_by_amount_url(str(key_id), amount)
    token = url.split("token=")[1].split("&")[0]

    app = _make_app()

    call_count = {"n": 0}
    lock = threading.Lock()

    async def _race_apply(kid, amt, **kwargs):
        with lock:
            call_count["n"] += 1
            this_call = call_count["n"]
        if this_call == 1:
            return "test-key", 1000, False
        # Loser path — IntegrityError carrying the unique-index name so
        # _is_token_sig_conflict returns True and the handler maps it to 400.
        raise IntegrityError(
            statement="INSERT INTO api_key_actions ...",
            params=None,
            orig=Exception("UNIQUE constraint failed: ix_api_key_actions_unique_token"),
        )

    def _post_once():
        client = TestClient(app, raise_server_exceptions=True)
        with (
            patch("tourniquet.routes.admin.get_session", _make_no_replay_session()),
            patch("tourniquet.routes.admin._apply_lift_by_amount", _race_apply),
        ):
            return client.post(
                f"/admin/lift-by-amount/{key_id}",
                data={"token": token, "amount": str(amount)},
            )

    # Fire two concurrent requests on a thread pool. TestClient is sync; the
    # threading.Lock around call_count guarantees a deterministic winner/loser
    # ordering inside _race_apply, but both requests still execute the full
    # admin path concurrently.
    results: list = [None, None]

    def _runner(i: int) -> None:
        results[i] = _post_once()

    t1 = threading.Thread(target=_runner, args=(0,))
    t2 = threading.Thread(target=_runner, args=(1,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = sorted(r.status_code for r in results)
    # Exactly one 200 and one 400 — the index is the source of truth.
    assert statuses == [200, 400], f"expected one winner / one loser, got {statuses}"

    loser = next(r for r in results if r.status_code == 400)
    assert "already been used" in loser.text
