import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

# Use disable_existing_loggers=False so pytest's caplog handler is not trampled
# when upgrade_to_head() is called mid-suite.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)  # type: ignore[arg-type]

# URL resolution order (highest priority last wins):
#   1. Programmatic override — migrate.py calls config.set_main_option() before
#      running migrations so tests and the runtime helper can pass their own URL.
#   2. DATABASE_URL env var.
#   3. Hard-coded SQLite fallback so `alembic history`/`heads` work without env.
#
# Async dialect prefixes are stripped: Alembic's command layer is synchronous
# and crashes with MissingGreenlet when handed an async URL.
_programmatic = config.get_main_option("sqlalchemy.url") or ""
if not _programmatic:
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
