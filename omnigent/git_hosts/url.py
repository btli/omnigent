"""Canonical-host extraction for git repository URLs.

Supports the two managed-clone URL forms (matching
:func:`omnigent.server.managed_hosts.parse_repo_workspace`): ``https://<host>/<path>``
and scp-style ``git@<host>:<path>``. Rejects embedded userinfo (a
credential-in-URL smuggling vector). Port is dropped for P1a — custom ports and
SSH transport land with the clone-wiring plan (design §9).
"""

from __future__ import annotations


def split_host(url: str) -> str:
    """Return the canonical lowercase host of a git repository URL.

    :param url: An ``https://<host>/<path>`` or ``git@<host>:<path>`` URL,
        optionally with a ``#<branch>`` fragment.
    :returns: The lowercase host, e.g. ``"git.acme.com"``.
    :raises ValueError: When the URL form is unsupported or embeds userinfo.
    """
    if url.startswith("https://"):
        authority = url[len("https://") :].split("/", 1)[0]
        if "@" in authority:
            raise ValueError("a repository URL must not embed userinfo (user[:password]@host)")
        host = authority.split(":", 1)[0]  # drop any :port
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
