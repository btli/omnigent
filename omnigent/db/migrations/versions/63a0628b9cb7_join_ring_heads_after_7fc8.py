"""Join the published ring head with the mobile-push merge after 27d1.

Revision ID: 63a0628b9cb7
Revises: 91c2e7a4d6b8, 7fc806e94a38
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "63a0628b9cb7"
down_revision: str | Sequence[str] | None = ("91c2e7a4d6b8", "7fc806e94a38")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
