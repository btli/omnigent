"""Published ring databases must remain upgradeable after feature PRs leave."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config


@pytest.mark.parametrize(
    "revision", [None, "e583339768f0", "5e92355b0960", "6c5a89455605", "e8dded566507"]
)
def test_published_ring_upgrade_preserves_projects_and_preferences(
    tmp_path: Path, revision: str | None
) -> None:
    uri = f"sqlite:///{tmp_path / 'ring.db'}"
    config = _build_alembic_config(uri)
    script = ScriptDirectory.from_config(config)
    assert script.get_current_head() == "8be94ccb7aef"
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
