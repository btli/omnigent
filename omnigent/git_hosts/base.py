"""Git-host provider abstraction: identity, URL normalization, and the
resolved, non-secret clone plan a launcher consumes.

A ``GitHostProvider`` is the per-forge behavior seam (github.com, GitHub
Enterprise, Forgejo/Gitea, …), mirroring the sandbox-launcher registry in
:mod:`omnigent.onboarding.sandboxes`. This module holds the abstract base and
the plain value types; concrete providers live in sibling modules and are
registered in :mod:`omnigent.git_hosts`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class HostConfig:
    """An operator-configured git host — topology only, never a secret.

    Built by :func:`omnigent.git_hosts.config.load_git_hosts`. ``credential_source``
    is a *reference* (e.g. ``"env:NAME"``), resolved later in the trusted parent —
    never a secret value.

    :param id: Operator-assigned stable id, e.g. ``"acme-forgejo"``.
    :param provider: Registered provider name, e.g. ``"forgejo"``.
    :param web_host: Canonical lowercase host, e.g. ``"git.acme.com"``.
    :param api_base: API base URL, e.g. ``"https://git.acme.com/api/v1"``.
    :param credential_source: Reference-source descriptor, e.g. ``"env:ACME_TOKEN"``.
    :param ssh_host: Optional SSH host override.
    :param ssh_port: Optional SSH port override.
    :param ca_bundle: Optional path to a CA bundle for a private forge.
    """

    id: str
    provider: str
    web_host: str
    api_base: str
    credential_source: str
    ssh_host: str | None = None
    ssh_port: int | None = None
    ca_bundle: str | None = None


@dataclass(frozen=True)
class CloneAuthBinding:
    """How a provider authenticates an HTTPS git operation.

    :param scheme: The git-credential scheme, ``"basic"`` or ``"token"``.
    :param username: The HTTPS basic-auth username the forge expects
        (GitHub: ``"x-access-token"``; GitLab: ``"oauth2"``).
    """

    scheme: str
    username: str


@dataclass(frozen=True)
class ClonePlan:
    """The resolved, non-secret plan for cloning one repository.

    Produced by :func:`omnigent.git_hosts.resolver.resolve_clone_plan`; consumed
    by the launcher clone path (a later plan). Carries no secret — only a
    ``credential_source`` reference the trusted parent resolves.

    :param provider: Provider name from operator config, or ``"github"`` for the
        built-in default.
    :param host_id: Operator host id, or ``"github"`` for the built-in default.
    :param canonical_host: Canonical lowercase host.
    :param normalized_url: The clone URL after provider normalization.
    :param api_base: API base URL for this host.
    :param auth: The HTTPS auth binding.
    :param credential_source: Reference-source descriptor, or ``None`` for the
        built-in github.com default (which uses the legacy ``GIT_TOKEN``).
    """

    provider: str
    host_id: str
    canonical_host: str
    normalized_url: str
    api_base: str
    auth: CloneAuthBinding
    credential_source: str | None
    ssh_host: str | None = None
    ssh_port: int | None = None
    ca_bundle: str | None = None


class GitHostProvider(ABC):
    """Per-forge behavior: host identity, URL normalization, and auth shape.

    Subclasses set ``provider`` and ``default_clone_username`` and implement
    :meth:`matches` and :meth:`default_api_base`. Later phases extend this with
    MCP, egress, and OAuth hooks.
    """

    provider: ClassVar[str]
    default_clone_username: ClassVar[str]

    @abstractmethod
    def matches(self, host: str) -> bool:
        """Whether this provider serves *host* (canonical lowercase).

        Used only for the built-in default (github.com); operator-configured
        hosts select their provider explicitly by name, so self-hosted providers
        return ``False`` here.
        """

    @abstractmethod
    def default_api_base(self, web_host: str) -> str:
        """The API base URL for *web_host* when the operator omits one."""

    def normalize_repo_url(self, url: str) -> str:
        """Return the clone URL, normalized. Default: unchanged."""
        return url

    def clone_binding(self) -> CloneAuthBinding:
        """The HTTPS auth shape for this provider (default: basic + username)."""
        return CloneAuthBinding(scheme="basic", username=self.default_clone_username)
