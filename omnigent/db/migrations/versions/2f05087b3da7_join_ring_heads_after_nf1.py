"""join the ring heads after nf1

#5381 (e48634618396) and #8883 (78b6d503592b) each join upstream's
nf12ab34cd56, which sits beside fork main's 8be94ccb7aef in every ring. The
production ring cannot take a branch pin, so the join lives on fork main.
Remove once #5381 and #8883 land upstream or leave the rings.

Revision ID: 2f05087b3da7
Revises: 8be94ccb7aef, e48634618396, 78b6d503592b
Create Date: 2026-10-20 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "2f05087b3da7"
down_revision: str | Sequence[str] | None = (
    "8be94ccb7aef",
    "e48634618396",
    "78b6d503592b",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
