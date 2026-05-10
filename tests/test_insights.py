"""Tests for tourniquet.analytics.insights.

All queries run against an in-memory SQLite database — nothing network-bound.
If A5 hasn't merged user_agent / metadata_user_id columns yet, the by_caller /
by_metadata_user_id tests skip gracefully rather than fail hard.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tourniquet.models import ApiKey, Base, UsageEvent

# ── Detect optional columns ───────────────────────────────────────────────────

HAS_USER_AGENT = getattr(UsageEvent, "user_agent", None) is not None
HAS_METADATA_USER_ID = getattr(UsageEvent, "metadata_user_id", None) is not None


# ── Engine / session fixtures ─────────────────────────────────────────────────


@pytest_asyncio.fixture()
async def session() -> AsyncSession:
    """In-memory SQLite session with schema created fresh per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add optional columns if they exist on the ORM model but weren't in initial schema
        if HAS_USER_AGENT:
            # column already exists
            with contextlib.suppress(Exception):
                await conn.execute(
                    text("ALTER TABLE usage_events ADD COLUMN user_agent VARCHAR(255)")
                )
        if HAS_METADATA_USER_ID:
            with contextlib.suppress(Exception):
                await conn.execute(
                    text("ALTER TABLE usage_events ADD COLUMN metadata_user_id VARCHAR(255)")
                )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


# ── Fixture data helpers ──────────────────────────────────────────────────────


def _make_key(session: AsyncSession) -> ApiKey:
    """Return (unsaved) ApiKey. Caller must add+flush."""
    key = ApiKey(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="test-key",
        tq_token_hash="$2b$12$fake",
        anthropic_key_encrypted="enc",
        profile="standard",
        daily_cap_usd_cents=5000,
        kill_enabled=True,
    )
    session.add(key)
    return key


def _event(
    api_key_id: uuid.UUID,
    cost: int,
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cap_hit: bool = False,
    created_at: datetime | None = None,
    user_agent: str | None = None,
    metadata_user_id: str | None = None,
) -> UsageEvent:
    ev = UsageEvent(
        id=uuid.uuid4(),
        api_key_id=api_key_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd_cents=cost,
        cap_hit=cap_hit,
        created_at=created_at or datetime.now(UTC),
    )
    if HAS_USER_AGENT and user_agent is not None:
        ev.user_agent = user_agent
    if HAS_METADATA_USER_ID and metadata_user_id is not None:
        ev.metadata_user_id = metadata_user_id
    return ev


# ── No network imports ────────────────────────────────────────────────────────


def test_no_network_imports():
    """insights.py must never import network-capable modules."""
    forbidden = {"httpx", "requests", "urllib", "urllib3", "socket", "aiohttp"}
    import tourniquet.analytics.insights as mod
    # Walk the module's direct imports via its __dict__ values
    for name in forbidden:
        # Check the module itself doesn't hold a reference to these
        assert name not in mod.__dict__, (
            f"insights.py imported '{name}' — this could phone home!"
        )

    # Stronger check: reload from source and assert no forbidden name in imports
    import inspect
    source = inspect.getsource(mod)
    for name in forbidden:
        assert f"import {name}" not in source, (
            f"insights.py has 'import {name}' — not allowed!"
        )
        assert f"from {name}" not in source, (
            f"insights.py has 'from {name}' — not allowed!"
        )


# ── Core computation tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_totals_add_up(session: AsyncSession):
    """by_model totals must sum to total_usd_cents."""
    from tourniquet.analytics.insights import compute_insights

    key = _make_key(session)
    await session.flush()

    now = datetime.now(UTC)
    session.add(_event(key.id, 300, model="claude-opus-4-7", created_at=now - timedelta(hours=1)))
    session.add(_event(key.id, 200, model="claude-sonnet-4-6", created_at=now - timedelta(hours=2)))
    session.add(_event(key.id, 100, model="claude-haiku-4-5", created_at=now - timedelta(hours=3)))
    await session.commit()

    report = await compute_insights(key.id, days=7, session=session)

    assert report.total_usd_cents == 600
    assert report.request_count == 3
    assert sum(r.cost_cents for r in report.by_model) == 600


@pytest.mark.asyncio
async def test_by_model_sorted_descending(session: AsyncSession):
    """by_model rows come back sorted by cost descending."""
    from tourniquet.analytics.insights import compute_insights

    key = _make_key(session)
    await session.flush()

    now = datetime.now(UTC)
    session.add(_event(key.id, 50, model="claude-haiku-4-5", created_at=now - timedelta(hours=1)))
    session.add(_event(key.id, 500, model="claude-opus-4-7", created_at=now - timedelta(hours=2)))
    session.add(_event(key.id, 200, model="claude-sonnet-4-6", created_at=now - timedelta(hours=3)))
    await session.commit()

    report = await compute_insights(key.id, days=7, session=session)

    costs = [r.cost_cents for r in report.by_model]
    assert costs == sorted(costs, reverse=True)
    assert report.by_model[0].name == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_biggest_request(session: AsyncSession):
    """biggest_request returns the highest-cost single row."""
    from tourniquet.analytics.insights import compute_insights

    key = _make_key(session)
    await session.flush()

    now = datetime.now(UTC)
    big = _event(
        key.id,
        999,
        model="claude-opus-4-7",
        input_tokens=200_000,
        created_at=now - timedelta(hours=1),
    )
    small = _event(key.id, 10, model="claude-haiku-4-5", created_at=now - timedelta(hours=2))
    session.add(big)
    session.add(small)
    await session.commit()

    report = await compute_insights(key.id, days=7, session=session)

    assert report.biggest_request is not None
    assert report.biggest_request.cost_usd_cents == 999
    assert round(report.biggest_request_pct) == round(999 / 1009 * 100)


@pytest.mark.asyncio
async def test_biggest_request_pct(session: AsyncSession):
    """biggest_request_pct is computed correctly as a percentage."""
    from tourniquet.analytics.insights import compute_insights

    key = _make_key(session)
    await session.flush()

    now = datetime.now(UTC)
    session.add(_event(key.id, 400, created_at=now - timedelta(hours=1)))
    session.add(_event(key.id, 600, created_at=now - timedelta(hours=2)))
    await session.commit()

    report = await compute_insights(key.id, days=7, session=session)

    assert report.biggest_request is not None
    assert report.biggest_request.cost_usd_cents == 600
    assert abs(report.biggest_request_pct - 60.0) < 0.5


@pytest.mark.asyncio
async def test_by_caller_groups_by_user_agent(session: AsyncSession):
    """by_caller groups events by user_agent and maps NULL to '(unknown)'."""
    if not HAS_USER_AGENT:
        pytest.skip("user_agent column not yet in model (A5 not merged)")

    from tourniquet.analytics.insights import compute_insights

    key = _make_key(session)
    await session.flush()

    now = datetime.now(UTC)
    session.add(_event(key.id, 300, user_agent="Claude Code", created_at=now - timedelta(hours=1)))
    session.add(_event(key.id, 200, user_agent="Claude Code", created_at=now - timedelta(hours=2)))
    session.add(_event(key.id, 100, user_agent=None, created_at=now - timedelta(hours=3)))
    await session.commit()

    report = await compute_insights(key.id, days=7, session=session)

    assert len(report.by_caller) == 2
    names = {r.name for r in report.by_caller}
    assert "Claude Code" in names
    assert "(unknown)" in names

    claude_code = next(r for r in report.by_caller if r.name == "Claude Code")
    assert claude_code.cost_cents == 500
    assert claude_code.request_count == 2


@pytest.mark.asyncio
async def test_by_metadata_user_id(session: AsyncSession):
    """by_metadata_user_id groups correctly and maps NULL to '(none)'."""
    if not HAS_METADATA_USER_ID:
        pytest.skip("metadata_user_id column not yet in model (A5 not merged)")

    from tourniquet.analytics.insights import compute_insights

    key = _make_key(session)
    await session.flush()

    now = datetime.now(UTC)
    session.add(
        _event(key.id, 400, metadata_user_id="task-001", created_at=now - timedelta(hours=1))
    )
    session.add(
        _event(key.id, 200, metadata_user_id="task-002", created_at=now - timedelta(hours=2))
    )
    session.add(
        _event(key.id, 50, metadata_user_id=None, created_at=now - timedelta(hours=3))
    )
    await session.commit()

    report = await compute_insights(key.id, days=7, session=session)

    names = {r.name for r in report.by_metadata_user_id}
    assert "task-001" in names
    assert "(none)" in names


@pytest.mark.asyncio
async def test_suggestion_top_caller_rule(session: AsyncSession):
    """When a single caller > 50% of spend, the sub-cap suggestion fires."""
    if not HAS_USER_AGENT:
        pytest.skip("user_agent column not yet in model (A5 not merged)")

    from tourniquet.analytics.insights import compute_insights

    key = _make_key(session)
    await session.flush()

    now = datetime.now(UTC)
    # big-spender is 80% of total
    for _ in range(8):
        session.add(
            _event(key.id, 100, user_agent="big-spender", created_at=now - timedelta(hours=1))
        )
    for _ in range(2):
        session.add(
            _event(key.id, 100, user_agent="small-spender", created_at=now - timedelta(hours=2))
        )
    await session.commit()

    report = await compute_insights(key.id, days=7, session=session)

    assert any("sub-cap" in s for s in report.suggestions), (
        f"Expected sub-cap suggestion, got: {report.suggestions}"
    )


@pytest.mark.asyncio
async def test_suggestion_biggest_request_rule(session: AsyncSession):
    """When biggest single request > 20% of window total, suggestion fires."""
    from tourniquet.analytics.insights import compute_insights

    key = _make_key(session)
    await session.flush()

    now = datetime.now(UTC)
    session.add(
        _event(
            key.id,
            800,
            model="claude-opus-4-7",
            input_tokens=100_000,
            created_at=now - timedelta(hours=1),
        )
    )
    session.add(_event(key.id, 100, created_at=now - timedelta(hours=2)))
    session.add(_event(key.id, 100, created_at=now - timedelta(hours=3)))
    await session.commit()

    report = await compute_insights(key.id, days=7, session=session)

    assert any("alone was" in s for s in report.suggestions), (
        f"Expected biggest-request suggestion, got: {report.suggestions}"
    )


@pytest.mark.asyncio
async def test_suggestion_cap_hit_rule(session: AsyncSession):
    """When cap_hit_days > 3, the cap suggestion fires."""
    from tourniquet.analytics.insights import compute_insights

    key = _make_key(session)
    await session.flush()

    now = datetime.now(UTC)
    # 4 separate days with cap_hit=True
    for day_offset in range(4):
        ts = now - timedelta(days=day_offset + 1)
        session.add(_event(key.id, 100, cap_hit=True, created_at=ts))
    await session.commit()

    report = await compute_insights(key.id, days=7, session=session)

    assert report.cap_hit_days == 4
    assert any("hit cap" in s for s in report.suggestions), (
        f"Expected cap suggestion, got: {report.suggestions}"
    )


@pytest.mark.asyncio
async def test_suggestion_opus_rule(session: AsyncSession):
    """When Opus > 60% of spend, the model-downgrade suggestion fires."""
    from tourniquet.analytics.insights import compute_insights

    key = _make_key(session)
    await session.flush()

    now = datetime.now(UTC)
    # 70% opus
    session.add(_event(key.id, 700, model="claude-opus-4-7", created_at=now - timedelta(hours=1)))
    session.add(_event(key.id, 300, model="claude-haiku-4-5", created_at=now - timedelta(hours=2)))
    await session.commit()

    report = await compute_insights(key.id, days=7, session=session)

    assert any("Opus" in s for s in report.suggestions), (
        f"Expected Opus suggestion, got: {report.suggestions}"
    )


@pytest.mark.asyncio
async def test_empty_window_returns_zeroes(session: AsyncSession):
    """No events in window → all totals are zero, no suggestions panic."""
    from tourniquet.analytics.insights import compute_insights

    key = _make_key(session)
    await session.flush()
    await session.commit()

    report = await compute_insights(key.id, days=7, session=session)

    assert report.total_usd_cents == 0
    assert report.request_count == 0
    assert report.by_model == []
    assert report.biggest_request is None
    assert report.biggest_request_pct == 0.0
    assert report.cap_hit_days == 0


@pytest.mark.asyncio
async def test_events_outside_window_excluded(session: AsyncSession):
    """Events older than 'days' must not appear in totals."""
    from tourniquet.analytics.insights import compute_insights

    key = _make_key(session)
    await session.flush()

    now = datetime.now(UTC)
    # inside window
    session.add(_event(key.id, 100, created_at=now - timedelta(days=3)))
    # outside window (8 days ago for a 7-day window)
    session.add(_event(key.id, 9999, created_at=now - timedelta(days=8)))
    await session.commit()

    report = await compute_insights(key.id, days=7, session=session)

    assert report.total_usd_cents == 100


@pytest.mark.asyncio
async def test_cap_hit_days_prior_window(session: AsyncSession):
    """cap_hit_days_prior counts cap hits in the window immediately before."""
    from tourniquet.analytics.insights import compute_insights

    key = _make_key(session)
    await session.flush()

    now = datetime.now(UTC)
    # current window (last 7 days): 2 cap-hit days
    session.add(_event(key.id, 100, cap_hit=True, created_at=now - timedelta(days=1)))
    session.add(_event(key.id, 100, cap_hit=True, created_at=now - timedelta(days=3)))
    # prior window (days 7-14 ago): 1 cap-hit day
    session.add(_event(key.id, 100, cap_hit=True, created_at=now - timedelta(days=9)))
    await session.commit()

    report = await compute_insights(key.id, days=7, session=session)

    assert report.cap_hit_days == 2
    assert report.cap_hit_days_prior == 1
