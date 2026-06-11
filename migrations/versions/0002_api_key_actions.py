"""api_key_actions audit log table

Adds the table that backs the per-key Action history view in the dashboard
and the proxy's "did we already fire this threshold today?" idempotency
check.

Dialect-aware: uses native UUID + JSONB on Postgres, CHAR(36) + JSON on
SQLite so `alembic upgrade head` runs cleanly on both backends.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
        from sqlalchemy.dialects.postgresql import UUID as PG_UUID

        id_col = sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        )
        api_key_id_col = sa.Column(
            "api_key_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="CASCADE"),
            nullable=False,
        )
        details_col = sa.Column("details", PG_JSONB, nullable=True)
        ts_now = sa.text("now()")
    else:
        id_col = sa.Column("id", sa.CHAR(36), primary_key=True)
        api_key_id_col = sa.Column(
            "api_key_id",
            sa.CHAR(36),
            sa.ForeignKey("api_keys.id", ondelete="CASCADE"),
            nullable=False,
        )
        details_col = sa.Column("details", sa.JSON, nullable=True)
        ts_now = sa.func.now()

    op.create_table(
        "api_key_actions",
        id_col,
        api_key_id_col,
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=ts_now,
            nullable=False,
        ),
        # action: kill_now | lift_by_amount | lift_mode | cap_set |
        #         recovery_offered | alert_fired
        sa.Column("action", sa.String(40), nullable=False),
        # source: slack_socket | telegram_poll | web | cli | proxy | auto
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        details_col,
    )
    # Composite-friendly indexes — the dashboard always filters by api_key_id
    # and orders by created_at desc.
    op.create_index(
        "ix_api_key_actions_api_key_id",
        "api_key_actions",
        ["api_key_id"],
    )
    op.create_index(
        "ix_api_key_actions_created_at",
        "api_key_actions",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_api_key_actions_created_at",
        table_name="api_key_actions",
    )
    op.drop_index(
        "ix_api_key_actions_api_key_id",
        table_name="api_key_actions",
    )
    op.drop_table("api_key_actions")
