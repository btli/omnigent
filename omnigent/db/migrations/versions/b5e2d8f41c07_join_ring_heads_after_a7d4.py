"""Join fork main's ring head with upstream's a7d4c2e9f610.
Fork main and every ring compose to one head.
Remove once fork main no longer carries a fork-only head.
Create Date: 2026-11-10T22:10:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "b5e2d8f41c07"
down_revision: str | Sequence[str] | None = ("e7d29b4c61a3", "a7d4c2e9f610")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
