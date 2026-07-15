"""Schema tests for the Projects MVP PR1 migration."""

from __future__ import annotations

import sqlalchemy as sa

from omnigent.db.db_models import OmnigentBase
from omnigent.db.utils import get_or_create_engine


def test_projects_tables_and_metadata_column_are_migrated(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    inspector = sa.inspect(engine)

    assert {"projects", "session_project_snapshots", "project_migration_ledger"} <= set(
        inspector.get_table_names()
    )
    assert "project_id" in {
        column["name"] for column in inspector.get_columns("omnigent_conversation_metadata")
    }
    assert {table.name for table in OmnigentBase.metadata.tables.values()} >= {
        "projects",
        "session_project_snapshots",
        "project_migration_ledger",
    }


def test_projects_schema_has_portable_keys_constraints_and_indexes(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    inspector = sa.inspect(engine)

    assert inspector.get_pk_constraint("projects")["constrained_columns"] == [
        "workspace_id",
        "id",
    ]
    assert inspector.get_pk_constraint("session_project_snapshots")["constrained_columns"] == [
        "workspace_id",
        "session_id",
    ]
    assert inspector.get_pk_constraint("project_migration_ledger")["constrained_columns"] == [
        "workspace_id",
        "id",
    ]

    project_uniques = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("projects")
    }
    assert ("workspace_id", "storage_key") in project_uniques
    assert (
        "workspace_id",
        "owner_principal_id",
        "normalized_name_checksum",
    ) in project_uniques
    assert all(not index["unique"] for index in inspector.get_indexes("projects"))
    assert all(
        foreign_keys == []
        for foreign_keys in (
            inspector.get_foreign_keys("projects"),
            inspector.get_foreign_keys("session_project_snapshots"),
            inspector.get_foreign_keys("project_migration_ledger"),
        )
    )


def test_project_id_index_includes_workspace_project_and_session_id(db_uri: str) -> None:
    indexes = {
        index["name"]: index["column_names"]
        for index in sa.inspect(get_or_create_engine(db_uri)).get_indexes(
            "omnigent_conversation_metadata"
        )
    }
    assert indexes["ix_conversation_metadata_project_id"] == [
        "workspace_id",
        "project_id",
        "id",
    ]
