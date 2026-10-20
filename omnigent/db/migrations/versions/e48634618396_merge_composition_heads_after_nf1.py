"""merge composition heads after nf1

698cf86c30ba is already published; upstream's nf12ab34cd56 grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: e48634618396
Revises: 698cf86c30ba, nf12ab34cd56
Create Date: 2026-10-20 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "e48634618396"
down_revision: str | Sequence[str] | None = ("698cf86c30ba", "nf12ab34cd56")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
