"""Join fork main's ring head with btli #8883's 9bb7aaefed93.
Fork main and every ring compose to one head.
Remove once fork main no longer carries a fork-only head.
Create Date: 2026-11-06T12:05:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "65b928fae9c7"
down_revision: str | Sequence[str] | None = ("3b3c058e024a", "9bb7aaefed93")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
