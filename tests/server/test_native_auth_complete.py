"""Tests for the native-shell login endpoints.

``GET /auth/native-complete`` creates a single-use, PKCE-bound flow for
an authenticated request and redirects the app an opaque one-time code;
``/auth/native-exchange`` (POST for natively reachable servers, GET for
the second Auth Tab hop behind a front door) turns code + state +
verifier into the credential. These tests drive the real router with a
``TestClient`` per auth mode, asserting on the redirect ``Location`` and
exchange responses — the app-facing contract — with a focus on the
attack paths: replay, initiation-less requests, expiry, and every
mismatch of state or verifier.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, quote, urlsplit

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import (
    OIDCConfig,
    derive_code_challenge,
    mint_session_token,
)
from omnigent.server.routes.native_auth import (
    create_native_auth_router,
    resolve_forwarded_token_header,
)

_SECRET = bytes.fromhex("ab" * 32)
_STATE = "state-nonce-1234"
_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # RFC 7636 App. B
_CHALLENGE = derive_code_challenge(_VERIFIER)


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


_HEADER_AUTH = {
    "X-Forwarded-Email": "alice@example.com",
    "X-Forwarded-Access-Token": "workspace-token-abc",
}


def _cookie_auth(provider_name: str) -> dict[str, str]:
    bearer = mint_session_token("alice@example.com", _SECRET, 3600, provider_name)
    return {"Authorization": f"Bearer {bearer}"}


def _callback_params(response) -> dict[str, list[str]]:
    location = response.headers["location"]
    parts = urlsplit(location)
    assert f"{parts.scheme}://{parts.netloc}" == "omnigent://auth-callback"
    return parse_qs(parts.query)


def _complete(
    client: TestClient,
    headers: dict[str, str],
    state: str = _STATE,
    challenge: str = _CHALLENGE,
) -> dict[str, list[str]]:
    response = client.get(
        "/auth/native-complete",
        params={"state": state, "code_challenge": challenge},
        headers=headers,
    )
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    return _callback_params(response)


class TestCompleteValidation:
    @pytest.mark.parametrize(
        "state",
        ["", "short", "has space in it", "semi;colon-injection", "x" * 129, "quer?y"],
    )
    def test_malformed_state_is_rejected(self, state: str) -> None:
        client = _client(_header_provider())
        response = client.get(
            "/auth/native-complete",
            params={"state": state, "code_challenge": _CHALLENGE} if state else {},
            headers=_HEADER_AUTH,
        )
        assert response.status_code == 400
        assert "location" not in response.headers

    def test_missing_or_malformed_challenge_is_rejected(self) -> None:
        # An authenticated cross-site GET without a PKCE challenge — the
        # "token oracle" shape — must yield nothing at all, not a flow.
        client = _client(_header_provider())
        for params in (
            {"state": _STATE},
            {"state": _STATE, "code_challenge": "short"},
            {"state": _STATE, "code_challenge": "!" * 43},
        ):
            response = client.get("/auth/native-complete", params=params, headers=_HEADER_AUTH)
            assert response.status_code == 400
            assert "location" not in response.headers

    def test_completion_redirect_never_carries_a_token(self) -> None:
        for provider, headers in (
            (_header_provider(), _HEADER_AUTH),
            (_oidc_provider(), _cookie_auth("oidc")),
            (_accounts_provider(), _cookie_auth("accounts")),
        ):
            params = _complete(_client(provider), headers)
            assert "token" not in params
            assert "token_type" not in params
            assert params["state"] == [_STATE]
            assert params["code"], "expected a one-time code"

    def test_unauthenticated_header_mode_is_401(self) -> None:
        client = _client(_header_provider())
        response = client.get(
            "/auth/native-complete",
            params={"state": _STATE, "code_challenge": _CHALLENGE},
        )
        assert response.status_code == 401

    @pytest.mark.parametrize(
        ("provider_factory", "login_url"),
        [(_oidc_provider, "/auth/login"), (_accounts_provider, "/login")],
    )
    def test_unauthenticated_cookie_mode_bounces_through_login(
        self, provider_factory, login_url: str
    ) -> None:
        client = _client(provider_factory())
        response = client.get(
            "/auth/native-complete",
            params={"state": _STATE, "code_challenge": _CHALLENGE},
        )
        assert response.status_code == 302
        expected_return = quote(
            f"/auth/native-complete?state={_STATE}&code_challenge={_CHALLENGE}", safe=""
        )
        assert response.headers["location"] == f"{login_url}?return_to={expected_return}"

    def test_header_mode_without_forwarded_token_reports_no_token(self) -> None:
        client = _client(_header_provider())
        response = client.get(
            "/auth/native-complete",
            params={"state": _STATE, "code_challenge": _CHALLENGE},
            headers={"X-Forwarded-Email": "alice@example.com"},
        )
        params = _callback_params(response)
        assert params["error"] == ["no_token"]
        assert "code" not in params

    def test_exchange_transport_matches_the_mode(self) -> None:
        # A native POST can't cross a front-door proxy, so header mode
        # exchanges through a second browser hop; cookie modes POST.
        assert _complete(_client(_header_provider()), _HEADER_AUTH)["exchange"] == ["tab"]
        assert _complete(_client(_oidc_provider()), _cookie_auth("oidc"))["exchange"] == ["post"]

    def test_forwarded_token_header_is_overridable(self, monkeypatch) -> None:
        monkeypatch.setenv("OMNIGENT_FORWARDED_TOKEN_HEADER", "X-Custom-Token")
        assert resolve_forwarded_token_header() == "X-Custom-Token"
        client = _client(_header_provider())
        params = _complete(
            client,
            {"X-Forwarded-Email": "alice@example.com", "X-Custom-Token": "custom-token"},
        )
        code = params["code"][0]
        exchanged = client.post(
            "/auth/native-exchange",
            data={"code": code, "state": _STATE, "code_verifier": _VERIFIER},
        )
        assert exchanged.json()["token"] == "custom-token"


class TestExchange:
    def test_header_mode_tab_exchange_relays_the_forwarded_token(self) -> None:
        client = _client(_header_provider())
        code = _complete(client, _HEADER_AUTH)["code"][0]

        response = client.get(
            "/auth/native-exchange",
            params={"code": code, "state": _STATE, "code_verifier": _VERIFIER},
        )

        assert response.status_code == 302
        params = _callback_params(response)
        assert params["token_type"] == ["bearer"]
        assert params["token"] == ["workspace-token-abc"]
        assert response.headers["cache-control"] == "no-store"

    @pytest.mark.parametrize(
        ("provider_factory", "provider_name"),
        [(_oidc_provider, "oidc"), (_accounts_provider, "accounts")],
    )
    def test_cookie_mode_post_exchange_mints_a_session_token(
        self, provider_factory, provider_name: str
    ) -> None:
        client = _client(provider_factory())
        code = _complete(client, _cookie_auth(provider_name))["code"][0]

        response = client.post(
            "/auth/native-exchange",
            data={"code": code, "state": _STATE, "code_verifier": _VERIFIER},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "session"
        claims = jwt.decode(body["token"], _SECRET, algorithms=["HS256"])
        assert claims["sub"] == "alice@example.com"
        assert response.headers["cache-control"] == "no-store"

    def test_replayed_code_is_rejected(self) -> None:
        client = _client(_header_provider())
        code = _complete(client, _HEADER_AUTH)["code"][0]
        fields = {"code": code, "state": _STATE, "code_verifier": _VERIFIER}

        first = client.get("/auth/native-exchange", params=fields)
        replay = client.get("/auth/native-exchange", params=fields)

        assert first.status_code == 302
        assert "token" in _callback_params(first)
        assert _callback_params(replay)["error"] == ["exchange_failed"]

    def test_wrong_verifier_burns_the_code(self) -> None:
        # The pop happens before the PKCE check, so a failed guess consumes
        # the code — the right verifier can't be retried afterwards either.
        client = _client(_header_provider())
        code = _complete(client, _HEADER_AUTH)["code"][0]

        wrong = client.post(
            "/auth/native-exchange",
            data={"code": code, "state": _STATE, "code_verifier": "A" * 43},
        )
        retry = client.post(
            "/auth/native-exchange",
            data={"code": code, "state": _STATE, "code_verifier": _VERIFIER},
        )

        assert wrong.status_code == 400
        assert retry.status_code == 400

    def test_wrong_state_is_rejected(self) -> None:
        client = _client(_header_provider())
        code = _complete(client, _HEADER_AUTH)["code"][0]

        response = client.post(
            "/auth/native-exchange",
            data={"code": code, "state": "different-state1", "code_verifier": _VERIFIER},
        )

        assert response.status_code == 400

    def test_unknown_code_is_rejected(self) -> None:
        client = _client(_header_provider())
        response = client.post(
            "/auth/native-exchange",
            data={"code": "never-issued-code", "state": _STATE, "code_verifier": _VERIFIER},
        )
        assert response.status_code == 400

    def test_expired_code_is_rejected(self, monkeypatch) -> None:
        client = _client(_header_provider())
        code = _complete(client, _HEADER_AUTH)["code"][0]

        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + 121)
        response = client.post(
            "/auth/native-exchange",
            data={"code": code, "state": _STATE, "code_verifier": _VERIFIER},
        )

        assert response.status_code == 400

    def test_malformed_exchange_fields_are_rejected(self) -> None:
        client = _client(_header_provider())
        _complete(client, _HEADER_AUTH)
        for fields in (
            {"state": _STATE, "code_verifier": _VERIFIER},
            {"code": "c;de", "state": _STATE, "code_verifier": _VERIFIER},
            {"code": "c0de-c0de-c0de-c0de", "state": "bad state", "code_verifier": _VERIFIER},
            {"code": "c0de-c0de-c0de-c0de", "state": _STATE, "code_verifier": "short"},
        ):
            assert client.post("/auth/native-exchange", data=fields).status_code == 400

    def test_tab_exchange_error_redirects_instead_of_stranding_the_tab(self) -> None:
        client = _client(_header_provider())
        response = client.get(
            "/auth/native-exchange",
            params={
                "code": "never-issued-code",
                "state": _STATE,
                "code_verifier": _VERIFIER,
            },
        )
        assert response.status_code == 302
        assert _callback_params(response)["error"] == ["exchange_failed"]
