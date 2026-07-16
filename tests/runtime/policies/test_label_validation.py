"""
Tests for :meth:`PolicyEngine.apply_label_writes` schema
validation (POLICIES.md §10 / §13).

Silent-drop semantics:

- Key not in ``LabelDef.values`` → dropped.
- Unknown key (no LabelDef) → set freely.
- Valid write → persisted via the store.

The drop path is silent by design (matches omnigent) —
a runtime validation failure does NOT raise. The surviving
writes still land atomically.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlConversationMetadata, SqlSessionProjectSnapshot
from omnigent.db.utils import get_or_create_engine
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.server.auth import LEVEL_OWNER
from omnigent.spec.types import LabelDef
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

# ── Engine-level filtering ────────────────────────────


def _build_engine_with_defs(
    store: SqlAlchemyConversationStore,
    label_defs: dict[str, LabelDef],
    *,
    initial_labels: dict[str, str] | None = None,
) -> PolicyEngine:
    """Build an engine with specific label_defs."""
    conv = store.create_conversation()
    return PolicyEngine(
        policies=[],
        label_defs=label_defs,
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels=initial_labels or {},
        conversation_store=store,
    )


def test_apply_label_writes_drops_value_outside_enum(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A value not in ``LabelDef.values`` is silently
    dropped. Prevents a policy (or a prompt-policy
    classifier) from injecting an arbitrary string into an
    enumerated label."""
    engine = _build_engine_with_defs(
        conversation_store,
        {"integrity": LabelDef(values=["0", "1"])},
    )
    # "2" is not in values → dropped. "integrity": "1" is
    # valid → lands.
    engine.apply_label_writes({"integrity": "1", "other": "x"})
    # Hot cache has the valid write + the unknown-key
    # write (unknown keys pass through per POLICIES.md §10
    # schemaless-set-freely rule).
    assert engine.labels == {"integrity": "1", "other": "x"}

    # Now try to set an out-of-enum value.
    engine.apply_label_writes({"integrity": "2"})
    # Dropped — cache still shows "1".
    assert engine.labels["integrity"] == "1"


def test_apply_label_writes_partial_batch_survives(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """One key in a multi-key batch violates the schema;
    OTHER keys still land. Silent-drop is per-key, not
    all-or-nothing."""
    engine = _build_engine_with_defs(
        conversation_store,
        {
            "integrity": LabelDef(values=["0", "1"]),
            "other": LabelDef(values=["a", "b"]),
        },
        initial_labels={"integrity": "0"},
    )
    # integrity "2" is out-of-enum (drop); other "a" is valid (land).
    engine.apply_label_writes({"integrity": "2", "other": "a"})
    # Only `other` landed; integrity unchanged.
    assert engine.labels == {"integrity": "0", "other": "a"}


def test_apply_label_writes_schemaless_keys_pass_freely(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Keys with no LabelDef are set freely — the
    omnigent-parity behavior that lets policies write
    ad-hoc labels without declaring a schema first
    (POLICIES.md §10)."""
    engine = _build_engine_with_defs(
        conversation_store,
        {},  # no label_defs at all
    )
    engine.apply_label_writes({"any": "value", "anything": "123"})
    # Both landed — no schema to enforce.
    assert engine.labels == {"any": "value", "anything": "123"}


def test_apply_label_writes_values_only_free_transitions(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """`values` declared — enum check only, transitions between
    declared values are free."""
    engine = _build_engine_with_defs(
        conversation_store,
        {"role": LabelDef(values=["admin", "user", "guest"])},
        initial_labels={"role": "user"},
    )
    # Free transitions within the enum.
    engine.apply_label_writes({"role": "admin"})
    assert engine.labels["role"] == "admin"
    engine.apply_label_writes({"role": "guest"})
    assert engine.labels["role"] == "guest"
    # Out-of-enum still rejected.
    engine.apply_label_writes({"role": "root"})
    assert engine.labels["role"] == "guest"


def test_policy_project_label_write_forwards_membership_once(
    db_uri: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation()
    store.set_labels(conversation.id, {"omni_project": "Stale project"})
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("alice")
    permissions.grant("alice", conversation.id, LEVEL_OWNER)
    engine = PolicyEngine(
        policies=[],
        label_defs={"omni_project": LabelDef(values=["Different project"])},
        ask_timeout=30,
        conversation_id=conversation.id,
        initial_labels={"omni_project": "Stale project"},
        conversation_store=store,
    )

    with caplog.at_level(logging.WARNING):
        engine.apply_label_writes({"omni_project": "Policy forwarded", "other": "kept"})

    project = SqlAlchemyProjectStore(db_uri).get_by_name("Policy forwarded", "alice")
    assert project is not None
    persisted = store.get_conversation(conversation.id)
    assert persisted is not None
    assert persisted.labels == {"other": "kept"}
    with Session(get_or_create_engine(db_uri)) as session:
        metadata = session.get(SqlConversationMetadata, (0, conversation.id))
        snapshot = session.get(SqlSessionProjectSnapshot, (0, conversation.id))
    assert metadata is not None and metadata.project_id == project.id
    assert snapshot is not None and snapshot.project_id == project.id
    assert snapshot.snapshot_origin == "moved"
    assert (
        caplog.messages.count(
            "omni_project label is deprecated; forwarded to project_id — "
            "migrate to the project_id API."
        )
        == 1
    )
