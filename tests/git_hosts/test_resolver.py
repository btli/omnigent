"""Tests for :func:`omnigent.git_hosts.resolver.resolve_clone_plan`."""

from __future__ import annotations

import re

import pytest

from omnigent.git_hosts.base import CloneAuthBinding, HostConfig
from omnigent.git_hosts.resolver import resolve_clone_plan

_ACME = HostConfig(
    id="acme-forgejo",
    provider="forgejo",
    web_host="git.acme.com",
    api_base="https://git.acme.com/api/v1",
    credential_source="env:ACME_FORGEJO_TOKEN",
)


def test_resolves_a_configured_host() -> None:
    plan = resolve_clone_plan("https://git.acme.com/team/proj#main", [_ACME])
    assert plan.provider == "forgejo"
    assert plan.host_id == "acme-forgejo"
    assert plan.canonical_host == "git.acme.com"
    assert plan.normalized_url == "https://git.acme.com/team/proj"
    assert plan.api_base == "https://git.acme.com/api/v1"
    assert plan.credential_source == "env:ACME_FORGEJO_TOKEN"
    assert plan.auth == CloneAuthBinding(scheme="basic", username="oauth2")


def test_resolves_a_configured_gitlab_host() -> None:
    gitlab = HostConfig(
        id="acme-gitlab",
        provider="gitlab",
        web_host="gitlab.acme.com",
        api_base="https://gitlab.acme.com/api/v4",
        credential_source="env:ACME_GITLAB_TOKEN",
    )
    plan = resolve_clone_plan("https://gitlab.acme.com/team/proj", [gitlab])
    assert plan.provider == "gitlab"
    assert plan.host_id == "acme-gitlab"
    assert plan.api_base == "https://gitlab.acme.com/api/v4"
    assert plan.auth == CloneAuthBinding(scheme="basic", username="oauth2")


def test_github_com_falls_back_to_builtin_default() -> None:
    plan = resolve_clone_plan("https://github.com/org/repo", [_ACME])
    assert plan.provider == "github"
    assert plan.host_id == "github"
    assert plan.api_base == "https://api.github.com"
    # Built-in default carries no per-host credential source (legacy GIT_TOKEN).
    assert plan.credential_source is None
    assert plan.auth == CloneAuthBinding(scheme="basic", username="x-access-token")


def test_unconfigured_non_github_host_is_rejected() -> None:
    expected_error = "no configured git host for 'git.unknown.com'"
    with pytest.raises(ValueError, match=re.escape(expected_error)):
        resolve_clone_plan("https://git.unknown.com/team/proj", [_ACME])


def test_host_match_is_case_insensitive() -> None:
    plan = resolve_clone_plan("https://Git.Acme.com/team/proj", [_ACME])
    assert plan.host_id == "acme-forgejo"


def test_resolves_scp_form_url() -> None:
    plan = resolve_clone_plan("git@git.acme.com:team/proj.git", [_ACME])
    assert plan.host_id == "acme-forgejo"
    assert plan.normalized_url == "git@git.acme.com:team/proj.git"
