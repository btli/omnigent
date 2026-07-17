"""Forgejo / Gitea provider (both speak the Gitea API)."""

from __future__ import annotations

from omnigent.git_hosts.base import GitHostProvider


class GiteaProvider(GitHostProvider):
    """Forgejo and Gitea. Registered under both names; the API is ``<host>/api/v1``.

    Token-authenticated HTTPS clones use ``oauth2`` as their default username.
    """

    provider = "gitea"
    default_clone_username = "oauth2"

    def matches(self, _host: str) -> bool:
        return False

    def default_api_base(self, web_host: str) -> str:
        return f"https://{web_host}/api/v1"


class ForgejoProvider(GiteaProvider):
    """Forgejo — identical Gitea API; distinct provider identity."""

    provider = "forgejo"
