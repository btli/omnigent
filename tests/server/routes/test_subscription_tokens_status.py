"""Tests for the subscription-token status route's operator reverse-view and
the session snapshot's active-credential mapping.

Covers the server seams added for the per-session credential indicator:

* ``_attach_active_sessions`` — annotates each account with the sessions
  currently *running* on it, via one batched lookup filtered to the live set.
* The status route's admin gate — ``active_sessions`` (cross-user session ids)
  is attached only for admin callers.
* ``_active_credential_info`` + ``_build_session_response`` — lift the facade's
  account dict into the typed :class:`ActiveCredentialInfo` and thread it onto
  the session snapshot.
"""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.entities import Conversation
from omnigent.server.routes import subscription_tokens as st
from omnigent.server.routes.sessions import _active_credential_info, _build_session_response
from omnigent.server.routes.subscription_tokens import create_subscription_tokens_router
from omnigent.server.schemas import ActiveCredentialInfo
from omnigent.stores.permission_store import PermissionStore


def test_attach_active_sessions_distributes_batched_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_batch(
        credential_ids: object, *, only_session_ids: object = None
    ) -> dict[str, list[str]]:
        captured["ids"] = list(cast("list[str]", credential_ids))
        captured["only"] = only_session_ids
        return {"acc1": ["s3", "s2"], "acc2": []}

    monkeypatch.setattr(st.subtokens_integration, "sessions_for_credentials", _fake_batch)
    pools: list[dict[str, object]] = [{"name": "p", "accounts": [{"id": "acc1"}, {"id": "acc2"}]}]

    st._attach_active_sessions(pools, lambda: {"s2", "s3"})

    # The running set is threaded as the live filter (so the cap counts live
    # sessions), and one batched call covers every account id.
    assert captured["ids"] == ["acc1", "acc2"]
    assert captured["only"] == {"s2", "s3"}
    accounts = pools[0]["accounts"]
    assert isinstance(accounts, list)
    assert accounts[0]["active_sessions"] == ["s3", "s2"]
    assert accounts[1]["active_sessions"] == []


def test_attach_active_sessions_tolerates_liveness_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        st.subtokens_integration,
        "sessions_for_credentials",
        lambda ids, *, only_session_ids=None: {},
    )

    def _boom() -> set[str]:
        raise RuntimeError("status cache unavailable")

    pools: list[dict[str, object]] = [{"name": "p", "accounts": [{"id": "acc1"}]}]
    st._attach_active_sessions(pools, _boom)  # must not raise — status call stays up

    accounts = pools[0]["accounts"]
    assert isinstance(accounts, list)
    assert accounts[0]["active_sessions"] == []  # empty running set on error


class _FakePermissionStore:
    """Minimal stand-in exposing only the ``is_admin`` the route consults."""

    def __init__(self, admins: set[str]) -> None:
        self._admins = admins

    def is_admin(self, user_id: str) -> bool:
        return user_id in self._admins


def _status_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_id: str | None,
    permission_store: PermissionStore | None,
) -> TestClient:
    """A TestClient over the status router with stubbed auth + snapshot data."""
    monkeypatch.setattr(st, "get_user_id", lambda request, auth_provider: user_id)
    monkeypatch.setattr(
        st.subtokens_integration,
        "status_snapshot",
        lambda: [{"name": "p", "family": "openai", "accounts": [{"id": "acc1", "name": "x1"}]}],
    )
    monkeypatch.setattr(
        st.subtokens_integration,
        "sessions_for_credentials",
        lambda ids, *, only_session_ids=None: {cid: ["s-live"] for cid in ids},
    )
    app = FastAPI()
    app.include_router(
        create_subscription_tokens_router(
            auth_provider=object() if permission_store is not None else None,
            permission_store=permission_store,
            running_session_ids=lambda: {"s-live"},
        ),
        prefix="/v1",
    )
    return TestClient(app)


def _accounts(body: dict[str, object]) -> list[dict[str, object]]:
    pools = cast("list[dict[str, object]]", body["pools"])
    return cast("list[dict[str, object]]", pools[0]["accounts"])


def test_status_route_attaches_active_sessions_for_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    store = cast(PermissionStore, _FakePermissionStore({"u-admin"}))
    client = _status_client(monkeypatch, user_id="u-admin", permission_store=store)
    body = client.get("/v1/subscription-tokens/status").json()
    assert _accounts(body)[0]["active_sessions"] == ["s-live"]


def test_status_route_withholds_active_sessions_from_non_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-admin still gets the snapshot, but never other users' session ids.
    store = cast(PermissionStore, _FakePermissionStore({"u-admin"}))
    client = _status_client(monkeypatch, user_id="u-regular", permission_store=store)
    body = client.get("/v1/subscription-tokens/status").json()
    account = _accounts(body)[0]
    assert "active_sessions" not in account
    assert account["name"] == "x1"  # the base snapshot is still served


def test_status_route_single_user_mode_attaches_active_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # permission_store=None → single-user/operator deployment → no gate.
    client = _status_client(monkeypatch, user_id=None, permission_store=None)
    body = client.get("/v1/subscription-tokens/status").json()
    assert _accounts(body)[0]["active_sessions"] == ["s-live"]


def test_active_credential_info_maps_valid_payload() -> None:
    assert _active_credential_info(
        {
            "id": "codex-pool/x1",
            "name": "x1",
            "kind": "subscription",
            "family": "openai",
            "limit_status": "available",
        }
    ) == ActiveCredentialInfo(
        id="codex-pool/x1",
        name="x1",
        kind="subscription",
        family="openai",
        limit_status="available",
    )


def test_active_credential_info_none_and_malformed_return_none() -> None:
    assert _active_credential_info(None) is None
    # Missing required fields → dropped (best-effort; never breaks a snapshot).
    assert _active_credential_info({"id": "x"}) is None
    # Out-of-vocabulary enum value → dropped.
    assert (
        _active_credential_info(
            {
                "id": "a",
                "name": "n",
                "kind": "bogus",
                "family": "openai",
                "limit_status": "available",
            }
        )
        is None
    )


def _conv() -> Conversation:
    return Conversation(
        id="conv_x",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_x",
        agent_id="ag_test",
    )


def test_build_session_response_threads_active_credential() -> None:
    cred = ActiveCredentialInfo(
        id="codex-pool/x1",
        name="x1",
        kind="subscription",
        family="openai",
        limit_status="available",
    )
    resp = _build_session_response(_conv(), [], "idle", active_credential=cred)
    assert resp.active_credential == cred


def test_build_session_response_omits_active_credential_by_default() -> None:
    # Single-account setups: the field is absent (None), invisible to clients.
    resp = _build_session_response(_conv(), [], "idle")
    assert resp.active_credential is None
