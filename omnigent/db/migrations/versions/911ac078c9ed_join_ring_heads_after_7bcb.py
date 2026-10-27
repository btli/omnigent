"""Join fork main's ring head with btli #8883's 7bcb8d508d68.
Fork main and every ring compose to one head.
Remove once fork main no longer carries a fork-only head.
Create Date: 2026-10-27T05:50:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "911ac078c9ed"
down_revision: str | Sequence[str] | None = ("04750d7b26cb", "7bcb8d508d68")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
