"""Tests for alembic upgrade_to_head on SQLite.

Hermetic: uses a temporary file-backed SQLite URL — no Postgres, no network,
no env vars beyond what the fixture injects.

Covers:
1. Fresh DB: upgrade_to_head creates all expected tables.
2. Idempotency: calling upgrade_to_head a second time is a no-op.
3. Pre-0003 simulation: a schema at 0002 head (missing tq_token_sha256)
   is correctly upgraded to include the column.
"""

from __future__ import annotations

import os
import tempfile

from sqlalchemy import create_engine, inspect


def _tmp_sqlite_url() -> tuple[str, str]:
    """Return (sync_url, file_path) for a fresh temp SQLite DB."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return f"sqlite:///{path}", path


def _table_names(url: str) -> list[str]:
    eng = create_engine(url)
    try:
        return sorted(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def _column_names(url: str, table: str) -> list[str]:
    eng = create_engine(url)
    try:
        return [c["name"] for c in inspect(eng).get_columns(table)]
    finally:
        eng.dispose()


_EXPECTED_TABLES = sorted(
    [
        "users",
        "api_keys",
        "usage_events",
        "triggers",
        "caps_today",
        "api_key_actions",
        "alembic_version",
    ]
)


class TestMigrationsSQLite:
    def test_fresh_db_upgrade_to_head(self):
        """upgrade_to_head on a blank SQLite file creates all expected tables."""
        # Import here so conftest env vars are already applied
        from tourniquet.migrate import upgrade_to_head

        url, path = _tmp_sqlite_url()
        try:
            upgrade_to_head(url)
            assert _table_names(url) == _EXPECTED_TABLES
        finally:
            os.unlink(path)

    def test_idempotency(self):
        """Calling upgrade_to_head twice is a no-op (no error, same tables)."""
        from tourniquet.migrate import upgrade_to_head

        url, path = _tmp_sqlite_url()
        try:
            upgrade_to_head(url)
            tables_after_first = _table_names(url)
            upgrade_to_head(url)
            tables_after_second = _table_names(url)
            assert tables_after_first == tables_after_second == _EXPECTED_TABLES
        finally:
            os.unlink(path)

    def test_pre_0003_schema_gets_tq_token_sha256(self):
        """A DB at migration 0002 (missing tq_token_sha256) is upgraded correctly.

        This reproduces the 2026-05-12 incident: existing dev DBs had the
        api_keys table but lacked the tq_token_sha256 column added in 0003.
        upgrade_to_head must detect the partial migration and apply 0003+0004.
        """

        from alembic import command
        from alembic.config import Config

        from tourniquet.migrate import _ALEMBIC_INI, upgrade_to_head

        url, path = _tmp_sqlite_url()
        try:
            # Bring to exactly 0002 — simulates the pre-incident state.
            cfg = Config(str(_ALEMBIC_INI))
            cfg.set_main_option("sqlalchemy.url", url)
            command.upgrade(cfg, "0002")

            cols_before = _column_names(url, "api_keys")
            assert "tq_token_sha256" not in cols_before, (
                "Sanity: tq_token_sha256 should not exist at 0002"
            )

            # Now run the full upgrade
            upgrade_to_head(url)

            cols_after = _column_names(url, "api_keys")
            assert "tq_token_sha256" in cols_after, (
                "tq_token_sha256 must exist after upgrade to head"
            )
            assert _table_names(url) == _EXPECTED_TABLES
        finally:
            os.unlink(path)
