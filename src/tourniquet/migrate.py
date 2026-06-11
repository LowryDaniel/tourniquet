"""Programmatic Alembic migration helper.

Provides upgrade_to_head(database_url) — the single entry point used by
main.py, cli.py, and the setup scripts to bring the schema to HEAD before
serving traffic.

Why not create_all?
  Base.metadata.create_all() only creates missing tables; it never adds
  columns to existing tables. Any deployment that already has a DB will miss
  columns added in later migrations (the 2026-05-12 incident).  Running
  `alembic upgrade head` is idempotent and handles both fresh and existing
  schemas correctly.

URL conversion:
  SQLAlchemy async drivers (sqlite+aiosqlite, postgresql+asyncpg) are rewritten
  to their sync equivalents before being handed to Alembic's synchronous command
  layer. The application continues to use the async URL from settings.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

# Canonical location of alembic.ini — one directory above this file's package.
_ALEMBIC_INI = Path(__file__).resolve().parent.parent.parent / "alembic.ini"


def _sync_url(database_url: str) -> str:
    """Strip async driver prefixes so Alembic's sync engine can connect."""
    url = database_url
    if url.startswith("sqlite+aiosqlite://"):
        url = url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    elif url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return url


def upgrade_to_head(database_url: str) -> None:
    """Run `alembic upgrade head` against *database_url*.

    Idempotent: if the schema is already at HEAD this is a fast no-op.
    Safe to call on every application start.

    Args:
        database_url: The SQLAlchemy database URL (async or sync form).
    """
    sync_url = _sync_url(database_url)
    cfg = Config(str(_ALEMBIC_INI))
    # Inject the URL programmatically so env.py picks it up without relying on
    # DATABASE_URL in the environment (important for tests with temp DBs).
    cfg.set_main_option("sqlalchemy.url", sync_url)
    command.upgrade(cfg, "head")
