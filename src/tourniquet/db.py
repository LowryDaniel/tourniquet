"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tourniquet.config import settings


def _resolve_database_url(url: str) -> str:
    """If *url* is a relative SQLite path (sqlite+aiosqlite:///./…), resolve it
    relative to the config dir so the DB lands next to the .env file rather than
    wherever the process happens to be running from.

    The config dir is:
      1. $TOURNIQUET_CONFIG_DIR — set by the CLI before importing settings
      2. ~/.tourniquet — default install location
      3. CWD — dev fallback (relative path stays relative)
    """
    if not url.startswith("sqlite") or "///" not in url:
        return url

    # Extract the path portion after sqlite+aiosqlite:///
    prefix, _, rel = url.partition("///")
    if not rel.startswith("./") and not rel.startswith(".\\"):
        return url  # absolute path, leave as-is

    config_dir_env = os.environ.get("TOURNIQUET_CONFIG_DIR")
    if config_dir_env:
        base = Path(config_dir_env)
    else:
        tq_home = Path.home() / ".tourniquet"
        # Only use ~/.tourniquet if it exists (i.e. we're in pip-install mode)
        base = tq_home if tq_home.exists() else Path.cwd()

    resolved = (base / rel.lstrip("./").lstrip(".\\")).resolve()
    return f"{prefix}///{resolved}"


engine = create_async_engine(_resolve_database_url(settings.database_url), pool_pre_ping=True)
_SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _SessionLocal() as session:
        yield session
