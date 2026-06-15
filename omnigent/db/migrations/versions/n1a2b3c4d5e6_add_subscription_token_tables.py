"""add multi-subscription tables

Revision ID: n1a2b3c4d5e6
Revises: m1a2b3c4d5e6
Create Date: 2026-06-14 00:00:00.000000

Adds the five tables backing native multi-subscription rotation
(see ``omnigent/subscription_tokens/``):

* ``credential_pools`` / ``provider_accounts`` — synced from the config
  ``pools:`` block (the source of truth).
* ``provider_account_limit_states`` — observed usage-limit state per
  account (reactive / poller / manual).
* ``provider_account_costs`` — per-account, per-day cost rollup.
* ``session_credential_bindings`` — the active account per session.

All are brand-new tables, so deployments whose database lacks them are
unaffected: the subscription-token code paths only touch them when a pool is
configured.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "n1a2b3c4d5e6"
down_revision: str | None = "m1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the five subscription-token tables."""
    op.create_table(
        "credential_pools",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("family", sa.String(32), nullable=False),
        sa.Column("failover_mode", sa.String(16), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "provider_accounts",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("pool_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("family", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claude_config_dir", sa.Text(), nullable=True),
        sa.Column("codex_config_dir", sa.Text(), nullable=True),
        sa.Column("api_key_ref", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["pool_id"], ["credential_pools.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_provider_accounts_family", "provider_accounts", ["family"])
    op.create_index("ix_provider_accounts_pool_id", "provider_accounts", ["pool_id"])
    op.create_table(
        "provider_account_limit_states",
        sa.Column("credential_id", sa.String(64), nullable=False),
        sa.Column("limit_status", sa.String(16), nullable=False),
        sa.Column("is_limited", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("limited_until", sa.Integer(), nullable=True),
        sa.Column("windows_json", sa.Text(), nullable=True),
        sa.Column("detection_source", sa.String(16), nullable=True),
        sa.Column("last_checked_at", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["credential_id"], ["provider_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("credential_id"),
    )
    op.create_table(
        "provider_account_costs",
        sa.Column("credential_id", sa.String(64), nullable=False),
        sa.Column("day_utc", sa.String(10), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["credential_id"], ["provider_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("credential_id", "day_utc"),
    )
    op.create_table(
        "session_credential_bindings",
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("credential_id", sa.String(64), nullable=False),
        sa.Column("family", sa.String(32), nullable=False),
        sa.Column("bound_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["credential_id"], ["provider_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        "ix_session_credential_bindings_credential_id",
        "session_credential_bindings",
        ["credential_id"],
    )


def downgrade() -> None:
    """Drop the five subscription-token tables."""
    op.drop_index(
        "ix_session_credential_bindings_credential_id",
        table_name="session_credential_bindings",
    )
    op.drop_table("session_credential_bindings")
    op.drop_table("provider_account_costs")
    op.drop_table("provider_account_limit_states")
    op.drop_index("ix_provider_accounts_pool_id", table_name="provider_accounts")
    op.drop_index("ix_provider_accounts_family", table_name="provider_accounts")
    op.drop_table("provider_accounts")
    op.drop_table("credential_pools")
