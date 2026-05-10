"""Tests for manage_keys CLI helpers.

Covers: argparse parsing, lookup-by-prefix, lookup-by-name, ambiguous-name handling.
Intentionally avoids full CLI integration flows (those need a live DB).
"""

from __future__ import annotations

import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make tourniquet importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


# ── Parser tests ──────────────────────────────────────────────────────────────


def test_parser_list():
    from manage_keys import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["list"])
    assert args.command == "list"


def test_parser_show():
    from manage_keys import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["show", "my-key"])
    assert args.command == "show"
    assert args.key == "my-key"


def test_parser_update_defaults():
    from manage_keys import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["update", "my-key"])
    assert args.command == "update"
    assert args.cap is None
    assert args.profile is None
    assert args.auto_tune is None
    assert args.alert_email is None
    assert args.ceiling is None


def test_parser_update_all_flags():
    from manage_keys import _build_parser

    parser = _build_parser()
    args = parser.parse_args(
        [
            "update",
            "my-key",
            "--cap",
            "15.00",
            "--currency",
            "GBP",
            "--profile",
            "monitor",
            "--kill-enabled",
            "--auto-tune",
            "suggest",
            "--alert-email",
            "me@example.com",
            "--ceiling",
            "50.00",
        ]
    )
    assert args.cap == 15.0
    assert args.currency == "GBP"
    assert args.profile == "monitor"
    assert args.auto_tune == "suggest"
    assert args.alert_email == "me@example.com"
    assert args.ceiling == 50.0


def test_parser_update_kill_disabled():
    from manage_keys import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["update", "my-key", "--kill-disabled"])
    assert args.kill_enabled is False


def test_parser_kill_mutex():
    """--kill-enabled and --kill-disabled are mutually exclusive."""

    from manage_keys import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["update", "my-key", "--kill-enabled", "--kill-disabled"])


def test_parser_rotate():
    from manage_keys import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["rotate", "abc12345"])
    assert args.command == "rotate"
    assert args.key == "abc12345"


def test_parser_delete():
    from manage_keys import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["delete", "some-key"])
    assert args.command == "delete"


def test_parser_suggest():
    from manage_keys import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["suggest", "some-key"])
    assert args.command == "suggest"


def test_parser_stats():
    from manage_keys import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["stats", "some-key"])
    assert args.command == "stats"


def test_parser_requires_command():
    from manage_keys import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parser_auto_tune_choices():
    from manage_keys import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["update", "k", "--auto-tune", "invalid"])


# ── Lookup helper tests (mocked DB) ──────────────────────────────────────────


def _make_key(name: str, key_id: uuid.UUID | None = None) -> MagicMock:
    k = MagicMock()
    k.id = key_id or uuid.uuid4()
    k.name = name
    return k


@pytest.mark.asyncio
async def test_lookup_by_exact_name(monkeypatch):
    from manage_keys import _lookup

    target = _make_key("my-key")

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    # by_name returns one result; no UUID prefix matches
    mock_session.execute = AsyncMock(
        side_effect=[
            _make_execute_result([target]),  # exact name query
            _make_execute_result([target]),  # all keys (for prefix check)
            _make_execute_result_scalar(target),  # get(ApiKey, id) refresh
        ]
    )
    mock_session.get = AsyncMock(return_value=target)
    mock_session.refresh = AsyncMock()

    with patch("manage_keys.get_session", return_value=mock_session):
        result = await _lookup("my-key")
    assert result.name == "my-key"


@pytest.mark.asyncio
async def test_lookup_no_match_exits(monkeypatch):
    from manage_keys import _lookup

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    # both queries return empty
    mock_session.execute = AsyncMock(
        side_effect=[
            _make_execute_result([]),  # exact name
            _make_execute_result([]),  # all keys
        ]
    )

    with (
        patch("manage_keys.get_session", return_value=mock_session),
        pytest.raises(SystemExit) as exc_info,
    ):
        await _lookup("nonexistent")
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_lookup_ambiguous_exits(monkeypatch):
    from manage_keys import _lookup

    k1 = _make_key("test-key-alpha")
    k2 = _make_key("test-key-beta")

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    # name query returns two keys with same name-prefix (simulate ambiguity via both results)
    mock_session.execute = AsyncMock(
        side_effect=[
            _make_execute_result([k1, k2]),  # exact name matches 2
            _make_execute_result([]),  # UUID prefix matches 0
        ]
    )

    with (
        patch("manage_keys.get_session", return_value=mock_session),
        pytest.raises(SystemExit) as exc_info,
    ):
        await _lookup("test-key-alpha")
    # SystemExit because 2 candidates (same id is deduplicated, but k1 and k2 have different IDs)
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_lookup_by_uuid_prefix(monkeypatch):
    from manage_keys import _lookup

    fixed_id = uuid.UUID("12345678-1234-1234-1234-123456789abc")
    target = _make_key("prefix-key", key_id=fixed_id)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_session.execute = AsyncMock(
        side_effect=[
            _make_execute_result([]),  # exact name: no match
            _make_execute_result([target]),  # all keys: prefix match
        ]
    )
    mock_session.get = AsyncMock(return_value=target)
    mock_session.refresh = AsyncMock()

    with patch("manage_keys.get_session", return_value=mock_session):
        result = await _lookup("12345678")
    assert result.name == "prefix-key"


# ── Table formatter sanity check ──────────────────────────────────────────────


def test_table_output():
    from manage_keys import _table

    rows = [["abc12345", "my-key", "$5.00", "$1.00", "standard", "yes", "off", "2025-01-01"]]
    headers = ["ID(short)", "Name", "Cap", "Spent today", "Profile", "Kill", "Auto-tune", "Created"]
    output = _table(rows, headers)
    assert "my-key" in output
    assert "standard" in output
    assert "ID(short)" in output


def test_table_empty_rows():
    from manage_keys import _table

    rows = []
    headers = ["A", "B"]
    output = _table(rows, headers)
    assert "A" in output


# ── Helpers for mocking SQLAlchemy results ────────────────────────────────────


def _make_execute_result(items):
    """Return a mock matching the SQLAlchemy execute() result for scalars().all()."""
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = items
    result.scalars.return_value = scalars_mock
    # Also support .scalar() for single-value queries
    result.scalar.return_value = items[0] if items else None
    result.first.return_value = items[0] if items else None
    result.all.return_value = items
    return result


def _make_execute_result_scalar(item):
    result = MagicMock()
    result.scalar_one_or_none.return_value = item
    result.scalar.return_value = item
    return result
