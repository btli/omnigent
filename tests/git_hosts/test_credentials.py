"""Tests for :mod:`omnigent.credential_sources`."""

from __future__ import annotations

import pytest

from omnigent.credential_sources import (
    CredentialSourceSpec,
    parse_credential_source,
    resolve_credential,
)


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


def test_malformed_ref_error_never_echoes_the_raw_value() -> None:
    with pytest.raises(ValueError) as exc:
        parse_credential_source("my-secret-value")
    assert "my-secret-value" not in str(exc.value)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("env: ", CredentialSourceSpec(kind="env", env=" ")),
        ("env:\n", CredentialSourceSpec(kind="env", env="\n")),
        ("env:A:B", CredentialSourceSpec(kind="env", env="A:B")),
        ("file: relative path ", CredentialSourceSpec(kind="file", path=" relative path ")),
        ("command: ", CredentialSourceSpec(kind="command", command=" ")),
    ],
)
def test_parse_acceptance_contract_preserves_nonempty_suffix_verbatim(
    ref: str, expected: CredentialSourceSpec
) -> None:
    assert parse_credential_source(ref) == expected


@pytest.mark.parametrize(
    "ref",
    [
        ":value",
        "ENV:NAME",
        " env:NAME",
        "env",
        "file",
        "command",
    ],
)
def test_parse_rejection_contract_remains_fail_closed(ref: str) -> None:
    with pytest.raises(ValueError):
        parse_credential_source(ref)


def test_resolve_credential_env_roundtrip() -> None:
    assert resolve_credential("env:ACME_TOKEN", parent_env={"ACME_TOKEN": "s3cret"}) == "s3cret"


def test_resolve_credential_accepts_parsed_source() -> None:
    source = CredentialSourceSpec(kind="env", env="ACME_TOKEN")
    assert resolve_credential(source, parent_env={"ACME_TOKEN": " s3cret\n"}) == "s3cret"


def test_resolve_credential_missing_env_raises() -> None:
    with pytest.raises(ValueError):
        resolve_credential("env:ACME_TOKEN", parent_env={})
