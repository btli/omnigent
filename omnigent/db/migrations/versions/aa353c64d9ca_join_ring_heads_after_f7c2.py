"""Join the published ring head with the mobile-push merge after f7c2.

Revision ID: aa353c64d9ca
Revises: 63a0628b9cb7, 8f1ffd29c77d
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "aa353c64d9ca"
down_revision: str | Sequence[str] | None = ("63a0628b9cb7", "8f1ffd29c77d")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
