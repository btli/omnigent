"""Shared owner-private Project assignment resolution."""

from __future__ import annotations

from omnigent.stores.project_store import ProjectStore


def resolve_owned_project_id(
    project_store: ProjectStore,
    project_id: str,
    *,
    user_id: str | None,
) -> str | None:
    """Return the canonical id when the caller owns the Project."""
    project = project_store.get(project_id, user_id=user_id)
    return project.id if project is not None else None
