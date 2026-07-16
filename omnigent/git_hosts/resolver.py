"""Resolve a repository URL + operator host config into a non-secret ``ClonePlan``.

Operator hosts are matched by exact canonical host; github.com falls back to the
built-in provider (legacy ``GIT_TOKEN``). Any other host is rejected — topology
is operator-only (design §7, §12.3). This function has no request identity; the
owner-aware wiring (which credential slot to use) is a later plan.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnigent.git_hosts import get_git_host
from omnigent.git_hosts.base import ClonePlan, HostConfig
from omnigent.git_hosts.url import split_host


def resolve_clone_plan(url: str, hosts: Sequence[HostConfig]) -> ClonePlan:
    """Resolve *url* against operator-configured *hosts*.

    :param url: A managed repository URL (``https://`` or ``git@`` form), optionally
        with a ``#<branch>`` fragment; the fragment never reaches the clone URL.
    :param hosts: The operator-configured hosts (from ``load_git_hosts``).
    :returns: The resolved :class:`ClonePlan`.
    :raises ValueError: When the URL form is unsupported, or its host is neither
        configured nor github.com.
    """
    # The workspace form may carry a "#<branch>" fragment; the clone URL never
    # does (the branch is tracked separately, as in RepoWorkspace).
    clone_url = url.partition("#")[0]
    host = split_host(url)
    for cfg in hosts:
        if cfg.web_host == host:
            provider = get_git_host(cfg.provider)
            return ClonePlan(
                provider=cfg.provider,
                host_id=cfg.id,
                canonical_host=host,
                normalized_url=provider.normalize_repo_url(clone_url),
                api_base=cfg.api_base,
                auth=provider.clone_binding(),
                credential_source=cfg.credential_source,
                ssh_host=cfg.ssh_host,
                ssh_port=cfg.ssh_port,
                ca_bundle=cfg.ca_bundle,
            )

    github = get_git_host("github")
    if github.matches(host):
        return ClonePlan(
            provider="github",
            host_id="github",
            canonical_host=host,
            normalized_url=github.normalize_repo_url(clone_url),
            api_base=github.default_api_base(host),
            auth=github.clone_binding(),
            credential_source=None,
        )

    raise ValueError(
        f"no configured git host for {host!r}; operators register hosts under the "
        "server-config 'git_hosts' key"
    )
