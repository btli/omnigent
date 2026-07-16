"""Tests for Sessions API CRUD endpoints (list, get, delete, patch).

Exercises the core session management routes through the ``client``
fixture. Since the lifespan event (which seeds agents) does not run
in test fixtures, we seed a test agent and conversation directly via
the stores.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.routes import sessions as sessions_module
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore


@pytest_asyncio.fixture()
async def session_id(db_uri: str) -> str:
    """Seed a test agent and conversation, return the session ID."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="test-agent", bundle_location="test:///bundle")
    conv = conv_store.create_conversation(agent_id=agent_id)
    return conv.id


# ── GET /v1/sessions (list) ─────────────────────────────────────────


async def test_list_sessions_empty(client: httpx.AsyncClient) -> None:
    """Empty database returns an empty list."""
    resp = await client.get("/v1/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["has_more"] is False


async def test_list_sessions_after_create(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """A created session appears in the list."""
    resp = await client.get("/v1/sessions")
    assert resp.status_code == 200
    body = resp.json()
    ids = [s["id"] for s in body["data"]]
    assert session_id in ids


async def test_list_sessions_pagination(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Pagination with limit returns at most N sessions."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="pag-agent", bundle_location="test:///bundle")
    conv_store.create_conversation(agent_id=agent_id)
    conv_store.create_conversation(agent_id=agent_id)

    resp = await client.get("/v1/sessions?limit=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1


# ── GET /v1/sessions/{id} (get snapshot) ────────────────────────────


async def test_get_session(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Get a session by ID returns its snapshot."""
    resp = await client.get(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == session_id


async def test_get_session_not_found(client: httpx.AsyncClient) -> None:
    """Getting a nonexistent session returns 404."""
    resp = await client.get("/v1/sessions/conv_nonexistent_12345")
    assert resp.status_code == 404


# ── DELETE /v1/sessions/{id} ────────────────────────────────────────


async def test_delete_session(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Deleting a session returns 200 with deleted: true."""
    resp = await client.delete(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True


async def test_delete_session_not_found(client: httpx.AsyncClient) -> None:
    """Deleting a nonexistent session returns 404."""
    resp = await client.delete("/v1/sessions/conv_nonexistent_12345")
    assert resp.status_code == 404


async def test_delete_running_session_attempts_stop(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Deleting a running session calls ``_stop_session_via_runner``."""
    mock_stop = AsyncMock(return_value=True)
    sessions_module._session_status_cache[session_id] = "running"
    try:
        with patch.object(sessions_module, "_stop_session_via_runner", mock_stop):
            resp = await client.delete(f"/v1/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        mock_stop.assert_awaited_once()
    finally:
        sessions_module._session_status_cache.pop(session_id, None)


async def test_delete_proceeds_when_stop_fails(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Delete succeeds even when the runner stop raises."""
    mock_stop = AsyncMock(side_effect=ConnectionError("runner gone"))
    sessions_module._session_status_cache[session_id] = "running"
    try:
        with patch.object(sessions_module, "_stop_session_via_runner", mock_stop):
            resp = await client.delete(f"/v1/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
    finally:
        sessions_module._session_status_cache.pop(session_id, None)


# ── PATCH /v1/sessions/{id} ─────────────────────────────────────────


async def test_patch_session_title(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Patching a session's title returns the updated session."""
    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"title": "New Title"},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200


async def test_patch_session_not_found(client: httpx.AsyncClient) -> None:
    """Patching a nonexistent session returns 404."""
    resp = await client.patch(
        "/v1/sessions/conv_nonexistent_12345",
        json={"title": "New Title"},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 404


# ── GET /v1/projects ─────────────────────────────────────────────────


async def test_list_projects_empty(client: httpx.AsyncClient) -> None:
    """No project rows anywhere means an empty project list."""
    resp = await client.get("/v1/projects")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_projects_returns_names_sorted(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Projects surface as sorted id/name identities."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    projects = SqlAlchemyProjectStore(db_uri)
    sprint = projects.create(RESERVED_USER_LOCAL, "Sprint 42")
    customer = projects.create(RESERVED_USER_LOCAL, "Customer X")
    a = conv_store.create_conversation()
    b = conv_store.create_conversation()
    conv_store.set_project_membership(a.id, sprint.id)
    conv_store.set_project_membership(b.id, customer.id)

    resp = await client.get("/v1/projects")
    assert resp.status_code == 200
    assert [{"id": item["id"], "name": item["name"]} for item in resp.json()] == [
        {"id": customer.id, "name": "Customer X"},
        {"id": sprint.id, "name": "Sprint 42"},
    ]


async def test_list_projects_includes_memberless_project(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A first-class project persists in the live list without sessions."""
    project = SqlAlchemyProjectStore(db_uri).create(RESERVED_USER_LOCAL, "Empty")

    resp = await client.get("/v1/projects")

    assert resp.status_code == 200
    assert [{"id": item["id"], "name": item["name"]} for item in resp.json()] == [
        {"id": project.id, "name": "Empty"}
    ]


async def test_legacy_session_projects_route_is_removed(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/sessions/projects")
    assert resp.status_code == 404


# ── GET /v1/sessions?project_id= (filter) ────────────────────────────


async def test_list_sessions_filtered_by_project(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """``?project_id=X`` returns only sessions in that project."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    project = SqlAlchemyProjectStore(db_uri).create(RESERVED_USER_LOCAL, "X")
    # GET /v1/sessions filters has_agent_id=True, so bind the conversations to
    # a seeded agent — otherwise the list comes back empty.
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="project-agent", bundle_location="test:///bundle")
    filed = conv_store.create_conversation(agent_id=agent_id)
    conv_store.create_conversation(agent_id=agent_id)  # unfiled
    conv_store.set_project_membership(filed.id, project.id)

    resp = await client.get(f"/v1/sessions?project_id={project.id}")
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()["data"]]
    assert ids == [filed.id]


async def test_list_sessions_empty_project_returns_unfiled(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An empty project id returns only unfiled sessions."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    project = SqlAlchemyProjectStore(db_uri).create(RESERVED_USER_LOCAL, "X")
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="project-agent", bundle_location="test:///bundle")
    filed = conv_store.create_conversation(agent_id=agent_id)
    unfiled = conv_store.create_conversation(agent_id=agent_id)
    conv_store.set_project_membership(filed.id, project.id)

    resp = await client.get("/v1/sessions?project_id=")
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()["data"]]
    assert unfiled.id in ids
    assert filed.id not in ids
