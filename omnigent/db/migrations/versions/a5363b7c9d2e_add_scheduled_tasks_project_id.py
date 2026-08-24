"""Add first-class Project membership to scheduled tasks.

Revision ID: a5363b7c9d2e
Revises: gb1b2c3d4e5f
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16


revision = "a5363b7c9d2e"
down_revision = "gb1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable Project pointer and filtered-list index."""
    sqlite = op.get_bind().dialect.name == "sqlite"
    with op.batch_alter_table(
        "scheduled_tasks", recreate="always" if sqlite else "auto"
    ) as batch_op:
        batch_op.add_column(sa.Column("project_id", Uuid16(), nullable=True))
    op.create_index(
        "ix_scheduled_tasks_project_id",
        "scheduled_tasks",
        ["workspace_id", "user_id", "project_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove Project assignment while preserving task and run rows."""
    op.drop_index("ix_scheduled_tasks_project_id", table_name="scheduled_tasks")
    sqlite = op.get_bind().dialect.name == "sqlite"
    with op.batch_alter_table(
        "scheduled_tasks", recreate="always" if sqlite else "auto"
    ) as batch_op:
        batch_op.drop_column("project_id")
