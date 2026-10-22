"""join the ring heads after c8e4

#8883's a24417751342 joins upstream's c8e4f2a6b901, which sits beside fork
main's 2f05087b3da7 in every ring. The production ring cannot take a branch
pin, so the join lives on fork main. Remove once #8883 lands upstream or
leaves the rings.

Revision ID: 9c694dc26f7e
Revises: 2f05087b3da7, a24417751342
Create Date: 2026-10-22 22:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "9c694dc26f7e"
down_revision: str | Sequence[str] | None = ("2f05087b3da7", "a24417751342")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
