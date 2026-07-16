"""Project membership integration for session routes."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import ValidationError

from omnigent.entities import LiveProjectSnapshot
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.projects.defaults import ProjectDefaultsBundle
from omnigent.projects.resolver import resolve_project_defaults
from omnigent.server.schemas import SessionCreateMetadata, SessionCreateRequest
from omnigent.stores.conversation_store import (
    PROJECT_LABEL_KEY,
    warn_deprecated_project_label,
)
from omnigent.stores.project_store import ProjectInputError, ProjectStore


async def resolve_legacy_project_label(
    labels: dict[str, str],
    owner_principal_id: str,
    project_store: ProjectStore | None,
    *,
    write_path: str,
    explicit_project_requested: bool = False,
    explicit_project_id: str | None = None,
) -> tuple[dict[str, str], str | None, bool]:
    """Consume a legacy project label and resolve its authoritative id."""
    if PROJECT_LABEL_KEY not in labels:
        return labels, None, False
    warn_deprecated_project_label(write_path)
    clean_labels = {key: value for key, value in labels.items() if key != PROJECT_LABEL_KEY}
    project_name = labels[PROJECT_LABEL_KEY]
    if not project_name:
        if explicit_project_requested and explicit_project_id is not None:
            raise OmnigentError(
                "project_id and omni_project label refer to different projects",
                code=ErrorCode.INVALID_INPUT,
            )
        return clean_labels, None, True
    if project_store is None:
        raise OmnigentError(
            "Project storage is not configured",
            code=ErrorCode.INTERNAL_ERROR,
        )

    if explicit_project_requested:
        project = await asyncio.to_thread(
            project_store.get_by_name,
            project_name,
            owner_principal_id,
        )
        if project is None or explicit_project_id != project.id:
            raise OmnigentError(
                "project_id and omni_project label refer to different projects",
                code=ErrorCode.INVALID_INPUT,
            )
        if project.archived_at is not None:
            raise OmnigentError("Project is archived", code=ErrorCode.CONFLICT)
    else:
        project = await asyncio.to_thread(
            project_store.get_or_create_by_name,
            owner_principal_id,
            project_name,
        )
    return clean_labels, project.id, True


async def prepare_json_session_project(
    body: SessionCreateRequest,
    owner_principal_id: str,
    project_store: ProjectStore | None,
) -> tuple[SessionCreateRequest, LiveProjectSnapshot | None]:
    """Resolve JSON-create project compatibility, defaults, and snapshot."""
    explicit_project_requested = "project_id" in body.model_fields_set
    clean_labels, legacy_project_id, legacy_project_requested = await resolve_legacy_project_label(
        body.labels,
        owner_principal_id,
        project_store,
        write_path="json_create",
        explicit_project_requested=explicit_project_requested,
        explicit_project_id=body.project_id or None,
    )
    body = body.model_copy(update={"labels": clean_labels})

    if body.project_id is not None and body.parent_session_id is None:
        if project_store is None:
            raise OmnigentError(
                "Project storage is not configured",
                code=ErrorCode.INTERNAL_ERROR,
            )
        project = await asyncio.to_thread(
            project_store.get_for_use,
            body.project_id,
            owner_principal_id,
        )
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)

        override_values: dict[str, Any] = {}
        for field_name in ("host_type", "host_id", "workspace", "reasoning_effort"):
            if field_name in body.model_fields_set:
                override_values[field_name] = getattr(body, field_name)
        if "model_override" in body.model_fields_set:
            override_values["model"] = body.model_override
        if "harness_override" in body.model_fields_set:
            override_values["harness"] = body.harness_override

        resolved = resolve_project_defaults(
            server_defaults=ProjectDefaultsBundle(host_type="external"),
            project_defaults=project.defaults_json,
            defaults_schema_version=project.defaults_schema_version,
            session_overrides=ProjectDefaultsBundle.model_validate(override_values),
            session_git=body.git,
            session_git_is_set="git" in body.model_fields_set,
        )
        resolved_updates = {
            key: value
            for key, value in resolved.model_dump().items()
            if key in SessionCreateRequest.model_fields
        }
        resolved_body = body.model_copy(update=resolved_updates)
        try:
            SessionCreateRequest.model_validate(resolved_body.model_dump())
        except ValidationError as error:
            message = "; ".join(item["msg"] for item in error.errors(include_context=False))
            raise ProjectInputError(f"Invalid resolved project defaults: {message}") from error
        return resolved_body, LiveProjectSnapshot(
            project_id=project.id,
            project_row_version=project.row_version,
            defaults_schema_version=project.defaults_schema_version,
            defaults_json=resolved.model_dump(mode="json"),
        )

    if (
        legacy_project_requested
        and not explicit_project_requested
        and legacy_project_id is not None
        and body.parent_session_id is None
    ):
        return body, LiveProjectSnapshot.moved(legacy_project_id)
    return body, None


async def prepare_multipart_session_project(
    metadata: SessionCreateMetadata,
    owner_principal_id: str,
    project_store: ProjectStore | None,
) -> tuple[SessionCreateMetadata, LiveProjectSnapshot | None]:
    """Resolve multipart-create legacy membership and snapshot."""
    clean_labels, legacy_project_id, legacy_project_requested = await resolve_legacy_project_label(
        metadata.labels,
        owner_principal_id,
        project_store,
        write_path="multipart_create",
    )
    metadata = metadata.model_copy(update={"labels": clean_labels})
    snapshot = (
        LiveProjectSnapshot.moved(legacy_project_id)
        if legacy_project_requested
        and legacy_project_id is not None
        and metadata.parent_session_id is None
        else None
    )
    return metadata, snapshot


async def resolve_patch_project_membership(
    *,
    labels: dict[str, str],
    explicit_project_requested: bool,
    explicit_project_id: str | None,
    legacy_project_requested: bool,
    owner_principal_id: str,
    project_store: ProjectStore | None,
) -> str | None:
    """Validate and resolve a PATCH membership request."""
    if project_store is None:
        raise OmnigentError(
            "Project storage is not configured",
            code=ErrorCode.INTERNAL_ERROR,
        )
    if explicit_project_id is not None:
        explicit_project = await asyncio.to_thread(
            project_store.get_for_use,
            explicit_project_id,
            owner_principal_id,
        )
        if explicit_project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)

    if not legacy_project_requested:
        return explicit_project_id
    _, legacy_project_id, _ = await resolve_legacy_project_label(
        labels,
        owner_principal_id,
        project_store,
        write_path="patch",
        explicit_project_requested=explicit_project_requested,
        explicit_project_id=explicit_project_id,
    )
    return legacy_project_id
