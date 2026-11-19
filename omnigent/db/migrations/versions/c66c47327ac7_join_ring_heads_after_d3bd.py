"""Join the published ring head with the mobile-push merge after d3bd.

Revision ID: c66c47327ac7
Revises: bee2c1838f6b, 506c62d89f0c
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "c66c47327ac7"
down_revision: str | Sequence[str] | None = ("bee2c1838f6b", "506c62d89f0c")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
