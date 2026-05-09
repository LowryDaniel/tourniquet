"""C1: cap enforcement under concurrency.

The old `proxy_messages` flow read `spent_cents`, decided 402-or-pass, then
wrote spend after the upstream call settled. Under bursty concurrency
(Claude Code firing 5–20 parallel tool calls on the same key) every parallel
request observed the same stale `spent_cents` and all passed — the cap was
soft. The fix is an atomic `INSERT ... ON CONFLICT DO UPDATE WHERE` reservation.

Two tests:

1. `test_concurrent_requests_respect_cap` — fire 10 `asyncio.gather` requests
   at a $1.00 cap with $0.10/request worst-case; assert exactly the number
   that fit (10) succeed if the worst-case fits 10×, OR the fitting count
   is enforced exactly under tighter caps. The exact prediction:
   `floor(cap / per_request_worst_case)` succeed, the remainder hit 402.

2. `test_streaming_reservation_reconciles_overestimate` — issue a streaming
   request whose worst-case is $0.50 but actual is $0.10; assert post-stream
   `caps_today.total` reflects $0.10, not $0.50.

Both tests run against real SQLite (file-backed for cross-session ACID)
because the whole point is concurrent SQL contention; an AsyncMock can't
reproduce that.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import date

import httpx
import pytest
import pytest_asyncio
import respx
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tourniquet.config import settings as app_settings
from tourniquet.models import ApiKey, Base


# ── Test infra: real SQLite with schema, real ApiKey row ──────────────────────


@pytest_asyncio.fixture()
async def db_engine():
    """File-backed SQLite engine so concurrent sessions share a journal.

    `:memory:` per-engine connections do NOT share a database; for the
    concurrency test we need actual cross-session ACID, which means a
    file-backed DB (or a `:memory:` shared cache, but file is simpler).
    """
    fd, path = tempfile.mkstemp(prefix="tq_concurrency_", suffix=".db")
    os.close(fd)
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest_asyncio.fixture()
async def session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest_asyncio.fixture()
async def make_get_session(session_factory):
    """Build a `get_session` shim that mirrors the production asynccontextmanager
    contract but binds to our test engine."""

    @asynccontextmanager
    async def _get_session():
        async with session_factory() as s:
            yield s

    return _get_session


@pytest_asyncio.fixture()
async def seeded_key(session_factory):
    """Insert a usable ApiKey row and return (token, key_id, fernet_encrypted_anthropic_key)."""
    token = "tq_concurrency_test_token"
    sha = hashlib.sha256(token.encode()).hexdigest()

    f = Fernet(app_settings.fernet_key.encode())
    enc_anthropic = f.encrypt(b"sk-ant-test-fixture").decode()

    user_id = uuid.uuid4()
    key_id = uuid.uuid4()

    async with session_factory() as s:
        # User row first — ApiKey FKs to users.id.
        await s.execute(
            text(
                "INSERT INTO users (id, email, created_at) "
                "VALUES (:id, :email, CURRENT_TIMESTAMP)"
            ),
            {"id": str(user_id), "email": f"u-{user_id}@example.com"},
        )
        s.add(
            ApiKey(
                id=key_id,
                user_id=user_id,
                name="concurrency-test",
                tq_token_hash="$2b$12$placeholder",  # SHA path is the fast path, bcrypt unused
                tq_token_sha256=sha,
                anthropic_key_encrypted=enc_anthropic,
                profile="standard",
                daily_cap_usd_cents=100,  # default for cap-bust test: overridden per-test below
                kill_enabled=True,
                absolute_ceiling_usd_cents=10000,
            )
        )
        await s.commit()

    return {"token": token, "key_id": key_id, "user_id": user_id}


@pytest_asyncio.fixture()
async def patch_router_session(monkeypatch, make_get_session):
    """Point `tourniquet.proxy.router.get_session` at the test DB."""
    import tourniquet.proxy.router as router_mod

    monkeypatch.setattr(router_mod, "get_session", make_get_session)
    yield


def _set_cap(session_factory, key_id: uuid.UUID, cap_cents: int):
    """Helper: synchronously update a key's daily_cap_usd_cents via a fresh session."""

    async def _do():
        async with session_factory() as s:
            await s.execute(
                text("UPDATE api_keys SET daily_cap_usd_cents = :c WHERE id = :id"),
                {"c": cap_cents, "id": str(key_id)},
            )
            await s.commit()

    return _do


async def _read_caps_today(session_factory, key_id: uuid.UUID) -> int:
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT total_usd_cents FROM caps_today "
                    "WHERE api_key_id = :id AND date = :d"
                ),
                {"id": str(key_id), "d": date.today()},
            )
        ).first()
        return row[0] if row else 0


# ── Test 1: concurrent burst against a tight cap ──────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_requests_respect_cap(
    seeded_key, session_factory, patch_router_session
):
    """Fire 10 parallel POST /v1/messages at a key whose effective cap can only
    accommodate floor(cap / per-request worst-case) of them. The rest must 402.

    Math: cap=$1.00=100c. Per-request worst-case is set so `cost_usd_cents`
    returns 10c (so ~10 fit, 0 fail) — but to demonstrate enforcement under
    concurrency we tighten the cap to 50c so exactly 5 fit and 5 fail.

    The assertion isn't that "exactly N succeed every run" by stochastic
    interleaving — it's that the *combined committed spend* never exceeds
    the cap, AND that the count of 200s + count of 402s = 10.
    """
    # Tight cap: 50¢ — at most 4 of 10 11¢-reservation requests fit.
    await _set_cap(session_factory, seeded_key["key_id"], 50)()

    # Mock Anthropic upstream — return a small 1¢ response so reconciliation
    # refunds most of each ~11¢ reservation. The CAP test cares about the
    # reservation, not the actual cost.
    upstream_body = json.dumps({
        "id": "msg_concurrency",
        "model": "claude-haiku-4-5-20251001",  # cheap model
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "content": [],
        "role": "assistant",
        "stop_reason": "end_turn",
        "type": "message",
    })

    # Build a request body whose pre-flight worst-case is ~11¢ on haiku-4-5
    # (input $0.80/M, output $4/M). max_tokens=25_000 → 25_000*400/1M = 10c
    # plus ~1c rounding → 11c reserved per request.
    request_body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 25_000,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()

    # Slow the upstream so reservations stack up BEFORE any reconcile fires.
    # On a single SQLite engine, requests serialize at the I/O layer; the
    # only way to reproduce the original race is to delay the upstream so
    # multiple reservations are committed before the first reconcile lands.
    # This mirrors real-world latency (Anthropic responses ~hundreds of ms
    # to seconds; reconciliation runs only after the full response is read).
    async def _slow_upstream(_req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(
            200,
            content=upstream_body,
            headers={"content-type": "application/json"},
        )

    from tourniquet.main import app

    async def _fire_one(client: httpx.AsyncClient) -> int:
        resp = await client.post(
            "/v1/messages",
            content=request_body,
            headers={
                "authorization": f"Bearer {seeded_key['token']}",
                "content-type": "application/json",
            },
        )
        return resp.status_code

    with respx.mock(assert_all_called=False) as rsx:
        rsx.post("https://api.anthropic.com/v1/messages").mock(side_effect=_slow_upstream)
        # Avoid noisy unmocked alert-channel requests if a threshold fires.
        rsx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        rsx.route(host="api.telegram.org").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=10.0
        ) as client:
            statuses = await asyncio.gather(*[_fire_one(client) for _ in range(10)])

    # 10 outcomes total.
    assert len(statuses) == 10, statuses
    succeeded = [s for s in statuses if s == 200]
    refused = [s for s in statuses if s == 402]
    assert len(succeeded) + len(refused) == 10, statuses
    # Cap of 50¢ with 11¢ reservations → at most floor(50/11) = 4 succeed.
    # The C1 race would let MORE through (all 10 in the worst case).
    assert len(succeeded) <= 4, (
        f"more requests passed than the cap allows: {statuses} — cap is soft"
    )
    # The reservation should usually fill the cap completely (4 succeed
    # under contention), so we also assert at least 1 — otherwise the
    # mocks aren't wired right.
    assert len(succeeded) >= 1, (
        f"no requests succeeded — check upstream mock: {statuses}"
    )

    # The hard-cap test: total committed spend must never exceed the cap,
    # even with 10 simultaneous requests racing the reservation.
    final_total = await _read_caps_today(session_factory, seeded_key["key_id"])
    assert final_total <= 50, (
        f"caps_today total {final_total}c exceeds cap of 50c — cap is soft"
    )


# ── Test 2: streaming over-estimate gets refunded ─────────────────────────────


@pytest.mark.asyncio
async def test_streaming_reservation_reconciles_overestimate(
    seeded_key, session_factory, patch_router_session
):
    """A streaming request reserves $0.50 worst-case but the actual SSE flow
    only books $0.10 of usage. After the stream completes, caps_today.total
    must reflect the actual $0.10, not the reserved $0.50.

    Setup:
      - Cap = $5.00 = 500¢ (plenty of room for the reservation).
      - max_tokens = 125_000 on haiku → reservation ≈ 50¢.
      - Upstream SSE reports input=2_500 output=2_500 on haiku → ~1¢ actual,
        so the refund is ~49¢. We don't pin the exact penny because rounding
        differs by a cent depending on input chars; we assert the strong
        invariant: final spend < reservation.
    """
    # Cap: 500¢ ($5) — comfortably above the 50¢ reservation.
    await _set_cap(session_factory, seeded_key["key_id"], 500)()

    sse_response = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_stream","model":"claude-haiku-4-5-20251001","usage":{"input_tokens":2500}}}\n\n'
        'event: content_block_start\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        'event: content_block_stop\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2500}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )

    request_body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 125_000,  # worst-case ≈ 50¢ on haiku
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()

    from tourniquet.main import app

    with respx.mock(assert_all_called=False) as rsx:
        rsx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200,
                text=sse_response,
                headers={"content-type": "text/event-stream"},
            )
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30.0
        ) as client:
            async with client.stream(
                "POST",
                "/v1/messages",
                content=request_body,
                headers={
                    "authorization": f"Bearer {seeded_key['token']}",
                    "content-type": "application/json",
                },
            ) as resp:
                # Drain the SSE so the proxy's _generate() finishes and runs
                # the post-stream reconcile path.
                body_bytes = b""
                async for chunk in resp.aiter_bytes():
                    body_bytes += chunk
                assert resp.status_code == 200, body_bytes

    # Compute expectations with the same pricing function the production code uses.
    from tourniquet.billing.pricing import cost_usd_cents
    actual_cost = cost_usd_cents("claude-haiku-4-5-20251001", 2500, 2500)

    final_total = await _read_caps_today(session_factory, seeded_key["key_id"])

    # The big-picture invariant: reconciliation refunded the over-estimate.
    # Reservation alone would have left ~50¢ booked. Actual cost is ~1¢.
    # Final caps_today must reflect the actual, not the reservation.
    assert final_total == actual_cost, (
        f"caps_today total {final_total}c != actual cost {actual_cost}c — "
        f"reconciliation didn't run or under/over-applied. (Reservation was "
        f"~50c worst-case; refund of (actual − reserved) should bring total "
        f"to actual_cost.)"
    )
    # And the strong invariant: final < the worst-case reservation.
    # If the reservation refund didn't run, final_total would be ~50¢.
    assert final_total < 50, (
        f"caps_today still reflects worst-case reservation ({final_total}c) "
        "instead of reconciled actual cost — over-estimate not refunded"
    )
