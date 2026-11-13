"""Join the published ring and scheduled-task kind heads.

Revision ID: 91c2e7a4d6b8
Revises: 04ad1cb0328e, 27d16059fab9
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "91c2e7a4d6b8"
down_revision: str | Sequence[str] | None = ("04ad1cb0328e", "27d16059fab9")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
