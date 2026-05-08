"""api_key_actions audit log table

Adds the table that backs the per-key Action history view in the dashboard
and the proxy's "did we already fire this threshold today?" idempotency
check.

For SQLite local dev, `Base.metadata.create_all()` in `cli.py::cmd_start`
already auto-creates this table on first launch — this migration only
matters for Postgres production deployments where `alembic upgrade head`
is the schema source of truth.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_key_actions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "api_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # action: kill_now | lift_by_amount | lift_mode | cap_set |
        #         recovery_offered | alert_fired
        sa.Column("action", sa.String(40), nullable=False),
        # source: slack_socket | telegram_poll | web | cli | proxy | auto
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        # JSONB on Postgres, JSON on SQLite (handled by JSONB alias in models.py)
        sa.Column("details", postgresql.JSONB, nullable=True),
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
