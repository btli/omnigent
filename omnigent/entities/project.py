"""Project entities for per-project contextual awareness."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
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
class LiveProjectSnapshot:
    """Resolved project provenance written with session metadata.

    Legacy-label forwarding uses ``moved`` with no project row version;
    explicit project creates retain the default ``live`` origin.
    """

    project_id: str
    project_row_version: int | None
    defaults_schema_version: int
    defaults_json: dict[str, Any]
    snapshot_origin: Literal["live", "moved", "backfill"] = "live"

    @classmethod
    def moved(cls, project_id: str) -> LiveProjectSnapshot:
        """Build the canonical snapshot for a moved session."""
        from omnigent.projects.defaults import DEFAULTS_SCHEMA_VERSION

        return cls(
            project_id=project_id,
            project_row_version=None,
            defaults_schema_version=DEFAULTS_SCHEMA_VERSION,
            defaults_json={},
            snapshot_origin="moved",
        )

    @classmethod
    def backfill(cls, project_id: str) -> LiveProjectSnapshot:
        """Build the canonical snapshot for a legacy-label backfill."""
        return dataclasses.replace(cls.moved(project_id), snapshot_origin="backfill")


def project_snapshot_values(
    project_snapshot: LiveProjectSnapshot,
    created_at: int,
) -> dict[str, Any]:
    """Build persistence values for a project snapshot."""
    return {
        "project_id": project_snapshot.project_id,
        "snapshot_origin": project_snapshot.snapshot_origin,
        "project_row_version": project_snapshot.project_row_version,
        "defaults_schema_version": project_snapshot.defaults_schema_version,
        "defaults_json": json.dumps(
            project_snapshot.defaults_json,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "created_at": created_at,
    }


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
    issues: tuple[ProjectMigrationIssue, ...] = ()

    @property
    def requires_mapping(self) -> bool:
        """Whether operator input is required before the backfill can run."""
        return bool(self.issues)
