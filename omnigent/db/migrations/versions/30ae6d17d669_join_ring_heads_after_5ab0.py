"""Join the published ring head with the mobile-push merge after 5ab0.

Revision ID: 30ae6d17d669
Revises: c66c47327ac7, 7df14f1e4697
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "30ae6d17d669"
down_revision: str | Sequence[str] | None = ("c66c47327ac7", "7df14f1e4697")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
