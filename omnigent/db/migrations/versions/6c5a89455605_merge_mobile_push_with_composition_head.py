"""merge mobile push with the composition head

The staging ring composes #8883 (mp1b2c3d4e5f, parented on upstream's
mm1a2b3c4d5e) alongside the ring-only f5b9e1a3d2c4 chain. Join the two so
the composed tree has a single alembic head. Staging-only branch pin:
#8883 is correct against upstream main and must not carry a ring merge.

Revision ID: 6c5a89455605
Revises: f5b9e1a3d2c4, mp1b2c3d4e5f
Create Date: 2026-10-03 14:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "6c5a89455605"
down_revision: str | Sequence[str] | None = (
    "f5b9e1a3d2c4",
    "mp1b2c3d4e5f",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
