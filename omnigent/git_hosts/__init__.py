"""Data specs for supported git-host providers.

Self-hosted GitLab accepts a PAT or OAuth token as the HTTPS basic-auth password
for username ``oauth2`` and serves its REST API under ``<host>/api/v4``.
"""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.git_hosts.base import CloneAuthBinding

DEFAULT_CLONE_USERNAME = "x-access-token"


@dataclass(frozen=True)
class ProviderSpec:
    """Per-forge data: identity, API base pattern, and HTTPS auth username."""

    name: str
    clone_username: str
    api_base_template: str
    builtin_host: str | None = None

    def default_api_base(self, web_host: str) -> str:
        """Build the default API base for *web_host*."""
        return self.api_base_template.format(host=web_host)

    def clone_binding(self) -> CloneAuthBinding:
        """Build the provider's HTTPS clone authentication binding."""
        return CloneAuthBinding(scheme="basic", username=self.clone_username)


_PROVIDERS: dict[str, ProviderSpec] = {
    "github": ProviderSpec(
        "github",
        "x-access-token",
        "https://api.github.com",
        builtin_host="github.com",
    ),
    "ghe": ProviderSpec("ghe", "x-access-token", "https://{host}/api/v3"),
    "gitea": ProviderSpec("gitea", "oauth2", "https://{host}/api/v1"),
    "forgejo": ProviderSpec("forgejo", "oauth2", "https://{host}/api/v1"),
    "gitlab": ProviderSpec("gitlab", "oauth2", "https://{host}/api/v4"),
}


def provider_spec(provider: str) -> ProviderSpec:
    """Return the spec registered under *provider*.

    :param provider: A registered provider name, e.g. ``"forgejo"``.
    :returns: The provider's immutable data spec.
    :raises ValueError: When *provider* is not registered.
    """
    try:
        return _PROVIDERS[provider]
    except KeyError:
        raise ValueError(
            f"unknown git host provider {provider!r}; known: {available_providers()}"
        ) from None


def available_providers() -> list[str]:
    """The sorted list of registered provider names."""
    return sorted(_PROVIDERS)
