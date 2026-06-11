"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _uuid_col(name: str, dialect: str, **kw) -> sa.Column:  # type: ignore[type-arg]
    """Return a UUID primary-key or FK column portable across Postgres and SQLite.

    Postgres: native UUID with gen_random_uuid() server default.
    SQLite:   CHAR(36) with no server default (Python uuid4() fills it).
    """
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import UUID as PG_UUID

        col_type = PG_UUID(as_uuid=True)
        server_default = sa.text("gen_random_uuid()") if kw.pop("pk", False) else None
    else:
        col_type = sa.CHAR(36)
        server_default = None
        kw.pop("pk", None)
    return sa.Column(name, col_type, server_default=server_default, **kw)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # pgcrypto provides gen_random_uuid() on Postgres; not needed on SQLite.
    if dialect == "postgresql":
        op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    # ── Portable column factories ────────────────────────────────────────────
    # Postgres: native UUID + gen_random_uuid(); SQLite: CHAR(36).
    # Boolean/Integer server_default values must be dialect strings; we use
    # sa.text("1")/sa.text("0") which compile cleanly on both engines.
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
        from sqlalchemy.dialects.postgresql import UUID as PG_UUID

        def _id_col(name: str = "id") -> sa.Column:  # type: ignore[type-arg]
            return sa.Column(
                name, PG_UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
            )

        def _fk_col(name: str, fk: str) -> sa.Column:  # type: ignore[type-arg]
            return sa.Column(name, PG_UUID(as_uuid=True), sa.ForeignKey(fk, ondelete="CASCADE"), nullable=False)

        json_type = PG_JSONB
        bool_true = sa.text("true")
        bool_false = sa.text("false")
        ts_now = sa.text("now()")
    else:
        def _id_col(name: str = "id") -> sa.Column:  # type: ignore[type-arg]
            return sa.Column(name, sa.CHAR(36), primary_key=True)

        def _fk_col(name: str, fk: str) -> sa.Column:  # type: ignore[type-arg]
            return sa.Column(name, sa.CHAR(36), sa.ForeignKey(fk, ondelete="CASCADE"), nullable=False)

        json_type = sa.JSON
        bool_true = sa.text("1")
        bool_false = sa.text("0")
        ts_now = sa.func.now()

    op.create_table(
        "users",
        _id_col(),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("magic_link_token", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=ts_now,
            nullable=False,
        ),
        sa.Column("stripe_customer_id", sa.String(255), nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "api_keys",
        _id_col(),
        _fk_col("user_id", "users.id"),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("tq_token_hash", sa.Text, nullable=False),
        sa.Column("anthropic_key_encrypted", sa.Text, nullable=False),
        sa.Column("profile", sa.String(50), nullable=False, server_default="hobby"),
        sa.Column("daily_cap_usd_cents", sa.Integer, nullable=False, server_default="500"),
        sa.Column("kill_enabled", sa.Boolean, nullable=False, server_default=bool_true),
        sa.Column("alert_email", sa.String(255), nullable=True),
        sa.Column("auto_tune_mode", sa.String(20), nullable=False, server_default="off"),
        sa.Column("absolute_ceiling_usd_cents", sa.Integer, nullable=False, server_default="10000"),
        sa.Column("lifted_cap_usd_cents", sa.Integer, nullable=True),
        sa.Column("lift_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=ts_now,
            nullable=False,
        ),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_index("ix_api_keys_tq_token_hash", "api_keys", ["tq_token_hash"])

    op.create_table(
        "usage_events",
        _id_col(),
        _fk_col("api_key_id", "api_keys.id"),
        sa.Column("request_id", sa.String(255), nullable=True),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cap_hit", sa.Boolean, nullable=False, server_default=bool_false),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("metadata_user_id", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=ts_now,
            nullable=False,
        ),
    )
    op.create_index("ix_usage_events_api_key_id", "usage_events", ["api_key_id"])
    op.create_index("ix_usage_events_created_at", "usage_events", ["created_at"])

    op.create_table(
        "triggers",
        _id_col(),
        _fk_col("api_key_id", "api_keys.id"),
        sa.Column("condition_json", json_type, nullable=False),
        sa.Column("actions_json", json_type, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=bool_false),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_triggers_api_key_id", "triggers", ["api_key_id"])

    op.create_table(
        "caps_today",
        _fk_col("api_key_id", "api_keys.id"),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("total_usd_cents", sa.Integer, nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("api_key_id", "date"),
    )
    op.create_index("ix_caps_today_date", "caps_today", ["date"])


def downgrade() -> None:
    op.drop_table("caps_today")
    op.drop_table("triggers")
    op.drop_table("usage_events")
    op.drop_table("api_keys")
    op.drop_table("users")
