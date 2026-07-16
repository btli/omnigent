"""SQLAlchemy-backed project store."""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationMetadata,
    SqlProject,
    SqlProjectMigrationLedger,
    SqlSessionProjectSnapshot,
    SqlUser,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_conversation_engine,
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities import (
    LegacyProjectLabel,
    LiveProjectSnapshot,
    Project,
    ProjectBackfillResult,
    ProjectMigrationIssue,
    ProjectMigrationMapping,
    project_snapshot_values,
)
from omnigent.errors import ErrorCode, OmnigentError, ProjectInputError
from omnigent.projects.defaults import DEFAULTS_SCHEMA_VERSION, validate_defaults_bundle
from omnigent.stores.conversation_store import PROJECT_LABEL_KEY, ConversationStore
from omnigent.stores.project_store import UNSET, ProjectStore, _Unset


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


def _ensure_user_row(session: Session, user_id: str) -> None:
    """Provision a user row without racing concurrent creators."""
    if session.get(SqlUser, (current_workspace_id(), user_id)) is not None:
        return
    try:
        with session.begin_nested():
            session.add(SqlUser(id=user_id, is_admin=False))
            session.flush()
    except IntegrityError:
        pass


def check_project_cas(
    session: Session,
    project_id: str,
    owner: str,
    expected_row_version: int | None,
    *,
    require_archived: bool | None,
) -> SqlProject | None:
    """Check the canonical project compare-and-set preconditions."""
    if expected_row_version is None:
        raise OmnigentError(
            "If-Match is required for project mutations",
            code=ErrorCode.PRECONDITION_FAILED,
        )
    row = session.get(SqlProject, (current_workspace_id(), project_id))
    if row is None or row.owner_principal_id != owner:
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
    return row


def mutate_project_row(
    session: Session,
    project_id: str,
    owner_principal_id: str,
    expected_row_version: int | None,
    values: dict[str, Any],
    *,
    require_archived: bool | None = None,
    prepare: Callable[[Session], None] | None = None,
    updated_at: int | None = None,
) -> Project | None:
    """Apply the canonical project compare-and-set mutation."""
    row = check_project_cas(
        session,
        project_id,
        owner_principal_id,
        expected_row_version,
        require_archived=require_archived,
    )
    if row is None:
        return None
    if prepare is not None:
        prepare(session)

    conditions = [
        SqlProject.workspace_id == current_workspace_id(),
        SqlProject.id == project_id,
        SqlProject.owner_principal_id == owner_principal_id,
        SqlProject.row_version == expected_row_version,
    ]
    if require_archived is True:
        conditions.append(SqlProject.archived_at.is_not(None))
    elif require_archived is False:
        conditions.append(SqlProject.archived_at.is_(None))
    result = session.execute(
        update(SqlProject)
        .where(*conditions)
        .values(
            **values,
            updated_at=now_epoch() if updated_at is None else updated_at,
            row_version=SqlProject.row_version + 1,
        )
    )
    if result.rowcount != 1:
        raise OmnigentError(
            "Project ETag is stale",
            code=ErrorCode.PRECONDITION_FAILED,
        )
    session.flush()
    session.expire_all()
    updated_row = session.get(SqlProject, (current_workspace_id(), project_id))
    return _to_entity(updated_row) if updated_row is not None else None


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


@dataclass(frozen=True)
class _BackfillProposal:
    owner: str
    normalized_name: str
    checksum: bytes
    assignments: list[LegacyProjectLabel]
    project_id: str
    existing_project: SqlProject | None
    has_ledger: bool


class SqlAlchemyProjectStore(ProjectStore):
    """Relational project persistence with workspace and owner isolation."""

    def __init__(
        self,
        storage_location: str,
        conversation_storage_location: str | None = None,
    ) -> None:
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        conv_uri = conversation_storage_location or storage_location
        self._conv_engine = (
            self._engine
            if conv_uri == storage_location
            else get_or_create_conversation_engine(conv_uri)
        )
        self._session = make_managed_session_maker(self._engine, immediate=True)
        self._conv_session = make_managed_session_maker(self._conv_engine)

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
                _ensure_user_row(session, owner_principal_id)
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

    def get_or_create_by_name(self, owner_principal_id: str, name: str) -> Project:
        project = self.get_by_name(name, owner_principal_id)
        if project is None:
            return self.create(owner_principal_id, name)
        if project.archived_at is not None:
            raise OmnigentError("Project is archived", code=ErrorCode.CONFLICT)
        return project

    def list(
        self,
        owner_principal_id: str,
        *,
        include_archived: bool = False,
        archived_members: bool = False,
    ) -> list[Project]:
        statement = select(SqlProject).where(
            SqlProject.workspace_id == current_workspace_id(),
            SqlProject.owner_principal_id == owner_principal_id,
        )
        if archived_members or not include_archived:
            statement = statement.where(SqlProject.archived_at.is_(None))
        statement = statement.order_by(SqlProject.normalized_name, SqlProject.id).distinct()
        with self._session() as session:
            projects = [_to_entity(row) for row in session.execute(statement).scalars().all()]
            if not archived_members or not projects:
                return projects
            memberships = session.execute(
                select(SqlConversationMetadata.id, SqlConversationMetadata.project_id).where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.project_id.in_([project.id for project in projects]),
                )
            ).all()

        session_to_project = {row.id: row.project_id for row in memberships}
        archived_session_ids: set[str] = set()
        with self._conv_session() as session:
            session_ids = list(session_to_project)
            for offset in range(0, len(session_ids), 500):
                archived_session_ids.update(
                    session.execute(
                        select(SqlConversation.id).where(
                            SqlConversation.workspace_id == current_workspace_id(),
                            SqlConversation.id.in_(session_ids[offset : offset + 500]),
                            SqlConversation.archived.is_(True),
                        )
                    ).scalars()
                )
        archived_project_ids = {
            session_to_project[session_id] for session_id in archived_session_ids
        }
        return [project for project in projects if project.id in archived_project_ids]

    def transfer(
        self,
        project_id: str,
        current_owner: str,
        new_owner: str,
        *,
        expected_row_version: int | None,
    ) -> Project | None:
        def prepare(session: Session) -> None:
            _ensure_user_row(session, new_owner)

        return self._mutate(
            project_id,
            current_owner,
            expected_row_version,
            {"owner_principal_id": new_owner},
            prepare=prepare,
        )

    def _mutate(
        self,
        project_id: str,
        owner_principal_id: str,
        expected_row_version: int | None,
        values: dict[str, Any],
        *,
        require_archived: bool | None = None,
        prepare: Callable[[Session], None] | None = None,
    ) -> Project | None:
        try:
            with self._session() as session:
                return mutate_project_row(
                    session,
                    project_id,
                    owner_principal_id,
                    expected_row_version,
                    values,
                    require_archived=require_archived,
                    prepare=prepare,
                )
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
            values["defaults_schema_version"] = DEFAULTS_SCHEMA_VERSION
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
        issue_groups: set[str] = set()

        def add_issue(issue: ProjectMigrationIssue) -> None:
            issues.append(issue)
            issue_groups.add(issue.normalized_name)

        proposed: list[_BackfillProposal] = []
        with self._session() as session:
            for normalized_name in sorted(grouped):
                group = grouped[normalized_name]
                display_labels = {
                    unicodedata.normalize("NFKC", assignment.label).strip() for assignment in group
                }
                if len(display_labels) > 1:
                    add_issue(self._issue(normalized_name, group, "normalized_name_alias"))
                owners = {assignment.owner_principal_id for assignment in group}
                if None in owners or len(owners) != 1:
                    add_issue(self._issue(normalized_name, group, "ambiguous_owner"))
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
                    add_issue(self._issue(normalized_name, group, "owned_name_collision"))
                mine = next(
                    (
                        project
                        for project in existing_projects
                        if project.owner_principal_id == owner
                    ),
                    None,
                )
                if mine is not None and mine.archived_at is not None:
                    add_issue(self._issue(normalized_name, group, "archived_project"))
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
                    add_issue(self._issue(normalized_name, group, "ledger_project_mismatch"))
                proposed.append(
                    _BackfillProposal(
                        owner=owner,
                        normalized_name=normalized_name,
                        checksum=checksum,
                        assignments=group,
                        project_id=resolved_id,
                        existing_project=mine,
                        has_ledger=ledger is not None,
                    )
                )

            pending: list[_BackfillProposal] = []
            for proposal in proposed:
                already_migrated = proposal.has_ledger
                for assignment in proposal.assignments:
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
                        add_issue(
                            self._issue(
                                proposal.normalized_name,
                                proposal.assignments,
                                "missing_session_metadata",
                            )
                        )
                        break
                    if metadata.project_id not in (None, proposal.project_id):
                        already_migrated = False
                        add_issue(
                            self._issue(
                                proposal.normalized_name,
                                proposal.assignments,
                                "existing_project_binding",
                            )
                        )
                        break
                    if snapshot is not None and snapshot.project_id != proposal.project_id:
                        already_migrated = False
                        add_issue(
                            self._issue(
                                proposal.normalized_name,
                                proposal.assignments,
                                "existing_snapshot_binding",
                            )
                        )
                        break
                    if metadata.project_id != proposal.project_id or snapshot is None:
                        already_migrated = False
                if not already_migrated:
                    pending.append(proposal)
            proposed = [
                proposal for proposal in pending if proposal.normalized_name not in issue_groups
            ]

            now = now_epoch()
            mappings: list[ProjectMigrationMapping] = []
            for proposal in proposed:
                owner = proposal.owner
                normalized_name = proposal.normalized_name
                checksum = proposal.checksum
                group = proposal.assignments
                resolved_id = proposal.project_id
                mine = proposal.existing_project
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
                if not proposal.has_ledger:
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
                        project_snapshot = LiveProjectSnapshot.backfill(resolved_id)
                        session.add(
                            SqlSessionProjectSnapshot(
                                session_id=assignment.session_id,
                                **project_snapshot_values(project_snapshot, now),
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
            result = ProjectBackfillResult(
                mappings=tuple(mappings),
                issues=tuple(
                    sorted(
                        issues,
                        key=lambda issue: (issue.normalized_name, issue.reason),
                    )
                ),
            )
        clean_groups = set(grouped) - issue_groups
        for assignment in assignments:
            _, normalized_name, _ = normalize_project_name(assignment.label)
            if normalized_name in clean_groups:
                conversation_store.delete_label(assignment.session_id, PROJECT_LABEL_KEY)
        return result
