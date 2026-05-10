import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
fileConfig(config.config_file_name)  # type: ignore[arg-type]

# Override sqlalchemy.url from DATABASE_URL env var, with two fix-ups so the
# alembic CLI is usable on a fresh checkout:
#   1. Don't KeyError when DATABASE_URL is unset — `alembic history`,
#      `alembic heads`, and the env.py import path all worked before only by
#      coincidence (env var happened to be set). Fall back to a local SQLite
#      path so those introspection commands run without ceremony.
#   2. Rewrite async dialect URLs (`sqlite+aiosqlite://`, `postgresql+asyncpg://`)
#      to their sync form. Alembic's command layer runs synchronously and
#      crashes with `MissingGreenlet` inside engine_from_config when handed an
#      async URL. The rewrite is only used here in env.py — application code
#      still uses the async URL via tourniquet.config.settings.database_url.
#
# Note: migration 0001 issues `CREATE EXTENSION IF NOT EXISTS "pgcrypto"` and
# uses postgres dialect types throughout, so `alembic upgrade head` requires
# a real Postgres URL. The SQLite fallback only makes the CLI itself runnable
# (history / heads / check after upgrade-was-already-applied), not the
# upgrade. CI that wants to validate the schema should point DATABASE_URL at
# a throwaway Postgres instance.
_db_url = os.environ.get("DATABASE_URL", "sqlite:///./alembic-check.db")
if _db_url.startswith("sqlite+aiosqlite://"):
    _db_url = _db_url.replace("sqlite+aiosqlite://", "sqlite://", 1)
elif _db_url.startswith("postgresql+asyncpg://"):
    _db_url = _db_url.replace("postgresql+asyncpg://", "postgresql://", 1)
config.set_main_option("sqlalchemy.url", _db_url)

# Import models so autogenerate can see them
from tourniquet.models import Base  # noqa: F401, E402

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),  # type: ignore[arg-type]
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
