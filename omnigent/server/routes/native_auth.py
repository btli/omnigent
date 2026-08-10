"""Native-shell login through a verified HTTPS Auth Tab callback.

The Android shell drives browser-based logins through Auth Tab, whose
cookie jar is isolated from the shell's WebView. These endpoints are the
completion legs of that flow, shaped like an OAuth public-client code
flow (RFC 7636) with Android Digital Asset Links binding the callback to
an operator-approved package and signing certificate:

1. The app opens ``GET /auth/native-complete?state=<nonce>&
   code_challenge=<S256>&client_package=<package>`` in the auth surface.
   Whatever authentication fronts the server (a front-door auth proxy
   and its IdP hops) runs in a real browser context.
2. Once the request arrives authenticated, the server creates a
   short-lived, single-use flow record binding the app's ``state`` and
   PKCE challenge to the authenticated identity and selected package,
   then 302s to ``https://<server-origin>/auth/native-callback`` with the
   state and an **opaque one-time code** — never a credential. Auth Tab
   returns that redirect only when the server's Digital Asset Links file
   verifies the *actual calling app's* package and signing certificate.
3. The app exchanges ``code + state + code_verifier`` at
   ``/auth/native-exchange`` for the credential: the proxy-forwarded
   per-user access token in header mode (Databricks Apps), or a freshly
   minted session JWT in oidc/accounts mode.

The exchange has two transports because a front-door proxy 302s every
unauthenticated *native* request to its IdP — a plain HTTPS ``POST``
from the app can never reach a header-mode server. The completion
redirect therefore tells the app which one to use:

- ``exchange=post`` (oidc/accounts): the app POSTs natively and the
  credential returns in the response body — it never touches a URL.
- ``exchange=tab`` (header mode): the app opens the exchange ``GET`` in
  a second auth-surface hop; the front-door session from step 1 is
  already warm in the browser, so the hop completes silently and the
  credential returns via one final verified HTTPS redirect. This is the
  only transport a front door leaves open, so the second-hop URL exposes
  the code + verifier and the final redirect exposes the credential to
  browser/proxy diagnostics. Both exchanges are transport-bound and the
  code is consumed on the first syntactically valid attempt.

There is deliberately no separate pre-registration endpoint: behind a
front door the app cannot reach one, and a public one would not
authenticate the initiator anyway. Operators configure approved Android
package/fingerprint pairs through ``android_auth_tab_apps`` in the server
config or ``OMNIGENT_ANDROID_AUTH_TAB_APPS`` as JSON. The default is an
empty allowlist, which creates no flow; Auth Tab verification then fails
closed and the Android shell returns to its inline-WebView fallback.

The flow records are held in process memory with a short TTL, mirroring
the OIDC router's CLI login tickets. Supported deployments keep one
server process/container; a custom multi-worker or multi-replica setup
can route the exchange to a process that never saw the code, yielding
``Unknown, expired, or already used code`` and an inline-login fallback.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import derive_code_challenge

_logger = logging.getLogger(__name__)

# The HTTPS callback stays on the authenticated server origin. Auth Tab
# verifies ownership with Digital Asset Links before returning it to the
# calling app; it is never caller-configurable.
_CALLBACK_PATH = "/auth/native-callback"

_ANDROID_AUTH_TAB_APPS_CONFIG = "android_auth_tab_apps"
_ANDROID_AUTH_TAB_APPS_ENV = "OMNIGENT_ANDROID_AUTH_TAB_APPS"
_ASSET_LINK_RELATION = "delegate_permission/common.handle_all_urls"

# Proxy-forwarded per-user access token read in header mode. Databricks
# Apps convention; other front doors can point the server at their
# equivalent header.
_FORWARDED_TOKEN_HEADER_ENV = "OMNIGENT_FORWARDED_TOKEN_HEADER"
_DEFAULT_FORWARDED_TOKEN_HEADER = "X-Forwarded-Access-Token"

# The app's nonce: URL-safe base64 alphabet, bounded. Anything else is
# rejected outright rather than echoed into the redirect.
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
# PKCE S256 challenge (base64url of a SHA-256 digest, unpadded) and
# verifier (RFC 7636 §4.1) shapes.
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
_FINGERPRINT_RE = re.compile(r"^[A-Fa-f0-9]{64}$")

# A code must be exchanged promptly: the ``post`` transport exchanges
# immediately, the ``tab`` transport within one silent browser hop.
_FLOW_TTL_SECONDS = 120
# Creating a flow requires an authenticated request, so this caps a
# *logged-in* flooder — still, never let the dict grow without limit.
_MAX_PENDING_FLOWS = 1000

# Session-JWT lifetime when no cookie config supplies one (defensive —
# both cookie modes always carry ``session_ttl_hours``).
_FALLBACK_TTL_SECONDS = 8 * 3600


@dataclass(frozen=True)
class AndroidAuthTabApp:
    """An Android package/signing-certificate association allowed to log in."""

    package_name: str
    sha256_cert_fingerprints: tuple[str, ...]


def _normalize_fingerprint(value: Any) -> str | None:
    compact = str(value).replace(":", "").strip()
    if not _FINGERPRINT_RE.fullmatch(compact):
        return None
    upper = compact.upper()
    return ":".join(upper[index : index + 2] for index in range(0, len(upper), 2))


def resolve_android_auth_tab_apps(
    server_config: dict[str, Any] | None = None,
) -> tuple[AndroidAuthTabApp, ...]:
    """Resolve approved Android apps from env JSON or server config.

    ``OMNIGENT_ANDROID_AUTH_TAB_APPS`` takes precedence when present. The
    accepted shape in either source is a list of mappings with
    ``package_name`` and ``sha256_cert_fingerprints``. Invalid entries are
    ignored; an absent or invalid configuration fails closed to ``()``.
    """
    env_value = os.environ.get(_ANDROID_AUTH_TAB_APPS_ENV)
    if env_value is not None:
        try:
            raw_apps: Any = json.loads(env_value)
        except json.JSONDecodeError as exc:
            _logger.warning("%s is invalid JSON: %s", _ANDROID_AUTH_TAB_APPS_ENV, exc)
            return ()
    else:
        raw_apps = (server_config or {}).get(_ANDROID_AUTH_TAB_APPS_CONFIG, [])

    if not isinstance(raw_apps, list):
        _logger.warning(
            "%s must be a list; native Auth Tab login disabled", _ANDROID_AUTH_TAB_APPS_CONFIG
        )
        return ()

    resolved: list[AndroidAuthTabApp] = []
    for index, raw_app in enumerate(raw_apps):
        if not isinstance(raw_app, dict):
            _logger.warning(
                "%s[%d] must be a mapping; ignoring", _ANDROID_AUTH_TAB_APPS_CONFIG, index
            )
            continue
        package_name = str(raw_app.get("package_name") or "").strip()
        if not _PACKAGE_RE.fullmatch(package_name):
            _logger.warning(
                "%s[%d] has an invalid package_name; ignoring",
                _ANDROID_AUTH_TAB_APPS_CONFIG,
                index,
            )
            continue
        raw_fingerprints = raw_app.get("sha256_cert_fingerprints")
        if isinstance(raw_fingerprints, str):
            raw_fingerprints = [raw_fingerprints]
        if not isinstance(raw_fingerprints, list):
            _logger.warning(
                "%s[%d] has no fingerprint list; ignoring",
                _ANDROID_AUTH_TAB_APPS_CONFIG,
                index,
            )
            continue
        fingerprints = tuple(
            dict.fromkeys(
                fingerprint
                for value in raw_fingerprints
                if (fingerprint := _normalize_fingerprint(value)) is not None
            )
        )
        if not fingerprints:
            _logger.warning(
                "%s[%d] has no valid SHA-256 fingerprints; ignoring",
                _ANDROID_AUTH_TAB_APPS_CONFIG,
                index,
            )
            continue
        resolved.append(AndroidAuthTabApp(package_name, fingerprints))
    return tuple(resolved)


def create_android_asset_links_router(
    allowed_apps: tuple[AndroidAuthTabApp, ...],
) -> APIRouter:
    """Serve the Digital Asset Links associations Auth Tab verifies."""
    router = APIRouter()

    @router.get("/.well-known/assetlinks.json", include_in_schema=False)
    async def android_asset_links() -> Response:
        response = JSONResponse(
            content=[
                {
                    "relation": [_ASSET_LINK_RELATION],
                    "target": {
                        "namespace": "android_app",
                        "package_name": app.package_name,
                        "sha256_cert_fingerprints": list(app.sha256_cert_fingerprints),
                    },
                }
                for app in allowed_apps
            ]
        )
        response.headers["Cache-Control"] = "public, max-age=300"
        return response

    return router


def resolve_forwarded_token_header() -> str:
    """Resolve the header carrying the proxy-forwarded user token.

    :returns: ``OMNIGENT_FORWARDED_TOKEN_HEADER`` when set and non-blank,
        else ``X-Forwarded-Access-Token``.
    """
    raw = os.environ.get(_FORWARDED_TOKEN_HEADER_ENV)
    if raw and raw.strip():
        return raw.strip()
    return _DEFAULT_FORWARDED_TOKEN_HEADER


@dataclass
class _NativeFlow:
    """A single-use login flow awaiting its code exchange.

    Created by the authenticated ``/auth/native-complete`` hit. After
    input-format validation, the first exchange attempt atomically pops
    the record before checking its state, verifier, or transport, so any
    syntactically valid failed guess also burns the code.

    :param state: The app's flow nonce, echoed on every redirect and
        required again at exchange.
    :param code_challenge: PKCE S256 challenge from the completion URL.
    :param user_id: The authenticated identity the flow completes as.
    :param token_type: ``"bearer"`` (header mode) or ``"session"``.
    :param exchange_transport: ``"tab"`` (header mode) or ``"post"``.
    :param forwarded_token: The proxy-forwarded token captured at
        completion time (header mode); ``None`` in cookie modes, which
        mint at exchange time instead.
    :param created_at: Unix timestamp for the TTL check.
    """

    state: str
    code_challenge: str
    user_id: str
    token_type: str
    exchange_transport: str
    forwarded_token: str | None
    created_at: float = field(default_factory=time.time)


def create_native_auth_router(
    auth_provider: UnifiedAuthProvider,
    allowed_apps: tuple[AndroidAuthTabApp, ...],
) -> APIRouter:
    """Create the router serving the native-login endpoints (mounted at ``/auth``).

    Mounted for every :class:`UnifiedAuthProvider` mode — unlike the
    login routers, which are per-mode — because header mode has no other
    ``/auth`` surface yet is exactly the mode that needs these endpoints.

    :param auth_provider: The active provider; supplies identity
        extraction and (in cookie modes) session-JWT minting.
    :param allowed_apps: Package/signing-certificate associations served
        through Digital Asset Links. An empty tuple disables flow
        creation and makes Auth Tab fall back after verification fails.
    :returns: A FastAPI router with the callback, completion, and exchange
        routes.
    """
    router = APIRouter()
    forwarded_token_header = resolve_forwarded_token_header()
    allowed_packages = frozenset(app.package_name for app in allowed_apps)

    # Pending flows keyed by their one-time code. In-memory like the OIDC
    # router's _cli_tickets: same TTL class, same single-process posture.
    _flows: dict[str, _NativeFlow] = {}

    def _evict_expired_flows() -> None:
        now = time.time()
        expired = [c for c, f in _flows.items() if now - f.created_at > _FLOW_TTL_SECONDS]
        for code in expired:
            del _flows[code]

    def _redirect_to_app(request: Request, params: dict[str, str]) -> Response:
        callback_url = request.url_for("native_auth_callback").include_query_params(**params)
        response = RedirectResponse(
            url=str(callback_url),
            status_code=302,
        )
        # Completion redirects carry one-time secrets — keep every one of
        # them out of every cache.
        response.headers["Cache-Control"] = "no-store"
        return response

    def _bad_request(error: str) -> Response:
        return JSONResponse(status_code=400, content={"error": error})

    @router.get("/native-callback", name="native_auth_callback", include_in_schema=False)
    async def native_auth_callback() -> Response:
        """Fail closed when a browser does not hand the callback to Auth Tab."""
        response = JSONResponse(
            status_code=400,
            content={"error": "native auth callback was not handled by a verified app"},
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/native-complete")
    async def native_complete(request: Request) -> Response:
        """Authenticate the flow and hand the app a one-time code.

        :param request: Carries ``state`` (the app's nonce),
            ``code_challenge`` (PKCE S256), ``client_package`` (the
            configured Android association), and, once authenticated,
            whatever identity the active mode uses.
        :returns: 302 to the server-origin verified callback URI with
            ``state``/``code``/``exchange`` (or ``error=no_token`` when
            header mode has no forwarded token to offer); 302 to the
            login page when a cookie-mode request is unauthenticated;
            400 on malformed parameters; 401 when header mode sees no
            identity.
        """
        state = request.query_params.get("state") or ""
        if not _STATE_RE.fullmatch(state):
            return _bad_request("Missing or malformed state parameter")
        challenge = request.query_params.get("code_challenge") or ""
        if not _CHALLENGE_RE.fullmatch(challenge):
            # No PKCE challenge, no flow: without one the code could be
            # exchanged by whoever sees the redirect, not just the app.
            return _bad_request("Missing or malformed code_challenge parameter")
        client_package = request.query_params.get("client_package") or ""
        if not _PACKAGE_RE.fullmatch(client_package):
            return _bad_request("Missing or malformed client_package parameter")
        if client_package not in allowed_packages:
            # No record is created. Redirecting only state + an error to
            # the HTTPS callback makes Auth Tab perform association
            # verification and close with a failure, which the app turns
            # into the inline login fallback.
            return _redirect_to_app(request, {"state": state, "error": "client_not_allowed"})

        user_id = auth_provider.get_user_id(request)
        if user_id is None:
            login_url = auth_provider.login_url
            if login_url:
                # Cookie modes own their login UX — bounce through it and
                # return here (state + challenge intact) once the session
                # cookie exists.
                return_params = urlencode(
                    {
                        "state": state,
                        "code_challenge": challenge,
                        "client_package": client_package,
                    }
                )
                return_to = quote(
                    f"/auth/native-complete?{return_params}",
                    safe="",
                )
                return RedirectResponse(
                    url=f"{login_url}?return_to={return_to}",
                    status_code=302,
                )
            # Header mode: an unauthenticated request means the fronting
            # proxy let it through without identity — nothing to grant.
            return JSONResponse(status_code=401, content={"error": "not authenticated"})

        if auth_provider._source == "header":
            forwarded_token = (request.headers.get(forwarded_token_header) or "").strip()
            if not forwarded_token:
                # Authenticated identity but no per-user token to relay
                # (proxy not configured to forward one). Tell the app so
                # it can fall back instead of waiting.
                _logger.info(
                    "native-complete: no %s to relay for %s",
                    forwarded_token_header,
                    user_id,
                )
                return _redirect_to_app(request, {"state": state, "error": "no_token"})
            token_type = "bearer"
            exchange_transport = "tab"
        else:
            forwarded_token = None
            token_type = "session"
            exchange_transport = "post"

        _evict_expired_flows()
        if len(_flows) >= _MAX_PENDING_FLOWS:
            return JSONResponse(status_code=429, content={"error": "too many pending logins"})

        code = secrets.token_urlsafe(32)
        _flows[code] = _NativeFlow(
            state=state,
            code_challenge=challenge,
            user_id=user_id,
            token_type=token_type,
            exchange_transport=exchange_transport,
            forwarded_token=forwarded_token,
        )
        return _redirect_to_app(
            request,
            {"state": state, "code": code, "exchange": exchange_transport},
        )

    def _exchange(
        code: str,
        state: str,
        verifier: str,
        transport: str,
    ) -> tuple[Response | None, _NativeFlow | None]:
        """Validate and atomically consume a flow for its credential.

        Parameter shapes are checked before looking up the record. A
        syntactically valid attempt then pops before the TTL, state,
        verifier, and transport checks, so any such mismatch burns the
        code and cannot be retried through the other transport.

        :returns: ``(error_response, None)`` on failure or
            ``(None, flow)`` on success.
        """
        if not _CODE_RE.fullmatch(code):
            return _bad_request("Missing or malformed code parameter"), None
        if not _STATE_RE.fullmatch(state):
            return _bad_request("Missing or malformed state parameter"), None
        if not _VERIFIER_RE.fullmatch(verifier):
            return _bad_request("Missing or malformed code_verifier parameter"), None

        flow = _flows.pop(code, None)
        if flow is None:
            return _bad_request("Unknown, expired, or already used code"), None
        if time.time() - flow.created_at > _FLOW_TTL_SECONDS:
            return _bad_request("Unknown, expired, or already used code"), None
        if not hmac.compare_digest(flow.state, state):
            return _bad_request("State mismatch"), None
        if not hmac.compare_digest(flow.code_challenge, derive_code_challenge(verifier)):
            return _bad_request("code_verifier does not match the challenge"), None
        if not hmac.compare_digest(flow.exchange_transport, transport):
            return _bad_request("Exchange transport mismatch"), None
        return None, flow

    def _credential_for(flow: _NativeFlow) -> str | None:
        """The credential a validated flow grants, or ``None`` if none can be."""
        if flow.token_type == "bearer":
            return flow.forwarded_token
        cookie_config = (
            auth_provider._oidc_config
            if auth_provider._source == "oidc"
            else auth_provider._accounts_config
        )
        ttl_seconds = (
            cookie_config.session_ttl_hours * 3600
            if cookie_config is not None
            else _FALLBACK_TTL_SECONDS
        )
        return auth_provider.mint_runner_token(flow.user_id, ttl_seconds)

    @router.post("/native-exchange")
    async def native_exchange_post(request: Request) -> Response:
        """Exchange a code for the credential over the response body.

        The transport for servers a native ``POST`` can reach
        (oidc/accounts): the credential never appears in a URL.

        :param request: Form fields ``code``, ``state``,
            ``code_verifier``. Query-string values are ignored.
        :returns: 200 with ``{"token_type": ..., "token": ...}``, or 400
            with an error.
        """
        form = await request.form()

        def _field(name: str) -> str:
            return str(form.get(name) or "")

        error, flow = _exchange(
            _field("code"),
            _field("state"),
            _field("code_verifier"),
            "post",
        )
        if error is not None:
            return error
        assert flow is not None
        token = _credential_for(flow)
        if not token:
            return _bad_request("No credential available for this flow")
        response = JSONResponse(
            status_code=200,
            content={"token_type": flow.token_type, "token": token},
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/native-exchange")
    async def native_exchange_tab(request: Request) -> Response:
        """Exchange a code through a second auth-surface hop (header mode).

        The only transport a front-door proxy leaves open: the browser
        that just completed the front-door login carries this GET through
        silently, and the credential returns via the final redirect. It
        is emitted only after the ``code_verifier`` proved the caller
        initiated the flow, so nothing a bystander captured earlier can
        reach this point.

        :param request: Query fields ``code``, ``state``,
            ``code_verifier``.
        :returns: 302 to the app's fixed callback URI with the
            credential, or 302 with ``error=exchange_failed`` so the app
            falls back instead of waiting.
        """
        code = request.query_params.get("code") or ""
        state = request.query_params.get("state") or ""
        verifier = request.query_params.get("code_verifier") or ""
        error, flow = _exchange(code, state, verifier, "tab")
        if error is not None:
            # The surface expects a redirect; a JSON body would strand the
            # tab. Redirect with an error when the state is at least
            # well-formed, else answer the 400 directly.
            if _STATE_RE.fullmatch(state):
                return _redirect_to_app(
                    request,
                    {"state": state, "error": "exchange_failed"},
                )
            return error
        assert flow is not None
        token = _credential_for(flow)
        if not token:
            return _redirect_to_app(request, {"state": state, "error": "exchange_failed"})
        return _redirect_to_app(
            request,
            {"state": state, "token_type": flow.token_type, "token": token},
        )

    return router
