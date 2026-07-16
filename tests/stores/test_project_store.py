"""Acceptance tests for the SQLAlchemy project store and label backfill."""

from __future__ import annotations

import hashlib
import logging

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlConversationMetadata,
    SqlProjectMigrationLedger,
    SqlSessionProjectSnapshot,
    current_workspace_id,
    workspace_scope,
)
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.projects.defaults import DEFAULTS_SCHEMA_VERSION
from omnigent.server.app import _backfill_legacy_project_labels_on_startup
from omnigent.server.auth import LEVEL_OWNER
from omnigent.stores.conversation_store import PROJECT_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from omnigent.stores.project_store import UNSET
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyProjectStore:
    return SqlAlchemyProjectStore(db_uri)


def test_create_normalizes_name_and_derives_storage_key_from_id(
    store: SqlAlchemyProjectStore,
) -> None:
    project = store.create("alice", "  Frontend  ")

    assert project.name == "Frontend"
    assert project.normalized_name == "frontend"
    assert project.normalized_name_checksum == hashlib.sha256(b"frontend").hexdigest()
    assert project.storage_key.startswith("proj-")
    assert project.storage_key == f"proj-{hashlib.sha256(project.id.encode()).hexdigest()[:32]}"
    assert project.row_version == 1


def test_name_uniqueness_is_normalized_and_per_owner(
    store: SqlAlchemyProjectStore,
) -> None:
    store.create("alice", "Omnigent")

    with pytest.raises(OmnigentError) as exc_info:
        store.create("alice", "  OMNIGENT  ")
    assert exc_info.value.code == ErrorCode.CONFLICT

    bob = store.create("bob", "omnigent")
    assert bob.owner_principal_id == "bob"


def test_transfer_rekeys_owner_and_provisions_destination_user(
    store: SqlAlchemyProjectStore,
) -> None:
    project = store.create("local", "Migrated")

    transferred = store.transfer(
        project.id,
        "local",
        "alice",
        expected_row_version=project.row_version,
    )

    assert transferred is not None
    assert transferred.owner_principal_id == "alice"
    assert transferred.row_version == project.row_version + 1
    assert store.get(project.id, "local") is None
    assert store.get(project.id, "alice") == transferred


def test_transfer_enforces_destination_name_uniqueness_and_if_match(
    store: SqlAlchemyProjectStore,
) -> None:
    source = store.create("local", "Existing")
    store.create("alice", "existing")

    with pytest.raises(OmnigentError) as collision:
        store.transfer(
            source.id,
            "local",
            "alice",
            expected_row_version=source.row_version,
        )
    assert collision.value.code == ErrorCode.CONFLICT

    with pytest.raises(OmnigentError) as stale:
        store.transfer(
            source.id,
            "local",
            "bob",
            expected_row_version=source.row_version + 1,
        )
    assert stale.value.code == ErrorCode.PRECONDITION_FAILED
    assert (
        store.transfer(
            source.id,
            "mallory",
            "bob",
            expected_row_version=source.row_version,
        )
        is None
    )


def test_owner_and_workspace_scoping_hide_projects(
    store: SqlAlchemyProjectStore,
) -> None:
    with workspace_scope(10):
        project = store.create("alice", "Private")
        assert store.get(project.id, "bob") is None

    with workspace_scope(11):
        assert store.get(project.id, "alice") is None
        other_workspace_project = store.create("alice", "Private")
        assert other_workspace_project.id != project.id


def test_all_mutations_require_matching_version_and_increment_atomically(
    store: SqlAlchemyProjectStore,
) -> None:
    project = store.create("alice", "One")

    operations = (
        lambda version: store.rename(project.id, "alice", "Two", expected_row_version=version),
        lambda version: store.update(
            project.id,
            "alice",
            expected_row_version=version,
            description="description",
            defaults_json=UNSET,
        ),
        lambda version: store.archive(project.id, "alice", expected_row_version=version),
        lambda version: store.restore(project.id, "alice", expected_row_version=version),
    )

    version = 1
    for operation in operations:
        with pytest.raises(OmnigentError) as missing:
            operation(None)
        assert missing.value.code == ErrorCode.PRECONDITION_FAILED

        updated = operation(version)
        assert updated is not None
        version += 1
        assert updated.row_version == version

        with pytest.raises(OmnigentError) as stale:
            operation(version - 1)
        assert stale.value.code == ErrorCode.PRECONDITION_FAILED


def test_rename_to_existing_name_is_conflict_and_preserves_source(
    store: SqlAlchemyProjectStore,
) -> None:
    source = store.create("alice", "Source")
    store.create("alice", "Existing")

    with pytest.raises(OmnigentError) as exc_info:
        store.rename(
            source.id,
            "alice",
            "Existing",
            expected_row_version=source.row_version,
        )

    assert exc_info.value.code == ErrorCode.CONFLICT
    unchanged = store.get(source.id, "alice")
    assert unchanged is not None
    assert unchanged.name == "Source"
    assert unchanged.row_version == source.row_version


def test_archive_and_restore_reject_already_satisfied_state(
    store: SqlAlchemyProjectStore,
) -> None:
    live = store.create("alice", "Lifecycle")

    with pytest.raises(OmnigentError) as restore_error:
        store.restore(
            live.id,
            "alice",
            expected_row_version=live.row_version,
        )
    assert restore_error.value.code == ErrorCode.CONFLICT

    archived = store.archive(
        live.id,
        "alice",
        expected_row_version=live.row_version,
    )
    assert archived is not None
    with pytest.raises(OmnigentError) as archive_error:
        store.archive(
            live.id,
            "alice",
            expected_row_version=archived.row_version,
        )
    assert archive_error.value.code == ErrorCode.CONFLICT


def test_stale_etag_write_is_not_applied_and_version_advances_once(
    store: SqlAlchemyProjectStore,
) -> None:
    project = store.create("alice", "Concurrent")
    first_etag = project.row_version

    winner = store.update(
        project.id,
        "alice",
        expected_row_version=first_etag,
        description="winner",
    )
    assert winner is not None
    assert winner.row_version == first_etag + 1

    with pytest.raises(OmnigentError) as stale_error:
        store.update(
            project.id,
            "alice",
            expected_row_version=first_etag,
            description="loser",
        )
    assert stale_error.value.code == ErrorCode.PRECONDITION_FAILED

    persisted = store.get(project.id, "alice")
    assert persisted is not None
    assert persisted.description == "winner"
    assert persisted.row_version == first_etag + 1


def test_archive_hides_by_default_and_keeps_name_reserved(
    store: SqlAlchemyProjectStore,
) -> None:
    project = store.create("alice", "Reserved")
    archived = store.archive(project.id, "alice", expected_row_version=1)

    assert archived is not None and archived.archived_at is not None
    assert store.list("alice") == []
    assert store.list("alice", include_archived=True) == [archived]
    with pytest.raises(OmnigentError) as exc_info:
        store.create("alice", "reserved")
    assert exc_info.value.code == ErrorCode.CONFLICT

    with pytest.raises(OmnigentError) as attach_error:
        store.get_for_use(project.id, "alice")
    assert attach_error.value.code == ErrorCode.CONFLICT

    restored = store.restore(project.id, "alice", expected_row_version=2)
    assert restored is not None and restored.archived_at is None


def test_defaults_bundle_validation_and_update(
    store: SqlAlchemyProjectStore,
) -> None:
    project = store.create(
        "alice",
        "Defaults",
        defaults_json={"host_type": "external", "workspace": "/repo", "model": None},
    )
    assert project.defaults_json["workspace"] == "/repo"

    with pytest.raises(OmnigentError) as unknown:
        store.create("alice", "Bad", defaults_json={"credential_ref": "secret"})
    assert unknown.value.code == ErrorCode.INVALID_INPUT

    with pytest.raises(OmnigentError) as managed_host:
        store.update(
            project.id,
            "alice",
            expected_row_version=1,
            defaults_json={"host_type": "managed", "host_id": "host-1"},
        )
    assert managed_host.value.code == ErrorCode.INVALID_INPUT


def test_update_stamps_current_defaults_schema_version(
    store: SqlAlchemyProjectStore,
) -> None:
    project = store.create("alice", "Versioned defaults")

    updated = store.update(
        project.id,
        "alice",
        expected_row_version=project.row_version,
        defaults_json={"model": "gpt-5"},
    )

    assert updated is not None
    assert updated.defaults_schema_version == DEFAULTS_SCHEMA_VERSION


def test_list_projects_returns_distinct_names_sorted(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """Every live owned project row is returned once in name order."""
    projects = SqlAlchemyProjectStore(db_uri)
    sprint = projects.create("alice", "Sprint 42")
    customer = projects.create("alice", "Customer X")
    empty = projects.create("alice", "Empty")
    archived_row = projects.create("alice", "Archived row")
    projects.archive(archived_row.id, "alice", expected_row_version=1)
    foreign = projects.create("bob", "Foreign")
    a1 = conversation_store.create_conversation()
    a2 = conversation_store.create_conversation()
    b1 = conversation_store.create_conversation()
    other = conversation_store.create_conversation()

    conversation_store.set_project_membership(a1.id, sprint.id)
    conversation_store.set_project_membership(a2.id, sprint.id)
    conversation_store.set_project_membership(b1.id, customer.id)
    conversation_store.set_project_membership(other.id, foreign.id)

    assert [(item.id, item.name) for item in projects.list("alice")] == [
        (customer.id, "Customer X"),
        (empty.id, "Empty"),
        (sprint.id, "Sprint 42"),
    ]


def test_list_projects_empty_when_no_project_labels(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """Legacy and non-project labels are not project read authorities."""
    conversation = conversation_store.create_conversation()
    conversation_store.set_labels(
        conversation.id,
        {"omni_project": "Legacy", "integrity": "1"},
    )
    assert SqlAlchemyProjectStore(db_uri).list("alice") == []


def test_list_projects_includes_projects_with_only_archived_members(
    db_uri: str,
) -> None:
    """The live variant lists rows; the archived variant still requires members."""
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    gone = projects.create("alice", "Gone")
    mixed = projects.create("alice", "Mixed")
    solo = conversations.create_conversation()
    mix_archived = conversations.create_conversation()
    mix_active = conversations.create_conversation()

    conversations.set_project_membership(solo.id, gone.id)
    conversations.set_project_membership(mix_archived.id, mixed.id)
    conversations.set_project_membership(mix_active.id, mixed.id)
    conversations.update_conversation(solo.id, archived=True)
    conversations.update_conversation(mix_archived.id, archived=True)

    assert [item.name for item in projects.list("alice")] == ["Gone", "Mixed"]
    assert [item.name for item in projects.list("alice", archived_members=True)] == [
        "Gone",
        "Mixed",
    ]


def test_list_projects_is_owner_scoped(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """A member in another owner's project never leaks into the result."""
    projects = SqlAlchemyProjectStore(db_uri)
    mine_project = projects.create("alice@example.com", "Mine")
    theirs_project = projects.create("bob@example.com", "Theirs")
    mine = conversation_store.create_conversation()
    theirs = conversation_store.create_conversation()
    conversation_store.set_project_membership(mine.id, mine_project.id)
    conversation_store.set_project_membership(theirs.id, theirs_project.id)

    assert [item.name for item in projects.list("alice@example.com")] == ["Mine"]


def _legacy_session(
    db_uri: str,
    conversation_store: SqlAlchemyConversationStore,
    permissions: SqlAlchemyPermissionStore,
    *,
    owner: str | None,
    label: str,
) -> str:
    conversation = conversation_store.create_conversation()
    conversation_store.set_labels(conversation.id, {PROJECT_LABEL_KEY: label})
    if owner is not None:
        permissions.ensure_user(owner)
        permissions.grant(owner, conversation.id, LEVEL_OWNER)
    return conversation.id


def test_label_backfill_is_idempotent_and_writes_atomic_backfill_snapshot(
    db_uri: str,
    store: SqlAlchemyProjectStore,
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    session_id = _legacy_session(
        db_uri, conversations, permissions, owner="alice", label="Omnigent"
    )

    first = store.backfill_legacy_labels(conversations)
    second = store.backfill_legacy_labels(conversations)

    assert first.requires_mapping is False
    assert len(first.mappings) == 1
    assert second.mappings == ()
    assert second.issues == ()
    project_id = first.mappings[0].project_id
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        metadata = session.execute(
            select(SqlConversationMetadata).where(SqlConversationMetadata.id == session_id)
        ).scalar_one()
        snapshot = session.execute(
            select(SqlSessionProjectSnapshot).where(
                SqlSessionProjectSnapshot.session_id == session_id
            )
        ).scalar_one()
        ledgers = session.execute(select(SqlProjectMigrationLedger)).scalars().all()
    assert metadata.project_id == project_id
    assert snapshot.project_id == project_id
    assert snapshot.snapshot_origin == "backfill"
    assert snapshot.project_row_version is None
    assert snapshot.defaults_json == "{}"
    assert len(ledgers) == 1
    persisted = conversations.get_conversation(session_id)
    assert persisted is not None and PROJECT_LABEL_KEY not in persisted.labels
    assert conversations.list_project_label_workspace_ids() == []
    assert [project.name for project in store.list("alice")] == ["Omnigent"]
    assert {
        conversation.id
        for conversation in conversations.list_conversations(project_id=project_id).data
    } == {session_id}


def test_backfill_keeps_same_name_distinct_per_workspace(
    db_uri: str,
    store: SqlAlchemyProjectStore,
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)

    project_ids: list[str] = []
    for workspace_id in (100, 200):
        with workspace_scope(workspace_id):
            _legacy_session(db_uri, conversations, permissions, owner="alice", label="omnigent")
            result = store.backfill_legacy_labels(conversations)
            assert result.requires_mapping is False
            project_ids.append(result.mappings[0].project_id)
            assert [project.name for project in store.list("alice")] == ["omnigent"]

    assert project_ids[0] != project_ids[1]


def test_startup_backfill_runs_all_labeled_workspaces_and_is_non_blocking(
    db_uri: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    projects = SqlAlchemyProjectStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    migrated_sessions: dict[int, str] = {}
    for workspace_id in (100, 200):
        with workspace_scope(workspace_id):
            migrated_sessions[workspace_id] = _legacy_session(
                db_uri,
                conversations,
                permissions,
                owner="alice",
                label=f"Project {workspace_id}",
            )
    with workspace_scope(300):
        _legacy_session(
            db_uri,
            conversations,
            permissions,
            owner=None,
            label="Needs mapping",
        )

    with caplog.at_level(logging.INFO):
        first = _backfill_legacy_project_labels_on_startup(conversations, projects)
        second = _backfill_legacy_project_labels_on_startup(conversations, projects)

    assert first == (2, 1)
    assert second == (0, 1)
    for workspace_id, session_id in migrated_sessions.items():
        with workspace_scope(workspace_id):
            with Session(get_or_create_engine(db_uri)) as session:
                metadata = session.get(SqlConversationMetadata, (workspace_id, session_id))
                snapshot = session.get(SqlSessionProjectSnapshot, (workspace_id, session_id))
            assert metadata is not None and metadata.project_id is not None
            assert snapshot is not None and snapshot.project_id == metadata.project_id
            persisted = conversations.get_conversation(session_id)
            assert persisted is not None and PROJECT_LABEL_KEY not in persisted.labels
    assert conversations.list_project_label_workspace_ids() == [300]
    mapping_logs = [
        record
        for record in caplog.records
        if record.getMessage() == "project label mapping required"
    ]
    assert len(mapping_logs) == 2
    assert all(record.workspace_id == 300 for record in mapping_logs)
    assert all(
        record.mapping_plan[0]["normalized_name"] == "needs mapping" for record in mapping_logs
    )
    assert any(
        record.getMessage() == "project label startup backfill complete"
        and record.migrated == 2
        and record.requires_mapping == 1
        for record in caplog.records
    )


def test_startup_backfill_continues_after_one_workspace_fails(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    projects = SqlAlchemyProjectStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    sessions: dict[int, str] = {}
    for workspace_id in (100, 200):
        with workspace_scope(workspace_id):
            sessions[workspace_id] = _legacy_session(
                db_uri,
                conversations,
                permissions,
                owner="alice",
                label=f"Project {workspace_id}",
            )

    real_backfill = projects.backfill_legacy_labels

    def _fail_one_workspace(conversation_store: SqlAlchemyConversationStore):
        if current_workspace_id() == 100:
            raise RuntimeError("simulated workspace failure")
        return real_backfill(conversation_store)

    monkeypatch.setattr(projects, "backfill_legacy_labels", _fail_one_workspace)
    with caplog.at_level(logging.WARNING):
        result = _backfill_legacy_project_labels_on_startup(conversations, projects)

    assert result == (1, 0)
    with workspace_scope(100):
        with Session(get_or_create_engine(db_uri)) as session:
            metadata = session.get(SqlConversationMetadata, (100, sessions[100]))
        assert metadata is not None and metadata.project_id is None
    with workspace_scope(200):
        with Session(get_or_create_engine(db_uri)) as session:
            metadata = session.get(SqlConversationMetadata, (200, sessions[200]))
        assert metadata is not None and metadata.project_id is not None
    assert any(
        record.getMessage() == "project label startup backfill failed"
        and record.workspace_id == 100
        for record in caplog.records
    )


def test_startup_backfill_unconfigured_or_scan_failure_is_non_blocking(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    projects = SqlAlchemyProjectStore(db_uri)

    assert _backfill_legacy_project_labels_on_startup(None, projects) == (0, 0)
    assert _backfill_legacy_project_labels_on_startup(conversations, None) == (0, 0)

    def _fail_scan() -> list[int]:
        raise RuntimeError("simulated workspace scan failure")

    monkeypatch.setattr(conversations, "list_project_label_workspace_ids", _fail_scan)
    assert _backfill_legacy_project_labels_on_startup(conversations, projects) == (0, 0)


@pytest.mark.parametrize("owners", [(), ("alice", "bob")])
def test_backfill_zero_or_multiple_owners_stops_with_mapping_plan(
    db_uri: str,
    store: SqlAlchemyProjectStore,
    owners: tuple[str, ...],
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    if not owners:
        _legacy_session(db_uri, conversations, permissions, owner=None, label="Needs owner")
    else:
        for owner in owners:
            _legacy_session(db_uri, conversations, permissions, owner=owner, label="Shared name")

    result = store.backfill_legacy_labels(conversations)

    assert result.requires_mapping is True
    assert len(result.issues) == 1
    assert store.list("alice", include_archived=True) == []


def test_backfill_alias_or_preexisting_other_owner_collision_is_non_mutating(
    db_uri: str,
    store: SqlAlchemyProjectStore,
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    first_session = _legacy_session(
        db_uri, conversations, permissions, owner="alice", label="Omnigent"
    )
    _legacy_session(db_uri, conversations, permissions, owner="alice", label=" OMNIGENT ")
    store.create("bob", "omnigent")

    result = store.backfill_legacy_labels(conversations)

    assert result.requires_mapping is True
    assert {issue.reason for issue in result.issues} == {
        "normalized_name_alias",
        "owned_name_collision",
    }
    engine = get_or_create_engine(db_uri)
    with engine.connect() as connection:
        project_id = connection.execute(
            select(SqlConversationMetadata.project_id).where(
                SqlConversationMetadata.id == first_session
            )
        ).scalar_one()
        ledger_count = len(connection.execute(select(SqlProjectMigrationLedger)).scalars().all())
    assert project_id is None
    assert ledger_count == 0


def test_backfill_applies_clean_group_and_reports_ambiguous_group(
    db_uri: str,
    store: SqlAlchemyProjectStore,
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    clean_session = _legacy_session(
        db_uri,
        conversations,
        permissions,
        owner="alice",
        label="Clean",
    )
    ambiguous_session = _legacy_session(
        db_uri,
        conversations,
        permissions,
        owner=None,
        label="Ambiguous",
    )

    result = store.backfill_legacy_labels(conversations)

    assert len(result.mappings) == 1
    assert result.mappings[0].normalized_name == "clean"
    assert [issue.normalized_name for issue in result.issues] == ["ambiguous"]
    clean = conversations.get_conversation(clean_session)
    ambiguous = conversations.get_conversation(ambiguous_session)
    assert clean is not None and clean.project_id == result.mappings[0].project_id
    assert PROJECT_LABEL_KEY not in clean.labels
    assert ambiguous is not None and ambiguous.project_id is None
    assert ambiguous.labels[PROJECT_LABEL_KEY] == "Ambiguous"


def test_startup_backfill_kill_switch_skips(
    db_uri: str,
    store: SqlAlchemyProjectStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    session_id = _legacy_session(
        db_uri,
        conversations,
        permissions,
        owner="alice",
        label="Disabled",
    )
    monkeypatch.setenv("OMNIGENT_DISABLE_LABEL_BACKFILL", "1")

    with caplog.at_level(logging.INFO):
        result = _backfill_legacy_project_labels_on_startup(conversations, store)

    assert result == (0, 0)
    persisted = conversations.get_conversation(session_id)
    assert persisted is not None and persisted.project_id is None
    assert any("disabled by environment" in message for message in caplog.messages)


def test_backfill_reuses_project_owned_by_candidate(
    db_uri: str,
    store: SqlAlchemyProjectStore,
) -> None:
    existing = store.create("alice", "Omnigent")
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    _legacy_session(db_uri, conversations, permissions, owner="alice", label="omnigent")

    result = store.backfill_legacy_labels(conversations)

    assert result.requires_mapping is False
    assert result.mappings[0].project_id == existing.id
    assert len(store.list("alice")) == 1


def test_backfill_never_repoints_an_existing_project_binding(
    db_uri: str,
    store: SqlAlchemyProjectStore,
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    session_id = _legacy_session(
        db_uri, conversations, permissions, owner="alice", label="Omnigent"
    )
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        metadata = session.get(SqlConversationMetadata, (0, session_id))
        assert metadata is not None
        metadata.project_id = "proj_already_bound"
        session.commit()

    result = store.backfill_legacy_labels(conversations)

    assert result.requires_mapping is True
    assert result.issues[0].reason == "existing_project_binding"
    with Session(engine) as session:
        metadata = session.get(SqlConversationMetadata, (0, session_id))
        assert metadata is not None and metadata.project_id == "proj_already_bound"
        assert session.execute(select(SqlProjectMigrationLedger)).scalar_one_or_none() is None
