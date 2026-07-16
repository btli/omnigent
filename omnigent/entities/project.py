"""Project entities for per-project contextual awareness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class Project:
    """A flat, owner-scoped project and its versioned defaults bundle."""

    id: str
    owner_principal_id: str
    name: str
    description: str | None
    normalized_name: str
    normalized_name_checksum: str
    storage_key: str
    defaults_json: dict[str, Any]
    defaults_schema_version: int
    row_version: int
    created_at: int
    updated_at: int
    archived_at: int | None


@dataclass(frozen=True)
class ProjectIdentity:
    """Project identity returned by session membership surfaces."""

    id: str
    name: str


@dataclass(frozen=True)
class LiveProjectSnapshot:
    """Resolved project provenance written with session metadata.

    Legacy-label forwarding uses ``moved`` with no project row version;
    explicit project creates retain the default ``live`` origin.
    """

    project_id: str
    project_row_version: int | None
    defaults_schema_version: int
    defaults_json: dict[str, Any]
    snapshot_origin: Literal["live", "moved"] = "live"


@dataclass(frozen=True)
class LegacyProjectLabel:
    """A legacy project-label assignment with its inferred session owner."""

    session_id: str
    label: str
    owner_principal_id: str | None


@dataclass(frozen=True)
class ProjectMigrationMapping:
    """One resolved legacy-name to flat-project mapping."""

    owner_principal_id: str
    normalized_name: str
    project_id: str
    session_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProjectMigrationIssue:
    """An ambiguity requiring an operator choice before cutover."""

    normalized_name: str
    labels: tuple[str, ...]
    session_ids: tuple[str, ...]
    candidate_owners: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ProjectBackfillResult:
    """Applied mappings or a non-mutating operator mapping plan."""

    mappings: tuple[ProjectMigrationMapping, ...] = ()
    issues: tuple[ProjectMigrationIssue, ...] = field(default_factory=tuple)

    @property
    def requires_mapping(self) -> bool:
        """Whether operator input is required before the backfill can run."""
        return bool(self.issues)
