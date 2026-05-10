"""Tests for tourniquet.billing.suggestions."""

from __future__ import annotations

import math

import pytest

from tourniquet.billing.suggestions import (
    InsufficientHistory,
    Suggestion,
    should_creep_up,
    suggest_from_history,
)

# ---------------------------------------------------------------------------
# suggest_from_history
# ---------------------------------------------------------------------------


def test_suggest_basic_history():
    """[200, 100, 250, 300, 50, 0, 180] → reasonable cap suggestion."""
    daily = [200, 100, 250, 300, 50, 0, 180]
    result = suggest_from_history(
        daily_totals_usd_cents=daily,
        current_cap_usd_cents=None,
        absolute_ceiling_usd_cents=100_000,
    )

    assert isinstance(result, Suggestion)
    # Active days (non-zero): [200, 100, 250, 300, 50, 180] = 6 days
    assert result.based_on_days == 6
    # P95 of sorted [50, 100, 180, 200, 250, 300]:
    # rank = ceil(0.95 * 6) = ceil(5.7) = 6 → index 5 → 300
    # suggested_cap = ceil(300 * 1.5) = 450
    assert result.p95_usd_cents == 300
    assert result.suggested_cap_usd_cents == 450
    assert result.soft_alert_50_usd_cents == math.ceil(450 * 0.5)  # 225
    assert result.soft_throttle_80_usd_cents == math.ceil(450 * 0.8)  # 360
    assert result.capped_by_ceiling is False
    assert result.max_usd_cents == 300


def test_suggest_all_zeros_raises_insufficient_history():
    """All-zero history → InsufficientHistory raised."""
    with pytest.raises(InsufficientHistory):
        suggest_from_history(
            daily_totals_usd_cents=[0, 0, 0, 0, 0],
            current_cap_usd_cents=None,
            absolute_ceiling_usd_cents=100_000,
        )


def test_suggest_only_two_nonzero_raises():
    """Fewer than 3 non-zero days → InsufficientHistory."""
    with pytest.raises(InsufficientHistory):
        suggest_from_history(
            daily_totals_usd_cents=[0, 100, 0, 200],
            current_cap_usd_cents=None,
            absolute_ceiling_usd_cents=100_000,
        )


def test_suggest_does_not_tighten_on_quiet_week():
    """When current_cap > p95*1.5, suggestion stays at current_cap."""
    daily = [50, 60, 40, 55, 0, 45, 70]
    # P95 of [40,45,50,55,60,70] → rank=ceil(0.95*6)=6 → 70; cap = ceil(70*1.5)=105
    # current_cap = 500, which is > 105, so result should be 500
    result = suggest_from_history(
        daily_totals_usd_cents=daily,
        current_cap_usd_cents=500,
        absolute_ceiling_usd_cents=100_000,
    )
    assert result.suggested_cap_usd_cents == 500
    assert result.capped_by_ceiling is False


def test_suggest_clamped_to_ceiling():
    """When p95*1.5 > absolute_ceiling → suggestion clamped to ceiling, capped_by_ceiling=True."""
    daily = [1000, 2000, 3000, 4000, 5000]
    # P95 of [1000,2000,3000,4000,5000] → rank=ceil(4.75)=5 → 5000; cap=ceil(7500)=7500
    # absolute_ceiling = 5000 → clamp to 5000
    result = suggest_from_history(
        daily_totals_usd_cents=daily,
        current_cap_usd_cents=None,
        absolute_ceiling_usd_cents=5000,
    )
    assert result.suggested_cap_usd_cents == 5000
    assert result.capped_by_ceiling is True


def test_suggest_alerts_proportional_to_capped_cap():
    """When ceiling clamps the cap, alerts are proportional to the clamped value."""
    daily = [1000, 2000, 3000, 4000, 5000]
    result = suggest_from_history(
        daily_totals_usd_cents=daily,
        current_cap_usd_cents=None,
        absolute_ceiling_usd_cents=5000,
    )
    assert result.soft_alert_50_usd_cents == math.ceil(5000 * 0.5)
    assert result.soft_throttle_80_usd_cents == math.ceil(5000 * 0.8)


# ---------------------------------------------------------------------------
# should_creep_up
# ---------------------------------------------------------------------------


def test_creep_up_heavy_use_bumps_cap():
    """Rolling avg = 70% of cap, ceiling far away → returns (True, cap × 1.2)."""
    cap = 1000
    rolling_avg = 700  # 70% of 1000 — above the 60% threshold
    ceiling = 100_000

    should_bump, new_cap = should_creep_up(rolling_avg, cap, ceiling)

    assert should_bump is True
    assert new_cap == math.ceil(cap * 1.2)  # 1200


def test_creep_up_light_use_no_bump():
    """Rolling avg = 30% of cap → returns (False, current cap)."""
    cap = 1000
    rolling_avg = 300  # 30% — below the 60% threshold

    should_bump, new_cap = should_creep_up(rolling_avg, cap, 100_000)

    assert should_bump is False
    assert new_cap == cap


def test_creep_up_clamped_to_ceiling():
    """Rolling avg = 90%, but cap × 1.2 > ceiling → returns (True, ceiling)."""
    cap = 1000
    rolling_avg = 900  # 90% of 1000 — above threshold
    ceiling = 1100  # cap * 1.2 = 1200 > 1100

    should_bump, new_cap = should_creep_up(rolling_avg, cap, ceiling)

    assert should_bump is True
    assert new_cap == ceiling


def test_creep_up_exactly_at_ceiling_no_bump_needed():
    """Cap already at ceiling: bump would be clipped to ceiling (no-op in effect)."""
    cap = 1000
    rolling_avg = 700  # above threshold
    ceiling = 1000  # already at ceiling

    should_bump, new_cap = should_creep_up(rolling_avg, cap, ceiling)

    assert should_bump is True
    assert new_cap == 1000  # ceil(1200) clipped to 1000


def test_creep_up_at_exactly_60_percent_does_not_bump():
    """Exactly at 60% threshold — not strictly above, so no bump."""
    cap = 1000
    rolling_avg = 600  # exactly 60%, not > 60%

    should_bump, new_cap = should_creep_up(rolling_avg, cap, 100_000)

    assert should_bump is False
    assert new_cap == cap
