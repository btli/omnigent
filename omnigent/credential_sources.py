"""Parse and resolve credential-source references in the trusted parent process."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_COMMAND_SOURCE_TIMEOUT_SECONDS = 30


@dataclass
class CredentialSourceSpec:
    """Where the parent process resolves a real secret from.

    The secret is resolved in the *parent* (trusted) process and never
    handed to the sandbox verbatim — only a synthetic placeholder is.

    :param kind: Resolution mode, one of ``"env"``, ``"file"``, or
        ``"command"``.
    :param env: Environment-variable name carrying the secret when
        ``kind="env"``, e.g. ``"OA_TEST_GITHUB_PAT"``.
    :param path: File path to read when ``kind="file"`` (``~`` is
        expanded), e.g. ``"~/.config/tokens/github_pat.txt"``.
    :param command: Shell command whose stdout is the secret when
        ``kind="command"``, e.g. ``"gh auth token"``.
    """

    kind: Literal["env", "file", "command"]
    env: str | None = None
    path: str | None = None
    command: str | None = None


def parse_credential_source(ref: str) -> CredentialSourceSpec:
    """Parse a ``"<kind>:<value>"`` credential reference.

    :param ref: e.g. ``"env:ACME_TOKEN"``.
    :returns: The equivalent :class:`CredentialSourceSpec`.
    :raises ValueError: When the kind is unknown or the value is empty.
    """
    kind, sep, value = ref.partition(":")
    if not sep or not value:
        raise ValueError(
            "credential_source must be '<kind>:<value>' with kind one of env, file, command"
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


def resolve_credential(
    source: str | CredentialSourceSpec,
    *,
    parent_env: dict[str, str],
) -> str:
    """Resolve a credential source to its non-empty secret value.

    :param source: A parsed source or a reference accepted by
        :func:`parse_credential_source`.
    :param parent_env: Parent process environment for ``env`` lookups and
        as the environment for ``command`` execution.
    :returns: The resolved secret with surrounding whitespace stripped.
    :raises ValueError: If the source is malformed, misconfigured,
        missing, empty, or (for ``command``) exits non-zero.
    """
    if isinstance(source, str):
        source = parse_credential_source(source)

    if source.kind == "env":
        if not source.env:
            raise ValueError("credential_proxy env source requires an 'env' name")
        value = parent_env.get(source.env)
        if value is None or not value.strip():
            raise ValueError(f"credential_proxy env source {source.env!r} is missing or empty")
        return value.strip()
    if source.kind == "file":
        if not source.path:
            raise ValueError("credential_proxy file source requires a 'path'")
        path = Path(os.path.expanduser(source.path))
        if not path.is_file():
            raise ValueError(f"credential_proxy file source does not exist: {path}")
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"credential_proxy file source is empty: {path}")
        return value
    if source.kind == "command":
        if not source.command:
            raise ValueError("credential_proxy command source requires a 'command'")
        # ``shell=True`` is intentional: the command is spec-author supplied and
        # runs in the trusted parent process, never inside the sandbox.
        completed = subprocess.run(
            source.command,
            shell=True,
            capture_output=True,
            text=True,
            env=parent_env,
            timeout=_COMMAND_SOURCE_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise ValueError(
                f"credential_proxy command source exited {completed.returncode}"
                + (f": {stderr}" if stderr else "")
            )
        value = completed.stdout.strip()
        if not value:
            raise ValueError("credential_proxy command source produced empty stdout")
        return value
    raise ValueError(f"unsupported credential_proxy source kind: {source.kind!r}")


__all__ = [
    "CredentialSourceSpec",
    "parse_credential_source",
    "resolve_credential",
]
