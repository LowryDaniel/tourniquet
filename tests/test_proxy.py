"""Proxy integration tests.

Three critical scenarios:
1. Request under cap → proxied cleanly, usage persisted
2. Request that crosses cap mid-stream → synthetic message_stop injected, connection closed
3. Request on a different key (multi-key isolation) → cap from the correct key used
"""

# Tests are stubs — implementations added during W1 build.

import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import bcrypt
import httpx
import pytest
import respx

from tourniquet.providers.anthropic import stream_request


@pytest.mark.asyncio
async def test_health(client):  # type: ignore[no-untyped-def]
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_proxy_under_cap():
    """Proxied request under cap returns Anthropic response unchanged."""
    pytest.skip("implement in W1 with respx mock")


@pytest.mark.asyncio
async def test_proxy_cap_hit_mid_stream():
    """Request that crosses cap mid-stream receives synthetic message_stop."""
    pytest.skip("implement in W1 with respx mock")


@pytest.mark.asyncio
async def test_multi_key_isolation():
    """Key A's spend does not affect Key B's cap."""
    pytest.skip("implement in W1")


# ─────────────────────────────────────────────────────────────────────────────
# M2: streaming cap-hit signal shape — see docs/code-review-remediation.md.
# These tests exercise the streaming path end-to-end via the provider so the
# emitted bytes match what a downstream Anthropic SDK would actually parse.
# ─────────────────────────────────────────────────────────────────────────────


def _split_sse_blocks(body: str) -> list[dict[str, str]]:
    """Parse an SSE byte-stream body into a list of {event, data} dicts."""
    blocks: list[dict[str, str]] = []
    for raw_block in body.split("\n\n"):
        block = raw_block.strip()
        if not block:
            continue
        ev: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith("event:"):
                ev["event"] = line[len("event:"):].strip()
            elif line.startswith("data:"):
                ev["data"] = line[len("data:"):].strip()
        if ev:
            blocks.append(ev)
    return blocks


@pytest.mark.asyncio
async def test_streaming_cap_hit_uses_documented_stop_reason():
    """M2: when the cap is hit mid-stream, the synthetic message_stop carries
    `stop_reason: end_turn` (a documented Anthropic enum) so strict SDKs do
    not reject the event.
    """
    sse_response = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_cap","model":"claude-sonnet-4-6","usage":{"input_tokens":1000000}}}\n\n'
    )

    async def hit_immediately(acc):
        return acc.input_tokens > 0

    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, text=sse_response, headers={"content-type": "text/event-stream"})
        )

        chunks = []
        async for chunk, _acc in stream_request(
            anthropic_key="sk-ant-test",
            request_body=b'{"model":"claude-sonnet-4-6","messages":[],"stream":true}',
            headers={"content-type": "application/json"},
            on_cap_check=hit_immediately,
        ):
            chunks.append(chunk)

        body = b"".join(chunks).decode()
        blocks = _split_sse_blocks(body)

        # Find the synthetic message_stop block.
        stop_blocks = [b for b in blocks if b.get("event") == "message_stop"]
        assert stop_blocks, f"no message_stop block in body:\n{body}"
        # The synthetic stop block — last one — must use documented enum.
        synthetic = stop_blocks[-1]
        payload = json.loads(synthetic["data"])
        assert payload["stop_reason"] == "end_turn", (
            f"expected stop_reason=end_turn, got {payload}"
        )
        # And the legacy unknown-enum value must not appear in the stop block.
        assert "tourniquet_cap_hit" not in synthetic["data"]


@pytest.mark.asyncio
async def test_streaming_cap_hit_emits_tourniquet_error_event():
    """M2: cap-hit signaling rides on a separate `event: error` SSE block
    that carries the documented tourniquet_cap_hit JSON. SDKs that surface
    unknown SSE events expose this; clients that ignore unknown events still
    get a clean end_turn from the message_stop block.
    """
    sse_response = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_cap","model":"claude-sonnet-4-6","usage":{"input_tokens":1000000}}}\n\n'
    )

    async def hit_immediately(acc):
        return acc.input_tokens > 0

    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, text=sse_response, headers={"content-type": "text/event-stream"})
        )

        chunks = []
        async for chunk, _acc in stream_request(
            anthropic_key="sk-ant-test",
            request_body=b'{"model":"claude-sonnet-4-6","messages":[],"stream":true}',
            headers={"content-type": "application/json"},
            on_cap_check=hit_immediately,
        ):
            chunks.append(chunk)

        body = b"".join(chunks).decode()
        blocks = _split_sse_blocks(body)

        error_blocks = [b for b in blocks if b.get("event") == "error"]
        assert error_blocks, f"no `event: error` block in body:\n{body}"
        err_payload = json.loads(error_blocks[-1]["data"])
        assert err_payload["type"] == "error"
        assert err_payload["error"]["type"] == "tourniquet_cap_hit"
        # Schema sanity — the documented fields are present.
        assert "cap_usd_cents" in err_payload["error"]
        assert "spent_usd_cents" in err_payload["error"]
        assert "resets_at" in err_payload["error"]


# ─────────────────────────────────────────────────────────────────────────────
# C3: SHA-256 indexed token-auth fast path. The proxy used to bcrypt-scan
# every ApiKey row per request — these tests pin the new behaviour so we
# can drop the bcrypt fallback in v0.2 without regressing.
# ─────────────────────────────────────────────────────────────────────────────


def _build_fake_key(token: str, *, with_sha256: bool = True) -> MagicMock:
    """Mock ApiKey row. Always has bcrypt hash; sha256 column is optional
    so we can simulate legacy rows that pre-date C3."""
    key = MagicMock()
    key.id = uuid.uuid4()
    key.name = "k"
    key.tq_token_hash = bcrypt.hashpw(token.encode(), bcrypt.gensalt()).decode()
    key.tq_token_sha256 = (
        hashlib.sha256(token.encode()).hexdigest() if with_sha256 else None
    )
    return key


def _make_query_counting_session(
    rows_by_predicate: dict[str, list],
) -> tuple[AsyncMock, list[str]]:
    """Build an AsyncMock session that records each `execute()` call and
    returns a result whose `.scalar_one_or_none()` / `.scalars().all()`
    are wired off whichever bucket the SQL hit.

    `rows_by_predicate` keys: "sha256" for the fast-path SELECT,
    "is_null" for the legacy bcrypt scan SELECT. Empty list → miss.
    """
    queries: list[str] = []
    session = AsyncMock()

    async def _execute(stmt):
        sql = str(stmt.compile(compile_kwargs={"literal_binds": False}))
        # Heuristic: the sha256 fast path filters by tq_token_sha256 = :param;
        # the legacy scan filters by tq_token_sha256 IS NULL.
        if "IS NULL" in sql.upper():
            queries.append("is_null")
            rows = rows_by_predicate.get("is_null", [])
        else:
            queries.append("sha256")
            rows = rows_by_predicate.get("sha256", [])
        result = MagicMock()
        result.scalar_one_or_none.return_value = rows[0] if rows else None
        scalars = MagicMock()
        scalars.all.return_value = rows
        result.scalars.return_value = scalars
        return result

    session.execute = AsyncMock(side_effect=_execute)
    session.commit = AsyncMock()
    return session, queries


@pytest.mark.asyncio
async def test_proxy_auth_uses_sha256_lookup():
    """C3: a token whose sha256 is already populated resolves with EXACTLY
    one indexed SELECT — no bcrypt fanout, no second query."""
    from tourniquet.proxy.router import _resolve_api_key

    token = "tq_fast_path_token"
    fake_key = _build_fake_key(token, with_sha256=True)
    session, queries = _make_query_counting_session({"sha256": [fake_key]})

    resolved = await _resolve_api_key(f"Bearer {token}", session)

    assert resolved is fake_key
    assert queries == ["sha256"], (
        f"expected one indexed SELECT, got {queries}"
    )
    # The fast path must not commit — there's nothing to backfill.
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_bcrypt_token_still_works():
    """C3: a token minted before the migration (sha256 column NULL) must
    still authenticate via bcrypt, AND the sha256 column must be backfilled
    so the next request hits the fast path."""
    from tourniquet.proxy.router import _resolve_api_key

    token = "tq_legacy_token"
    legacy_key = _build_fake_key(token, with_sha256=False)
    assert legacy_key.tq_token_sha256 is None  # pre-condition
    session, queries = _make_query_counting_session({
        "sha256": [],            # fast path misses
        "is_null": [legacy_key], # legacy scan finds it
    })

    resolved = await _resolve_api_key(f"Bearer {token}", session)

    assert resolved is legacy_key
    # Fast path runs first, then the legacy scan — exactly two queries.
    assert queries == ["sha256", "is_null"], queries
    # Backfill: the row must now carry the sha256 hex so subsequent
    # requests short-circuit to the indexed path.
    expected_sha = hashlib.sha256(token.encode()).hexdigest()
    assert legacy_key.tq_token_sha256 == expected_sha
    # And the backfill must have been committed.
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_proxy_auth_rejects_unknown_token():
    """C3: an unknown bearer token returns 401 fast — the bcrypt scan only
    runs against legacy rows (tq_token_sha256 IS NULL), so with no legacy
    rows the rejection is effectively two indexed lookups, not a fanout."""
    from fastapi import HTTPException

    from tourniquet.proxy.router import _resolve_api_key

    session, queries = _make_query_counting_session({
        "sha256": [],   # fast path misses
        "is_null": [],  # no legacy rows to bcrypt-check
    })

    t0 = time.perf_counter()
    with pytest.raises(HTTPException) as exc_info:
        await _resolve_api_key("Bearer tq_does_not_exist", session)
    elapsed = time.perf_counter() - t0

    assert exc_info.value.status_code == 401
    # No bcrypt fanout: rejection took two trivial DB lookups.
    assert queries == ["sha256", "is_null"], queries
    # Bcrypt at default cost is ~100ms+ per check; with no rows to check,
    # this whole path should resolve in milliseconds.
    assert elapsed < 0.05, (
        f"unknown-token rejection took {elapsed * 1000:.1f}ms — should be <50ms"
    )


# ─────────────────────────────────────────────────────────────────────────────
# M4 + M5: request-body ceiling and idempotency-key forwarding.
# ─────────────────────────────────────────────────────────────────────────────


def test_proxy_rejects_oversized_body(client, monkeypatch):
    """M4: a body larger than `settings.max_request_body_bytes` is refused with
    413 Payload Too Large *before* any DB lookup runs. The body-size check
    fires inside the streamed read, so a malicious 1GB POST never gets buffered
    fully into memory.
    """
    # Tighten the ceiling so the test stays fast (1 KiB instead of 10 MiB).
    import tourniquet.config as cfg
    monkeypatch.setattr(cfg.settings, "max_request_body_bytes", 1024)

    # Body just over the configured ceiling.
    payload = b"x" * 2048
    resp = client.post(
        "/v1/messages",
        content=payload,
        headers={
            "authorization": "Bearer tq_does_not_matter",  # body check fires first
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 413, resp.text
    assert "exceeds" in resp.text.lower()


@pytest.mark.asyncio
async def test_proxy_forwards_idempotency_key(monkeypatch):
    """M5: the proxy forwards the `idempotency-key` header upstream. Anthropic
    treats this header as a retry-safety token; stripping it makes client
    retries double-bill. The whitelist is the single source of truth in
    `providers/anthropic.py:FORWARD_HEADERS` — this test pins the contract.
    """
    import os
    import tempfile

    from cryptography.fernet import Fernet
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from tourniquet.config import settings as app_settings
    from tourniquet.models import ApiKey, Base

    # ── Spin up a file-backed SQLite for cross-session ACID ────────────────
    fd, path = tempfile.mkstemp(prefix="tq_idempotency_", suffix=".db")
    os.close(fd)
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        # ── Seed an ApiKey + matching User ─────────────────────────────────
        token = "tq_idempotency_test_token"
        sha = hashlib.sha256(token.encode()).hexdigest()
        f = Fernet(app_settings.fernet_key.encode())
        enc_anthropic = f.encrypt(b"sk-ant-test-fixture").decode()

        user_id = uuid.uuid4()
        key_id = uuid.uuid4()
        async with session_factory() as s:
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
                    name="idempotency-test",
                    tq_token_hash="$2b$12$placeholder",
                    tq_token_sha256=sha,
                    anthropic_key_encrypted=enc_anthropic,
                    profile="standard",
                    daily_cap_usd_cents=10_000,  # plenty of room
                    kill_enabled=True,
                    absolute_ceiling_usd_cents=10_000,
                )
            )
            await s.commit()

        # ── Patch get_session in the router to point at our test engine ───
        @asynccontextmanager
        async def _get_session():
            async with session_factory() as sess:
                yield sess

        import tourniquet.proxy.router as router_mod
        monkeypatch.setattr(router_mod, "get_session", _get_session)

        # ── Capture the upstream request via respx ─────────────────────────
        captured: dict[str, str] = {}

        def _record(req: httpx.Request) -> httpx.Response:
            for hk, hv in req.headers.items():
                captured[hk.lower()] = hv
            return httpx.Response(
                200,
                content=json.dumps({
                    "id": "msg_idem",
                    "model": "claude-haiku-4-5-20251001",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [],
                    "role": "assistant",
                    "stop_reason": "end_turn",
                    "type": "message",
                }),
                headers={"content-type": "application/json"},
            )

        request_body = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()

        from tourniquet.main import app

        with respx.mock(assert_all_called=False) as rsx:
            rsx.post("https://api.anthropic.com/v1/messages").mock(side_effect=_record)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver", timeout=10.0
            ) as ac:
                resp = await ac.post(
                    "/v1/messages",
                    content=request_body,
                    headers={
                        "authorization": f"Bearer {token}",
                        "content-type": "application/json",
                        "idempotency-key": "11111111-2222-3333-4444-555555555555",
                        # SDK fingerprint headers — also on the whitelist.
                        "x-stainless-lang": "python",
                        # A header that must NOT be forwarded.
                        "user-agent": "secret-leak-canary/1.0",
                    },
                )

        assert resp.status_code == 200, resp.text
        # The forwarded set must include idempotency-key and the stainless one.
        assert captured.get("idempotency-key") == "11111111-2222-3333-4444-555555555555", (
            f"idempotency-key not forwarded; upstream saw: {sorted(captured.keys())}"
        )
        assert captured.get("x-stainless-lang") == "python", (
            f"x-stainless-lang not forwarded; upstream saw: {sorted(captured.keys())}"
        )
        # The whitelist is exclusive — non-listed headers (user-agent) are dropped.
        assert "secret-leak-canary" not in captured.get("user-agent", "")
    finally:
        await engine.dispose()
        try:
            os.unlink(path)
        except OSError:
            pass
