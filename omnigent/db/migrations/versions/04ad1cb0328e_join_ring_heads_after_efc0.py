"""Join fork main's ring head with btli's efc019f4d3dd.
Fork main and every ring compose to one head.
Remove once fork main no longer carries a fork-only head.
Create Date: 2026-11-13T15:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "04ad1cb0328e"
down_revision: str | Sequence[str] | None = ("d3907fe28e17", "efc019f4d3dd")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
