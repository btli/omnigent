"""SQLAlchemy-backed project store."""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import (
    SqlConversationMetadata,
    SqlProject,
    SqlProjectMigrationLedger,
    SqlSessionProjectSnapshot,
    SqlUser,
    current_workspace_id,
)
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch
from omnigent.entities import (
    LegacyProjectLabel,
    Project,
    ProjectBackfillResult,
    ProjectMigrationIssue,
    ProjectMigrationMapping,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.projects.defaults import DEFAULTS_SCHEMA_VERSION, validate_defaults_bundle
from omnigent.stores.conversation_store import PROJECT_LABEL_KEY, ConversationStore
from omnigent.stores.project_store import UNSET, ProjectInputError, ProjectStore, _Unset


def normalize_project_name(name: str) -> tuple[str, str, bytes]:
    """Return display, normalized, and SHA-256 forms of a project name."""
    display_name = unicodedata.normalize("NFKC", name).strip()
    normalized_name = display_name.casefold()
    if not display_name:
        raise ProjectInputError("Project name must not be empty")
    if len(display_name) > 256 or len(normalized_name) > 256:
        raise ProjectInputError("Project name must be at most 256 characters")
    checksum = hashlib.sha256(normalized_name.encode("utf-8")).digest()
    return display_name, normalized_name, checksum


def _to_entity(row: SqlProject) -> Project:
    return Project(
        id=row.id,
        owner_principal_id=row.owner_principal_id,
        name=row.name,
        description=row.description,
        normalized_name=row.normalized_name,
        normalized_name_checksum=row.normalized_name_checksum.hex(),
        storage_key=row.storage_key,
        defaults_json=json.loads(row.defaults_json),
        defaults_schema_version=row.defaults_schema_version,
        row_version=row.row_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        archived_at=row.archived_at,
    )


def _project_id() -> str:
    return f"proj_{uuid.uuid4().hex}"


def _storage_key(project_id: str) -> str:
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
    return f"proj-{digest[:32]}"


def _source_fingerprint(assignments: list[LegacyProjectLabel]) -> bytes:
    source = "\n".join(
        f"{assignment.session_id}\0{assignment.label}"
        for assignment in sorted(assignments, key=lambda item: (item.session_id, item.label))
    )
    return hashlib.sha256(source.encode("utf-8")).digest()


class SqlAlchemyProjectStore(ProjectStore):
    """Relational project persistence with workspace and owner isolation."""

    def __init__(self, storage_location: str) -> None:
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine, immediate=True)

    def create(
        self,
        owner_principal_id: str,
        name: str,
        *,
        description: str | None = None,
        defaults_json: dict[str, Any] | None = None,
        defaults_schema_version: int = DEFAULTS_SCHEMA_VERSION,
    ) -> Project:
        display_name, normalized_name, checksum = normalize_project_name(name)
        bundle = validate_defaults_bundle(defaults_json, defaults_schema_version)
        resolved_id = _project_id()
        now = now_epoch()
        row = SqlProject(
            id=resolved_id,
            owner_principal_id=owner_principal_id,
            name=display_name,
            description=description,
            normalized_name=normalized_name,
            normalized_name_checksum=checksum,
            storage_key=_storage_key(resolved_id),
            defaults_json=json.dumps(
                bundle.model_dump(exclude_unset=True), sort_keys=True, separators=(",", ":")
            ),
            defaults_schema_version=defaults_schema_version,
            row_version=1,
            created_at=now,
            updated_at=now,
            archived_at=None,
        )
        try:
            with self._session() as session:
                if session.get(SqlUser, (current_workspace_id(), owner_principal_id)) is None:
                    try:
                        with session.begin_nested():
                            session.add(SqlUser(id=owner_principal_id, is_admin=False))
                            session.flush()
                    except IntegrityError:
                        pass
                session.add(row)
                session.flush()
                return _to_entity(row)
        except IntegrityError as error:
            raise OmnigentError(
                "A project with that name already exists",
                code=ErrorCode.CONFLICT,
            ) from error

    def get(self, project_id: str, owner_principal_id: str) -> Project | None:
        with self._session() as session:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if row is None or row.owner_principal_id != owner_principal_id:
                return None
            return _to_entity(row)

    def get_for_use(self, project_id: str, owner_principal_id: str) -> Project | None:
        project = self.get(project_id, owner_principal_id)
        if project is not None and project.archived_at is not None:
            raise OmnigentError("Project is archived", code=ErrorCode.CONFLICT)
        return project

    def get_by_name(self, name: str, owner_principal_id: str) -> Project | None:
        _, _, checksum = normalize_project_name(name)
        statement = select(SqlProject).where(
            SqlProject.workspace_id == current_workspace_id(),
            SqlProject.owner_principal_id == owner_principal_id,
            SqlProject.normalized_name_checksum == checksum,
        )
        with self._session() as session:
            row = session.execute(statement).scalar_one_or_none()
            return _to_entity(row) if row is not None else None

    def list(self, owner_principal_id: str, *, include_archived: bool = False) -> list[Project]:
        statement = select(SqlProject).where(
            SqlProject.workspace_id == current_workspace_id(),
            SqlProject.owner_principal_id == owner_principal_id,
        )
        if not include_archived:
            statement = statement.where(SqlProject.archived_at.is_(None))
        statement = statement.order_by(SqlProject.normalized_name, SqlProject.id)
        with self._session() as session:
            return [_to_entity(row) for row in session.execute(statement).scalars().all()]

    def _mutate(
        self,
        project_id: str,
        owner_principal_id: str,
        expected_row_version: int | None,
        values: dict[str, Any],
        *,
        require_archived: bool | None = None,
    ) -> Project | None:
        if expected_row_version is None:
            raise OmnigentError(
                "If-Match is required for project mutations",
                code=ErrorCode.PRECONDITION_FAILED,
            )
        try:
            with self._session() as session:
                row = session.get(SqlProject, (current_workspace_id(), project_id))
                if row is None or row.owner_principal_id != owner_principal_id:
                    return None
                if row.row_version != expected_row_version:
                    raise OmnigentError(
                        "Project ETag is stale",
                        code=ErrorCode.PRECONDITION_FAILED,
                    )
                if require_archived is True and row.archived_at is None:
                    raise OmnigentError("Project is not archived", code=ErrorCode.CONFLICT)
                if require_archived is False and row.archived_at is not None:
                    raise OmnigentError("Project is already archived", code=ErrorCode.CONFLICT)
                statement = (
                    update(SqlProject)
                    .where(
                        SqlProject.workspace_id == current_workspace_id(),
                        SqlProject.id == project_id,
                        SqlProject.owner_principal_id == owner_principal_id,
                        SqlProject.row_version == expected_row_version,
                    )
                    .values(
                        **values,
                        updated_at=now_epoch(),
                        row_version=SqlProject.row_version + 1,
                    )
                )
                result = session.execute(statement)
                if result.rowcount != 1:
                    raise OmnigentError(
                        "Project ETag is stale",
                        code=ErrorCode.PRECONDITION_FAILED,
                    )
                session.flush()
                session.expire_all()
                updated_row = session.get(SqlProject, (current_workspace_id(), project_id))
                if updated_row is None:
                    return None
                return _to_entity(updated_row)
        except IntegrityError as error:
            raise OmnigentError(
                "A project with that name already exists",
                code=ErrorCode.CONFLICT,
            ) from error

    def rename(
        self,
        project_id: str,
        owner_principal_id: str,
        name: str,
        *,
        expected_row_version: int | None,
    ) -> Project | None:
        display_name, normalized_name, checksum = normalize_project_name(name)
        return self._mutate(
            project_id,
            owner_principal_id,
            expected_row_version,
            {
                "name": display_name,
                "normalized_name": normalized_name,
                "normalized_name_checksum": checksum,
            },
        )

    def update(
        self,
        project_id: str,
        owner_principal_id: str,
        *,
        expected_row_version: int | None,
        description: str | None | _Unset = UNSET,
        defaults_json: dict[str, Any] | None | _Unset = UNSET,
    ) -> Project | None:
        values: dict[str, Any] = {}
        if description is not UNSET:
            values["description"] = description
        if defaults_json is not UNSET:
            if not isinstance(defaults_json, (dict, type(None))):
                raise ProjectInputError("defaults_json must be an object or null")
            bundle = validate_defaults_bundle(defaults_json)
            values["defaults_json"] = json.dumps(
                bundle.model_dump(exclude_unset=True), sort_keys=True, separators=(",", ":")
            )
        if not values:
            raise ProjectInputError("At least one mutable field is required")
        return self._mutate(
            project_id,
            owner_principal_id,
            expected_row_version,
            values,
        )

    def archive(
        self,
        project_id: str,
        owner_principal_id: str,
        *,
        expected_row_version: int | None,
    ) -> Project | None:
        return self._mutate(
            project_id,
            owner_principal_id,
            expected_row_version,
            {"archived_at": now_epoch()},
            require_archived=False,
        )

    def restore(
        self,
        project_id: str,
        owner_principal_id: str,
        *,
        expected_row_version: int | None,
    ) -> Project | None:
        return self._mutate(
            project_id,
            owner_principal_id,
            expected_row_version,
            {"archived_at": None},
            require_archived=True,
        )

    @staticmethod
    def _issue(
        normalized_name: str,
        assignments: list[LegacyProjectLabel],
        reason: str,
    ) -> ProjectMigrationIssue:
        return ProjectMigrationIssue(
            normalized_name=normalized_name,
            labels=tuple(sorted({assignment.label for assignment in assignments})),
            session_ids=tuple(sorted(assignment.session_id for assignment in assignments)),
            candidate_owners=tuple(
                sorted(
                    {
                        assignment.owner_principal_id
                        for assignment in assignments
                        if assignment.owner_principal_id is not None
                    }
                )
            ),
            reason=reason,
        )

    def backfill_legacy_labels(
        self, conversation_store: ConversationStore
    ) -> ProjectBackfillResult:
        assignments = conversation_store.list_project_label_assignments()
        grouped: dict[str, list[LegacyProjectLabel]] = defaultdict(list)
        checksums: dict[str, bytes] = {}
        for assignment in assignments:
            _, normalized_name, checksum = normalize_project_name(assignment.label)
            grouped[normalized_name].append(assignment)
            checksums[normalized_name] = checksum
        if not grouped:
            return ProjectBackfillResult()

        issues: list[ProjectMigrationIssue] = []
        proposed: list[
            tuple[str, str, bytes, list[LegacyProjectLabel], str, SqlProject | None, bool]
        ] = []
        with self._session() as session:
            for normalized_name in sorted(grouped):
                group = grouped[normalized_name]
                display_labels = {
                    unicodedata.normalize("NFKC", assignment.label).strip() for assignment in group
                }
                if len(display_labels) > 1:
                    issues.append(self._issue(normalized_name, group, "normalized_name_alias"))
                owners = {assignment.owner_principal_id for assignment in group}
                if None in owners or len(owners) != 1:
                    issues.append(self._issue(normalized_name, group, "ambiguous_owner"))
                    continue
                owner = next(iter(owners))
                if owner is None:
                    continue
                checksum = checksums[normalized_name]
                existing_projects = (
                    session.execute(
                        select(SqlProject).where(
                            SqlProject.workspace_id == current_workspace_id(),
                            SqlProject.normalized_name_checksum == checksum,
                        )
                    )
                    .scalars()
                    .all()
                )
                foreign_projects = [
                    project for project in existing_projects if project.owner_principal_id != owner
                ]
                if foreign_projects:
                    issues.append(self._issue(normalized_name, group, "owned_name_collision"))
                mine = next(
                    (
                        project
                        for project in existing_projects
                        if project.owner_principal_id == owner
                    ),
                    None,
                )
                if mine is not None and mine.archived_at is not None:
                    issues.append(self._issue(normalized_name, group, "archived_project"))
                ledger = session.execute(
                    select(SqlProjectMigrationLedger).where(
                        SqlProjectMigrationLedger.workspace_id == current_workspace_id(),
                        SqlProjectMigrationLedger.owner_principal_id == owner,
                        SqlProjectMigrationLedger.normalized_name_checksum == checksum,
                    )
                ).scalar_one_or_none()
                if ledger is not None:
                    resolved_id = ledger.project_id
                elif mine is not None:
                    resolved_id = mine.id
                else:
                    resolved_id = _project_id()
                if ledger is not None and (mine is None or mine.id != ledger.project_id):
                    issues.append(self._issue(normalized_name, group, "ledger_project_mismatch"))
                proposed.append(
                    (
                        owner,
                        normalized_name,
                        checksum,
                        group,
                        resolved_id,
                        mine,
                        ledger is not None,
                    )
                )

            pending: list[
                tuple[str, str, bytes, list[LegacyProjectLabel], str, SqlProject | None, bool]
            ] = []
            for proposal in proposed:
                (
                    _owner,
                    normalized_name,
                    _checksum,
                    group,
                    resolved_id,
                    _mine,
                    has_ledger,
                ) = proposal
                already_migrated = has_ledger
                for assignment in group:
                    metadata = session.get(
                        SqlConversationMetadata,
                        (current_workspace_id(), assignment.session_id),
                    )
                    snapshot = session.get(
                        SqlSessionProjectSnapshot,
                        (current_workspace_id(), assignment.session_id),
                    )
                    if metadata is None:
                        already_migrated = False
                        issues.append(
                            self._issue(normalized_name, group, "missing_session_metadata")
                        )
                        break
                    if metadata.project_id not in (None, resolved_id):
                        already_migrated = False
                        issues.append(
                            self._issue(normalized_name, group, "existing_project_binding")
                        )
                        break
                    if snapshot is not None and snapshot.project_id != resolved_id:
                        already_migrated = False
                        issues.append(
                            self._issue(normalized_name, group, "existing_snapshot_binding")
                        )
                        break
                    if metadata.project_id != resolved_id or snapshot is None:
                        already_migrated = False
                if not already_migrated:
                    pending.append(proposal)
            if issues:
                return ProjectBackfillResult(
                    issues=tuple(
                        sorted(
                            issues,
                            key=lambda issue: (issue.normalized_name, issue.reason),
                        )
                    )
                )
            proposed = pending

            now = now_epoch()
            mappings: list[ProjectMigrationMapping] = []
            for (
                owner,
                normalized_name,
                checksum,
                group,
                resolved_id,
                mine,
                _has_ledger,
            ) in proposed:
                display_name = unicodedata.normalize("NFKC", group[0].label).strip()
                if mine is None:
                    session.add(
                        SqlProject(
                            id=resolved_id,
                            owner_principal_id=owner,
                            name=display_name,
                            description=None,
                            normalized_name=normalized_name,
                            normalized_name_checksum=checksum,
                            storage_key=_storage_key(resolved_id),
                            defaults_json="{}",
                            defaults_schema_version=DEFAULTS_SCHEMA_VERSION,
                            row_version=1,
                            created_at=now,
                            updated_at=now,
                            archived_at=None,
                        )
                    )
                ledger = session.execute(
                    select(SqlProjectMigrationLedger).where(
                        SqlProjectMigrationLedger.workspace_id == current_workspace_id(),
                        SqlProjectMigrationLedger.owner_principal_id == owner,
                        SqlProjectMigrationLedger.normalized_name_checksum == checksum,
                    )
                ).scalar_one_or_none()
                if ledger is None:
                    session.add(
                        SqlProjectMigrationLedger(
                            id=f"pmig_{uuid.uuid4().hex}",
                            owner_principal_id=owner,
                            normalized_name=normalized_name,
                            normalized_name_checksum=checksum,
                            project_id=resolved_id,
                            source_fingerprint=_source_fingerprint(group),
                            created_at=now,
                        )
                    )
                for assignment in group:
                    metadata = session.get(
                        SqlConversationMetadata,
                        (current_workspace_id(), assignment.session_id),
                    )
                    if metadata is None:
                        raise RuntimeError("project backfill metadata disappeared")
                    metadata.project_id = resolved_id
                    snapshot = session.get(
                        SqlSessionProjectSnapshot,
                        (current_workspace_id(), assignment.session_id),
                    )
                    if snapshot is None:
                        session.add(
                            SqlSessionProjectSnapshot(
                                session_id=assignment.session_id,
                                project_id=resolved_id,
                                snapshot_origin="backfill",
                                project_row_version=None,
                                defaults_schema_version=DEFAULTS_SCHEMA_VERSION,
                                defaults_json="{}",
                                created_at=now,
                            )
                        )
                mappings.append(
                    ProjectMigrationMapping(
                        owner_principal_id=owner,
                        normalized_name=normalized_name,
                        project_id=resolved_id,
                        session_ids=tuple(sorted(assignment.session_id for assignment in group)),
                    )
                )
            session.flush()
            result = ProjectBackfillResult(mappings=tuple(mappings))
        for assignment in assignments:
            conversation_store.delete_label(assignment.session_id, PROJECT_LABEL_KEY)
        return result
