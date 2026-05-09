"""C3 + m7: tq_token_sha256 column + api_key_actions token uniqueness

C3 — Add nullable ``api_keys.tq_token_sha256`` (String(64)) with a unique
index ``ix_api_keys_tq_token_sha256``. The SHA-256 column lets the proxy
auth path do a single indexed lookup instead of a bcrypt linear scan over
all keys. ``tq_token_hash`` (bcrypt) is retained for the legacy fallback
during rollout — see code-review-remediation.md C3 step 3.

m7 — Add a unique index on ``api_key_actions(api_key_id, action,
details->>'token_sig')`` filtered to rows where ``token_sig`` is non-null.
Closes the TOCTOU window in ``_assert_token_unused`` so two concurrent
posts of the same one-shot action token can't both succeed.

SQLite-compat: Postgres uses the JSONB ``->>`` operator inside the index
expression; SQLite uses ``json_extract(details, '$.token_sig')``. Both
backends support partial unique indexes via ``WHERE``. We branch on the
dialect so the same migration runs cleanly under either engine.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


_PG_UNIQUE_INDEX_SQL = """
CREATE UNIQUE INDEX ix_api_key_actions_unique_token
ON api_key_actions (api_key_id, action, ((details->>'token_sig')))
WHERE (details->>'token_sig') IS NOT NULL
"""

_SQLITE_UNIQUE_INDEX_SQL = """
CREATE UNIQUE INDEX ix_api_key_actions_unique_token
ON api_key_actions (
    api_key_id,
    action,
    json_extract(details, '$.token_sig')
)
WHERE json_extract(details, '$.token_sig') IS NOT NULL
"""


def upgrade() -> None:
    # ── C3: indexed SHA-256 token lookup column ───────────────────────────
    op.add_column(
        "api_keys",
        sa.Column("tq_token_sha256", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_api_keys_tq_token_sha256",
        "api_keys",
        ["tq_token_sha256"],
        unique=True,
    )

    # ── m7: token-replay uniqueness on api_key_actions ────────────────────
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(_PG_UNIQUE_INDEX_SQL)
    elif dialect == "sqlite":
        op.execute(_SQLITE_UNIQUE_INDEX_SQL)
    else:
        # Future-proof: at least try the Postgres form rather than silently
        # skipping uniqueness on an unknown dialect.
        op.execute(_PG_UNIQUE_INDEX_SQL)


def downgrade() -> None:
    # m7 first (depends on api_key_actions table only — symmetric to upgrade)
    op.execute("DROP INDEX IF EXISTS ix_api_key_actions_unique_token")

    # C3 — drop unique index then column
    op.drop_index("ix_api_keys_tq_token_sha256", table_name="api_keys")
    op.drop_column("api_keys", "tq_token_sha256")
