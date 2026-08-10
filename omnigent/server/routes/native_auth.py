"""Native-shell login: ``/auth/native-complete`` + ``/auth/native-exchange``.

The mobile shells drive browser-based logins through an OS auth surface
(Android Auth Tab / iOS ``ASWebAuthenticationSession``) whose cookie jar
is isolated from the shell's WebView. These endpoints are the completion
legs of that flow, shaped like an OAuth public-client code flow (RFC
7636) so no bearer material is ever handed to a browser redirect:

1. The app opens ``GET /auth/native-complete?state=<nonce>&
   code_challenge=<S256>`` in the auth surface. Whatever authentication
   fronts the server (a front-door auth proxy and its IdP hops) runs in
   a real browser context.
2. Once the request arrives authenticated, the server creates a
   short-lived, single-use flow record binding the app's ``state`` and
   PKCE challenge to the authenticated identity, and 302s to the fixed
   ``omnigent://auth-callback`` target with the state and an **opaque
   one-time code** — never a credential.
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
  credential returns via one final redirect. This is the only transport
  a front door leaves open, so the credential's single appearance in a
  redirect URI is unavoidable there — but it is emitted only after the
  ``code_verifier`` proved the caller is the app that initiated step 1,
  so a leaked completion URL (browser history, an intercepted intent)
  yields an attacker nothing.

There is deliberately no separate pre-registration endpoint: behind a
front door the app cannot reach one, and a public one would not
authenticate the initiator anyway (anyone could register). The
initiation record is created at the authenticated completion hit, and
the binding property comes from PKCE — only the holder of the verifier
can turn the code into a credential — plus atomic single-use
consumption of the record.

The flow records are held in process memory with a short TTL, mirroring
the OIDC router's CLI login tickets (the same lifetime class); a
database table for two-minute nonces would outlive its purpose and
break the routers' shared single-process posture.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import derive_code_challenge

_logger = logging.getLogger(__name__)

# Fixed completion target. The scheme/host pair is registered by the
# mobile shells; deliberately NOT caller-configurable per request so the
# endpoints can never be aimed at a foreign URI.
_CALLBACK_URI = "omnigent://auth-callback"

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

# A code must be exchanged promptly: the ``post`` transport exchanges
# immediately, the ``tab`` transport within one silent browser hop.
_FLOW_TTL_SECONDS = 120
# Creating a flow requires an authenticated request, so this caps a
# *logged-in* flooder — still, never let the dict grow without limit.
_MAX_PENDING_FLOWS = 1000

# Session-JWT lifetime when no cookie config supplies one (defensive —
# both cookie modes always carry ``session_ttl_hours``).
_FALLBACK_TTL_SECONDS = 8 * 3600


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

    Created by the authenticated ``/auth/native-complete`` hit, consumed
    atomically (``dict.pop``) by the first ``/auth/native-exchange``
    attempt — matching or not, so a failed guess also burns the code.

    :param state: The app's flow nonce, echoed on every redirect and
        required again at exchange.
    :param code_challenge: PKCE S256 challenge from the completion URL.
    :param user_id: The authenticated identity the flow completes as.
    :param token_type: ``"bearer"`` (header mode) or ``"session"``.
    :param forwarded_token: The proxy-forwarded token captured at
        completion time (header mode); ``None`` in cookie modes, which
        mint at exchange time instead.
    :param created_at: Unix timestamp for the TTL check.
    """

    state: str
    code_challenge: str
    user_id: str
    token_type: str
    forwarded_token: str | None
    created_at: float = field(default_factory=time.time)


def create_native_auth_router(auth_provider: UnifiedAuthProvider) -> APIRouter:
    """Create the router serving the native-login endpoints (mounted at ``/auth``).

    Mounted for every :class:`UnifiedAuthProvider` mode — unlike the
    login routers, which are per-mode — because header mode has no other
    ``/auth`` surface yet is exactly the mode that needs these endpoints.

    :param auth_provider: The active provider; supplies identity
        extraction and (in cookie modes) session-JWT minting.
    :returns: A FastAPI router with the completion and exchange routes.
    """
    router = APIRouter()
    forwarded_token_header = resolve_forwarded_token_header()

    # Pending flows keyed by their one-time code. In-memory like the OIDC
    # router's _cli_tickets: same TTL class, same single-process posture.
    _flows: dict[str, _NativeFlow] = {}

    def _evict_expired_flows() -> None:
        now = time.time()
        expired = [c for c, f in _flows.items() if now - f.created_at > _FLOW_TTL_SECONDS]
        for code in expired:
            del _flows[code]

    def _redirect_to_app(params: dict[str, str]) -> Response:
        response = RedirectResponse(
            url=f"{_CALLBACK_URI}?{urlencode(params)}",
            status_code=302,
        )
        # Completion redirects carry one-time secrets — keep every one of
        # them out of every cache.
        response.headers["Cache-Control"] = "no-store"
        return response

    def _bad_request(error: str) -> Response:
        return JSONResponse(status_code=400, content={"error": error})

    @router.get("/native-complete")
    async def native_complete(request: Request) -> Response:
        """Authenticate the flow and hand the app a one-time code.

        :param request: Carries ``state`` (the app's nonce) and
            ``code_challenge`` (PKCE S256) plus, once authenticated,
            whatever identity the active mode uses.
        :returns: 302 to the app's fixed callback URI with
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

        user_id = auth_provider.get_user_id(request)
        if user_id is None:
            login_url = auth_provider.login_url
            if login_url:
                # Cookie modes own their login UX — bounce through it and
                # return here (state + challenge intact) once the session
                # cookie exists.
                return_to = quote(
                    f"/auth/native-complete?state={state}&code_challenge={challenge}",
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
                return _redirect_to_app({"state": state, "error": "no_token"})
            token_type = "bearer"
        else:
            forwarded_token = None
            token_type = "session"

        _evict_expired_flows()
        if len(_flows) >= _MAX_PENDING_FLOWS:
            return JSONResponse(status_code=429, content={"error": "too many pending logins"})

        code = secrets.token_urlsafe(32)
        _flows[code] = _NativeFlow(
            state=state,
            code_challenge=challenge,
            user_id=user_id,
            token_type=token_type,
            forwarded_token=forwarded_token,
        )
        # A native POST can't cross a front-door proxy, so header mode
        # exchanges through a second (already-authenticated) browser hop.
        exchange = "tab" if auth_provider._source == "header" else "post"
        return _redirect_to_app({"state": state, "code": code, "exchange": exchange})

    def _exchange(
        code: str,
        state: str,
        verifier: str,
    ) -> tuple[Response | None, _NativeFlow | None]:
        """Validate and atomically consume a flow for its credential.

        The pop happens before any check, so a failed guess burns the
        code — a code is never available for a second attempt, matching
        or not.

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

        :param request: Form or query fields ``code``, ``state``,
            ``code_verifier``.
        :returns: 200 with ``{"token_type": ..., "token": ...}``, or 400
            with an error.
        """
        form = await request.form()

        def _field(name: str) -> str:
            return str(form.get(name) or request.query_params.get(name) or "")

        error, flow = _exchange(_field("code"), _field("state"), _field("code_verifier"))
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
        error, flow = _exchange(code, state, verifier)
        if error is not None:
            # The surface expects a redirect; a JSON body would strand the
            # tab. Redirect with an error when the state is at least
            # well-formed, else answer the 400 directly.
            if _STATE_RE.fullmatch(state):
                return _redirect_to_app({"state": state, "error": "exchange_failed"})
            return error
        assert flow is not None
        token = _credential_for(flow)
        if not token:
            return _redirect_to_app({"state": state, "error": "exchange_failed"})
        return _redirect_to_app({"state": state, "token_type": flow.token_type, "token": token})

    return router
