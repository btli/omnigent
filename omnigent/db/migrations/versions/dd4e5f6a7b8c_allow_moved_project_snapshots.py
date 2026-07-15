"""Allow post-create project membership snapshots.

Revision ID: dd4e5f6a7b8c
Revises: cc3d4e5f6a7b
Create Date: 2026-07-15 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "dd4e5f6a7b8c"
down_revision: str | None = "cc3d4e5f6a7b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_session_project_snapshots_origin"


def upgrade() -> None:
    with op.batch_alter_table("session_project_snapshots") as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT,
            "snapshot_origin IN ('live', 'backfill', 'moved')",
        )


def downgrade() -> None:
    op.execute(
        "UPDATE session_project_snapshots SET snapshot_origin='live' WHERE snapshot_origin='moved'"
    )
    with op.batch_alter_table("session_project_snapshots") as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT,
            "snapshot_origin IN ('live', 'backfill')",
        )
