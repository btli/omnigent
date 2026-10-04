"""merge the staging mobile-push join with the rebuild join

Fork main now carries 5e92355b0960 (joining e583339768f0 with #8883's
0ffc4690e229 rebuild), which sits beside this branch's e8dded566507.
e8dded566507 may already be stamped on staging, so join both here.
Staging-only pin.

Revision ID: 8be94ccb7aef
Revises: e8dded566507, 5e92355b0960
Create Date: 2026-10-04 16:12:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "8be94ccb7aef"
down_revision: str | Sequence[str] | None = (
    "e8dded566507",
    "5e92355b0960",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
