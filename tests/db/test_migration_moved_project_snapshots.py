"""Round-trip coverage for moved project snapshot migration."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import _build_alembic_config

_PRIOR = "cc3d4e5f6a7b"
_THIS = "dd4e5f6a7b8c"


def _migrate(engine: sa.Engine, uri: str, target: str, *, downgrade: bool = False) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        if downgrade:
            command.downgrade(config, target)
        else:
            command.upgrade(config, target)


def test_moved_snapshot_downgrade_sweeps_origin_before_constraint(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'moved-snapshot.db'}"
    engine = sa.create_engine(uri)
    try:
        _migrate(engine, uri, _THIS)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO session_project_snapshots "
                    "(workspace_id, session_id, project_id, snapshot_origin, "
                    "project_row_version, defaults_schema_version, defaults_json, created_at) "
                    "VALUES (0, 'conv_moved', 'proj_target', 'moved', NULL, 1, '{}', 1)"
                )
            )

        _migrate(engine, uri, _PRIOR, downgrade=True)

        with engine.connect() as connection:
            origin = connection.execute(
                sa.text(
                    "SELECT snapshot_origin FROM session_project_snapshots "
                    "WHERE session_id='conv_moved'"
                )
            ).scalar_one()
        assert origin == "live"
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO session_project_snapshots "
                        "(workspace_id, session_id, project_id, snapshot_origin, "
                        "project_row_version, defaults_schema_version, defaults_json, created_at) "
                        "VALUES (0, 'conv_rejected', 'proj_target', 'moved', NULL, 1, '{}', 1)"
                    )
                )
    finally:
        engine.dispose()
