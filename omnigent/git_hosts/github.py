"""GitHub.com and GitHub Enterprise Server providers."""

from __future__ import annotations

from omnigent.git_hosts.base import GitHostProvider

DEFAULT_CLONE_USERNAME = "x-access-token"


class GitHubProvider(GitHostProvider):
    """github.com — the built-in default."""

    provider = "github"
    default_clone_username = DEFAULT_CLONE_USERNAME

    def matches(self, host: str) -> bool:
        return host == "github.com"

    def default_api_base(self, _web_host: str) -> str:
        return "https://api.github.com"


class GitHubEnterpriseProvider(GitHostProvider):
    """GitHub Enterprise Server — same API as github.com, operator-configured host.

    The API lives at ``<host>/api/v3`` (not ``api.<host>``); same-host git/API
    credential binding is a P2 concern (design §10).
    """

    provider = "ghe"
    default_clone_username = DEFAULT_CLONE_USERNAME

    def matches(self, _host: str) -> bool:
        return False

    def default_api_base(self, web_host: str) -> str:
        return f"https://{web_host}/api/v3"
