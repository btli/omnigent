"""add kind discriminator to git_credentials

Revision ID: z9a2b3c4d5e6
Revises: z8a2b3c4d5e6
Create Date: 2026-07-16 00:00:00.000000

Adds ``git_credentials.kind`` — a stable int code (pat=1, oauth=2; see
omnigent.db.enum_codecs GIT_CREDENTIAL_KIND) recording the credential type.
``server_default='1'`` backfills existing rows to ``pat`` (P1 ships pat only),
which is required for a NOT NULL add against a populated table. A
``CHECK (kind IN (1, 2))`` mirrors the other enum columns. No foreign keys
(Rule R032); no partial indexes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z9a2b3c4d5e6"
down_revision: str | None = "z8a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    """Add the NOT NULL ``kind`` column (default pat) and its CHECK."""
    sqlite = _is_sqlite()
    with op.batch_alter_table(
        "git_credentials", recreate="always" if sqlite else "auto"
    ) as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.SmallInteger(), nullable=False, server_default="1")
        )
        batch_op.create_check_constraint("ck_git_credentials_kind", "kind IN (1, 2)")


def downgrade() -> None:
    """Drop the ``kind`` column and its CHECK."""
    sqlite = _is_sqlite()
    with op.batch_alter_table(
        "git_credentials", recreate="always" if sqlite else "auto"
    ) as batch_op:
        batch_op.drop_constraint("ck_git_credentials_kind", type_="check")
        batch_op.drop_column("kind")
