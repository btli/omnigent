"""
Tests for credential-label seeding in :func:`build_policy_engine`.

The build step projects the session's bound provider account into engine-owned
``credential.*`` labels (see :mod:`omnigent.subscription_tokens.labels`), keyed
by the **root** conversation id so a sub-agent inherits its session's
credential. These tests stub the always-safe facade
(``credential_labels_for_session``) to assert:

- a bound session seeds ``credential.kind/family/account`` into the engine hot
  cache and the persisted labels;
- an unbound session (or no pool configured) seeds nothing and never raises;
- a sub-agent resolves the binding by its ROOT id, not its own conversation id;
- the labels overwrite on change (so a reactive failover to the fallback
  account is reflected) but a non-credential label is left untouched.
"""

from __future__ import annotations

import pytest

from omnigent.runtime.policies.builder import build_policy_engine
from omnigent.spec.types import AgentSpec
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.subscription_tokens.labels import (
    CREDENTIAL_ACCOUNT_LABEL,
    CREDENTIAL_FAMILY_LABEL,
    CREDENTIAL_KIND_LABEL,
)

_SUB_LABELS = {
    CREDENTIAL_KIND_LABEL: "subscription",
    CREDENTIAL_FAMILY_LABEL: "anthropic",
    CREDENTIAL_ACCOUNT_LABEL: "pacct_pro_1",
}


def _patch_facade(monkeypatch: pytest.MonkeyPatch, fake: object) -> None:
    """Replace the builder's credential-label facade with *fake*.

    The builder imports ``credential_labels_for_session`` lazily from
    :mod:`omnigent.subscription_tokens.integration`, so patching the module
    attribute is seen at call time.
    """
    monkeypatch.setattr(
        "omnigent.subscription_tokens.integration.credential_labels_for_session",
        fake,
    )


def test_seeds_credential_labels_from_binding(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound session seeds the three credential labels into the hot cache
    and persists them — available even with a no-guardrails spec."""
    _patch_facade(monkeypatch, lambda _session_id: dict(_SUB_LABELS))
    conv = conversation_store.create_conversation()

    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="agent"),
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )

    for key, value in _SUB_LABELS.items():
        assert engine.labels[key] == value
    persisted = conversation_store.get_conversation(conv.id)
    assert persisted is not None
    for key, value in _SUB_LABELS.items():
        assert persisted.labels[key] == value


def test_unbound_session_seeds_nothing(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No binding / no pool (facade returns ``{}``) → no credential labels and
    no error."""
    _patch_facade(monkeypatch, lambda _session_id: {})
    conv = conversation_store.create_conversation()

    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="agent"),
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )

    assert not any(key.startswith("credential.") for key in engine.labels)


def test_subagent_resolves_binding_by_root_id(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-agent inherits its session's credential: the lookup is keyed by
    the ROOT id, so a facade that only knows the root id still seeds the
    child's labels (a child-id lookup would miss)."""
    root = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )

    # Binding is registered on the session top only.
    def fake(session_id: str) -> dict[str, str]:
        return dict(_SUB_LABELS) if session_id == root.id else {}

    _patch_facade(monkeypatch, fake)

    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="child"),
        conversation_id=child.id,
        conversation_store=conversation_store,
    )

    assert engine.labels[CREDENTIAL_KIND_LABEL] == "subscription"
    persisted = conversation_store.get_conversation(child.id)
    assert persisted is not None
    assert persisted.labels[CREDENTIAL_ACCOUNT_LABEL] == "pacct_pro_1"


def test_overwrites_on_failover(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second build after the binding rotates to the fallback account
    overwrites the credential labels (tracking reactive failover), while an
    unrelated label set in between is left untouched."""
    conv = conversation_store.create_conversation()

    _patch_facade(monkeypatch, lambda _session_id: dict(_SUB_LABELS))
    build_policy_engine(
        spec=AgentSpec(spec_version=1, name="agent"),
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    conversation_store.set_labels(conv.id, {"team": "ml"})

    # Failover: the session is now bound to the api_key fallback.
    failed_over = {
        CREDENTIAL_KIND_LABEL: "api_key",
        CREDENTIAL_FAMILY_LABEL: "anthropic",
        CREDENTIAL_ACCOUNT_LABEL: "pacct_api_fallback",
    }
    _patch_facade(monkeypatch, lambda _session_id: dict(failed_over))
    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="agent"),
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )

    assert engine.labels[CREDENTIAL_KIND_LABEL] == "api_key"
    assert engine.labels[CREDENTIAL_ACCOUNT_LABEL] == "pacct_api_fallback"
    # The unrelated label survives the credential overwrite.
    assert engine.labels["team"] == "ml"


def test_policy_set_labels_cannot_forge_credential(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``credential.*`` is engine-owned: a policy ``set_labels`` write to it is
    dropped by ``apply_label_writes`` (store + hot cache), so a policy can't
    forge the label mid-turn to neuter a credential-governance policy. An
    ordinary label in the same write still lands."""
    _patch_facade(monkeypatch, lambda _session_id: dict(_SUB_LABELS))
    conv = conversation_store.create_conversation()
    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="agent"),
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    assert engine.labels[CREDENTIAL_KIND_LABEL] == "subscription"

    # A policy attempts to forge the credential label alongside a real write.
    engine.apply_label_writes({CREDENTIAL_KIND_LABEL: "api_key", "team": "ml"})

    # The forged credential.* key is dropped; the ordinary label persists.
    assert engine.labels[CREDENTIAL_KIND_LABEL] == "subscription"
    assert engine.labels["team"] == "ml"
    persisted = conversation_store.get_conversation(conv.id)
    assert persisted is not None
    assert persisted.labels[CREDENTIAL_KIND_LABEL] == "subscription"
    assert persisted.labels["team"] == "ml"
