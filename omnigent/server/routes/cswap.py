"""Routes for multi-subscription (cswap) status and management.

* ``GET /v1/cswap/status`` — pools, accounts, usage-limit state, and
  today's per-account cost. Requires authentication in multi-user mode.
* ``POST /v1/cswap/accounts/{credential_id}/mark-available`` — manually
  clear an account's limited state. Requires admin in multi-user mode.

All endpoints are inert (empty / 404-free no-ops) when no ``pools:`` block
is configured.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from omnigent.cswap import integration as cswap_integration
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.stores.permission_store import PermissionStore


def create_cswap_router(
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the cswap status/management router (mounted under ``/v1``).

    :param auth_provider: Auth provider, or ``None`` in single-user mode.
    :param permission_store: Permission store for admin checks, or ``None``
        to skip enforcement.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _require_auth(request: Request) -> str | None:
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and user_id is None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        return user_id

    async def _require_admin(request: Request) -> None:
        user_id = _require_auth(request)
        if permission_store is None:
            return
        if user_id is None or not await asyncio.to_thread(permission_store.is_admin, user_id):
            raise OmnigentError(
                "Admin privileges required to manage subscriptions",
                code=ErrorCode.FORBIDDEN,
            )

    @router.get("/cswap/status")
    async def cswap_status(request: Request) -> dict[str, Any]:
        """Return the multi-subscription status snapshot."""
        _require_auth(request)
        pools = await asyncio.to_thread(cswap_integration.status_snapshot)
        return {"object": "cswap_status", "active": bool(pools), "pools": pools}

    @router.post("/cswap/accounts/{credential_id}/mark-available")
    async def cswap_mark_available(request: Request, credential_id: str) -> dict[str, Any]:
        """Manually clear an account's limited state."""
        await _require_admin(request)
        applied = await asyncio.to_thread(cswap_integration.mark_available, credential_id)
        if not applied:
            raise OmnigentError("Multi-subscription is not configured", code=ErrorCode.NOT_FOUND)
        return {"object": "cswap_account", "id": credential_id, "limit_status": "available"}

    return router
