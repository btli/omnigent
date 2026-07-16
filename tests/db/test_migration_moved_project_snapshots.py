"""Round-trip coverage for the squashed projects migration."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config

_PRIOR = "bb2c3d4e5f6a"
_THIS = "cc3d4e5f6a7b"


def _migrate(engine: sa.Engine, uri: str, target: str, *, downgrade: bool = False) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        if downgrade:
            command.downgrade(config, target)
        else:
            command.upgrade(config, target)


def test_projects_migration_accepts_all_origins_and_downgrades_cleanly(
    tmp_path: Path,
) -> None:
    uri = f"sqlite:///{tmp_path / 'moved-snapshot.db'}"
    engine = sa.create_engine(uri)
    try:
        _migrate(engine, uri, _THIS)
        constraints = sa.inspect(engine).get_check_constraints("session_project_snapshots")
        origin_constraint = next(
            item for item in constraints if item["name"] == "ck_session_project_snapshots_origin"
        )
        sqltext = origin_constraint["sqltext"]
        assert sqltext is not None
        assert all(origin in sqltext for origin in ("live", "backfill", "moved"))

        with engine.begin() as connection:
            for origin in ("live", "backfill", "moved"):
                connection.execute(
                    sa.text(
                        "INSERT INTO session_project_snapshots "
                        "(workspace_id, session_id, project_id, snapshot_origin, "
                        "project_row_version, defaults_schema_version, defaults_json, created_at) "
                        "VALUES (0, :session_id, 'proj_target', :origin, NULL, 1, '{}', 1)"
                    ),
                    {"session_id": f"conv_{origin}", "origin": origin},
                )

        _migrate(engine, uri, _PRIOR, downgrade=True)

        inspector = sa.inspect(engine)
        assert "session_project_snapshots" not in inspector.get_table_names()
        metadata_columns = {
            column["name"] for column in inspector.get_columns("omnigent_conversation_metadata")
        }
        assert "project_id" not in metadata_columns
    finally:
        engine.dispose()
