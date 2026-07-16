"""Route tests for ``/v1/git-credentials`` (:func:`create_git_credentials_router`).

Mounts the router on a minimal app (mirrors
``tests/server/test_app_git_hosts.py``'s ``app_factory``, but this router
predates ``create_app`` wiring, so the app here is a bare ``FastAPI()``
with just the ``OmnigentError`` handler and this router registered — the
same minimal-app pattern used by
``tests/server/routes/test_sessions_input_policy_deny.py``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.git_hosts.base import HostConfig
from omnigent.git_hosts.config import load_git_hosts
from omnigent.git_hosts.crypto import GitCredentialCipher
from omnigent.server.app import sanitized_validation_error_handler
from omnigent.server.auth import AuthProvider
from omnigent.server.routes.git_credentials import create_git_credentials_router
from omnigent.stores.git_credential_store import GitCredentialStore

_HOST_ID = "acme-forgejo"
_SECRET_TOKEN = "ghp_super_secret_value_do_not_leak"


class _HeaderAuthProvider:
    """Test auth: the X-Test-User header is the authenticated user (absent -> 401)."""

    def get_user_id(self, request: Request) -> str | None:
        """Return the caller identity from the ``X-Test-User`` header.

        :param request: The incoming request.
        :returns: The header value, or ``None`` if absent (unauthenticated).
        """
        return request.headers.get("X-Test-User")


def _build_app(
    git_hosts: tuple[HostConfig, ...],
    store: GitCredentialStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> FastAPI:
    """Build a minimal app mounting the git-credentials router.

    Registers the same two handlers ``create_app`` does for this router:
    the ``OmnigentError`` handler (for 401/404/403/409) and the sanitized
    ``RequestValidationError`` handler (for 422s that must not echo a
    submitted token).

    :param git_hosts: Operator-configured hosts to pass to the router.
    :param store: The backing :class:`GitCredentialStore`.
    :param auth_provider: Auth provider, or ``None`` for single-user mode.
    :returns: A configured :class:`FastAPI` app.
    """
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    # Mirror create_app: strip echoed input from 422s so a malformed body
    # never reflects the submitted token.
    app.add_exception_handler(RequestValidationError, sanitized_validation_error_handler)

    app.include_router(
        create_git_credentials_router(store, git_hosts, auth_provider=auth_provider),
        prefix="/v1",
    )
    return app


@pytest.fixture()
def route_client(tmp_path: Path) -> Iterator[TestClient]:
    """A git-credentials router mounted on a minimal app, with one host configured."""
    cipher = GitCredentialCipher([Fernet.generate_key().decode()])
    store = GitCredentialStore(f"sqlite:///{tmp_path}/creds.db", cipher)
    git_hosts = load_git_hosts(
        [
            {
                "id": _HOST_ID,
                "provider": "forgejo",
                "web_host": "git.acme.com",
                "credential_source": "env:ACME_TOKEN",
            }
        ]
    )

    app = _build_app(git_hosts, store)

    with TestClient(app) as client:
        yield client


@pytest.fixture()
def multi_user_route_client(tmp_path: Path) -> Iterator[TestClient]:
    """The same router, but mounted with a real ``auth_provider`` — the security boundary."""
    cipher = GitCredentialCipher([Fernet.generate_key().decode()])
    store = GitCredentialStore(f"sqlite:///{tmp_path}/creds_multi_user.db", cipher)
    git_hosts = load_git_hosts(
        [
            {
                "id": _HOST_ID,
                "provider": "forgejo",
                "web_host": "git.acme.com",
                "credential_source": "env:ACME_TOKEN",
            }
        ]
    )

    app = _build_app(git_hosts, store, auth_provider=_HeaderAuthProvider())

    with TestClient(app) as client:
        yield client


def test_create_returns_metadata_without_token(route_client: TestClient) -> None:
    resp = route_client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "label": "work", "token": _SECRET_TOKEN, "username": "alice"},
    )
    assert resp.status_code in (200, 201)
    body = resp.json()
    assert body["host_id"] == _HOST_ID
    assert body["provider"] == "forgejo"
    assert body["label"] == "work"
    assert body["username"] == "alice"
    assert "id" in body
    assert "created_at" in body
    assert "token" not in body
    assert _SECRET_TOKEN not in resp.text


def test_malformed_body_422_does_not_echo_token(route_client: TestClient) -> None:
    # A body missing a required field triggers request validation *before* the
    # handler runs; the 422 must not reflect the submitted token.
    resp = route_client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "token": _SECRET_TOKEN},  # 'label' missing
    )
    assert resp.status_code == 422
    assert _SECRET_TOKEN not in resp.text
    # The structural error info is still present.
    detail = resp.json()["detail"]
    assert any("label" in err.get("loc", []) for err in detail)


def test_create_unknown_host_returns_4xx(route_client: TestClient) -> None:
    resp = route_client.post(
        "/v1/git-credentials",
        json={"host_id": "no-such-host", "label": "work", "token": _SECRET_TOKEN},
    )
    assert 400 <= resp.status_code < 500
    assert _SECRET_TOKEN not in resp.text


def test_create_second_label_same_host_succeeds(route_client: TestClient) -> None:
    first = route_client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "label": "work", "token": _SECRET_TOKEN},
    )
    second = route_client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "label": "personal", "token": "another-token"},
    )
    assert first.status_code in (200, 201)
    assert second.status_code in (200, 201)
    assert first.json()["id"] != second.json()["id"]


def test_create_duplicate_label_conflicts(route_client: TestClient) -> None:
    first = route_client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "label": "work", "token": _SECRET_TOKEN},
    )
    assert first.status_code in (200, 201)

    dup = route_client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "label": "work", "token": "different-token"},
    )
    assert dup.status_code == 409
    assert _SECRET_TOKEN not in dup.text
    assert "different-token" not in dup.text


def test_list_returns_created_credentials_without_token(route_client: TestClient) -> None:
    route_client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "label": "work", "token": _SECRET_TOKEN},
    )
    route_client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "label": "personal", "token": "another-token"},
    )

    resp = route_client.get("/v1/git-credentials")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    labels = {item["label"] for item in body["data"]}
    assert labels == {"work", "personal"}
    for item in body["data"]:
        assert "token" not in item
    assert _SECRET_TOKEN not in resp.text
    assert "another-token" not in resp.text


def test_delete_removes_credential(route_client: TestClient) -> None:
    created = route_client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "label": "work", "token": _SECRET_TOKEN},
    ).json()

    other = route_client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "label": "personal", "token": "another-token"},
    ).json()

    del_resp = route_client.delete(f"/v1/git-credentials/{created['id']}")
    assert del_resp.status_code == 200
    assert del_resp.json() == {"deleted": True}

    remaining = route_client.get("/v1/git-credentials").json()["data"]
    assert len(remaining) == 1
    assert remaining[0]["id"] == other["id"]


def test_create_oversized_token_rejected(route_client: TestClient) -> None:
    oversized = "x" * 9000
    resp = route_client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "label": "work", "token": oversized},
    )
    assert resp.status_code == 422
    assert oversized not in resp.text


def test_create_empty_label_rejected(route_client: TestClient) -> None:
    resp = route_client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "label": "", "token": _SECRET_TOKEN},
    )
    assert resp.status_code == 422
    assert _SECRET_TOKEN not in resp.text


# ── Multi-user authorization boundary ──────────────────────────────────


def test_missing_auth_header_401s_on_every_verb(multi_user_route_client: TestClient) -> None:
    client = multi_user_route_client
    post_resp = client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "label": "work", "token": _SECRET_TOKEN},
    )
    assert post_resp.status_code == 401

    get_resp = client.get("/v1/git-credentials")
    assert get_resp.status_code == 401

    del_resp = client.delete("/v1/git-credentials/some-id")
    assert del_resp.status_code == 401


def test_cross_user_isolation_and_foreign_delete_denied(
    multi_user_route_client: TestClient,
) -> None:
    client = multi_user_route_client
    alice_headers = {"X-Test-User": "alice"}
    bob_headers = {"X-Test-User": "bob"}

    created = client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "label": "work", "token": _SECRET_TOKEN},
        headers=alice_headers,
    )
    assert created.status_code in (200, 201)
    alice_cred_id = created.json()["id"]

    # Bob has his own credential on the same host; isolation is bilateral.
    bob_created = client.post(
        "/v1/git-credentials",
        json={"host_id": _HOST_ID, "label": "work", "token": "bob-token"},
        headers=bob_headers,
    )
    assert bob_created.status_code in (200, 201)
    bob_cred_id = bob_created.json()["id"]

    # Bob's list has only his; Alice's list has only hers.
    def _list_ids(headers: dict[str, str]) -> set[str]:
        data = client.get("/v1/git-credentials", headers=headers).json()["data"]
        return {item["id"] for item in data}

    assert _list_ids(bob_headers) == {bob_cred_id}
    assert _list_ids(alice_headers) == {alice_cred_id}

    # Bob deleting Alice's credential must not succeed and must not delete it.
    bob_delete = client.delete(f"/v1/git-credentials/{alice_cred_id}", headers=bob_headers)
    assert bob_delete.status_code in (403, 404)

    # Alice can still list her own credential afterward.
    alice_list = client.get("/v1/git-credentials", headers=alice_headers)
    assert alice_list.status_code == 200
    assert {item["id"] for item in alice_list.json()["data"]} == {alice_cred_id}


@pytest.mark.parametrize("field", ["owner_user_id", "provider", "workspace_id"])
def test_client_supplied_authority_field_rejected(
    multi_user_route_client: TestClient, field: str
) -> None:
    # None of the server-derived authority fields may be supplied by the client
    # (the request model forbids extras).
    resp = multi_user_route_client.post(
        "/v1/git-credentials",
        json={
            "host_id": _HOST_ID,
            "label": "work",
            "token": _SECRET_TOKEN,
            field: "attacker-controlled",
        },
        headers={"X-Test-User": "alice"},
    )
    assert resp.status_code == 422
    assert _SECRET_TOKEN not in resp.text
