"""Join the published ring head with the mobile-push merge after 47d3.

Revision ID: 9fa1f284ce0f
Revises: 30ae6d17d669, 4f1dfa40b1ab
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "9fa1f284ce0f"
down_revision: str | Sequence[str] | None = ("30ae6d17d669", "4f1dfa40b1ab")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
