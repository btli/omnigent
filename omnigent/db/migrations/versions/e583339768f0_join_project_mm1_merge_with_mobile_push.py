"""join the project mm1 merge with mobile push for the rings

The rings compose #5381 (698cf86c30ba) beside #8883 (mp1b2c3d4e5f), and the
production ring cannot take a branch pin, so the join lives on fork main.
Remove once either PR lands upstream or leaves the composition.

Revision ID: e583339768f0
Revises: 698cf86c30ba, mp1b2c3d4e5f
Create Date: 2026-10-03 18:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "e583339768f0"
down_revision: str | Sequence[str] | None = (
    "698cf86c30ba",
    "mp1b2c3d4e5f",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
