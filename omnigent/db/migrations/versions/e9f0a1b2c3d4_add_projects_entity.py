"""Add the flat Projects MVP schema.

Revision ID: e9f0a1b2c3d4
Revises: a7b3c4d5e6f7
Create Date: 2026-07-15 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary

revision: str = "e9f0a1b2c3d4"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CKSUM32 = sa.LargeBinary(32).with_variant(MySQLBinary(32), "mysql")


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("owner_principal_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("normalized_name", sa.String(256), nullable=False),
        sa.Column("normalized_name_checksum", _CKSUM32, nullable=False),
        sa.Column("storage_key", sa.String(64), nullable=False),
        sa.Column("defaults_json", sa.Text(), nullable=False),
        sa.Column("defaults_schema_version", sa.Integer(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint("workspace_id", "storage_key", name="uq_projects_storage_key"),
        sa.UniqueConstraint(
            "workspace_id",
            "owner_principal_id",
            "normalized_name_checksum",
            name="uq_projects_owner_name_checksum",
        ),
    )
    op.create_index(
        "ix_projects_owner_id",
        "projects",
        ["workspace_id", "owner_principal_id", "id"],
    )

    op.create_table(
        "session_project_snapshots",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("snapshot_origin", sa.String(16), nullable=False),
        sa.Column("project_row_version", sa.Integer(), nullable=True),
        sa.Column("defaults_schema_version", sa.Integer(), nullable=False),
        sa.Column("defaults_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "snapshot_origin IN ('live', 'backfill', 'moved')",
            name="ck_session_project_snapshots_origin",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "session_id"),
    )
    op.create_index(
        "ix_session_project_snapshots_project_id",
        "session_project_snapshots",
        ["workspace_id", "project_id", "session_id"],
    )

    op.create_table(
        "project_migration_ledger",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("owner_principal_id", sa.String(128), nullable=False),
        sa.Column("normalized_name", sa.String(256), nullable=False),
        sa.Column("normalized_name_checksum", _CKSUM32, nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("source_fingerprint", _CKSUM32, nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "owner_principal_id",
            "normalized_name_checksum",
            name="uq_project_migration_ledger_owner_name_checksum",
        ),
    )
    op.create_index(
        "ix_project_migration_ledger_project_id",
        "project_migration_ledger",
        ["workspace_id", "project_id", "id"],
    )

    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.String(64), nullable=True))
    op.create_index(
        "ix_conversation_metadata_project_id",
        "omnigent_conversation_metadata",
        ["workspace_id", "project_id", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_metadata_project_id",
        table_name="omnigent_conversation_metadata",
    )
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("project_id")

    op.drop_index(
        "ix_project_migration_ledger_project_id",
        table_name="project_migration_ledger",
    )
    op.drop_table("project_migration_ledger")
    op.drop_index(
        "ix_session_project_snapshots_project_id",
        table_name="session_project_snapshots",
    )
    op.drop_table("session_project_snapshots")
    op.drop_index("ix_projects_owner_id", table_name="projects")
    op.drop_table("projects")
