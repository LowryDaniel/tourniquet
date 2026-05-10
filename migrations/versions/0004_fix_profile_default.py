"""fix profile default — was 'hobby', should match models.py 'standard'

Migration 0001 declared ``api_keys.profile`` with ``server_default="hobby"``,
but ``billing/profiles.py`` defines only ``standard | aggressive | monitor``
and ``models.py`` uses ``default="standard"``. A Postgres deployment seeded
via ``alembic upgrade head`` therefore got ``profile='hobby'`` for any key
created without an explicit profile — a value no profile config exists for.
SQLite deployments seeded via ``Base.metadata.create_all()`` already used the
Python-side ``"standard"`` default, so they were unaffected. This migration
aligns Postgres with the Python-side default.

Existing rows with ``profile='hobby'`` are intentionally NOT migrated:
that's a data change, separate from this schema-default fix. Operators
running long-lived Postgres instances should run a one-shot
``UPDATE api_keys SET profile='standard' WHERE profile='hobby'`` once they
verify nothing in their tooling depends on the legacy value.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "api_keys",
        "profile",
        existing_type=sa.String(50),
        server_default="standard",
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "api_keys",
        "profile",
        existing_type=sa.String(50),
        server_default="hobby",
        existing_nullable=False,
    )
