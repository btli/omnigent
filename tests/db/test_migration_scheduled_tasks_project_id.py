"""Migration coverage for scheduled-task Project assignment."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.db_models import SqlScheduledTask, Uuid16
from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

_PREVIOUS_HEAD = "e5d9bc8ac650"
_TASK_ID = "11111111111111111111111111111111"
_RUN_ID = "22222222222222222222222222222222"
_PROJECT_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _insert_task_and_run(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(workspace_id, id, name, prompt, rrule, user_id, agent_id, timezone, "
                "state, execution_target, permission_mode, created_at) VALUES "
                f"(0, X'{_TASK_ID}', 'nightly', 'prompt', 'FREQ=DAILY', 'alice', "
                "X'33333333333333333333333333333333', 'UTC', 1, 1, 'acceptEdits', 10)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_task_runs "
                "(workspace_id, id, scheduled_task_id, status, scheduled_at) VALUES "
                f"(0, X'{_RUN_ID}', X'{_TASK_ID}', 1, 11)"
            )
        )


def _row_counts(engine: sa.Engine) -> tuple[int, int]:
    with engine.connect() as conn:
        return (
            conn.execute(sa.text("SELECT count(*) FROM scheduled_tasks")).scalar_one(),
            conn.execute(sa.text("SELECT count(*) FROM scheduled_task_runs")).scalar_one(),
        )


def test_project_id_column_and_index_at_head(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'head.db'}"
    engine = get_or_create_engine(uri)
    inspector = sa.inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("scheduled_tasks")}
    project_column = columns["project_id"]
    assert project_column["nullable"] is True
    assert isinstance(project_column["type"], sa.LargeBinary)
    assert isinstance(SqlScheduledTask.__table__.c.project_id.type, Uuid16)
    assert inspector.get_foreign_keys("scheduled_tasks") == []

    indexes = {index["name"]: index for index in inspector.get_indexes("scheduled_tasks")}
    assert indexes["ix_scheduled_tasks_project_id"]["unique"] == 0
    assert indexes["ix_scheduled_tasks_project_id"]["column_names"] == [
        "workspace_id",
        "user_id",
        "project_id",
        "created_at",
        "id",
    ]
    assert indexes["ix_scheduled_tasks_user_scope"]["column_names"] == [
        "workspace_id",
        "user_id",
        "created_at",
        "id",
    ]
    clear_engine_cache()


def test_upgrade_leaves_existing_tasks_project_id_null(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'upgrade.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, _PREVIOUS_HEAD)
    _insert_task_and_run(engine)

    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "a5363b7c9d2e")

    with engine.connect() as conn:
        project_id, permission_mode = conn.execute(
            sa.text("SELECT project_id, permission_mode FROM scheduled_tasks")
        ).one()
    assert project_id is None
    assert permission_mode == "acceptEdits"
    assert _row_counts(engine) == (1, 1)
    engine.dispose()


def test_project_assignment_migration_downgrade_round_trip(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'round_trip.db'}"
    engine = get_or_create_engine(uri)
    _insert_task_and_run(engine)
    with engine.begin() as conn:
        conn.execute(sa.text(f"UPDATE scheduled_tasks SET project_id = X'{_PROJECT_ID}'"))

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, _PREVIOUS_HEAD)

    inspector = sa.inspect(engine)
    assert "project_id" not in {
        column["name"] for column in inspector.get_columns("scheduled_tasks")
    }
    assert "ix_scheduled_tasks_project_id" not in {
        index["name"] for index in inspector.get_indexes("scheduled_tasks")
    }
    with engine.connect() as conn:
        assert (
            conn.execute(sa.text("SELECT permission_mode FROM scheduled_tasks")).scalar_one()
            == "acceptEdits"
        )
    assert _row_counts(engine) == (1, 1)

    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
    assert "project_id" in {
        column["name"] for column in sa.inspect(engine).get_columns("scheduled_tasks")
    }
    assert _row_counts(engine) == (1, 1)
    engine.dispose()
    clear_engine_cache()


def test_single_alembic_head_at_project_id_revision(tmp_path: Path) -> None:
    config = _build_alembic_config(f"sqlite:///{tmp_path / 'heads.db'}")
    assert ScriptDirectory.from_config(config).get_heads() == ["a5363b7c9d2e"]
