"""add git_credentials table

Revision ID: z8a2b3c4d5e6
Revises: z7a2b3c4d5e6
Create Date: 2026-07-16 00:00:00.000000

Per-user, per-host git credentials, encrypted at rest. No foreign keys
(Rule R032); the application enforces the (workspace_id, owner_user_id,
host_id, label) uniqueness declared here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "z8a2b3c4d5e6"
down_revision: str | None = "z7a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``git_credentials`` table."""
    op.create_table(
        "git_credentials",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=256), nullable=False),
        sa.Column("host_id", sa.String(length=256), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("token_ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "owner_user_id",
            "host_id",
            "label",
            name="uq_git_credentials_workspace_owner_host_label",
        ),
    )


def downgrade() -> None:
    """Drop the ``git_credentials`` table."""
    op.drop_table("git_credentials")
