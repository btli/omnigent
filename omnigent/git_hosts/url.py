"""Host extraction and credential path scoping for git repository URLs.

Supports the two managed-clone URL forms (matching
:func:`omnigent.git_hosts.managed_workspace.parse_repo_workspace`):
``https://<host>/<path>``
and scp-style ``git@<host>:<path>``. Rejects embedded userinfo (a
credential-in-URL smuggling vector) and explicit ``https`` ports or IPv6
literal hosts — custom ports and SSH transport land with the clone-wiring
plan (design §9).
"""

from __future__ import annotations

import re

_GIT_PATH_SAFE = re.compile(r"\A[A-Za-z0-9._~/-]+\Z")


def managed_repo_path(url: str) -> str:
    """Derive the repo-path prefix an egress rule scopes to from a clone URL.

    :param url: The resolved clone URL, e.g. ``"https://git.acme.com/team/proj.git"``.
    :returns: A leading-slash path with no ``.git`` / trailing slash, e.g.
        ``"/team/proj"``; ``""`` when the URL carries no path.
    """
    from urllib.parse import urlparse

    path = urlparse(url).path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path


def managed_repo_path_allows(request_path: str, repo_path: str) -> bool:
    """Whether a repo-scoped credential may attach to *request_path*.

    Fails closed via an allowlist: a legit git smart-HTTP path is plain ASCII
    within ``[A-Za-z0-9._~/-]`` (repo/namespace names + fixed endpoints; the
    ``?query`` is stripped first), so anything with ``%`` (any/double
    percent-encoding), ``\\``, ``;`` (matrix params), a control/null byte, or a
    non-ASCII char — every path a forge might normalize to escape the prefix —
    is rejected in one rule. Then the ``..``-segment and repo-prefix checks
    (bare and ``.git``) pin it to this repository.

    The prefix match is case-sensitive: a case-variant path on a
    case-insensitive forge declines the swap (fail-closed, functional-only —
    git uses the clone URL's exact case).
    """
    if not repo_path:
        # An empty prefix would make every path "within" the repo — refuse
        # rather than blanket-attach. Unreachable via the os_env install
        # (it fails closed on an empty repo_path), but this is the security
        # boundary; it must never depend on the caller for that.
        return False
    target = request_path.split("?", 1)[0]
    if not _GIT_PATH_SAFE.match(target):
        return False
    if ".." in target.split("/"):
        return False
    for base in (repo_path, f"{repo_path}.git"):
        if target == base or target.startswith(f"{base}/"):
            return True
    return False


def split_host(url: str) -> str:
    """Return the canonical lowercase host of a git repository URL.

    :param url: An ``https://<host>/<path>`` or ``git@<host>:<path>`` URL,
        optionally with a ``#<branch>`` fragment.
    :returns: The lowercase host, e.g. ``"git.acme.com"``.
    :raises ValueError: When the URL form is unsupported, embeds userinfo, or
        specifies an explicit port or IPv6 literal host.
    """
    if url.startswith("https://"):
        authority = url[len("https://") :].split("/", 1)[0]
        if "@" in authority:
            raise ValueError("a repository URL must not embed userinfo (user[:password]@host)")
        if "[" in authority:
            raise ValueError(f"'{url}': IPv6 literal hosts are not supported")
        if ":" in authority:
            raise ValueError(f"'{url}': custom ports are not supported yet")
        host = authority
    elif url.startswith("git@"):
        rest = url[len("git@") :]
        host, sep, path = rest.partition(":")
        if not sep:
            raise ValueError(
                f"'{url}' is not a usable ssh repository URL — expected 'git@<host>:<path>'"
            )
        if "@" in host or "@" in path.split("/", 1)[0]:
            raise ValueError("a repository URL must not embed userinfo (user[:password]@host)")
    else:
        raise ValueError(
            f"'{url}' is not a supported repository URL — use "
            "'https://<host>/<path>' or 'git@<host>:<path>'"
        )
    if not host:
        raise ValueError(f"could not extract a host from '{url}'")
    return host.lower()
