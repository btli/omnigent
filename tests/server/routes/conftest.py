"""Shared builders for focused session-route tests."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server import managed_hosts
from omnigent.server.app import add_workspace_scope_middleware
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore


def sessions_test_app(db_uri: str, **overrides: Any) -> FastAPI:
    """Build the standard header-auth app for session route tests."""
    managed = bool(overrides.pop("managed", False))
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_error(request: Request, error: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=error.http_status,
            content={"error": {"code": error.code, "message": error.message}},
        )

    add_workspace_scope_middleware(app)
    route_args: dict[str, Any] = {
        "conversation_store": SqlAlchemyConversationStore(db_uri),
        "agent_store": SqlAlchemyAgentStore(db_uri),
        "auth_provider": UnifiedAuthProvider(source="header"),
        "permission_store": SqlAlchemyPermissionStore(db_uri),
        "project_store": SqlAlchemyProjectStore(db_uri),
    }
    route_args.update(overrides)
    app.include_router(create_sessions_router(**route_args), prefix="/v1")
    if managed:
        app.state.sandbox_config = object()
        app.state.host_store = object()
        app.state.managed_launches = managed_hosts.ManagedLaunchTracker()
    return app
