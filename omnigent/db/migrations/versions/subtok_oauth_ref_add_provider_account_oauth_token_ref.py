"""add provider_accounts.oauth_token_ref

Revision ID: subtok_oauth_ref
Revises: subtok_tables
Create Date: 2026-06-15 00:00:00.000000

Adds ``provider_accounts.oauth_token_ref`` — the secret reference for a
subscription authenticated by a *headless OAuth token* (``claude setup-token``
→ ``CLAUDE_CODE_OAUTH_TOKEN``, or a Codex access token → ``CODEX_ACCESS_TOKEN``)
instead of an isolated config dir. Additive + nullable, so existing rows and
deployments without a ``pools:`` block are unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "subtok_oauth_ref"
down_revision: str | None = "subtok_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``oauth_token_ref`` column to ``provider_accounts``."""
    # batch_alter_table for SQLite safety (the table-rebuild path); a plain
    # ALTER on the other dialects.
    with op.batch_alter_table("provider_accounts") as batch_op:
        batch_op.add_column(sa.Column("oauth_token_ref", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop ``provider_accounts.oauth_token_ref``."""
    with op.batch_alter_table("provider_accounts") as batch_op:
        batch_op.drop_column("oauth_token_ref")
