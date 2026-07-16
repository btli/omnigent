"""Tests for :mod:`omnigent.git_hosts.crypto`."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from omnigent.git_hosts.crypto import GitCredentialCipher, load_cipher_from_env


def test_encrypt_decrypt_roundtrip() -> None:
    cipher = GitCredentialCipher([Fernet.generate_key().decode()])
    token = cipher.encrypt("ghp_secret")
    assert token != "ghp_secret"  # ciphertext, not plaintext
    assert cipher.decrypt(token) == "ghp_secret"


def test_empty_key_list_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        GitCredentialCipher([])


def test_key_rotation_old_key_still_decrypts() -> None:
    old = Fernet.generate_key().decode()
    new = Fernet.generate_key().decode()
    old_cipher = GitCredentialCipher([old])
    token = old_cipher.encrypt("s3cret")
    # New primary key first, old key retained -> can still decrypt the old token,
    # and new writes use the new key.
    rotated = GitCredentialCipher([new, old])
    assert rotated.decrypt(token) == "s3cret"
    fresh = rotated.encrypt("s3cret")
    assert GitCredentialCipher([new]).decrypt(fresh) == "s3cret"


def test_decrypt_unknown_token_raises() -> None:
    cipher = GitCredentialCipher([Fernet.generate_key().decode()])
    other = GitCredentialCipher([Fernet.generate_key().decode()]).encrypt("x")
    with pytest.raises(InvalidToken):
        cipher.decrypt(other)


def test_load_cipher_from_env_absent_returns_none() -> None:
    assert load_cipher_from_env({}) is None
    assert load_cipher_from_env({"OMNIGENT_GIT_CREDENTIAL_KEYS": "  "}) is None


def test_load_cipher_from_env_parses_key_list() -> None:
    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    cipher = load_cipher_from_env({"OMNIGENT_GIT_CREDENTIAL_KEYS": f"{k1}, {k2}"})
    assert cipher is not None
    assert cipher.decrypt(cipher.encrypt("v")) == "v"


def test_load_cipher_from_env_malformed_key_raises() -> None:
    with pytest.raises(RuntimeError, match="OMNIGENT_GIT_CREDENTIAL_KEYS"):
        load_cipher_from_env({"OMNIGENT_GIT_CREDENTIAL_KEYS": "not-a-valid-fernet-key"})
