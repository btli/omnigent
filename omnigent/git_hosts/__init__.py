"""Git-host provider abstraction (design docs/claude/custom-git-hosts-design.md).

The registry maps a provider name to a ``"module:ClassName"`` target imported
lazily on first use — mirroring :mod:`omnigent.onboarding.sandboxes` — so a
provider's optional dependencies never load unless it is selected.
"""

from __future__ import annotations

import importlib

from omnigent.git_hosts.base import GitHostProvider

# Provider name → "module:ClassName". Closed set; forgejo and gitea share one
# implementation (identical API).
_GIT_HOSTS: dict[str, str] = {
    "github": "omnigent.git_hosts.github:GitHubProvider",
    "ghe": "omnigent.git_hosts.github:GitHubEnterpriseProvider",
    "gitea": "omnigent.git_hosts.gitea:GiteaProvider",
    "forgejo": "omnigent.git_hosts.gitea:ForgejoProvider",
}


def get_git_host(provider: str) -> GitHostProvider:
    """Instantiate the provider registered under *provider*.

    :param provider: A registered provider name, e.g. ``"forgejo"``.
    :returns: A fresh provider instance.
    :raises ValueError: When *provider* is not registered.
    """
    try:
        target = _GIT_HOSTS[provider]
    except KeyError:
        raise ValueError(
            f"unknown git host provider {provider!r}; known: {available_providers()}"
        ) from None
    module_path, _, class_name = target.partition(":")
    module = importlib.import_module(module_path)
    provider_cls: type[GitHostProvider] = getattr(module, class_name)
    return provider_cls()


def available_providers() -> list[str]:
    """The sorted list of registered provider names."""
    return sorted(_GIT_HOSTS)
