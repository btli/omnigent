"""add launch_generation to conversations

Revision ID: za1b2c3d4e5f
Revises: z8a2b3c4d5e6
Create Date: 2026-07-16 00:00:00.000000

Adds ``conversations.launch_generation``: a monotonic per-session launch
counter (create launch = 1, each managed relaunch increments, wake does not).
``server_default='0'`` backfills existing rows (required for a NOT NULL add
against a populated table) and matches the ORM model default. Batch mode for
SQLite, consistent with the other conversations migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "za1b2c3d4e5f"
down_revision: str | None = "z8a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the NOT NULL ``launch_generation`` column (default 0)."""
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "launch_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    """Drop the ``launch_generation`` column."""
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("launch_generation")
