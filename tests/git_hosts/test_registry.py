"""Tests for the git-host provider specs."""

from __future__ import annotations

import re

import pytest

import omnigent.git_hosts as git_hosts
from omnigent.git_hosts.base import CloneAuthBinding


def test_available_providers_lists_known_names() -> None:
    assert git_hosts.available_providers() == ["forgejo", "ghe", "gitea", "github", "gitlab"]


@pytest.mark.parametrize(
    ("name", "host", "api_base", "username", "builtin_host"),
    [
        ("github", "github.com", "https://api.github.com", "x-access-token", "github.com"),
        ("ghe", "ghe.acme.com", "https://ghe.acme.com/api/v3", "x-access-token", None),
        ("gitea", "git.acme.com", "https://git.acme.com/api/v1", "oauth2", None),
        ("forgejo", "git.acme.com", "https://git.acme.com/api/v1", "oauth2", None),
        ("gitlab", "gitlab.acme.com", "https://gitlab.acme.com/api/v4", "oauth2", None),
    ],
)
def test_provider_specs_preserve_forge_defaults(
    name: str,
    host: str,
    api_base: str,
    username: str,
    builtin_host: str | None,
) -> None:
    spec = git_hosts.provider_spec(name)
    assert spec.name == name
    assert spec.default_api_base(host) == api_base
    assert spec.clone_binding() == CloneAuthBinding(scheme="basic", username=username)
    assert spec.builtin_host == builtin_host


def test_provider_spec_unknown_name_raises_with_known_set() -> None:
    known = ["forgejo", "ghe", "gitea", "github", "gitlab"]
    expected = f"unknown git host provider 'svn'; known: {known}"
    with pytest.raises(ValueError, match=re.escape(expected)):
        git_hosts.provider_spec("svn")
