"""Project inheritance tests for the JSON session-create path."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlAgentConfiguration,
    SqlConversationMetadata,
    SqlSessionProjectSnapshot,
)
from omnigent.db.utils import get_or_create_conversation_engine, get_or_create_engine
from omnigent.errors import OmnigentError
from omnigent.server.app import add_workspace_scope_middleware
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.server.schemas import SessionCreateMetadata
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
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
        ),
        prefix="/v1",
    )
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
