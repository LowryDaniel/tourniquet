"""Local-only anomaly insights for Tourniquet.

All computation is against the local SQLite database.
No network calls are made. No prompt/response content is accessed.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tourniquet.billing.formatting import format_money
from tourniquet.config import settings
from tourniquet.models import UsageEvent

# ── Data structures ───────────────────────────────────────────────────────────


class BreakdownRow(NamedTuple):
    name: str
    cost_cents: int
    request_count: int
    pct_of_total: float


class HourBucket(NamedTuple):
    weekday: int  # 0=Mon … 6=Sun
    hour: int  # 0–23
    cost_cents: int
    z_score: float


@dataclass
class InsightReport:
    api_key_name: str
    days: int
    total_usd_cents: int
    request_count: int
    by_model: list[BreakdownRow]
    by_caller: list[BreakdownRow]  # grouped by user_agent
    by_metadata_user_id: list[BreakdownRow]  # grouped by metadata.user_id
    hottest_hour: HourBucket | None
    biggest_request: UsageEvent | None
    biggest_request_pct: float
    cap_hit_days: int
    cap_hit_days_prior: int
    suggestions: list[str] = field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────────────────

_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _weekday_name(n: int) -> str:
    return _WEEKDAY_NAMES[n % 7]


def _pct(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return round(part / total * 100, 1)


def _z_score(value: float, mean: float, std: float) -> float:
    if std == 0:
        return 0.0
    return (value - mean) / std


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


# ── Core function ─────────────────────────────────────────────────────────────


async def compute_insights(
    api_key_id: uuid.UUID,
    days: int,
    session: AsyncSession,
) -> InsightReport:
    """Compute the full insight report. All queries against the local SQLite DB.

    Never makes a network call. Never returns prompt content (we don't store it).
    """
    now = datetime.now(UTC)
    window_start = now - timedelta(days=days)
    prior_start = now - timedelta(days=days * 2)

    kid = str(api_key_id)
    currency = settings.display_currency

    # ── Key name ──────────────────────────────────────────────────────────────
    key_result = await session.execute(
        text("SELECT name FROM api_keys WHERE id = :kid"),
        {"kid": kid},
    )
    key_row = key_result.first()
    api_key_name = key_row[0] if key_row else str(api_key_id)[:8]

    # ── Totals for the window ─────────────────────────────────────────────────
    totals_result = await session.execute(
        text(
            "SELECT COALESCE(SUM(cost_usd_cents), 0), COUNT(*) "
            "FROM usage_events "
            "WHERE api_key_id = :kid AND created_at >= :since"
        ),
        {"kid": kid, "since": window_start},
    )
    total_row = totals_result.first()
    total_usd_cents = int(total_row[0]) if total_row else 0
    request_count = int(total_row[1]) if total_row else 0

    # ── By model ──────────────────────────────────────────────────────────────
    model_result = await session.execute(
        text(
            "SELECT model, SUM(cost_usd_cents) as cost, COUNT(*) as cnt "
            "FROM usage_events "
            "WHERE api_key_id = :kid AND created_at >= :since "
            "GROUP BY model "
            "ORDER BY cost DESC "
            "LIMIT 5"
        ),
        {"kid": kid, "since": window_start},
    )
    by_model = [
        BreakdownRow(
            name=r[0],
            cost_cents=int(r[1]),
            request_count=int(r[2]),
            pct_of_total=_pct(int(r[1]), total_usd_cents),
        )
        for r in model_result.all()
    ]

    # ── By caller (user_agent) ────────────────────────────────────────────────
    # Gracefully handle the case where user_agent column doesn't exist yet (A5 not merged)
    _has_user_agent = getattr(UsageEvent, "user_agent", None) is not None
    _has_metadata_user_id = getattr(UsageEvent, "metadata_user_id", None) is not None

    if _has_user_agent:
        caller_result = await session.execute(
            text(
                "SELECT COALESCE(user_agent, '(unknown)') as caller, "
                "SUM(cost_usd_cents) as cost, COUNT(*) as cnt "
                "FROM usage_events "
                "WHERE api_key_id = :kid AND created_at >= :since "
                "GROUP BY caller "
                "ORDER BY cost DESC "
                "LIMIT 5"
            ),
            {"kid": kid, "since": window_start},
        )
        by_caller = [
            BreakdownRow(
                name=r[0],
                cost_cents=int(r[1]),
                request_count=int(r[2]),
                pct_of_total=_pct(int(r[1]), total_usd_cents),
            )
            for r in caller_result.all()
        ]
    else:
        by_caller = []

    # ── By metadata.user_id ───────────────────────────────────────────────────
    if _has_metadata_user_id:
        muid_result = await session.execute(
            text(
                "SELECT COALESCE(metadata_user_id, '(none)') as muid, "
                "SUM(cost_usd_cents) as cost, COUNT(*) as cnt "
                "FROM usage_events "
                "WHERE api_key_id = :kid AND created_at >= :since "
                "GROUP BY muid "
                "ORDER BY cost DESC "
                "LIMIT 5"
            ),
            {"kid": kid, "since": window_start},
        )
        by_metadata_user_id = [
            BreakdownRow(
                name=r[0],
                cost_cents=int(r[1]),
                request_count=int(r[2]),
                pct_of_total=_pct(int(r[1]), total_usd_cents),
            )
            for r in muid_result.all()
        ]
    else:
        by_metadata_user_id = []

    # ── Hottest hour ──────────────────────────────────────────────────────────
    # SQLite: strftime('%w', ...) → 0=Sun, 1=Mon … 6=Sat; convert to 0=Mon..6=Sun
    # strftime('%H', ...) → '00'..'23'
    hour_result = await session.execute(
        text(
            "SELECT "
            "  CAST(strftime('%w', datetime(created_at)) AS INTEGER) as dow_sun, "
            "  CAST(strftime('%H', datetime(created_at)) AS INTEGER) as hr, "
            "  SUM(cost_usd_cents) as cost "
            "FROM usage_events "
            "WHERE api_key_id = :kid AND created_at >= :since "
            "GROUP BY dow_sun, hr "
            "ORDER BY cost DESC"
        ),
        {"kid": kid, "since": window_start},
    )
    hour_rows = hour_result.all()

    # Baseline: same (weekday, hour) buckets over the prior window
    baseline_result = await session.execute(
        text(
            "SELECT "
            "  CAST(strftime('%w', datetime(created_at)) AS INTEGER) as dow_sun, "
            "  CAST(strftime('%H', datetime(created_at)) AS INTEGER) as hr, "
            "  SUM(cost_usd_cents) as cost "
            "FROM usage_events "
            "WHERE api_key_id = :kid AND created_at >= :prior AND created_at < :since "
            "GROUP BY dow_sun, hr"
        ),
        {"kid": kid, "prior": prior_start, "since": window_start},
    )
    baseline_rows = baseline_result.all()

    hottest_hour: HourBucket | None = None
    if hour_rows:
        # Build baseline map: (dow_sun, hr) → cost
        baseline_map: dict[tuple[int, int], int] = {(r[0], r[1]): int(r[2]) for r in baseline_rows}

        # Compute mean/std of baseline costs for z-score
        baseline_costs = list(baseline_map.values())
        baseline_mean = sum(baseline_costs) / len(baseline_costs) if baseline_costs else 0.0
        baseline_std = _stdev([float(c) for c in baseline_costs])

        top_row = hour_rows[0]
        top_dow_sun = int(top_row[0])
        top_hr = int(top_row[1])
        top_cost = int(top_row[2])

        # Convert Sunday=0..Saturday=6 → Monday=0..Sunday=6
        top_weekday = (top_dow_sun - 1) % 7

        z = _z_score(float(top_cost), baseline_mean, baseline_std)
        if z > 2:
            hottest_hour = HourBucket(
                weekday=top_weekday,
                hour=top_hr,
                cost_cents=top_cost,
                z_score=z,
            )

    # ── Biggest request ───────────────────────────────────────────────────────
    biggest_result = await session.execute(
        text(
            "SELECT id, api_key_id, request_id, model, input_tokens, output_tokens, "
            "cost_usd_cents, cap_hit, created_at "
            "FROM usage_events "
            "WHERE api_key_id = :kid AND created_at >= :since "
            "ORDER BY cost_usd_cents DESC "
            "LIMIT 1"
        ),
        {"kid": kid, "since": window_start},
    )
    biggest_row = biggest_result.first()
    biggest_request: UsageEvent | None = None
    biggest_request_pct = 0.0
    if biggest_row:
        biggest_request = await session.get(UsageEvent, biggest_row[0])
        if biggest_request and total_usd_cents > 0:
            biggest_request_pct = _pct(biggest_request.cost_usd_cents, total_usd_cents)

    # ── Cap hit days ──────────────────────────────────────────────────────────
    cap_days_result = await session.execute(
        text(
            "SELECT COUNT(DISTINCT date(created_at)) "
            "FROM usage_events "
            "WHERE api_key_id = :kid AND created_at >= :since AND cap_hit = 1"
        ),
        {"kid": kid, "since": window_start},
    )
    cap_hit_days = int(cap_days_result.scalar() or 0)

    cap_days_prior_result = await session.execute(
        text(
            "SELECT COUNT(DISTINCT date(created_at)) "
            "FROM usage_events "
            "WHERE api_key_id = :kid AND created_at >= :prior "
            "AND created_at < :since AND cap_hit = 1"
        ),
        {"kid": kid, "prior": prior_start, "since": window_start},
    )
    cap_hit_days_prior = int(cap_days_prior_result.scalar() or 0)

    # ── Suggestions ───────────────────────────────────────────────────────────
    suggestions: list[str] = []

    # Rule 1: single caller > 50% of spend
    if by_caller:
        top_caller = by_caller[0]
        if top_caller.pct_of_total > 50:
            suggestions.append(
                f"Consider a sub-cap on `{top_caller.name}` — "
                f"it's responsible for {top_caller.pct_of_total:.0f}%."
            )

    # Rule 2: hottest hour z-score > 3
    if hottest_hour and hottest_hour.z_score > 3:
        wday = _weekday_name(hottest_hour.weekday)
        fmtcost = format_money(hottest_hour.cost_cents, currency)
        baseline_for_bucket = baseline_map.get(
            ((hottest_hour.weekday + 1) % 7, hottest_hour.hour), 0
        )
        multiplier = (
            round(hottest_hour.cost_cents / baseline_for_bucket) if baseline_for_bucket else "∞"
        )
        suggestions.append(
            f"On {wday} {hottest_hour.hour:02d}:00 you spent {fmtcost} — "
            f"{multiplier}x your usual {wday} {hottest_hour.hour:02d}:00 baseline."
        )

    # Rule 3: biggest single request > 20% of window total
    if biggest_request and biggest_request_pct > 20:
        ts = biggest_request.created_at
        if ts:
            ts_str = ts.strftime("%a %H:%M") if hasattr(ts, "strftime") else str(ts)
        else:
            ts_str = "unknown time"
        input_k = (
            f"{biggest_request.input_tokens // 1000}k"
            if biggest_request.input_tokens >= 1000
            else str(biggest_request.input_tokens)
        )
        suggestions.append(
            f"One request on {ts_str} alone was {biggest_request_pct:.0f}% of the week's spend "
            f"({input_k} input tokens, model {biggest_request.model})."
        )

    # Rule 4: cap hit > 3 days
    if cap_hit_days > 3:
        top_culprit = (
            by_metadata_user_id[0].name
            if by_metadata_user_id
            else (by_caller[0].name if by_caller else "your top caller")
        )
        suggestions.append(
            f"You hit cap {cap_hit_days} days this week (vs {cap_hit_days_prior} the prior "
            f"{days}). Consider raising the cap or investigating {top_culprit}."
        )

    # Rule 5: Opus dominates spend (> 60%)
    if by_model and by_model[0].name.startswith("claude-opus") and by_model[0].pct_of_total > 60:
        suggestions.append(
            "Most of your spend was on Opus. Could parts of this run on Sonnet/Haiku for cheaper?"
        )

    return InsightReport(
        api_key_name=api_key_name,
        days=days,
        total_usd_cents=total_usd_cents,
        request_count=request_count,
        by_model=by_model,
        by_caller=by_caller,
        by_metadata_user_id=by_metadata_user_id,
        hottest_hour=hottest_hour,
        biggest_request=biggest_request,
        biggest_request_pct=biggest_request_pct,
        cap_hit_days=cap_hit_days,
        cap_hit_days_prior=cap_hit_days_prior,
        suggestions=suggestions,
    )
