"""Tests for the engine-owned ``credential.*`` label namespace.

The policy-engine build seeds ``credential.kind`` / ``credential.family`` /
``credential.account`` from the session's bound provider account (see
:mod:`omnigent.subscription_tokens.labels`), so a guardrail policy can govern
on *which* credential a session runs (e.g. "a personal subscription may not
touch restricted data without approval"). That only holds if a client cannot
forge the label: an editor (or even the owner, or a bound runner) who could
PATCH ``credential.kind`` to ``api_key`` would slip restricted work past the
policy.

Unlike ``cost_control.*`` — which the session's bound *runner* may write — the
``credential.*`` namespace has **no** legitimate external writer (it is seeded
server-side from the binding), so these tests assert it is rejected for every
caller at both ``POST /v1/sessions`` and ``PATCH /v1/sessions/{id}``, including
a request carrying a valid runner tunnel token.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.subscription_tokens.labels import CREDENTIAL_KIND_LABEL

ALICE = "alice@example.com"
BOB = "bob@example.com"
_RUNNER_TOKEN = "test-binding-token-abc123"

_Stores = tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore]


@pytest.fixture
def stores(db_uri: str) -> _Stores:
    """Real file-backed stores backing the routes under test.

    :param db_uri: Per-test SQLite URI from the root conftest.
    :returns: ``(conversation_store, agent_store, permission_store)``.
    """
    return (
        SqlAlchemyConversationStore(db_uri),
        SqlAlchemyAgentStore(db_uri),
        SqlAlchemyPermissionStore(db_uri),
    )


def _install_error_handler(app: FastAPI) -> None:
    """Mirror ``create_app()``'s OmnigentError → HTTP translation.

    :param app: The bare test app mounting only the sessions router.
    """

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        """Translate OmnigentError to its HTTP status."""
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )


def _multi_user_app(stores: _Stores) -> FastAPI:
    """Build a multi-user app (header auth + real permission store).

    :param stores: The shared store fixture.
    :returns: A FastAPI app mounting the sessions router at ``/v1``.
    """
    conversation_store, agent_store, permission_store = stores
    app = FastAPI()
    _install_error_handler(app)
    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store,
            agent_store=agent_store,
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=permission_store,
            runner_tunnel_tokens=frozenset({_RUNNER_TOKEN}),
        ),
        prefix="/v1",
    )
    return app


def _seed_session(
    stores: _Stores,
    *,
    owner: str | None = ALICE,
    editor: str | None = None,
    runner_id: str | None = None,
) -> str:
    """Create a session-shaped conversation with optional grants/runner.

    :param stores: The shared store fixture.
    :param owner: User granted ``LEVEL_OWNER``, or ``None`` to skip grants.
    :param editor: Optional user granted ``LEVEL_EDIT``.
    :param runner_id: Optional runner id to bind.
    :returns: The new session/conversation id.
    """
    conversation_store, agent_store, permission_store = stores
    if agent_store.get("ag_test") is None:
        agent_store.create(agent_id="ag_test", name="test-agent", bundle_location="ag_test/bundle")
    conv = conversation_store.create_conversation(title="cred session", agent_id="ag_test")
    if owner is not None:
        permission_store.ensure_user(owner)
        permission_store.grant(owner, conv.id, LEVEL_OWNER)
    if editor is not None:
        permission_store.ensure_user(editor)
        permission_store.grant(editor, conv.id, LEVEL_EDIT)
    if runner_id is not None:
        conversation_store.replace_runner_id(conv.id, runner_id)
    return conv.id


# ── PATCH: no client may write the namespace ─────────────────────────────────


def test_editor_cannot_patch_credential_label(stores: _Stores) -> None:
    """Bob (edit access) cannot forge ``credential.kind`` — the attack a
    governance policy gating on it must be protected from."""
    conversation_store = stores[0]
    app = _multi_user_app(stores)
    conv_id = _seed_session(stores, editor=BOB)

    resp = TestClient(app).patch(
        f"/v1/sessions/{conv_id}",
        json={"labels": {CREDENTIAL_KIND_LABEL: "api_key"}},
        headers={"X-Forwarded-Email": BOB},
    )
    assert resp.status_code == 400
    assert "credential" in resp.json()["error"]["message"]
    conv = conversation_store.get_conversation(conv_id)
    assert conv is not None
    assert CREDENTIAL_KIND_LABEL not in conv.labels


def test_owner_cannot_patch_credential_label(stores: _Stores) -> None:
    """Even the OWNER cannot set it from an ordinary client: it is engine
    state derived from the binding, not user preference."""
    conversation_store = stores[0]
    app = _multi_user_app(stores)
    conv_id = _seed_session(stores)

    resp = TestClient(app).patch(
        f"/v1/sessions/{conv_id}",
        json={"labels": {CREDENTIAL_KIND_LABEL: "api_key"}},
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 400
    conv = conversation_store.get_conversation(conv_id)
    assert conv is not None
    assert CREDENTIAL_KIND_LABEL not in conv.labels


def test_bound_runner_token_cannot_patch_credential_label(stores: _Stores) -> None:
    """The key divergence from ``cost_control.*``: even the session's bound
    runner (presenting its tunnel token) cannot write ``credential.*`` — there
    is no authorized external writer at all, so the gate rejects the runner
    too (400), unlike the cost-plan namespace which the runner may write."""
    conversation_store = stores[0]
    app = _multi_user_app(stores)
    conv_id = _seed_session(stores, runner_id=token_bound_runner_id(_RUNNER_TOKEN))

    resp = TestClient(app).patch(
        f"/v1/sessions/{conv_id}",
        json={"labels": {CREDENTIAL_KIND_LABEL: "api_key"}},
        headers={"X-Forwarded-Email": ALICE, RUNNER_TUNNEL_TOKEN_HEADER: _RUNNER_TOKEN},
    )
    assert resp.status_code == 400
    conv = conversation_store.get_conversation(conv_id)
    assert conv is not None
    assert CREDENTIAL_KIND_LABEL not in conv.labels


def test_rejected_credential_write_leaves_other_fields_untouched(stores: _Stores) -> None:
    """The gate runs BEFORE any store mutation: a mixed PATCH (title +
    credential label) must not half-apply the title."""
    conversation_store = stores[0]
    app = _multi_user_app(stores)
    conv_id = _seed_session(stores)

    resp = TestClient(app).patch(
        f"/v1/sessions/{conv_id}",
        json={"title": "smuggled rename", "labels": {CREDENTIAL_KIND_LABEL: "api_key"}},
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 400
    conv = conversation_store.get_conversation(conv_id)
    assert conv is not None
    assert conv.title == "cred session"
    assert CREDENTIAL_KIND_LABEL not in conv.labels


def test_editor_can_still_patch_ordinary_labels(stores: _Stores) -> None:
    """The gate is namespace-scoped: ordinary label writes still succeed."""
    conversation_store = stores[0]
    app = _multi_user_app(stores)
    conv_id = _seed_session(stores, editor=BOB)

    resp = TestClient(app).patch(
        f"/v1/sessions/{conv_id}",
        json={"labels": {"team": "ml"}},
        headers={"X-Forwarded-Email": BOB},
    )
    assert resp.status_code == 200
    conv = conversation_store.get_conversation(conv_id)
    assert conv is not None
    assert conv.labels["team"] == "ml"


# ── Create: no client may seed the namespace ─────────────────────────────────


def test_create_session_rejects_credential_label_seed(stores: _Stores) -> None:
    """``POST /v1/sessions`` with a ``credential.*`` seed fails 400 — a seeded
    forgery would mis-govern from turn one."""
    _seed_session(stores)  # ensures ag_test exists
    app = _multi_user_app(stores)

    resp = TestClient(app).post(
        "/v1/sessions",
        json={"agent_id": "ag_test", "labels": {CREDENTIAL_KIND_LABEL: "api_key"}},
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 400
    assert "credential" in resp.json()["error"]["message"]


def test_create_session_with_ordinary_labels_succeeds(stores: _Stores) -> None:
    """Counterpart: ordinary seeds still work — the create gate is scoped."""
    conversation_store = stores[0]
    _seed_session(stores)  # ensures ag_test exists
    app = _multi_user_app(stores)

    resp = TestClient(app).post(
        "/v1/sessions",
        json={"agent_id": "ag_test", "labels": {"team": "ml"}},
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 201
    conv = conversation_store.get_conversation(resp.json()["id"])
    assert conv is not None
    assert conv.labels["team"] == "ml"
