"""Join the published ring head with the mobile-push merge after bf30.

Revision ID: 7d57e0ab7319
Revises: 30ae6d17d669, 5cee91b68c65
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "7d57e0ab7319"
down_revision: str | Sequence[str] | None = ("30ae6d17d669", "5cee91b68c65")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
