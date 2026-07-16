"""Field-by-field project defaults resolution and host mapping."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from omnigent.errors import ProjectInputError
from omnigent.projects.defaults import ProjectDefaultsBundle, validate_defaults_bundle
from omnigent.server.schemas import SessionGitOptions


class ResolvedProjectDefaults(BaseModel):
    """Fully resolved values mapped onto JSON session-create fields.

    Mirrored by hand in web/src/hooks/projectQueries.ts (ResolvedProjectDefaults).
    """

    model_config = ConfigDict(extra="forbid")

    host_type: Literal["managed", "external"]
    host_id: str | None
    workspace: str | None
    git: SessionGitOptions | None
    harness_override: str | None
    model_override: str | None
    reasoning_effort: str | None


def _mint_branch_name() -> str:
    return f"omnigent/{uuid.uuid4().hex}"


def default_server_defaults() -> ProjectDefaultsBundle:
    """Server-level bundle every resolution starts from; the resolved-defaults
    preview endpoint must use the same base as session create."""
    return ProjectDefaultsBundle(host_type="external")


def _values(bundle: ProjectDefaultsBundle) -> dict[str, Any]:
    return bundle.model_dump(exclude_unset=True)


def resolve_project_defaults(
    *,
    server_defaults: ProjectDefaultsBundle,
    project_defaults: dict[str, Any] | None,
    defaults_schema_version: int,
    session_overrides: ProjectDefaultsBundle,
    session_git: SessionGitOptions | None = None,
    session_git_is_set: bool = False,
    branch_name_factory: Callable[[], str] = _mint_branch_name,
) -> ResolvedProjectDefaults:
    """Resolve server, project, and explicit session values field by field."""
    project = validate_defaults_bundle(project_defaults, defaults_schema_version)
    merged = _values(server_defaults)
    merged.update(_values(project))
    merged.update(_values(session_overrides))

    host_type = merged.get("host_type")
    if host_type not in ("managed", "external"):
        raise ProjectInputError("Resolved project default host_type must be managed or external")

    host_id = merged.get("host_id")
    workspace = merged.get("workspace")
    if host_type == "managed":
        if host_id is not None:
            raise ProjectInputError("Managed project defaults prohibit host_id")
        if session_git_is_set and session_git is not None:
            raise ProjectInputError("Managed project defaults prohibit git worktree options")
        repo_url = merged.get("repo_url")
        if workspace is None and repo_url is not None:
            default_branch = merged.get("default_branch")
            workspace = f"{repo_url}#{default_branch}" if default_branch is not None else repo_url
        git = None
    else:
        if session_git_is_set:
            git = session_git
        elif (default_branch := merged.get("default_branch")) is not None:
            if host_id is None:
                raise ProjectInputError(
                    "External project default_branch requires a pinned host_id"
                )
            git = SessionGitOptions(
                branch_name=branch_name_factory(),
                base_branch=default_branch,
            )
        else:
            git = None

    return ResolvedProjectDefaults(
        host_type=host_type,
        host_id=host_id,
        workspace=workspace,
        git=git,
        harness_override=merged.get("harness"),
        model_override=merged.get("model"),
        reasoning_effort=merged.get("reasoning_effort"),
    )
