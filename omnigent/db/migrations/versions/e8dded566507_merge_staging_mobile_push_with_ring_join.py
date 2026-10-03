"""merge the staging mobile-push join with the ring join

Fork main now carries e583339768f0 (joining #5381's 698cf86c30ba with #8883's
mp1b2c3d4e5f), which sits beside this branch's 6c5a89455605. 6c5a89455605
may already be stamped on staging, so join both here. Staging-only pin.

Revision ID: e8dded566507
Revises: e583339768f0, 6c5a89455605
Create Date: 2026-10-03 18:25:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "e8dded566507"
down_revision: str | Sequence[str] | None = (
    "e583339768f0",
    "6c5a89455605",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
