"""add credential_pools.rotation_mode

Revision ID: subtok_rotmode
Revises: subtok_oauth_ref
Create Date: 2026-06-19 00:00:00.000000

Adds ``credential_pools.rotation_mode`` — how the router ranks the *available*
members of a pool: ``"max_headroom"`` (the default; most remaining capacity) or
``"soonest_reset"`` (the member whose weekly renewal window resets soonest, to
spend allowance before it lapses). Non-null with a ``"max_headroom"`` server
default so existing pool rows backfill to today's behaviour and deployments
without a ``pools:`` block are unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "subtok_rotmode"
down_revision: str | None = "subtok_oauth_ref"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the ``rotation_mode`` column to ``credential_pools``."""
    # batch_alter_table for SQLite safety (the table-rebuild path); a plain
    # ALTER on the other dialects. The server_default backfills existing rows.
    with op.batch_alter_table("credential_pools") as batch_op:
        batch_op.add_column(
            sa.Column(
                "rotation_mode",
                sa.String(length=16),
                nullable=False,
                server_default="max_headroom",
            )
        )


def downgrade() -> None:
    """Drop ``credential_pools.rotation_mode``."""
    with op.batch_alter_table("credential_pools") as batch_op:
        batch_op.drop_column("rotation_mode")
