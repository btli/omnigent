"""Join fork main's ring head with btli's host-drain mn1a2b3c4d5e.
Fork main and every ring compose to one head.
Remove once fork main no longer carries a fork-only head.
Create Date: 2026-11-02T21:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "d99f1e66450e"
down_revision: str | Sequence[str] | None = ("00ebc9b7d7f6", "mn1a2b3c4d5e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
