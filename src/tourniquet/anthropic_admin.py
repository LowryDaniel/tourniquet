"""Anthropic Admin API client — bootstrap cost history for suggestion engine.

The admin key is used once to pull cost data and is never persisted.
Memory hygiene: admin_key is deleted from locals before return.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

_ADMIN_BASE = "https://api.anthropic.com"
_COST_REPORT_PATH = "/v1/organizations/cost_report"
_ANTHROPIC_VERSION = "2023-06-01"


@dataclass
class DailyCost:
    date: date
    usd_cents: int
    request_count: int


async def fetch_cost_history(
    admin_key: str,
    days: int = 14,
    api_key_id: str | None = None,
) -> list[DailyCost]:
    """Pull daily cost in USD cents from Anthropic Admin API.

    Args:
        admin_key: sk-ant-admin-... — used ONCE, never persisted, zeroed on return.
        days: how far back to look. Default 14.
        api_key_id: filter to a single regular key's history. None = org-wide.

    Returns: list of DailyCost(date, usd_cents, request_count) sorted oldest→newest.
    """
    today = date.today()
    starting_at = today - timedelta(days=days)
    ending_at = today

    params: dict[str, str | list[str]] = {
        "starting_at": starting_at.isoformat(),
        "ending_at": ending_at.isoformat(),
        "group_by[]": "api_key_id",
    }
    if api_key_id is not None:
        params["api_key_id"] = api_key_id

    headers = {
        "x-api-key": admin_key,
        "anthropic-version": _ANTHROPIC_VERSION,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{_ADMIN_BASE}{_COST_REPORT_PATH}",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        # Re-raise without exposing the key in the message
        status = exc.response.status_code
        raise httpx.HTTPStatusError(
            f"Admin API returned {status}",
            request=exc.request,
            response=exc.response,
        ) from None
    except httpx.HTTPError as exc:
        raise httpx.HTTPError(f"Admin API request failed: {type(exc).__name__}") from None
    finally:
        # CRITICAL: zero out the key reference regardless of success or failure
        del admin_key

    # Aggregate by date across all api_key_id buckets (or just the filtered one)
    daily: dict[date, tuple[int, int]] = {}  # date -> (usd_cents, request_count)
    for bucket in payload.get("data", []):
        bucket_date_str = bucket.get("date") or bucket.get("period_start") or bucket.get("start_date")
        if bucket_date_str is None:
            continue
        bucket_date = date.fromisoformat(bucket_date_str[:10])
        # Anthropic returns cost in USD — convert to cents
        cost_usd = bucket.get("cost_usd", 0.0) or bucket.get("total_cost", 0.0)
        usd_cents = int(round(float(cost_usd) * 100))
        req_count = int(bucket.get("request_count", 0))
        if bucket_date in daily:
            prev_cents, prev_count = daily[bucket_date]
            daily[bucket_date] = (prev_cents + usd_cents, prev_count + req_count)
        else:
            daily[bucket_date] = (usd_cents, req_count)

    results = [
        DailyCost(date=d, usd_cents=cents, request_count=count)
        for d, (cents, count) in sorted(daily.items())
    ]
    return results
