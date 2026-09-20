"""Merge the automation and project-order repair heads.

Revision ID: c5a9d2e7f410
Revises: 1fc4075f725e, d29f3a8b5c01
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "c5a9d2e7f410"
down_revision: str | Sequence[str] | None = ("1fc4075f725e", "d29f3a8b5c01")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
