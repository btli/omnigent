"""Non-secret git-host configuration and resolved clone-plan value types."""

from __future__ import annotations

from dataclasses import dataclass


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
    by the launcher clone path. Carries no secret — only a
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
