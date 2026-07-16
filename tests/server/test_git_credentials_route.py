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
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.git_hosts.config import load_git_hosts
from omnigent.git_hosts.crypto import GitCredentialCipher
from omnigent.server.routes.git_credentials import create_git_credentials_router
from omnigent.stores.git_credential_store import GitCredentialStore

_HOST_ID = "acme-forgejo"
_SECRET_TOKEN = "ghp_super_secret_value_do_not_leak"


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

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_git_credentials_router(store, git_hosts),
        prefix="/v1",
    )

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
