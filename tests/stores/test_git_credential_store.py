"""Tests for :class:`omnigent.stores.git_credential_store.GitCredentialStore`."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

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
    assert store.resolve_token(cred.id) == "tok"
    assert store.resolve_token("nonexistent-id") is None


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
    assert store.resolve_token(work.id) == "wtok"
    assert store.resolve_token(personal.id) == "ptok"


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
    assert store.resolve_token(cred.id) is None
