"""Resolve an operator ``credential_source`` reference to a secret value.

Operator host config carries a compact reference string (``"env:NAME"``,
``"file:PATH"``, ``"command:CMD"``) — never a secret. This module bridges that
string onto the existing :class:`CredentialSourceSpec` /
:func:`_resolve_secret` machinery so lookup semantics (strip, fail on
missing/empty, 30s command timeout) stay single-sourced.
"""

from __future__ import annotations

from omnigent.inner.credential_proxy import _resolve_secret
from omnigent.inner.datamodel import CredentialSourceSpec


def parse_credential_source(ref: str) -> CredentialSourceSpec:
    """Parse a ``"<kind>:<value>"`` credential reference.

    :param ref: e.g. ``"env:ACME_TOKEN"``.
    :returns: The equivalent :class:`CredentialSourceSpec`.
    :raises ValueError: When the kind is unknown or the value is empty.
    """
    kind, sep, value = ref.partition(":")
    if not sep or not value:
        raise ValueError(
            f"credential_source {ref!r} must be '<kind>:<value>' with kind one of "
            "env, file, command"
        )
    if kind == "env":
        return CredentialSourceSpec(kind="env", env=value)
    if kind == "file":
        return CredentialSourceSpec(kind="file", path=value)
    if kind == "command":
        return CredentialSourceSpec(kind="command", command=value)
    raise ValueError(
        f"credential_source kind {kind!r} is not supported; use env, file, or command"
    )


def resolve_credential(ref: str, *, parent_env: dict[str, str]) -> str:
    """Resolve *ref* to its secret value in the trusted server process.

    :param ref: A reference accepted by :func:`parse_credential_source`.
    :param parent_env: The environment to resolve ``env:``/``command:`` against.
    :returns: The non-empty secret value.
    :raises ValueError: When the reference is malformed or the source is
        missing/empty.
    """
    return _resolve_secret(parse_credential_source(ref), parent_env=parent_env)
