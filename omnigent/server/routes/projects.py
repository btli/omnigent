"""Owner-only REST routes for flat projects."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, ConfigDict

from omnigent.entities import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.project_store import UNSET, ProjectStore


class CreateProjectRequest(BaseModel):
    """Fields accepted when creating a project."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    defaults_json: dict[str, Any] | None = None
    defaults_schema_version: int = 1


class RenameProjectRequest(BaseModel):
    """Project rename payload."""

    model_config = ConfigDict(extra="forbid")

    name: str


class UpdateProjectRequest(BaseModel):
    """Mutable project settings payload."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    defaults_json: dict[str, Any] | None = None


def _owner(request: Request, auth_provider: AuthProvider | None) -> str:
    return require_user(request, auth_provider) or RESERVED_USER_LOCAL


def _expected_version(if_match: str | None) -> int | None:
    if if_match is None:
        return None
    value = if_match.strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    try:
        version = int(value)
    except ValueError as error:
        raise OmnigentError(
            "If-Match must contain a project row version",
            code=ErrorCode.PRECONDITION_FAILED,
        ) from error
    if version < 1:
        raise OmnigentError(
            "If-Match must contain a positive project row version",
            code=ErrorCode.PRECONDITION_FAILED,
        )
    return version


def _serialized(project: Project, response: Response) -> dict[str, Any]:
    response.headers["ETag"] = f'"{project.row_version}"'
    return asdict(project)


def _require_project(project: Project | None) -> Project:
    if project is None:
        raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
    return project


def create_projects_router(
    store: ProjectStore,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the owner-only ``/projects`` router."""
    router = APIRouter()

    @router.post("/projects", status_code=201)
    async def create_project(
        request: Request,
        response: Response,
        body: CreateProjectRequest,
    ) -> dict[str, Any]:
        project = await asyncio.to_thread(
            store.create,
            _owner(request, auth_provider),
            body.name,
            description=body.description,
            defaults_json=body.defaults_json,
            defaults_schema_version=body.defaults_schema_version,
        )
        return _serialized(project, response)

    @router.get("/projects")
    async def list_projects(
        request: Request,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        projects = await asyncio.to_thread(
            store.list,
            _owner(request, auth_provider),
            include_archived=include_archived,
        )
        return [asdict(project) for project in projects]

    @router.get("/projects/{project_id}")
    async def get_project(
        request: Request,
        response: Response,
        project_id: str,
    ) -> dict[str, Any]:
        project = await asyncio.to_thread(
            store.get,
            project_id,
            _owner(request, auth_provider),
        )
        return _serialized(_require_project(project), response)

    @router.post("/projects/{project_id}/rename")
    async def rename_project(
        request: Request,
        response: Response,
        project_id: str,
        body: RenameProjectRequest,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> dict[str, Any]:
        project = await asyncio.to_thread(
            store.rename,
            project_id,
            _owner(request, auth_provider),
            body.name,
            expected_row_version=_expected_version(if_match),
        )
        return _serialized(_require_project(project), response)

    @router.patch("/projects/{project_id}")
    async def update_project(
        request: Request,
        response: Response,
        project_id: str,
        body: UpdateProjectRequest,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> dict[str, Any]:
        description = body.description if "description" in body.model_fields_set else UNSET
        defaults_json = body.defaults_json if "defaults_json" in body.model_fields_set else UNSET
        project = await asyncio.to_thread(
            store.update,
            project_id,
            _owner(request, auth_provider),
            expected_row_version=_expected_version(if_match),
            description=description,
            defaults_json=defaults_json,
        )
        return _serialized(_require_project(project), response)

    async def _change_archive_state(
        request: Request,
        response: Response,
        project_id: str,
        if_match: str | None,
        *,
        restore: bool,
    ) -> dict[str, Any]:
        operation = store.restore if restore else store.archive
        project = await asyncio.to_thread(
            operation,
            project_id,
            _owner(request, auth_provider),
            expected_row_version=_expected_version(if_match),
        )
        return _serialized(_require_project(project), response)

    @router.post("/projects/{project_id}/archive")
    async def archive_project(
        request: Request,
        response: Response,
        project_id: str,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> dict[str, Any]:
        return await _change_archive_state(
            request,
            response,
            project_id,
            if_match,
            restore=False,
        )

    @router.post("/projects/{project_id}/restore")
    async def restore_project(
        request: Request,
        response: Response,
        project_id: str,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> dict[str, Any]:
        return await _change_archive_state(
            request,
            response,
            project_id,
            if_match,
            restore=True,
        )

    return router
