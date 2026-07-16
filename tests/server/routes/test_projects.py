"""Acceptance tests for ``/v1/projects`` and request workspace scoping."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event

from omnigent.db.utils import get_or_create_engine
from omnigent.server.auth import RESERVED_USER_PUBLIC
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.routes.conftest import sessions_test_app


@pytest.fixture()
def project_app(db_uri: str) -> FastAPI:
    return sessions_test_app(db_uri)


@pytest_asyncio.fixture()
async def project_client(project_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=project_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _headers(user: str, workspace: int = 0, **extra: str) -> dict[str, str]:
    return {
        "X-Forwarded-Email": user,
        "X-Databricks-Org-Id": str(workspace),
        **extra,
    }


async def test_create_list_get_are_owner_only_and_owner_is_server_derived(
    project_client: httpx.AsyncClient,
) -> None:
    spoof = await project_client.post(
        "/v1/projects",
        headers=_headers("alice"),
        json={"name": "Spoof", "owner_principal_id": "bob"},
    )
    assert spoof.status_code == 422

    created = await project_client.post(
        "/v1/projects",
        headers=_headers("alice"),
        json={"name": "Frontend", "description": "UI"},
    )
    assert created.status_code == 201
    assert created.json()["owner_principal_id"] == "alice"
    assert created.headers["etag"] == '"1"'
    project_id = created.json()["id"]

    owner_get = await project_client.get(f"/v1/projects/{project_id}", headers=_headers("alice"))
    assert owner_get.status_code == 200
    assert owner_get.headers["etag"] == '"1"'
    assert [
        item["id"]
        for item in (await project_client.get("/v1/projects", headers=_headers("alice"))).json()
    ] == [project_id]

    assert (
        await project_client.get(f"/v1/projects/{project_id}", headers=_headers("bob"))
    ).status_code == 404
    assert (await project_client.get("/v1/projects", headers=_headers("bob"))).json() == []


async def test_name_collision_and_every_mutation_uses_if_match(
    project_client: httpx.AsyncClient,
) -> None:
    first = await project_client.post(
        "/v1/projects", headers=_headers("alice"), json={"name": "One"}
    )
    project_id = first.json()["id"]
    collision = await project_client.post(
        "/v1/projects", headers=_headers("alice"), json={"name": " ONE "}
    )
    assert collision.status_code == 409

    missing = await project_client.post(
        f"/v1/projects/{project_id}/rename",
        headers=_headers("alice"),
        json={"name": "Two"},
    )
    assert missing.status_code == 412

    renamed = await project_client.post(
        f"/v1/projects/{project_id}/rename",
        headers=_headers("alice", **{"If-Match": '"1"'}),
        json={"name": "Two"},
    )
    assert renamed.status_code == 200
    assert renamed.headers["etag"] == '"2"'

    stale = await project_client.patch(
        f"/v1/projects/{project_id}",
        headers=_headers("alice", **{"If-Match": '"1"'}),
        json={"description": "stale"},
    )
    assert stale.status_code == 412

    updated = await project_client.patch(
        f"/v1/projects/{project_id}",
        headers=_headers("alice", **{"If-Match": '"2"'}),
        json={"description": "current", "defaults_json": {"model": "gpt-5"}},
    )
    assert updated.status_code == 200
    assert updated.json()["description"] == "current"
    assert updated.headers["etag"] == '"3"'

    archived = await project_client.post(
        f"/v1/projects/{project_id}/archive",
        headers=_headers("alice", **{"If-Match": '"3"'}),
    )
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    assert archived.headers["etag"] == '"4"'

    restore_missing = await project_client.post(
        f"/v1/projects/{project_id}/restore", headers=_headers("alice")
    )
    assert restore_missing.status_code == 412
    restored = await project_client.post(
        f"/v1/projects/{project_id}/restore",
        headers=_headers("alice", **{"If-Match": '"4"'}),
    )
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None
    assert restored.headers["etag"] == '"5"'


async def test_wrong_owner_mutation_is_404_even_with_matching_etag(
    project_client: httpx.AsyncClient,
) -> None:
    created = await project_client.post(
        "/v1/projects", headers=_headers("alice"), json={"name": "Private"}
    )

    response = await project_client.post(
        f"/v1/projects/{created.json()['id']}/archive",
        headers=_headers("bob", **{"If-Match": '"1"'}),
    )

    assert response.status_code == 404


async def test_transfer_rekeys_owner_and_rejects_collision_stale_and_wrong_caller(
    project_client: httpx.AsyncClient,
) -> None:
    source = await project_client.post(
        "/v1/projects", headers=_headers("carol"), json={"name": "Migrated"}
    )
    project_id = source.json()["id"]

    wrong = await project_client.post(
        f"/v1/projects/{project_id}/transfer",
        headers=_headers("mallory", **{"If-Match": '"1"'}),
        json={"new_owner_principal_id": "alice"},
    )
    assert wrong.status_code == 404

    stale = await project_client.post(
        f"/v1/projects/{project_id}/transfer",
        headers=_headers("carol", **{"If-Match": '"2"'}),
        json={"new_owner_principal_id": "alice"},
    )
    assert stale.status_code == 412

    transferred = await project_client.post(
        f"/v1/projects/{project_id}/transfer",
        headers=_headers("carol", **{"If-Match": '"1"'}),
        json={"new_owner_principal_id": "alice"},
    )
    assert transferred.status_code == 200
    assert transferred.json()["owner_principal_id"] == "alice"
    assert transferred.headers["etag"] == '"2"'
    assert (
        await project_client.get(f"/v1/projects/{project_id}", headers=_headers("carol"))
    ).status_code == 404
    assert (
        await project_client.get(f"/v1/projects/{project_id}", headers=_headers("alice"))
    ).status_code == 200

    collision_source = await project_client.post(
        "/v1/projects", headers=_headers("carol"), json={"name": "Taken"}
    )
    await project_client.post("/v1/projects", headers=_headers("alice"), json={"name": "taken"})
    collision = await project_client.post(
        f"/v1/projects/{collision_source.json()['id']}/transfer",
        headers=_headers("carol", **{"If-Match": '"1"'}),
        json={"new_owner_principal_id": "alice"},
    )
    assert collision.status_code == 409


async def test_archive_include_sessions_archives_members_and_project(
    db_uri: str,
    project_client: httpx.AsyncClient,
) -> None:
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    project = projects.create("alice", "Delete me")
    member_ids = []
    for _ in range(2):
        session = conversations.create_conversation()
        conversations.set_project_membership(session.id, project.id)
        permissions.ensure_user("alice")
        permissions.grant("alice", session.id, 4)
        member_ids.append(session.id)

    response = await project_client.post(
        f"/v1/projects/{project.id}/archive?include_sessions=true",
        headers=_headers("alice", **{"If-Match": '"1"'}),
    )

    assert response.status_code == 200
    assert response.json()["archived_sessions"] == 2
    assert response.json()["archived_at"] is not None
    assert projects.get(project.id, "alice").archived_at is not None  # type: ignore[union-attr]
    assert all(conversations.get_conversation(item).archived for item in member_ids)  # type: ignore[union-attr]


async def test_archive_include_sessions_is_owner_scoped(
    db_uri: str,
    project_client: httpx.AsyncClient,
) -> None:
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    project = projects.create("alice", "Owner scoped")
    alice_session = conversations.create_conversation()
    bob_session = conversations.create_conversation()
    for session in (alice_session, bob_session):
        conversations.set_project_membership(session.id, project.id)
    for owner, session in (("alice", alice_session), ("bob", bob_session)):
        permissions.ensure_user(owner)
        permissions.grant(owner, session.id, 4)

    response = await project_client.post(
        f"/v1/projects/{project.id}/archive?include_sessions=true",
        headers=_headers("alice", **{"If-Match": '"1"'}),
    )

    assert response.status_code == 200
    assert response.json()["archived_sessions"] == 1
    assert conversations.get_conversation(alice_session.id).archived is True  # type: ignore[union-attr]
    assert conversations.get_conversation(bob_session.id).archived is False  # type: ignore[union-attr]


async def test_stale_archive_etag_fails_before_member_timestamps_advance(
    db_uri: str,
    project_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    project = projects.create("alice", "Stale archive")
    member = conversations.create_conversation()
    conversations.set_project_membership(member.id, project.id)
    permissions.ensure_user("alice")
    permissions.grant("alice", member.id, 4)
    original_updated_at = member.updated_at
    monkeypatch.setattr(
        "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch",
        lambda: original_updated_at + 100,
    )

    response = await project_client.post(
        f"/v1/projects/{project.id}/archive?include_sessions=true",
        headers=_headers("alice", **{"If-Match": '"2"'}),
    )

    assert response.status_code == 412
    assert response.json() == {
        "error": {
            "code": "precondition_failed",
            "message": "Project ETag is stale",
        }
    }
    persisted = conversations.get_conversation(member.id)
    assert persisted is not None
    assert persisted.updated_at == original_updated_at
    assert persisted.archived is False


async def test_public_owner_grant_is_not_listed_or_archived_as_membership(
    db_uri: str,
    project_client: httpx.AsyncClient,
) -> None:
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    project = projects.create("alice", "Public grant")
    public_session = conversations.create_conversation()
    conversations.set_project_membership(public_session.id, project.id)
    permissions.ensure_user(RESERVED_USER_PUBLIC)
    permissions.grant(RESERVED_USER_PUBLIC, public_session.id, 4)

    listed = conversations.list_conversations(
        project_id=project.id,
        owned_by=RESERVED_USER_PUBLIC,
    )
    response = await project_client.post(
        f"/v1/projects/{project.id}/archive?include_sessions=true",
        headers=_headers("alice", **{"If-Match": '"1"'}),
    )

    assert listed.data == []
    assert response.status_code == 200
    assert response.json()["archived_sessions"] == 0
    assert conversations.get_conversation(public_session.id).archived is False  # type: ignore[union-attr]


def test_archive_include_sessions_rolls_back_metadata_with_project_failure(db_uri: str) -> None:
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    project = projects.create("alice", "Atomic")
    member = conversations.create_conversation()
    conversations.set_project_membership(member.id, project.id)
    permissions.ensure_user("alice")
    permissions.grant("alice", member.id, 4)

    def fail_project_archive(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.startswith("UPDATE projects SET") and "archived_at" in statement:
            raise RuntimeError("simulated project archive failure")

    engine = get_or_create_engine(db_uri)
    event.listen(engine, "before_cursor_execute", fail_project_archive)
    try:
        with pytest.raises(RuntimeError, match="simulated project archive failure"):
            conversations.archive_project_with_sessions(
                project.id,
                "alice",
                expected_row_version=project.row_version,
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail_project_archive)

    assert projects.get(project.id, "alice").archived_at is None  # type: ignore[union-attr]
    assert conversations.get_conversation(member.id).archived is False  # type: ignore[union-attr]


async def test_archive_without_include_sessions_leaves_members_live(
    db_uri: str,
    project_client: httpx.AsyncClient,
) -> None:
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    project = projects.create("alice", "Archive row only")
    session = conversations.create_conversation()
    conversations.set_project_membership(session.id, project.id)
    permissions.ensure_user("alice")
    permissions.grant("alice", session.id, 4)

    response = await project_client.post(
        f"/v1/projects/{project.id}/archive",
        headers=_headers("alice", **{"If-Match": '"1"'}),
    )

    assert response.status_code == 200
    assert "archived_sessions" not in response.json()
    assert conversations.get_conversation(session.id).archived is False  # type: ignore[union-attr]


async def test_workspace_middleware_keeps_tenants_distinct(
    project_client: httpx.AsyncClient,
) -> None:
    first = await project_client.post(
        "/v1/projects", headers=_headers("alice", 101), json={"name": "Omnigent"}
    )
    second = await project_client.post(
        "/v1/projects", headers=_headers("alice", 202), json={"name": "Omnigent"}
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]

    hidden = await project_client.get(
        f"/v1/projects/{first.json()['id']}", headers=_headers("alice", 202)
    )
    assert hidden.status_code == 404
    assert [
        project["id"]
        for project in (
            await project_client.get("/v1/projects", headers=_headers("alice", 101))
        ).json()
    ] == [first.json()["id"]]


async def test_invalid_defaults_returns_422(project_client: httpx.AsyncClient) -> None:
    response = await project_client.post(
        "/v1/projects",
        headers=_headers("alice"),
        json={"name": "Bad", "defaults_json": {"unknown": "value"}},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "defaults_json",
    [
        {"host_type": "managed", "repo_url": "github.com/acme/widgets"},
        {
            "host_type": "managed",
            "repo_url": "https://github.com/acme/widgets.git",
            "default_branch": "a" * 40,
        },
    ],
)
async def test_invalid_managed_repo_defaults_return_422_at_save(
    project_client: httpx.AsyncClient,
    defaults_json: dict[str, str],
) -> None:
    response = await project_client.post(
        "/v1/projects",
        headers=_headers("alice"),
        json={"name": "Bad managed", "defaults_json": defaults_json},
    )

    assert response.status_code == 422
