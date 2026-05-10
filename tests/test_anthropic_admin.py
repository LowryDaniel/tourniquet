"""Tests for tourniquet.anthropic_admin — Admin API client."""

from __future__ import annotations

import io
import logging
from datetime import date, timedelta

import httpx
import pytest
import respx

from tourniquet.anthropic_admin import fetch_cost_history

_FAKE_KEY = "sk-ant-admin-SUPERSECRETKEY"
_BASE = "https://api.anthropic.com/v1/organizations/cost_report"


def _make_bucket(d: date, cost_usd: float, request_count: int = 5) -> dict:
    return {"date": d.isoformat(), "cost_usd": cost_usd, "request_count": request_count}


@pytest.mark.asyncio
async def test_happy_path_returns_sorted_daily_costs():
    """Returns DailyCost list sorted oldest→newest with correct cents conversion."""
    today = date.today()
    day1 = today - timedelta(days=3)
    day2 = today - timedelta(days=2)
    day3 = today - timedelta(days=1)

    payload = {
        "data": [
            _make_bucket(day3, 1.50, 10),
            _make_bucket(day1, 0.25, 3),
            _make_bucket(day2, 2.00, 7),
        ]
    }

    with respx.mock:
        respx.get(_BASE).mock(return_value=httpx.Response(200, json=payload))
        results = await fetch_cost_history(_FAKE_KEY, days=14)

    assert len(results) == 3
    assert results[0].date == day1
    assert results[0].usd_cents == 25  # $0.25 = 25 cents
    assert results[1].usd_cents == 200  # $2.00 = 200 cents
    assert results[2].usd_cents == 150  # $1.50 = 150 cents
    assert results[2].request_count == 10
    # Verify sorted oldest→newest
    assert results[0].date < results[1].date < results[2].date


@pytest.mark.asyncio
async def test_date_range_params_are_correct():
    """Confirms starting_at and ending_at are passed with correct ISO format."""
    today = date.today()
    expected_start = (today - timedelta(days=7)).isoformat()

    captured_params: dict = {}

    def capture_request(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, json={"data": []})

    with respx.mock:
        respx.get(_BASE).mock(side_effect=capture_request)
        await fetch_cost_history(_FAKE_KEY, days=7)

    assert captured_params["starting_at"] == expected_start
    assert captured_params["ending_at"] == today.isoformat()
    assert captured_params["group_by[]"] == "api_key_id"


@pytest.mark.asyncio
async def test_api_key_id_filter_passed_in_params():
    """When api_key_id is supplied, it appears in the request params."""
    captured_params: dict = {}

    def capture_request(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, json={"data": []})

    with respx.mock:
        respx.get(_BASE).mock(side_effect=capture_request)
        await fetch_cost_history(_FAKE_KEY, days=14, api_key_id="key_abc123")

    assert captured_params.get("api_key_id") == "key_abc123"


@pytest.mark.asyncio
async def test_admin_key_never_appears_in_logged_output(capfd):
    """The admin key must never appear in stdout, stderr, or log output."""
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    try:
        with respx.mock:
            respx.get(_BASE).mock(return_value=httpx.Response(200, json={"data": []}))
            await fetch_cost_history(_FAKE_KEY, days=14)
    finally:
        root_logger.removeHandler(handler)

    captured = capfd.readouterr()
    log_output = log_capture.getvalue()

    assert _FAKE_KEY not in captured.out
    assert _FAKE_KEY not in captured.err
    assert _FAKE_KEY not in log_output


@pytest.mark.asyncio
async def test_401_raises_clean_error_without_key():
    """A 401 from the Admin API raises an error that does NOT contain the key."""
    with respx.mock:
        respx.get(_BASE).mock(
            return_value=httpx.Response(401, json={"error": {"message": "Unauthorized"}})
        )
        with pytest.raises(Exception) as exc_info:
            await fetch_cost_history(_FAKE_KEY, days=14)

    error_text = str(exc_info.value)
    assert _FAKE_KEY not in error_text
    assert "401" in error_text


@pytest.mark.asyncio
async def test_empty_data_returns_empty_list():
    """When the API returns no buckets, result is an empty list."""
    with respx.mock:
        respx.get(_BASE).mock(return_value=httpx.Response(200, json={"data": []}))
        results = await fetch_cost_history(_FAKE_KEY, days=14)

    assert results == []


@pytest.mark.asyncio
async def test_multiple_buckets_same_date_aggregated():
    """Multiple api_key_id buckets for the same date are summed together."""
    today = date.today()
    day = today - timedelta(days=1)

    payload = {
        "data": [
            {"date": day.isoformat(), "cost_usd": 1.00, "request_count": 5, "api_key_id": "key1"},
            {"date": day.isoformat(), "cost_usd": 0.50, "request_count": 3, "api_key_id": "key2"},
        ]
    }

    with respx.mock:
        respx.get(_BASE).mock(return_value=httpx.Response(200, json=payload))
        results = await fetch_cost_history(_FAKE_KEY, days=14)

    assert len(results) == 1
    assert results[0].usd_cents == 150  # $1.50 combined
    assert results[0].request_count == 8
