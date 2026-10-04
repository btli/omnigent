"""join the ring head with the mobile push rebuild

#8883 now follows the shipped mp1b2c3d4e5f with 0ffc4690e229, which sits beside
e583339768f0 on fork main. The production ring cannot take a branch pin, so the
join lives on fork main. Remove once #8883 lands upstream or leaves the rings.

Revision ID: 5e92355b0960
Revises: e583339768f0, 0ffc4690e229
Create Date: 2026-10-04 16:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "5e92355b0960"
down_revision: str | Sequence[str] | None = (
    "e583339768f0",
    "0ffc4690e229",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
