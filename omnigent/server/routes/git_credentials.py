"""Routes for per-user git credential CRUD (``/v1/git-credentials``).

Credentials are encrypted at rest by :class:`GitCredentialStore` and scoped
to the authenticated caller. ``provider`` is derived server-side from the
matching :class:`HostConfig` in ``app.state.git_hosts`` — the client only
names a ``host_id``. The response shape never carries the token: neither
the create nor the list response include it, and :class:`GitCredential`
itself has no token field to leak.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.git_hosts.base import HostConfig
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.git_credential_store import (
    GitCredential,
    GitCredentialDeleteResult,
    GitCredentialStore,
)


class CreateGitCredentialRequest(BaseModel):
    """Request body for ``POST /v1/git-credentials``.

    :param host_id: The operator-configured host to store a credential
        for, e.g. ``"acme-forgejo"``. Must match a :class:`HostConfig`
        in ``app.state.git_hosts``.
    :param label: Caller-chosen name distinguishing this identity from
        others on the same host, e.g. ``"work"``. Unique per
        ``(owner, host_id)``.
    :param token: The secret credential value. Encrypted immediately by
        the store and never echoed back.
    """

    model_config = ConfigDict(extra="forbid")

    host_id: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1, max_length=128)
    token: str = Field(min_length=1, max_length=8192)


def _find_host(git_hosts: tuple[HostConfig, ...], host_id: str) -> HostConfig | None:
    """Return the :class:`HostConfig` matching *host_id*, or ``None``.

    :param git_hosts: Operator-configured hosts from ``app.state.git_hosts``.
    :param host_id: The id to look up, e.g. ``"acme-forgejo"``.
    :returns: The matching host, or ``None`` if no host has that id.
    """
    for host in git_hosts:
        if host.id == host_id:
            return host
    return None


def _entity_to_response(cred: GitCredential) -> dict[str, Any]:
    """Convert a :class:`GitCredential` entity to its API response shape.

    Deliberately omits ``owner_user_id`` and ``updated_at`` (not part of
    the documented response) and, like the entity itself, never carries
    the secret token.

    :param cred: The entity to convert.
    :returns: A dict with ``id, host_id, provider, label, created_at``.
    """
    return {
        "id": cred.id,
        "host_id": cred.host_id,
        "provider": cred.provider,
        "label": cred.label,
        "created_at": cred.created_at,
    }


def create_git_credentials_router(
    git_credential_store: GitCredentialStore,
    git_hosts: tuple[HostConfig, ...],
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for git-credential REST endpoints.

    Mounted with ``prefix="/v1"`` so paths are ``/v1/git-credentials...``.

    :param git_credential_store: The shared :class:`GitCredentialStore`
        instance.
    :param git_hosts: Operator-configured git hosts, used to derive
        ``provider`` from the caller-supplied ``host_id`` and to reject
        an unknown one.
    :param auth_provider: Auth provider used to identify the requesting
        user. ``None`` in single-user mode, where credentials are owned
        by the reserved ``"local"`` user.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _credential_owner(request: Request) -> str:
        """Return the credential owner for the current auth mode."""
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    @router.post("/git-credentials")
    async def create_credential(
        request: Request,
        body: CreateGitCredentialRequest,
    ) -> dict[str, Any]:
        """Create a new git credential for the authenticated user.

        :param request: The incoming request, used to extract the user
            identity.
        :param body: Credential payload including ``host_id``, ``label``,
            and ``token``.
        :returns: The created credential's metadata (never the token).
        :raises OmnigentError: 401 if unauthenticated, 400 if ``host_id``
            does not match a configured host, or 409 if this owner
            already has a credential with that ``(host_id, label)``.
        """
        owner_user_id = _credential_owner(request)
        host = _find_host(git_hosts, body.host_id)
        if host is None:
            raise OmnigentError(
                f"Unknown git host {body.host_id!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        try:
            cred = await asyncio.to_thread(
                git_credential_store.create,
                owner_user_id=owner_user_id,
                host_id=body.host_id,
                provider=host.provider,
                label=body.label,
                token=body.token,
            )
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc
        return _entity_to_response(cred)

    @router.get("/git-credentials")
    async def list_credentials(request: Request) -> dict[str, Any]:
        """List the authenticated user's git credentials.

        :param request: The incoming request, used to extract the user
            identity.
        :returns: ``{"object": "list", "data": [...]}``, token-free.
        :raises OmnigentError: 401 if unauthenticated.
        """
        owner_user_id = _credential_owner(request)
        creds = await asyncio.to_thread(git_credential_store.list_for_owner, owner_user_id)
        return {"object": "list", "data": [_entity_to_response(c) for c in creds]}

    @router.delete("/git-credentials/{credential_id}")
    async def delete_credential(
        request: Request,
        credential_id: str,
    ) -> dict[str, Any]:
        """Delete a git credential owned by the authenticated user.

        :param request: The incoming request, used to extract the user
            identity.
        :param credential_id: The credential to delete, e.g.
            ``"a1b2c3d4..."``.
        :returns: ``{"deleted": true}``.
        :raises OmnigentError: 401 if unauthenticated, 404 if the
            credential does not exist, or 403 if it belongs to a
            different user.
        """
        result = await asyncio.to_thread(
            git_credential_store.delete_for_owner,
            _credential_owner(request),
            credential_id,
        )
        if result is GitCredentialDeleteResult.NOT_FOUND:
            raise OmnigentError("Git credential not found", code=ErrorCode.NOT_FOUND)
        if result is GitCredentialDeleteResult.NOT_OWNER:
            raise OmnigentError("Not your git credential", code=ErrorCode.FORBIDDEN)
        return {"deleted": True}

    return router
