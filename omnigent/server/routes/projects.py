"""Owner-only REST routes for flat projects."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, ConfigDict

from omnigent.entities import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider, resolve_owner_principal
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.project_store import UNSET, ProjectStore

_logger = logging.getLogger(__name__)


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


class TransferProjectRequest(BaseModel):
    """Destination principal for an ownership transfer."""

    model_config = ConfigDict(extra="forbid")

    new_owner_principal_id: str


def _owner(request: Request, auth_provider: AuthProvider | None) -> str:
    return resolve_owner_principal(require_user(request, auth_provider))


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
    *,
    conversation_store: ConversationStore | None = None,
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
        archived_members: bool = False,
    ) -> list[dict[str, Any]]:
        projects = await asyncio.to_thread(
            store.list,
            _owner(request, auth_provider),
            include_archived=include_archived,
            archived_members=archived_members,
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

    @router.post("/projects/{project_id}/transfer")
    async def transfer_project(
        request: Request,
        response: Response,
        project_id: str,
        body: TransferProjectRequest,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> dict[str, Any]:
        current_owner = _owner(request, auth_provider)
        project = await asyncio.to_thread(
            store.transfer,
            project_id,
            current_owner,
            body.new_owner_principal_id,
            expected_row_version=_expected_version(if_match),
        )
        transferred = _require_project(project)
        _logger.info(
            "project ownership transferred",
            extra={
                "project_id": project_id,
                "current_owner_principal_id": current_owner,
                "new_owner_principal_id": body.new_owner_principal_id,
                "row_version": transferred.row_version,
            },
        )
        return _serialized(transferred, response)

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
        include_sessions: bool = False,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> dict[str, Any]:
        if include_sessions:
            if conversation_store is None:
                raise RuntimeError("conversation store is required to archive project sessions")
            project, archived_sessions = await asyncio.to_thread(
                conversation_store.archive_project_with_sessions,
                project_id,
                _owner(request, auth_provider),
                expected_row_version=_expected_version(if_match),
            )
            serialized = _serialized(_require_project(project), response)
            serialized["archived_sessions"] = archived_sessions
            return serialized
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
