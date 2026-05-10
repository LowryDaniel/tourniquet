"""Suggestion engine — recommend spend caps and alert thresholds.

Works over two data sources:
  - DailyCost list from Anthropic Admin API (bootstrap)
  - usage_events table (ongoing tuning after 7+ days of traffic)

All amounts are in USD cents (integer), matching the canonical currency used
throughout the rest of tourniquet.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class InsufficientHistory(Exception):  # noqa: N818 — public API name; renaming would break call sites
    """Raised when fewer than 3 non-zero spend days are available."""


@dataclass
class Suggestion:
    suggested_cap_usd_cents: int
    soft_alert_50_usd_cents: int
    soft_throttle_80_usd_cents: int
    rationale: str           # human-readable: "P95 of last 14 days × 1.5"
    based_on_days: int
    avg_daily_usd_cents: int
    p50_usd_cents: int
    p95_usd_cents: int
    max_usd_cents: int
    capped_by_ceiling: bool  # True if hit absolute_ceiling_usd_cents


@dataclass
class ProfileRecommendation:
    profile: str             # "standard" | "monitor"
    reason: str              # plain-English explanation
    avg_daily_usd_cents: int
    daily_cv: float          # coefficient of variation (stddev/mean) — measures volatility


def recommend_profile(daily_totals_usd_cents: list[int]) -> ProfileRecommendation:
    """Pick standard/monitor based on the user's actual spending pattern.

    Heuristic:
      - monitor: low variance (CV < 0.4) AND meaningful spend (avg ≥ $20/day)
                 — looks like steady production traffic; alerts without auto-kill
      - standard: anything else (default — hard kill with lead-time alerts)

    `strict` is rarely auto-recommended; reserved for explicit user selection.
    """
    active = [v for v in daily_totals_usd_cents if v > 0]
    if len(active) < 3:
        return ProfileRecommendation(
            profile="standard",
            reason=(
                "Not enough spending history yet — defaulting to standard "
                "(hard kill at 100% with warnings at 50% and 80%)."
            ),
            avg_daily_usd_cents=0,
            daily_cv=0.0,
        )

    avg = sum(active) / len(active)
    variance = sum((v - avg) ** 2 for v in active) / len(active)
    stddev = variance ** 0.5
    cv = stddev / avg if avg > 0 else 0.0

    if cv < 0.4 and avg >= 2000:  # steady ≥ $20/day
        return ProfileRecommendation(
            profile="monitor",
            reason=(
                f"Your spend is steady (variance only {cv * 100:.0f}%) at "
                f"~${avg / 100:.2f}/day. That looks like production traffic — "
                f"the monitor profile alerts you at 50/80/100% but won't "
                f"auto-kill requests. Every alert includes a one-click kill link "
                f"so you can enforce manually when you decide to."
            ),
            avg_daily_usd_cents=int(avg),
            daily_cv=cv,
        )

    if cv > 1.0:
        reason = (
            f"Your spend is volatile (variance {cv * 100:.0f}%) — some quiet days, "
            f"some bursty. The standard profile gives a firm budget wall with "
            f"lead-time alerts at 50% and 80% before the kill at 100%."
        )
    elif avg < 1000:
        reason = (
            f"Light usage at ~${avg / 100:.2f}/day average. Standard's hard kill "
            f"at 100% is the right default for personal projects and small workloads."
        )
    else:
        reason = (
            f"Mixed pattern at ~${avg / 100:.2f}/day. Standard's hard kill is the "
            f"safe default — you can switch to monitor later if your traffic "
            f"becomes steadier and you need uninterrupted flow."
        )

    return ProfileRecommendation(
        profile="standard",
        reason=reason,
        avg_daily_usd_cents=int(avg),
        daily_cv=cv,
    )


def _percentile(sorted_values: list[int], pct: float) -> int:
    """Numpy-free percentile via sorted index.

    Uses the nearest-rank method: index = ceil(pct/100 * n) - 1.
    """
    n = len(sorted_values)
    if n == 0:
        return 0
    rank = max(1, math.ceil(pct / 100.0 * n))
    return sorted_values[min(rank - 1, n - 1)]


def suggest_from_history(
    daily_totals_usd_cents: list[int],
    current_cap_usd_cents: int | None,
    absolute_ceiling_usd_cents: int,
) -> Suggestion:
    """Given a list of daily spend totals, suggest cap + thresholds.

    Algorithm:
      - Skip days with zero spend (user wasn't using the API)
      - Need at least 3 non-zero days; raise InsufficientHistory if fewer
      - p50 = numpy-free percentile via sorted indexing
      - p95 same
      - suggested_cap = ceil(p95 * 1.5)
      - But never below current_cap (don't suggest tightening on quiet weeks)
      - And never above absolute_ceiling (hard wall)
      - soft_alert = ceil(suggested_cap * 0.5)
      - soft_throttle = ceil(suggested_cap * 0.8)
    """
    active_days = [d for d in daily_totals_usd_cents if d > 0]

    if len(active_days) < 3:
        raise InsufficientHistory(
            f"Need at least 3 non-zero spend days; got {len(active_days)}."
        )

    sorted_days = sorted(active_days)
    avg_daily = math.ceil(sum(active_days) / len(active_days))
    p50 = _percentile(sorted_days, 50)
    p95 = _percentile(sorted_days, 95)
    max_day = max(sorted_days)

    raw_cap = math.ceil(p95 * 1.5)

    # Never tighten: respect existing cap if it's higher
    if current_cap_usd_cents is not None:
        raw_cap = max(raw_cap, current_cap_usd_cents)

    capped_by_ceiling = raw_cap > absolute_ceiling_usd_cents
    suggested_cap = min(raw_cap, absolute_ceiling_usd_cents)

    soft_alert = math.ceil(suggested_cap * 0.5)
    soft_throttle = math.ceil(suggested_cap * 0.8)

    rationale = f"P95 of last {len(active_days)} active days × 1.5"
    if capped_by_ceiling:
        rationale += f" (clamped to ceiling ${absolute_ceiling_usd_cents / 100:.2f})"
    elif current_cap_usd_cents is not None and suggested_cap == current_cap_usd_cents:
        rationale += " (kept at current cap — quiet period)"

    return Suggestion(
        suggested_cap_usd_cents=suggested_cap,
        soft_alert_50_usd_cents=soft_alert,
        soft_throttle_80_usd_cents=soft_throttle,
        rationale=rationale,
        based_on_days=len(active_days),
        avg_daily_usd_cents=avg_daily,
        p50_usd_cents=p50,
        p95_usd_cents=p95,
        max_usd_cents=max_day,
        capped_by_ceiling=capped_by_ceiling,
    )


def should_creep_up(
    rolling_7d_avg_usd_cents: int,
    current_cap_usd_cents: int,
    absolute_ceiling_usd_cents: int,
) -> tuple[bool, int]:
    """Auto-tune `creep` mode: should we bump the cap today?

    Rules:
      - Only bump UP, never down
      - Bump only if rolling 7d avg > 60% of current cap (sustained heavy use)
      - Maximum bump per call = 20% of current cap
      - Never exceed absolute_ceiling
    Returns: (should_bump, new_cap_usd_cents)
    """
    threshold = current_cap_usd_cents * 0.6
    if rolling_7d_avg_usd_cents <= threshold:
        return (False, current_cap_usd_cents)

    proposed = math.ceil(current_cap_usd_cents * 1.2)
    new_cap = min(proposed, absolute_ceiling_usd_cents)
    return (True, new_cap)


async def get_rolling_avg_from_db(
    api_key_id: uuid.UUID,
    days: int,
    session: AsyncSession,
) -> int:
    """Average daily spend over last `days` days. Returns USD cents.

    Queries usage_events grouped by calendar day, then averages the daily totals.
    Days with no events count as zero spend.
    """
    cutoff = date.today() - timedelta(days=days)
    result = await session.execute(
        text("""
            SELECT
                DATE(created_at) AS day,
                SUM(cost_usd_cents) AS daily_total
            FROM usage_events
            WHERE api_key_id = :kid
              AND created_at >= :cutoff
            GROUP BY DATE(created_at)
        """),
        {"kid": str(api_key_id), "cutoff": cutoff},
    )
    rows = result.fetchall()
    if not rows:
        return 0
    total = sum(row[1] for row in rows)
    return math.ceil(total / days)
