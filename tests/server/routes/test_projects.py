"""Acceptance tests for ``/v1/projects`` and request workspace scoping."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.app import add_workspace_scope_middleware
from omnigent.server.auth import AuthProvider
from omnigent.server.routes.projects import create_projects_router
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore


class HeaderAuthProvider(AuthProvider):
    """Resolve the test caller from ``X-Test-User``."""

    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("X-Test-User")


@pytest.fixture()
def project_app(db_uri: str) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_error(_request: Request, error: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=error.http_status,
            content={"error": {"code": error.code, "message": error.message}},
        )

    add_workspace_scope_middleware(app)
    app.include_router(
        create_projects_router(SqlAlchemyProjectStore(db_uri), HeaderAuthProvider()),
        prefix="/v1",
    )
    return app


@pytest_asyncio.fixture()
async def project_client(project_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=project_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _headers(user: str, workspace: int = 0, **extra: str) -> dict[str, str]:
    return {
        "X-Test-User": user,
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
