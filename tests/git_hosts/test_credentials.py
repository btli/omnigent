"""Tests for :mod:`omnigent.git_hosts.credentials`."""

from __future__ import annotations

import pytest

from omnigent.git_hosts.credentials import parse_credential_source, resolve_credential
from omnigent.inner.datamodel import CredentialSourceSpec


def test_parses_env_file_and_command_refs() -> None:
    assert parse_credential_source("env:ACME_TOKEN") == CredentialSourceSpec(
        kind="env", env="ACME_TOKEN"
    )
    assert parse_credential_source("file:/run/secrets/acme") == CredentialSourceSpec(
        kind="file", path="/run/secrets/acme"
    )
    assert parse_credential_source("command:pass show acme") == CredentialSourceSpec(
        kind="command", command="pass show acme"
    )


@pytest.mark.parametrize("ref", ["", "env:", "file:", "command:", "vault:xyz", "ACME_TOKEN"])
def test_rejects_malformed_refs(ref: str) -> None:
    with pytest.raises(ValueError, match="credential_source"):
        parse_credential_source(ref)


def test_resolve_credential_env_roundtrip() -> None:
    assert resolve_credential("env:ACME_TOKEN", parent_env={"ACME_TOKEN": "s3cret"}) == "s3cret"


def test_resolve_credential_missing_env_raises() -> None:
    with pytest.raises(ValueError):
        resolve_credential("env:ACME_TOKEN", parent_env={})
