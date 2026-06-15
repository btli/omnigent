"""Routes for multi-subscription status and management.

* ``GET /v1/subscription-tokens/status`` — pools, accounts, usage-limit state, and
  today's per-account cost. Requires authentication in multi-user mode.
* ``POST /v1/subscription-tokens/accounts/{credential_id}/mark-available`` — manually
  clear an account's limited state. Requires admin in multi-user mode.

All endpoints are inert (empty / 404-free no-ops) when no ``pools:`` block
is configured.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.stores.permission_store import PermissionStore
from omnigent.subscription_tokens import integration as subtokens_integration

logger = logging.getLogger(__name__)


def _account_ids(pools: list[dict[str, object]]) -> list[str]:
    """Collect every account's credential id across *pools* (status snapshot)."""
    ids: list[str] = []
    for pool in pools:
        accounts = pool.get("accounts")
        if not isinstance(accounts, list):
            continue
        for account in accounts:
            credential_id = account.get("id")
            if isinstance(credential_id, str):
                ids.append(credential_id)
    return ids


def _attach_active_sessions(
    pools: list[dict[str, object]],
    running_session_ids: Callable[[], set[str]],
) -> None:
    """Annotate each account in *pools* with its currently-running sessions.

    Mutates *pools* in place: every account gains an ``active_sessions`` list —
    the sessions currently executing a turn that are bound to that account. The
    live set comes from *running_session_ids* (the server's in-memory status
    cache), so this is best-effort: a session missing from that cache (e.g. just
    after a restart, before its runner next reports) won't appear until its next
    status tick. Resolved in one batched query filtered to the live set, so a
    long-running session is never hidden behind newer dead bindings.

    :param pools: The :func:`status_snapshot` pool dicts (each with an
        ``accounts`` list of dicts carrying an ``id``).
    :param running_session_ids: Returns the set of session ids running a turn.
    """
    try:
        running = running_session_ids()
    except Exception:
        logger.exception("subscription-token running-session lookup failed")
        running = set()
    by_credential = subtokens_integration.sessions_for_credentials(
        _account_ids(pools), only_session_ids=running
    )
    for pool in pools:
        accounts = pool.get("accounts")
        if not isinstance(accounts, list):
            continue
        for account in accounts:
            credential_id = account.get("id")
            account["active_sessions"] = (
                by_credential.get(credential_id, []) if isinstance(credential_id, str) else []
            )


def create_subscription_tokens_router(
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    running_session_ids: Callable[[], set[str]] | None = None,
) -> APIRouter:
    """Build the subscription-token status/management router (mounted under ``/v1``).

    :param auth_provider: Auth provider, or ``None`` in single-user mode.
    :param permission_store: Permission store for admin checks, or ``None``
        to skip enforcement.
    :param running_session_ids: Returns the ids of sessions currently executing
        a turn, used to compute each account's ``active_sessions``. ``None``
        omits the reverse-view (the field is left off each account) — the status
        snapshot still reports per-account limit/cost as before.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _require_auth(request: Request) -> str | None:
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and user_id is None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        return user_id

    async def _is_admin(user_id: str | None) -> bool:
        """Whether *user_id* may see operator-only data (always true single-user)."""
        if permission_store is None:
            return True
        return user_id is not None and await asyncio.to_thread(permission_store.is_admin, user_id)

    async def _require_admin(request: Request) -> None:
        if not await _is_admin(_require_auth(request)):
            raise OmnigentError(
                "Admin privileges required to manage subscriptions",
                code=ErrorCode.FORBIDDEN,
            )

    @router.get("/subscription-tokens/status")
    async def subscription_tokens_status(request: Request) -> dict[str, Any]:
        """Return the multi-subscription status snapshot.

        For admin callers each account additionally carries ``active_sessions``
        — the sessions currently running on it. That field enumerates session
        ids across users, so it is omitted for non-admins (who still get the
        pools/accounts/limit/cost snapshot).
        """
        user_id = _require_auth(request)
        pools = await asyncio.to_thread(subtokens_integration.status_snapshot)
        if running_session_ids is not None and await _is_admin(user_id):
            await asyncio.to_thread(_attach_active_sessions, pools, running_session_ids)
        return {"object": "subscription_tokens_status", "active": bool(pools), "pools": pools}

    @router.post("/subscription-tokens/accounts/{credential_id}/mark-available")
    async def subscription_tokens_mark_available(
        request: Request, credential_id: str
    ) -> dict[str, Any]:
        """Manually clear an account's limited state."""
        await _require_admin(request)
        applied = await asyncio.to_thread(subtokens_integration.mark_available, credential_id)
        if not applied:
            raise OmnigentError("Multi-subscription is not configured", code=ErrorCode.NOT_FOUND)
        return {
            "object": "subscription_tokens_account",
            "id": credential_id,
            "limit_status": "available",
        }

    return router
