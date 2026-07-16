"""Tests for :class:`omnigent.stores.git_credential_store.GitCredentialStore`."""

from __future__ import annotations

import traceback

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session

from omnigent.git_hosts.crypto import GitCredentialCipher
from omnigent.stores.git_credential_store import GitCredential, GitCredentialStore


def _store(tmp_path) -> GitCredentialStore:
    cipher = GitCredentialCipher([Fernet.generate_key().decode()])
    return GitCredentialStore(f"sqlite:///{tmp_path}/creds.db", cipher)


def test_create_returns_entity_without_secret(tmp_path) -> None:
    store = _store(tmp_path)
    cred = store.create(
        owner_user_id="alice",
        host_id="acme-forgejo",
        provider="forgejo",
        label="work",
        username="alice",
        token="ghp_secret",
    )
    assert isinstance(cred, GitCredential)
    assert cred.host_id == "acme-forgejo"
    assert cred.provider == "forgejo"
    assert cred.label == "work"
    # The entity must not expose the secret in any field.
    assert "ghp_secret" not in repr(cred)
    assert not hasattr(cred, "token")
    assert not hasattr(cred, "token_ciphertext")


def test_resolve_token_by_id_roundtrips(tmp_path) -> None:
    store = _store(tmp_path)
    cred = store.create(
        owner_user_id="alice",
        host_id="h",
        provider="forgejo",
        label="default",
        username=None,
        token="tok",
    )
    assert store.resolve_token(owner_user_id="alice", host_id="h", credential_id=cred.id) == "tok"
    assert (
        store.resolve_token(owner_user_id="alice", host_id="h", credential_id="nonexistent-id")
        is None
    )


def test_resolve_token_requires_matching_owner_and_host(tmp_path) -> None:
    store = _store(tmp_path)
    cred = store.create(
        owner_user_id="alice",
        host_id="acme-forgejo",
        provider="forgejo",
        label="work",
        username=None,
        token="s3cret",
    )
    # The full, correct authorization tuple decrypts.
    assert (
        store.resolve_token(owner_user_id="alice", host_id="acme-forgejo", credential_id=cred.id)
        == "s3cret"
    )
    # A different user who knows the id gets nothing (the id is not a capability).
    assert (
        store.resolve_token(owner_user_id="bob", host_id="acme-forgejo", credential_id=cred.id)
        is None
    )
    # The right owner but the wrong host also gets nothing.
    assert (
        store.resolve_token(owner_user_id="alice", host_id="other-host", credential_id=cred.id)
        is None
    )


def test_resolve_token_malformed_id_returns_none(tmp_path) -> None:
    store = _store(tmp_path)
    store.create(
        owner_user_id="alice",
        host_id="h",
        provider="forgejo",
        label="work",
        username=None,
        token="tok",
    )
    # A non-hex id addresses no row (InvalidUuidError path) -> None, not a raise.
    assert (
        store.resolve_token(owner_user_id="alice", host_id="h", credential_id="not-a-uuid") is None
    )


def test_multiple_identities_per_host_coexist(tmp_path) -> None:
    store = _store(tmp_path)
    work = store.create(
        owner_user_id="alice",
        host_id="h",
        provider="forgejo",
        label="work",
        username="alice-w",
        token="wtok",
    )
    personal = store.create(
        owner_user_id="alice",
        host_id="h",
        provider="forgejo",
        label="personal",
        username="alice-p",
        token="ptok",
    )
    candidates = store.list_for_owner_host("alice", "h")
    assert {c.label for c in candidates} == {"work", "personal"}
    assert store.resolve_token(owner_user_id="alice", host_id="h", credential_id=work.id) == "wtok"
    assert (
        store.resolve_token(owner_user_id="alice", host_id="h", credential_id=personal.id)
        == "ptok"
    )


def test_list_is_owner_scoped(tmp_path) -> None:
    store = _store(tmp_path)
    store.create(
        owner_user_id="alice",
        host_id="h1",
        provider="forgejo",
        label="default",
        username=None,
        token="a",
    )
    store.create(
        owner_user_id="bob",
        host_id="h2",
        provider="gitea",
        label="default",
        username=None,
        token="b",
    )
    alice = store.list_for_owner("alice")
    assert [c.host_id for c in alice] == ["h1"]


def test_duplicate_owner_host_label_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    store.create(
        owner_user_id="alice",
        host_id="h",
        provider="forgejo",
        label="work",
        username=None,
        token="a",
    )
    with pytest.raises(ValueError, match="already"):
        store.create(
            owner_user_id="alice",
            host_id="h",
            provider="forgejo",
            label="work",
            username=None,
            token="b",
        )


def test_create_non_integrity_statement_error_drops_ciphertext(tmp_path, monkeypatch) -> None:
    # A DB failure on INSERT that isn't a unique-constraint violation (e.g. a
    # DataError/OperationalError on a real backend) must not let the
    # ciphertext-bearing SQLAlchemy error message reach the caller or a
    # traceback log.
    store = _store(tmp_path)
    secret_marker = "very-secret-ciphertext-blob"

    def _boom(self: Session, *args: object, **kwargs: object) -> None:
        # The pre-check duplicate-label SELECT also triggers autoflush; only
        # the explicit flush() after session.add(row) has a pending insert.
        if not self.new:
            return
        message = (
            f"(driver.Error) value too long "
            f"[parameters: {{'token_ciphertext': {secret_marker!r}}}]"
        )
        raise StatementError(
            message,
            statement="INSERT INTO git_credentials ...",
            params={"token_ciphertext": secret_marker},
            orig=Exception("driver-level failure"),
        )

    monkeypatch.setattr(Session, "flush", _boom)

    with pytest.raises(RuntimeError) as exc_info:
        store.create(
            owner_user_id="alice",
            host_id="h",
            provider="forgejo",
            label="work",
            username=None,
            token="tok",
        )

    assert secret_marker not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    exc = exc_info.value
    formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert secret_marker not in formatted


def test_delete_then_absent(tmp_path) -> None:
    store = _store(tmp_path)
    cred = store.create(
        owner_user_id="alice",
        host_id="h",
        provider="forgejo",
        label="default",
        username=None,
        token="a",
    )
    store.delete(cred.id)
    assert store.get(cred.id) is None
    assert store.resolve_token(owner_user_id="alice", host_id="h", credential_id=cred.id) is None


def test_unknown_well_formed_id_returns_none(tmp_path) -> None:
    import uuid

    store = _store(tmp_path)
    absent = uuid.uuid4().hex  # valid Uuid16, but no such row
    assert store.get(absent) is None
    assert store.resolve_token(owner_user_id="alice", host_id="h", credential_id=absent) is None
    store.delete(absent)  # no-op, does not raise


def test_credentials_are_workspace_isolated(tmp_path) -> None:
    from omnigent.db.db_models import workspace_scope

    store = _store(tmp_path)
    with workspace_scope(1):
        cred = store.create(
            owner_user_id="alice",
            host_id="h",
            provider="forgejo",
            label="work",
            username=None,
            token="secret-w1",
        )
    # A different workspace must not see, resolve, or list workspace 1's credential.
    with workspace_scope(2):
        assert store.get(cred.id) is None
        assert (
            store.resolve_token(owner_user_id="alice", host_id="h", credential_id=cred.id) is None
        )
        assert store.list_for_owner("alice") == []
        assert store.list_for_owner_host("alice", "h") == []
    # Back in its own workspace it resolves normally.
    with workspace_scope(1):
        assert (
            store.resolve_token(owner_user_id="alice", host_id="h", credential_id=cred.id)
            == "secret-w1"
        )
