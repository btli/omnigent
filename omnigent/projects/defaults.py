"""Typed, versioned project defaults bundles."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from omnigent.errors import ProjectInputError

DEFAULTS_SCHEMA_VERSION = 1


class ProjectDefaultsBundle(BaseModel):
    """Version-one flat defaults with absent/null/value field semantics."""

    model_config = ConfigDict(extra="forbid")

    repo_url: str | None = None
    default_branch: str | None = None
    host_type: Literal["managed", "external"] | None = None
    host_id: str | None = None
    workspace: str | None = None
    harness: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None

    @model_validator(mode="after")
    def _check_explicit_managed_host(self) -> ProjectDefaultsBundle:
        if self.host_type == "managed" and self.host_id is not None:
            raise ValueError("managed project defaults prohibit host_id")
        if self.host_type == "managed" and self.repo_url is not None:
            from omnigent.server.managed_hosts import parse_repo_workspace

            workspace = (
                f"{self.repo_url}#{self.default_branch}"
                if self.default_branch is not None
                else self.repo_url
            )
            try:
                parse_repo_workspace(workspace)
            except ValueError as exc:
                raise ValueError(f"invalid managed repository defaults: {exc}") from exc
        return self


def validate_defaults_bundle(
    defaults_json: dict[str, Any] | None,
    defaults_schema_version: int = DEFAULTS_SCHEMA_VERSION,
) -> ProjectDefaultsBundle:
    """Validate a bundle against the schema selected by its version."""
    if defaults_schema_version != DEFAULTS_SCHEMA_VERSION:
        raise ProjectInputError(f"Unsupported defaults_schema_version: {defaults_schema_version}")
    try:
        return ProjectDefaultsBundle.model_validate(defaults_json or {})
    except ValidationError as error:
        message = "; ".join(item["msg"] for item in error.errors(include_context=False))
        raise ProjectInputError(f"Invalid project defaults: {message}") from error
