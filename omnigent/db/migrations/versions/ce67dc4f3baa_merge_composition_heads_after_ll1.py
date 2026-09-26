"""merge composition heads after ll1 and repair

d29f3a8b5c01 is a repair migration that merges the b7e41c0d92af and ll1a2b3c4d5e
branches to ensure the project_order column exists on older schemas.

Revision ID: ce67dc4f3baa
Revises: d29f3a8b5c01
Create Date: 2026-09-26 00:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "ce67dc4f3baa"
down_revision: str | Sequence[str] | None = "d29f3a8b5c01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
