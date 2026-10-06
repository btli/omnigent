"""Published ring databases must remain upgradeable after feature PRs leave."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db import ConversationBase, OmnigentBase
from omnigent.db.utils import _build_alembic_config, _initialize_or_verify_schema, _run_migrations


def test_bootstrap_metadata_preserves_migrated_tables(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'metadata.db'}"
    engine = sa.create_engine(uri)
    try:
        command.upgrade(_build_alembic_config(uri), "head")
        actual = sa.inspect(engine)
        models = {
            table.name: set(table.columns.keys())
            for metadata in (OmnigentBase.metadata, ConversationBase.metadata)
            for table in metadata.sorted_tables
        }
        for table in actual.get_table_names():
            if table != "alembic_version":
                assert {column["name"] for column in actual.get_columns(table)} <= models.get(
                    table, set()
                ), table
    finally:
        engine.dispose()


@pytest.mark.parametrize("upgrade", [_initialize_or_verify_schema, _run_migrations])
def test_resume_published_branches(tmp_path: Path, upgrade) -> None:
    uri = f"sqlite:///{tmp_path / 'branches.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "f5b9e1a3d2c4")
            command.upgrade(config, "mp1b2c3d4e5f")
            assert (
                len(connection.execute(sa.text("SELECT version_num FROM alembic_version")).all())
                == 2
            )
        upgrade(engine, uri)
        with engine.connect() as connection:
            assert connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one() == (ScriptDirectory.from_config(config).get_current_head())
    finally:
        engine.dispose()


def test_unknown_branch_prevents_any_migration(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'future.db'}"
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text("CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY)")
            )
            connection.execute(
                sa.text("INSERT INTO alembic_version VALUES ('5e92355b0960'), ('future')")
            )
        with pytest.raises(RuntimeError, match="newer"):
            _initialize_or_verify_schema(engine, uri)
        with engine.connect() as connection:
            assert set(
                connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()
            ) == {"5e92355b0960", "future"}
            assert sa.inspect(connection).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "revision", [None, "e583339768f0", "5e92355b0960", "6c5a89455605", "e8dded566507"]
)
def test_published_ring_upgrade_preserves_projects_and_preferences(
    tmp_path: Path, revision: str | None
) -> None:
    uri = f"sqlite:///{tmp_path / 'ring.db'}"
    config = _build_alembic_config(uri)
    script = ScriptDirectory.from_config(config)
    assert script.get_current_head() is not None
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, revision or "head")
            connection.execute(sa.text("INSERT INTO users (id, is_admin) VALUES ('owner', false)"))
            connection.execute(
                sa.text(
                    "INSERT INTO projects "
                    "(workspace_id, id, name, user_id, created_at, updated_at) "
                    "VALUES (1, :id, 'Preserved project', 'owner', 123, 456)"
                ),
                {"id": bytes.fromhex("a" * 32)},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO preferences (workspace_id, user_id, key, value) "
                    "VALUES (1, 'owner', 'project_order', :value)"
                ),
                {"value": b"preserved ordering"},
            )
            command.upgrade(config, "head")
            command.upgrade(config, "head")
            assert connection.execute(sa.text("SELECT name FROM projects")).scalar_one() == (
                "Preserved project"
            )
            assert (
                connection.execute(
                    sa.text("SELECT value FROM preferences WHERE key = 'project_order'")
                ).scalar_one()
                == b"preserved ordering"
            )
            assert connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one() == (script.get_current_head())
            assert "project_order" not in {
                column["name"] for column in sa.inspect(connection).get_columns("users")
            }
    finally:
        engine.dispose()
