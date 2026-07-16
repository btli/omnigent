"""Project inheritance tests for the JSON session-create path."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlAgentConfiguration,
    SqlConversation,
    SqlConversationMetadata,
    SqlSessionProjectSnapshot,
)
from omnigent.db.utils import get_or_create_conversation_engine, get_or_create_engine
from omnigent.entities import Conversation, LiveProjectSnapshot
from omnigent.errors import OmnigentError
from omnigent.server import managed_hosts
from omnigent.server.app import add_workspace_scope_middleware
from omnigent.server.auth import LEVEL_EDIT, UnifiedAuthProvider
from omnigent.server.routes import sessions
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.server.schemas import SessionCreateMetadata
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

ALICE = "alice@example.com"
BOB = "bob@example.com"


class FailingOverrideConversationStore(SqlAlchemyConversationStore):
    """Simulate the split-DB configuration seed failing after create."""

    def update_conversation(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("simulated agent configuration failure")


def _app(
    db_uri: str,
    *,
    conversation_store: SqlAlchemyConversationStore | None = None,
    managed: bool = False,
    artifact_store: LocalArtifactStore | None = None,
) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_error(request: Request, error: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=error.http_status,
            content={"error": {"code": error.code, "message": error.message}},
        )

    add_workspace_scope_middleware(app)
    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store or SqlAlchemyConversationStore(db_uri),
            agent_store=SqlAlchemyAgentStore(db_uri),
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=SqlAlchemyPermissionStore(db_uri),
            project_store=SqlAlchemyProjectStore(db_uri),
            artifact_store=artifact_store,
        ),
        prefix="/v1",
    )
    if managed:
        app.state.sandbox_config = object()
        app.state.host_store = object()
        app.state.managed_launches = managed_hosts.ManagedLaunchTracker()
    return app


@pytest.fixture()
def project_setup(db_uri: str) -> Iterator[tuple[SqlAlchemyProjectStore, TestClient]]:
    agents = SqlAlchemyAgentStore(db_uri)
    agents.create(agent_id="ag_test", name="test-agent", bundle_location="ag_test/bundle")
    app = _app(db_uri)
    with TestClient(app) as client:
        yield SqlAlchemyProjectStore(db_uri), client


def _headers(user: str) -> dict[str, str]:
    return {"X-Forwarded-Email": user}


def _rows(db_uri: str, session_id: str) -> tuple[object, object, object]:
    with Session(get_or_create_engine(db_uri)) as session:
        metadata = session.get(SqlConversationMetadata, (0, session_id))
        snapshot = session.get(SqlSessionProjectSnapshot, (0, session_id))
    with Session(get_or_create_conversation_engine(db_uri)) as session:
        config = session.get(SqlAgentConfiguration, (0, session_id))
    return metadata, snapshot, config


def test_project_create_writes_snapshot_and_seeds_agent_configuration(
    db_uri: str,
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
) -> None:
    projects, client = project_setup
    project = projects.create(
        ALICE,
        "Widgets",
        defaults_json={"model": "gpt-5", "reasoning_effort": "high"},
    )

    response = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={"agent_id": "ag_test", "project_id": project.id},
    )

    assert response.status_code == 201, response.text
    metadata, snapshot, config = _rows(db_uri, response.json()["id"])
    assert isinstance(metadata, SqlConversationMetadata)
    assert metadata.project_id == project.id
    assert isinstance(snapshot, SqlSessionProjectSnapshot)
    assert snapshot.project_id == project.id
    assert snapshot.snapshot_origin == "live"
    assert snapshot.project_row_version == 1
    assert snapshot.defaults_schema_version == 1
    assert json.loads(snapshot.defaults_json) == {
        "git": None,
        "harness_override": None,
        "host_id": None,
        "host_type": "external",
        "model_override": "gpt-5",
        "reasoning_effort": "high",
        "workspace": None,
    }
    assert isinstance(config, SqlAgentConfiguration)
    assert config.model_override == "gpt-5"
    assert config.reasoning_effort == "high"


def test_managed_project_create_maps_repo_branch_without_git_or_host(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _skip_managed_launch(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sessions, "_run_managed_launch", _skip_managed_launch)
    agents = SqlAlchemyAgentStore(db_uri)
    agents.create(agent_id="ag_test", name="test-agent", bundle_location="ag_test/bundle")
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create(
        ALICE,
        "Managed widgets",
        defaults_json={
            "host_type": "managed",
            "repo_url": "https://github.com/acme/widgets.git",
            "default_branch": "release",
        },
    )

    with TestClient(_app(db_uri, managed=True)) as client:
        response = client.post(
            "/v1/sessions",
            headers=_headers(ALICE),
            json={"agent_id": "ag_test", "project_id": project.id},
        )

    assert response.status_code == 201, response.text
    _, snapshot, _ = _rows(db_uri, response.json()["id"])
    assert isinstance(snapshot, SqlSessionProjectSnapshot)
    assert json.loads(snapshot.defaults_json) == {
        "git": None,
        "harness_override": None,
        "host_id": None,
        "host_type": "managed",
        "model_override": None,
        "reasoning_effort": None,
        "workspace": "https://github.com/acme/widgets.git#release",
    }


def test_session_overrides_win_alongside_project_id(
    db_uri: str,
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions, "_validated_harness_override", lambda value, _agent: value)
    projects, client = project_setup
    project = projects.create(
        ALICE,
        "Overrides",
        defaults_json={"model": "project-model", "harness": "project-harness"},
    )

    response = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={
            "agent_id": "ag_test",
            "project_id": project.id,
            "model_override": "session-model",
            "harness_override": "session-harness",
        },
    )

    assert response.status_code == 201, response.text
    _, snapshot, config = _rows(db_uri, response.json()["id"])
    assert isinstance(snapshot, SqlSessionProjectSnapshot)
    snapshot_defaults = json.loads(snapshot.defaults_json)
    assert snapshot_defaults["model_override"] == "session-model"
    assert snapshot_defaults["harness_override"] == "session-harness"
    assert isinstance(config, SqlAgentConfiguration)
    assert config.model_override == "session-model"
    assert config.harness_override == "session-harness"


def test_invalid_resolved_project_defaults_return_422(
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
) -> None:
    projects, client = project_setup
    project = projects.create(
        ALICE,
        "Invalid resolved values",
        defaults_json={
            "host_type": "external",
            "workspace": "https://github.com/acme/widgets.git",
        },
    )

    response = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={"agent_id": "ag_test", "project_id": project.id},
    )

    assert response.status_code == 422
    assert "Invalid resolved project defaults" in response.json()["error"]["message"]


def test_snapshot_survives_config_seed_failure_and_project_edit(
    db_uri: str,
) -> None:
    agents = SqlAlchemyAgentStore(db_uri)
    agents.create(agent_id="ag_test", name="test-agent", bundle_location="ag_test/bundle")
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create(ALICE, "Durable", defaults_json={"model": "gpt-5"})
    failing_store = FailingOverrideConversationStore(db_uri)
    app = _app(db_uri, conversation_store=failing_store)

    with (
        TestClient(app) as client,
        pytest.raises(RuntimeError, match="simulated agent configuration failure"),
    ):
        client.post(
            "/v1/sessions",
            headers=_headers(ALICE),
            json={"agent_id": "ag_test", "project_id": project.id},
        )

    with Session(get_or_create_engine(db_uri)) as session:
        snapshot = session.execute(select(SqlSessionProjectSnapshot)).scalar_one()
        metadata = session.get(SqlConversationMetadata, (0, snapshot.session_id))
        before = snapshot.defaults_json
    assert metadata is not None and metadata.project_id == project.id

    projects.update(
        project.id,
        ALICE,
        expected_row_version=1,
        defaults_json={"model": "gpt-5.1"},
    )
    with Session(get_or_create_engine(db_uri)) as session:
        persisted = session.get(SqlSessionProjectSnapshot, (0, snapshot.session_id))
        assert persisted is not None and persisted.defaults_json == before


def test_new_session_picks_up_project_edit_without_changing_first_snapshot(
    db_uri: str,
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
) -> None:
    projects, client = project_setup
    project = projects.create(ALICE, "Editable", defaults_json={"model": "first-model"})
    first = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={"agent_id": "ag_test", "project_id": project.id},
    )
    assert first.status_code == 201, first.text

    projects.update(
        project.id,
        ALICE,
        expected_row_version=project.row_version,
        defaults_json={"model": "second-model"},
    )
    second = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={"agent_id": "ag_test", "project_id": project.id},
    )

    assert second.status_code == 201, second.text
    _, first_snapshot, first_config = _rows(db_uri, first.json()["id"])
    _, second_snapshot, second_config = _rows(db_uri, second.json()["id"])
    assert isinstance(first_snapshot, SqlSessionProjectSnapshot)
    assert isinstance(second_snapshot, SqlSessionProjectSnapshot)
    assert json.loads(first_snapshot.defaults_json)["model_override"] == "first-model"
    assert json.loads(second_snapshot.defaults_json)["model_override"] == "second-model"
    assert isinstance(first_config, SqlAgentConfiguration)
    assert isinstance(second_config, SqlAgentConfiguration)
    assert first_config.model_override == "first-model"
    assert second_config.model_override == "second-model"


def test_project_attach_hides_wrong_owner_and_rejects_archived(
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
) -> None:
    projects, client = project_setup
    project = projects.create(ALICE, "Private")

    hidden = client.post(
        "/v1/sessions",
        headers=_headers(BOB),
        json={"agent_id": "ag_test", "project_id": project.id},
    )
    assert hidden.status_code == 404

    projects.archive(project.id, ALICE, expected_row_version=1)
    archived = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={"agent_id": "ag_test", "project_id": project.id},
    )
    assert archived.status_code == 409


def test_project_id_is_top_level_only_for_children_and_forks(
    db_uri: str,
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
) -> None:
    projects, client = project_setup
    project = projects.create(ALICE, "Top Level", defaults_json={"model": "gpt-5"})
    parent = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={"agent_id": "ag_test", "project_id": project.id},
    )
    assert parent.status_code == 201

    child = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={
            "agent_id": "ag_test",
            "parent_session_id": parent.json()["id"],
            "sub_agent_name": "helper",
            "project_id": project.id,
        },
    )
    assert child.status_code == 201, child.text
    child_metadata, child_snapshot, _ = _rows(db_uri, child.json()["id"])
    assert isinstance(child_metadata, SqlConversationMetadata)
    assert child_metadata.project_id is None
    assert child_snapshot is None

    fork = SqlAlchemyConversationStore(db_uri).fork_conversation(parent.json()["id"])
    fork_metadata, fork_snapshot, _ = _rows(db_uri, fork.id)
    assert isinstance(fork_metadata, SqlConversationMetadata)
    assert fork_metadata.project_id is None
    assert fork_snapshot is None


def test_projectless_create_and_multipart_schema_remain_unscoped(
    db_uri: str,
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
) -> None:
    _, client = project_setup
    response = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={"agent_id": "ag_test", "model_override": "gpt-5"},
    )

    assert response.status_code == 201
    metadata, snapshot, config = _rows(db_uri, response.json()["id"])
    assert isinstance(metadata, SqlConversationMetadata)
    assert metadata.project_id is None
    assert snapshot is None
    assert isinstance(config, SqlAgentConfiguration)
    assert config.model_override == "gpt-5"
    assert "project_id" not in SessionCreateMetadata.model_fields


def test_session_projects_list_uses_membership_and_archived_variant(
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
) -> None:
    projects, client = project_setup
    live_only = projects.create(ALICE, "Live only")
    archived_only = projects.create(ALICE, "Archived only")
    mixed = projects.create(ALICE, "Mixed")
    empty = projects.create(ALICE, "Empty")
    bob = projects.create(BOB, "Bob")

    def create(project_id: str, *, archived: bool = False, user: str = ALICE) -> str:
        response = client.post(
            "/v1/sessions",
            headers=_headers(user),
            json={"agent_id": "ag_test", "project_id": project_id},
        )
        assert response.status_code == 201, response.text
        session_id = response.json()["id"]
        if archived:
            archived_response = client.patch(
                f"/v1/sessions/{session_id}",
                headers=_headers(user),
                json={"archived": True},
            )
            assert archived_response.status_code == 200, archived_response.text
        return session_id

    create(live_only.id)
    create(archived_only.id, archived=True)
    create(mixed.id)
    create(mixed.id, archived=True)
    create(bob.id, user=BOB)

    live_response = client.get("/v1/sessions/projects", headers=_headers(ALICE))
    archived_response = client.get("/v1/sessions/projects?archived=true", headers=_headers(ALICE))

    assert live_response.status_code == 200
    assert live_response.json() == [
        {"id": live_only.id, "name": "Live only"},
        {"id": mixed.id, "name": "Mixed"},
    ]
    assert archived_response.status_code == 200
    assert archived_response.json() == [
        {"id": archived_only.id, "name": "Archived only"},
        {"id": mixed.id, "name": "Mixed"},
    ]
    assert empty.id not in {item["id"] for item in live_response.json()}


def test_session_list_filters_by_project_id_and_legacy_name_alias(
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
) -> None:
    projects, client = project_setup
    project = projects.create(ALICE, "Widgets")
    filed = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={"agent_id": "ag_test", "project_id": project.id},
    )
    unfiled = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={"agent_id": "ag_test"},
    )
    assert filed.status_code == unfiled.status_code == 201

    by_id = client.get(f"/v1/sessions?project_id={project.id}", headers=_headers(ALICE))
    by_name = client.get("/v1/sessions?project=Widgets", headers=_headers(ALICE))
    without_project = client.get("/v1/sessions?project_id=", headers=_headers(ALICE))

    assert [item["id"] for item in by_id.json()["data"]] == [filed.json()["id"]]
    assert [item["id"] for item in by_name.json()["data"]] == [filed.json()["id"]]
    assert unfiled.json()["id"] in {item["id"] for item in without_project.json()["data"]}
    assert filed.json()["id"] not in {item["id"] for item in without_project.json()["data"]}


def test_patch_project_id_moves_and_unfiles_with_snapshot(
    db_uri: str,
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
) -> None:
    projects, client = project_setup
    project = projects.create(ALICE, "Moved")
    created = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={"agent_id": "ag_test"},
    )
    session_id = created.json()["id"]

    moved = client.patch(
        f"/v1/sessions/{session_id}",
        headers=_headers(ALICE),
        json={"project_id": project.id},
    )

    assert moved.status_code == 200, moved.text
    metadata, snapshot, _ = _rows(db_uri, session_id)
    assert isinstance(metadata, SqlConversationMetadata)
    assert metadata.project_id == project.id
    assert isinstance(snapshot, SqlSessionProjectSnapshot)
    assert snapshot.project_id == project.id
    assert snapshot.snapshot_origin == "moved"
    assert snapshot.project_row_version is None
    assert snapshot.defaults_schema_version == 1
    assert json.loads(snapshot.defaults_json) == {}

    unfiled = client.patch(
        f"/v1/sessions/{session_id}",
        headers=_headers(ALICE),
        json={"project_id": None},
    )
    assert unfiled.status_code == 200, unfiled.text
    metadata, snapshot, _ = _rows(db_uri, session_id)
    assert isinstance(metadata, SqlConversationMetadata)
    assert metadata.project_id is None
    assert snapshot is None


def test_patch_project_id_hides_wrong_owner_and_rejects_archived(
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
) -> None:
    projects, client = project_setup
    mine = projects.create(ALICE, "Mine")
    archived = projects.create(ALICE, "Archived")
    projects.archive(archived.id, ALICE, expected_row_version=1)
    created = client.post(
        "/v1/sessions",
        headers=_headers(BOB),
        json={"agent_id": "ag_test"},
    )
    session_id = created.json()["id"]

    wrong_owner = client.patch(
        f"/v1/sessions/{session_id}",
        headers=_headers(BOB),
        json={"project_id": mine.id},
    )
    assert wrong_owner.status_code == 404

    alice_session = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={"agent_id": "ag_test"},
    ).json()["id"]
    archived_response = client.patch(
        f"/v1/sessions/{alice_session}",
        headers=_headers(ALICE),
        json={"project_id": archived.id},
    )
    assert archived_response.status_code == 409


def test_patch_project_membership_is_session_owner_only(
    db_uri: str,
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
) -> None:
    projects, client = project_setup
    bob_project = projects.create(BOB, "Bob project")
    created = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={"agent_id": "ag_test"},
    )
    session_id = created.json()["id"]
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(BOB)
    permissions.grant(BOB, session_id, LEVEL_EDIT)

    response = client.patch(
        f"/v1/sessions/{session_id}",
        headers=_headers(BOB),
        json={"project_id": bob_project.id},
    )

    assert response.status_code == 403


def test_legacy_project_label_write_forwards_membership_without_persisting_label(
    db_uri: str,
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
    caplog: pytest.LogCaptureFixture,
) -> None:
    projects, client = project_setup
    created = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={"agent_id": "ag_test"},
    )
    session_id = created.json()["id"]
    SqlAlchemyConversationStore(db_uri).set_labels(
        session_id,
        {"omni_project": "Stale legacy value"},
    )

    with caplog.at_level(logging.WARNING):
        response = client.patch(
            f"/v1/sessions/{session_id}",
            headers=_headers(ALICE),
            json={"labels": {"omni_project": "Legacy bridge"}},
        )

    assert response.status_code == 200, response.text
    project = next(item for item in projects.list(ALICE) if item.name == "Legacy bridge")
    metadata, snapshot, _ = _rows(db_uri, session_id)
    assert isinstance(metadata, SqlConversationMetadata)
    assert metadata.project_id == project.id
    assert isinstance(snapshot, SqlSessionProjectSnapshot)
    assert snapshot.project_id == project.id
    assert snapshot.snapshot_origin == "moved"
    persisted = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert persisted is not None and "omni_project" not in persisted.labels
    assert (
        caplog.messages.count(
            "omni_project label is deprecated; forwarded to project_id — "
            "migrate to the project_id API."
        )
        == 1
    )

    cleared = client.patch(
        f"/v1/sessions/{session_id}",
        headers=_headers(ALICE),
        json={"labels": {"omni_project": ""}},
    )
    assert cleared.status_code == 200, cleared.text
    metadata, snapshot, _ = _rows(db_uri, session_id)
    assert isinstance(metadata, SqlConversationMetadata)
    assert metadata.project_id is None
    assert snapshot is None


def test_json_create_forwards_legacy_project_label_and_validates_explicit_id(
    db_uri: str,
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
    caplog: pytest.LogCaptureFixture,
) -> None:
    projects, client = project_setup
    with caplog.at_level(logging.WARNING):
        forwarded = client.post(
            "/v1/sessions",
            headers=_headers(ALICE),
            json={"agent_id": "ag_test", "labels": {"omni_project": "Forwarded"}},
        )

    assert forwarded.status_code == 201, forwarded.text
    project = next(item for item in projects.list(ALICE) if item.name == "Forwarded")
    metadata, snapshot, _ = _rows(db_uri, forwarded.json()["id"])
    assert isinstance(metadata, SqlConversationMetadata) and metadata.project_id == project.id
    assert isinstance(snapshot, SqlSessionProjectSnapshot)
    assert snapshot.project_id == project.id and snapshot.snapshot_origin == "moved"
    persisted = SqlAlchemyConversationStore(db_uri).get_conversation(forwarded.json()["id"])
    assert persisted is not None and "omni_project" not in persisted.labels
    assert (
        caplog.messages.count(
            "omni_project label is deprecated; forwarded to project_id — "
            "migrate to the project_id API."
        )
        == 1
    )

    agreed = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={
            "agent_id": "ag_test",
            "project_id": project.id,
            "labels": {"omni_project": "Forwarded"},
        },
    )
    assert agreed.status_code == 201, agreed.text
    _, agreed_snapshot, _ = _rows(db_uri, agreed.json()["id"])
    assert isinstance(agreed_snapshot, SqlSessionProjectSnapshot)
    assert agreed_snapshot.snapshot_origin == "live"

    other = projects.create(ALICE, "Other")
    disagreed = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={
            "agent_id": "ag_test",
            "project_id": other.id,
            "labels": {"omni_project": "Forwarded"},
        },
    )
    assert disagreed.status_code == 400

    orphan_name = "Must not be created"
    rejected_new_label = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={
            "agent_id": "ag_test",
            "project_id": other.id,
            "labels": {"omni_project": orphan_name},
        },
    )
    assert rejected_new_label.status_code == 400
    assert projects.get_by_name(orphan_name, ALICE) is None

    rejected_explicit_unfile = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={
            "agent_id": "ag_test",
            "project_id": None,
            "labels": {"omni_project": orphan_name},
        },
    )
    assert rejected_explicit_unfile.status_code == 400
    assert projects.get_by_name(orphan_name, ALICE) is None


def test_json_forwarded_snapshot_failure_rolls_back_session(
    db_uri: str,
) -> None:
    agents = SqlAlchemyAgentStore(db_uri)
    agents.create(agent_id="ag_test", name="test-agent", bundle_location="ag_test/bundle")

    def _fail_snapshot_write(
        session: Session,
        flush_context: object,
        instances: object,
    ) -> None:
        del flush_context, instances
        if any(isinstance(row, SqlSessionProjectSnapshot) for row in session.new):
            raise RuntimeError("simulated snapshot write failure")

    event.listen(Session, "before_flush", _fail_snapshot_write)
    try:
        with TestClient(_app(db_uri), raise_server_exceptions=False) as client:
            response = client.post(
                "/v1/sessions",
                headers=_headers(ALICE),
                json={
                    "agent_id": "ag_test",
                    "title": "atomic-json-forward",
                    "labels": {"omni_project": "Atomic JSON"},
                },
            )
    finally:
        event.remove(Session, "before_flush", _fail_snapshot_write)

    assert response.status_code == 500
    with Session(get_or_create_conversation_engine(db_uri)) as session:
        assert (
            session.execute(
                select(SqlConversation).where(SqlConversation.title == "atomic-json-forward")
            ).scalar_one_or_none()
            is None
        )


def test_multipart_create_forwards_legacy_project_label(
    db_uri: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.server.helpers import build_agent_bundle

    projects = SqlAlchemyProjectStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    bundle = build_agent_bundle(name="multipart-project-agent")
    with TestClient(_app(db_uri, artifact_store=artifact_store)) as client:
        with caplog.at_level(logging.WARNING):
            response = client.post(
                "/v1/sessions",
                headers=_headers(ALICE),
                data={"metadata": json.dumps({"labels": {"omni_project": "Multipart forwarded"}})},
                files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
            )

    assert response.status_code == 201, response.text
    project = next(item for item in projects.list(ALICE) if item.name == "Multipart forwarded")
    session_id = response.json()["session_id"]
    metadata, snapshot, _ = _rows(db_uri, session_id)
    assert isinstance(metadata, SqlConversationMetadata) and metadata.project_id == project.id
    assert isinstance(snapshot, SqlSessionProjectSnapshot)
    assert snapshot.project_id == project.id and snapshot.snapshot_origin == "moved"
    persisted = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert persisted is not None and "omni_project" not in persisted.labels
    assert (
        caplog.messages.count(
            "omni_project label is deprecated; forwarded to project_id — "
            "migrate to the project_id API."
        )
        == 1
    )


def test_multipart_forwarded_snapshot_failure_rolls_back_session(
    db_uri: str,
    tmp_path: Path,
) -> None:
    from tests.server.helpers import build_agent_bundle

    artifact_store = LocalArtifactStore(str(tmp_path / "atomic-artifacts"))
    bundle = build_agent_bundle(name="atomic-multipart-agent")

    def _fail_snapshot_write(
        session: Session,
        flush_context: object,
        instances: object,
    ) -> None:
        del flush_context, instances
        if any(isinstance(row, SqlSessionProjectSnapshot) for row in session.new):
            raise RuntimeError("simulated snapshot write failure")

    event.listen(Session, "before_flush", _fail_snapshot_write)
    try:
        with TestClient(
            _app(db_uri, artifact_store=artifact_store),
            raise_server_exceptions=False,
        ) as client:
            response = client.post(
                "/v1/sessions",
                headers=_headers(ALICE),
                data={
                    "metadata": json.dumps(
                        {
                            "title": "atomic-multipart-forward",
                            "labels": {"omni_project": "Atomic multipart"},
                        }
                    )
                },
                files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
            )
    finally:
        event.remove(Session, "before_flush", _fail_snapshot_write)

    assert response.status_code == 500
    with Session(get_or_create_conversation_engine(db_uri)) as session:
        assert (
            session.execute(
                select(SqlConversation).where(SqlConversation.title == "atomic-multipart-forward")
            ).scalar_one_or_none()
            is None
        )


def test_patch_project_label_agreement_and_disagreement(
    project_setup: tuple[SqlAlchemyProjectStore, TestClient],
) -> None:
    projects, client = project_setup
    project = projects.create(ALICE, "Same")
    other = projects.create(ALICE, "Different")
    session_id = client.post(
        "/v1/sessions",
        headers=_headers(ALICE),
        json={"agent_id": "ag_test"},
    ).json()["id"]

    agreed = client.patch(
        f"/v1/sessions/{session_id}",
        headers=_headers(ALICE),
        json={"project_id": project.id, "labels": {"omni_project": "Same"}},
    )
    assert agreed.status_code == 200, agreed.text

    disagreed = client.patch(
        f"/v1/sessions/{session_id}",
        headers=_headers(ALICE),
        json={"project_id": other.id, "labels": {"omni_project": "Same"}},
    )
    assert disagreed.status_code == 400

    orphan_name = "Rejected patch project"
    rejected_new_label = client.patch(
        f"/v1/sessions/{session_id}",
        headers=_headers(ALICE),
        json={
            "project_id": other.id,
            "labels": {"omni_project": orphan_name},
        },
    )
    assert rejected_new_label.status_code == 400
    assert projects.get_by_name(orphan_name, ALICE) is None


class ArchiveBeforeMembershipStore(SqlAlchemyConversationStore):
    def __init__(self, db_uri: str, projects: SqlAlchemyProjectStore, project_id: str) -> None:
        super().__init__(db_uri)
        self._projects = projects
        self._project_id = project_id

    def set_project_membership(self, conversation_id: str, project_id: str | None) -> bool:
        if project_id == self._project_id:
            self._projects.archive(project_id, ALICE, expected_row_version=1)
        return super().set_project_membership(conversation_id, project_id)


class ArchiveBeforeCreateStore(SqlAlchemyConversationStore):
    def __init__(self, db_uri: str, projects: SqlAlchemyProjectStore, project_id: str) -> None:
        super().__init__(db_uri)
        self._projects = projects
        self._project_id = project_id

    def create_conversation(self, *args: Any, **kwargs: Any) -> Conversation:
        project_snapshot = kwargs.get("project_snapshot")
        if (
            isinstance(project_snapshot, LiveProjectSnapshot)
            and project_snapshot.project_id == self._project_id
        ):
            self._projects.archive(self._project_id, ALICE, expected_row_version=1)
        return super().create_conversation(*args, **kwargs)


def test_patch_rechecks_archived_project_at_membership_write(db_uri: str) -> None:
    agents = SqlAlchemyAgentStore(db_uri)
    agents.create(agent_id="ag_test", name="test-agent", bundle_location="ag_test/bundle")
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create(ALICE, "Race")
    store = ArchiveBeforeMembershipStore(db_uri, projects, project.id)
    with TestClient(_app(db_uri, conversation_store=store)) as client:
        session_id = client.post(
            "/v1/sessions",
            headers=_headers(ALICE),
            json={"agent_id": "ag_test"},
        ).json()["id"]
        response = client.patch(
            f"/v1/sessions/{session_id}",
            headers=_headers(ALICE),
            json={"project_id": project.id},
        )

    assert response.status_code == 409
    metadata, snapshot, _ = _rows(db_uri, session_id)
    assert isinstance(metadata, SqlConversationMetadata) and metadata.project_id is None
    assert snapshot is None


def test_create_rechecks_archived_project_before_snapshot_write(db_uri: str) -> None:
    agents = SqlAlchemyAgentStore(db_uri)
    agents.create(agent_id="ag_test", name="test-agent", bundle_location="ag_test/bundle")
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create(ALICE, "Create race")
    store = ArchiveBeforeCreateStore(db_uri, projects, project.id)
    with TestClient(_app(db_uri, conversation_store=store)) as client:
        response = client.post(
            "/v1/sessions",
            headers=_headers(ALICE),
            json={"agent_id": "ag_test", "project_id": project.id},
        )

    assert response.status_code == 409
    with Session(get_or_create_conversation_engine(db_uri)) as session:
        assert session.execute(select(SqlConversation)).scalars().all() == []
    with Session(get_or_create_engine(db_uri)) as session:
        assert session.execute(select(SqlConversationMetadata)).scalars().all() == []
        assert session.execute(select(SqlSessionProjectSnapshot)).scalars().all() == []
