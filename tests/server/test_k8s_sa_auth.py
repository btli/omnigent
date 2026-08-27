"""Tests for the in-cluster Kubernetes ServiceAccount Bearer auth fallback.

Covers both halves of :mod:`omnigent.server.auth`'s K8s SA support:

- :func:`resolve_k8s_sa_auth_config` — env-driven config resolution,
  default-off, fail-loud on a half-configured explicit opt-in.
- :meth:`UnifiedAuthProvider._check_k8s_service_account` (and its
  wiring into :meth:`UnifiedAuthProvider._check_cookie` /
  :meth:`UnifiedAuthProvider.get_user_id`) — signature/issuer/audience/
  subject verification, with the primary HS256 session-token decode
  proven unchanged.

Tokens are genuinely RS256-signed and verified via a real ``jwt.decode``
call; only the network JWKS fetch is stubbed (the same boundary
``tests/server/test_oidc_callback.py`` stubs for the generic-OIDC
``id_token`` path), so this exercises the production verification logic
end to end, offline.
"""

from __future__ import annotations

import datetime
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import jwt
import pytest
from jwt.algorithms import RSAAlgorithm

from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.auth import (
    RESERVED_USER_LOCAL,
    K8sServiceAccountAuthConfig,
    UnifiedAuthProvider,
    resolve_k8s_sa_auth_config,
)
from omnigent.server.oidc import mint_session_token

_ISSUER = "https://kubernetes.default.svc.cluster.local"
_AUDIENCE = "omnigent-webhook-receiver"
_SUBJECT = "system:serviceaccount:webhooks:webhook"


@dataclass
class _FakeRequest:
    """Minimal stand-in for the ``HTTPConnection`` duck type ``_check_cookie`` reads."""

    cookies: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


class _SaKeys:
    """An RSA keypair plus the JWKS signing key derived from its public half.

    Mirrors ``tests/server/test_oidc_callback.py``'s ``_IdpKeys`` helper.
    """

    def __init__(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk_dict = json.loads(RSAAlgorithm.to_jwk(self.private_key.public_key()))
        jwk_dict["alg"] = "RS256"
        self.signing_key = jwt.PyJWK.from_dict(jwk_dict)

    def sign(self, claims: dict[str, object]) -> str:
        """Sign *claims* into an RS256 JWT, filling iss/aud/sub/exp if absent.

        :param claims: Claim overrides, e.g. ``{"aud": "wrong-audience"}``.
        :returns: A compact-serialized signed JWT string.
        """
        now = int(time.time())
        payload: dict[str, object] = {
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "sub": _SUBJECT,
            "iat": now,
            "exp": now + 300,
        }
        payload.update(claims)
        return jwt.encode(payload, self.private_key, algorithm="RS256")


def _sa_config(**overrides: object) -> K8sServiceAccountAuthConfig:
    """Build a :class:`K8sServiceAccountAuthConfig` with test defaults."""
    defaults: dict[str, object] = {
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
        "subjects": frozenset({_SUBJECT}),
        "jwks_uri": f"{_ISSUER}/openid/v1/jwks",
        "ssl_context": None,
    }
    defaults.update(overrides)
    return K8sServiceAccountAuthConfig(**defaults)  # type: ignore[arg-type]


def _accounts_config() -> AccountsConfig:
    """Build a minimal, valid :class:`AccountsConfig` for cookie-check plumbing."""
    return AccountsConfig(
        cookie_secret=bytes.fromhex("bb" * 32),
        session_ttl_hours=8,
        base_url="http://localhost:6767",
        init_admin_password=None,
        invite_ttl_seconds=72 * 3600,
        magic_ttl_seconds=600,
    )


@pytest.fixture
def sa_keys(monkeypatch: pytest.MonkeyPatch) -> _SaKeys:
    """SA keypair with the JWKS signing-key lookup stubbed to return it.

    Stubs only ``PyJWKClient.get_signing_key_from_jwt`` (no network JWKS
    fetch); the returned key still goes through a real ``jwt.decode``
    signature/issuer/audience check.
    """
    keys = _SaKeys()
    monkeypatch.setattr(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda self, token: keys.signing_key,
    )
    return keys


# ── _check_k8s_service_account (direct) ────────────────────────────


class TestCheckK8sServiceAccount:
    def test_valid_token_accepted(self, sa_keys: _SaKeys) -> None:
        provider = UnifiedAuthProvider(source="accounts", k8s_sa_config=_sa_config())
        token = sa_keys.sign({})
        assert provider._check_k8s_service_account(token) == _SUBJECT

    def test_wrong_audience_rejected(self, sa_keys: _SaKeys) -> None:
        provider = UnifiedAuthProvider(source="accounts", k8s_sa_config=_sa_config())
        token = sa_keys.sign({"aud": "some-other-audience"})
        assert provider._check_k8s_service_account(token) is None

    def test_wrong_issuer_rejected(self, sa_keys: _SaKeys) -> None:
        provider = UnifiedAuthProvider(source="accounts", k8s_sa_config=_sa_config())
        token = sa_keys.sign({"iss": "https://not-kubernetes.example.com"})
        assert provider._check_k8s_service_account(token) is None

    def test_other_namespace_subject_rejected(self, sa_keys: _SaKeys) -> None:
        provider = UnifiedAuthProvider(source="accounts", k8s_sa_config=_sa_config())
        token = sa_keys.sign({"sub": "system:serviceaccount:attacker-ns:attacker-sa"})
        assert provider._check_k8s_service_account(token) is None

    def test_near_miss_subject_rejected(self, sa_keys: _SaKeys) -> None:
        """A subject that merely starts with the allowlisted one is not a match."""
        provider = UnifiedAuthProvider(source="accounts", k8s_sa_config=_sa_config())
        token = sa_keys.sign({"sub": _SUBJECT + "-imposter"})
        assert provider._check_k8s_service_account(token) is None

    def test_reserved_subject_rejected_even_if_allowlisted(self, sa_keys: _SaKeys) -> None:
        """A reserved name can never authenticate, even via a misconfigured allowlist."""
        provider = UnifiedAuthProvider(
            source="accounts",
            k8s_sa_config=_sa_config(subjects=frozenset({RESERVED_USER_LOCAL})),
        )
        token = sa_keys.sign({"sub": RESERVED_USER_LOCAL})
        assert provider._check_k8s_service_account(token) is None

    def test_expired_rejected(self, sa_keys: _SaKeys) -> None:
        provider = UnifiedAuthProvider(source="accounts", k8s_sa_config=_sa_config())
        now = int(time.time())
        token = sa_keys.sign({"iat": now - 3600, "exp": now - 1})
        assert provider._check_k8s_service_account(token) is None

    def test_malformed_token_rejected(self, sa_keys: _SaKeys) -> None:
        provider = UnifiedAuthProvider(source="accounts", k8s_sa_config=_sa_config())
        assert provider._check_k8s_service_account("not-a-jwt-at-all") is None

    def test_disabled_returns_none(self, sa_keys: _SaKeys) -> None:
        """No config attached (feature off) rejects immediately, no JWKS lookup needed."""
        provider = UnifiedAuthProvider(source="accounts", k8s_sa_config=None)
        token = sa_keys.sign({})
        assert provider._check_k8s_service_account(token) is None


# ── _check_cookie / get_user_id (end to end) ────────────────────────


class TestCheckCookieK8sFallback:
    def test_sa_bearer_accepted_via_get_user_id(self, sa_keys: _SaKeys) -> None:
        provider = UnifiedAuthProvider(
            source="accounts",
            accounts_config=_accounts_config(),
            k8s_sa_config=_sa_config(),
        )
        token = sa_keys.sign({})
        request = _FakeRequest(headers={"Authorization": f"Bearer {token}"})
        assert provider.get_user_id(request) == _SUBJECT

    def test_sa_bearer_rejected_when_feature_disabled(self, sa_keys: _SaKeys) -> None:
        provider = UnifiedAuthProvider(
            source="accounts",
            accounts_config=_accounts_config(),
            k8s_sa_config=None,
        )
        token = sa_keys.sign({})
        request = _FakeRequest(headers={"Authorization": f"Bearer {token}"})
        assert provider.get_user_id(request) is None

    def test_human_session_token_unaffected_by_k8s_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid human/CLI HS256 token succeeds on the first decode and never
        reaches the K8s verification path — proven by making that path explode
        if it is ever invoked."""

        def _must_not_be_called(self: object, token: object) -> None:
            raise AssertionError(
                "K8s SA verification must not run for a valid HS256 session token"
            )

        monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt", _must_not_be_called)

        accounts_config = _accounts_config()
        provider = UnifiedAuthProvider(
            source="accounts",
            accounts_config=accounts_config,
            k8s_sa_config=_sa_config(),
        )
        session_token = mint_session_token(
            "alice@example.com", accounts_config.cookie_secret, 3600, "accounts"
        )
        request = _FakeRequest(cookies={accounts_config.session_cookie_name: session_token})
        assert provider.get_user_id(request) == "alice@example.com"

    def test_garbage_bearer_rejected_when_feature_disabled(self) -> None:
        """The pre-existing 401 path for an unrecognized Bearer token is unchanged."""
        accounts_config = _accounts_config()
        provider = UnifiedAuthProvider(
            source="accounts", accounts_config=accounts_config, k8s_sa_config=None
        )
        request = _FakeRequest(headers={"Authorization": "Bearer garbage-token"})
        assert provider.get_user_id(request) is None


# ── resolve_k8s_sa_auth_config ──────────────────────────────────────


class TestResolveK8sSaAuthConfig:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OMNIGENT_K8S_SA_AUTH_ENABLED", raising=False)
        assert resolve_k8s_sa_auth_config() is None

    def test_explicitly_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNIGENT_K8S_SA_AUTH_ENABLED", "0")
        assert resolve_k8s_sa_auth_config() is None

    def _set_full_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNIGENT_K8S_SA_AUTH_ENABLED", "1")
        monkeypatch.setenv("OMNIGENT_K8S_SA_ISSUER", _ISSUER)
        monkeypatch.setenv("OMNIGENT_K8S_SA_AUDIENCE", _AUDIENCE)
        monkeypatch.setenv("OMNIGENT_K8S_SA_SUBJECTS", _SUBJECT)
        monkeypatch.delenv("OMNIGENT_K8S_SA_JWKS_URI", raising=False)
        monkeypatch.delenv("OMNIGENT_K8S_SA_CA_BUNDLE", raising=False)

    def test_enabled_missing_issuer_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_full_env(monkeypatch)
        monkeypatch.delenv("OMNIGENT_K8S_SA_ISSUER", raising=False)
        with pytest.raises(RuntimeError, match="OMNIGENT_K8S_SA_ISSUER"):
            resolve_k8s_sa_auth_config()

    def test_enabled_missing_audience_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_full_env(monkeypatch)
        monkeypatch.delenv("OMNIGENT_K8S_SA_AUDIENCE", raising=False)
        with pytest.raises(RuntimeError, match="OMNIGENT_K8S_SA_AUDIENCE"):
            resolve_k8s_sa_auth_config()

    def test_enabled_missing_subjects_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_full_env(monkeypatch)
        monkeypatch.delenv("OMNIGENT_K8S_SA_SUBJECTS", raising=False)
        with pytest.raises(RuntimeError, match="OMNIGENT_K8S_SA_SUBJECTS"):
            resolve_k8s_sa_auth_config()

    def test_enabled_full_config_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_full_env(monkeypatch)
        config = resolve_k8s_sa_auth_config()
        assert config is not None
        assert config.issuer == _ISSUER
        assert config.audience == _AUDIENCE
        assert config.subjects == frozenset({_SUBJECT})
        assert config.jwks_uri == f"{_ISSUER}/openid/v1/jwks"
        # No in-cluster CA bundle on this test machine, no explicit override:
        # falls back to the interpreter's default trust store.
        assert config.ssl_context is None

    def test_jwks_uri_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_full_env(monkeypatch)
        monkeypatch.setenv("OMNIGENT_K8S_SA_JWKS_URI", "https://example.com/custom/jwks")
        config = resolve_k8s_sa_auth_config()
        assert config is not None
        assert config.jwks_uri == "https://example.com/custom/jwks"

    def test_ca_bundle_override_builds_ssl_context(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A syntactically valid (self-signed) PEM cert is enough for
        # ssl.create_default_context to accept the file as a CA bundle.
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .sign(key, hashes.SHA256())
        )
        ca_path = tmp_path / "ca.crt"
        ca_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        self._set_full_env(monkeypatch)
        monkeypatch.setenv("OMNIGENT_K8S_SA_CA_BUNDLE", str(ca_path))
        config = resolve_k8s_sa_auth_config()
        assert config is not None
        assert config.ssl_context is not None

    def test_subjects_supports_comma_separated_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_full_env(monkeypatch)
        monkeypatch.setenv(
            "OMNIGENT_K8S_SA_SUBJECTS",
            "system:serviceaccount:webhooks:webhook, system:serviceaccount:webhooks:other",
        )
        config = resolve_k8s_sa_auth_config()
        assert config is not None
        assert config.subjects == frozenset(
            {
                "system:serviceaccount:webhooks:webhook",
                "system:serviceaccount:webhooks:other",
            }
        )
