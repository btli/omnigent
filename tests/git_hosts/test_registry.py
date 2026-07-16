"""Tests for the git-host provider registry."""

from __future__ import annotations

import pytest

from omnigent.git_hosts import available_providers, get_git_host
from omnigent.git_hosts.gitea import GiteaProvider
from omnigent.git_hosts.github import GitHubProvider


def test_available_providers_lists_known_names() -> None:
    assert available_providers() == ["forgejo", "ghe", "gitea", "github"]


def test_get_git_host_returns_provider_instance() -> None:
    assert isinstance(get_git_host("github"), GitHubProvider)
    # Forgejo and Gitea share the Gitea API implementation.
    assert isinstance(get_git_host("forgejo"), GiteaProvider)
    assert isinstance(get_git_host("gitea"), GiteaProvider)


def test_get_git_host_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown git host provider 'svn'"):
        get_git_host("svn")


def test_github_matches_only_github_com() -> None:
    gh = GitHubProvider()
    assert gh.matches("github.com") is True
    assert gh.matches("git.acme.com") is False
    assert gh.default_api_base("github.com") == "https://api.github.com"


def test_enterprise_and_gitea_default_api_bases() -> None:
    assert get_git_host("ghe").default_api_base("ghe.acme.com") == "https://ghe.acme.com/api/v3"
    assert get_git_host("gitea").default_api_base("git.acme.com") == "https://git.acme.com/api/v1"
    # Self-hosted providers never auto-match a host; they are selected by config.
    assert get_git_host("ghe").matches("ghe.acme.com") is False
    assert get_git_host("gitea").matches("git.acme.com") is False
