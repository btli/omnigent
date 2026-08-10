"""Tests for ``GET /auth/native-complete`` (native-shell login completion).

The endpoint hands a credential to the mobile shells through a fixed
``omnigent://auth-callback`` redirect: the proxy-forwarded access token
in header mode, a minted session JWT in the cookie modes. These tests
drive the real router with a ``TestClient`` per auth mode and assert on
the redirect ``Location`` — the app-facing contract.
"""

from __future__ import annotations

from urllib.parse import parse_qs, quote, urlsplit

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig, mint_session_token
from omnigent.server.routes.native_auth import (
    create_native_auth_router,
    resolve_forwarded_token_header,
)

_SECRET = bytes.fromhex("ab" * 32)
_STATE = "state-nonce-1234"


def _client(provider: UnifiedAuthProvider) -> TestClient:
    app = FastAPI()
    app.include_router(create_native_auth_router(provider), prefix="/auth")
    return TestClient(app, follow_redirects=False)


def _header_provider() -> UnifiedAuthProvider:
    return UnifiedAuthProvider(
        "header",
        local_single_user=False,
        header_name="X-Forwarded-Email",
        header_strip_prefix="",
    )


def _oidc_provider() -> UnifiedAuthProvider:
    config = OIDCConfig(
        issuer="https://idp.example.com",
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://server.example.com/auth/callback",
        cookie_secret=_SECRET,
        scopes="openid email",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type="oidc",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token",
        jwks_uri="https://idp.example.com/jwks",
        userinfo_endpoint=None,
        allow_invites=False,
    )
    return UnifiedAuthProvider("oidc", oidc_config=config)


def _accounts_provider() -> UnifiedAuthProvider:
    config = AccountsConfig(
        cookie_secret=_SECRET,
        session_ttl_hours=8,
        base_url="http://server.example.com",
        init_admin_password=None,
        invite_ttl_seconds=3600,
        magic_ttl_seconds=600,
    )
    return UnifiedAuthProvider("accounts", accounts_config=config)


def _callback_params(response) -> dict[str, list[str]]:
    location = response.headers["location"]
    parts = urlsplit(location)
    assert f"{parts.scheme}://{parts.netloc}" == "omnigent://auth-callback"
    return parse_qs(parts.query)


class TestStateValidation:
    @pytest.mark.parametrize(
        "state",
        ["", "short", "has space in it", "semi;colon-injection", "x" * 129, "quer?y"],
    )
    def test_malformed_state_is_rejected(self, state: str) -> None:
        client = _client(_header_provider())
        response = client.get(
            "/auth/native-complete",
            params={"state": state} if state else {},
            headers={"X-Forwarded-Email": "alice@example.com"},
        )
        assert response.status_code == 400

    def test_state_is_never_echoed_unvalidated(self) -> None:
        # A rejected state must not appear in any redirect Location.
        client = _client(_header_provider())
        response = client.get(
            "/auth/native-complete",
            params={"state": "bad state"},
            headers={"X-Forwarded-Email": "alice@example.com"},
        )
        assert "location" not in response.headers


class TestHeaderMode:
    def test_relays_forwarded_access_token(self) -> None:
        client = _client(_header_provider())
        response = client.get(
            "/auth/native-complete",
            params={"state": _STATE},
            headers={
                "X-Forwarded-Email": "alice@example.com",
                "X-Forwarded-Access-Token": "workspace-token-abc",
            },
        )
        assert response.status_code == 302
        params = _callback_params(response)
        assert params["state"] == [_STATE]
        assert params["token_type"] == ["bearer"]
        assert params["token"] == ["workspace-token-abc"]
        # The Location carries a credential — it must never be cached.
        assert response.headers["cache-control"] == "no-store"

    def test_no_forwarded_token_redirects_with_error(self) -> None:
        client = _client(_header_provider())
        response = client.get(
            "/auth/native-complete",
            params={"state": _STATE},
            headers={"X-Forwarded-Email": "alice@example.com"},
        )
        assert response.status_code == 302
        params = _callback_params(response)
        assert params["state"] == [_STATE]
        assert params["error"] == ["no_token"]
        assert "token" not in params

    def test_unauthenticated_is_401(self) -> None:
        # Header mode has no login page to bounce through: a request
        # without identity means the fronting proxy failed at its job.
        client = _client(_header_provider())
        response = client.get("/auth/native-complete", params={"state": _STATE})
        assert response.status_code == 401

    def test_forwarded_token_header_is_overridable(self, monkeypatch) -> None:
        monkeypatch.setenv("OMNIGENT_FORWARDED_TOKEN_HEADER", "X-Custom-Token")
        assert resolve_forwarded_token_header() == "X-Custom-Token"
        client = _client(_header_provider())
        response = client.get(
            "/auth/native-complete",
            params={"state": _STATE},
            headers={
                "X-Forwarded-Email": "alice@example.com",
                "X-Custom-Token": "custom-token",
            },
        )
        params = _callback_params(response)
        assert params["token"] == ["custom-token"]


class TestCookieModes:
    @pytest.mark.parametrize(
        ("provider_factory", "login_url"),
        [(_oidc_provider, "/auth/login"), (_accounts_provider, "/login")],
    )
    def test_unauthenticated_bounces_through_login(
        self, provider_factory, login_url: str
    ) -> None:
        client = _client(provider_factory())
        response = client.get("/auth/native-complete", params={"state": _STATE})
        assert response.status_code == 302
        expected_return = quote(f"/auth/native-complete?state={_STATE}", safe="")
        assert response.headers["location"] == f"{login_url}?return_to={expected_return}"

    @pytest.mark.parametrize(
        ("provider_factory", "provider_name"),
        [(_oidc_provider, "oidc"), (_accounts_provider, "accounts")],
    )
    def test_authenticated_mints_session_token(
        self, provider_factory, provider_name: str
    ) -> None:
        provider = provider_factory()
        client = _client(provider)
        bearer = mint_session_token("alice@example.com", _SECRET, 3600, provider_name)
        response = client.get(
            "/auth/native-complete",
            params={"state": _STATE},
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert response.status_code == 302
        params = _callback_params(response)
        assert params["state"] == [_STATE]
        assert params["token_type"] == ["session"]
        claims = jwt.decode(params["token"][0], _SECRET, algorithms=["HS256"])
        assert claims["sub"] == "alice@example.com"
        assert response.headers["cache-control"] == "no-store"
