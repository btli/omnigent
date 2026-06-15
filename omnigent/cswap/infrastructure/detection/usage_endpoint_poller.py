"""Proactive usage-limit gateway: probe each account for headroom.

Wraps the per-provider :mod:`probes` behind the :class:`UsageLimitGateway`
port. Given an account, it resolves the right credential, runs the
matching probe, and normalises the response into a
:class:`LimitDetectionResult` the track-usage-limit use case can persist.

Proactive polling is **opt-in** via the ``OMNIGENT_CSWAP_POLL_ENABLED``
environment variable; the background sweep checks :func:`is_poll_enabled`
before running. The gateway itself always supports both kinds so it can be
used for an on-demand probe regardless of the flag.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import httpx

from omnigent.cswap.application.ports.ports import UsageLimitGateway
from omnigent.cswap.domain.entities.provider_account import ProviderAccount
from omnigent.cswap.domain.value_objects.enums import AccountKind
from omnigent.cswap.domain.value_objects.limit_state import LimitDetectionResult
from omnigent.cswap.infrastructure.detection.credentials import (
    load_subscription_token,
    resolve_account_api_key,
)
from omnigent.cswap.infrastructure.detection.probes import (
    PROBE_TIMEOUT_S,
    ProbeOutcome,
    probe_anthropic,
    probe_openai,
)

ClientFactory = Callable[[], httpx.AsyncClient]
TokenLoader = Callable[[ProviderAccount], str | None]

POLL_ENABLED_ENV = "OMNIGENT_CSWAP_POLL_ENABLED"


def is_poll_enabled() -> bool:
    """Whether proactive polling is enabled via the environment flag."""
    return os.environ.get(POLL_ENABLED_ENV, "").strip() in ("1", "true", "True")


def _default_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=PROBE_TIMEOUT_S)


class UsageEndpointPoller(UsageLimitGateway):
    """Probes an account's live headroom via the provider API."""

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
        token_loader: TokenLoader = load_subscription_token,
        api_key_loader: TokenLoader = resolve_account_api_key,
    ) -> None:
        """:param client_factory: Builds the async HTTP client (injectable
        for tests). :param token_loader: Loads a subscription OAuth
        token. :param api_key_loader: Resolves an api_key secret.
        """
        self._client_factory = client_factory or _default_client_factory
        self._token_loader = token_loader
        self._api_key_loader = api_key_loader

    def supports(self, kind: AccountKind) -> bool:
        """Both subscription and api_key accounts can be probed."""
        return kind in ("subscription", "api_key")

    async def fetch_limit_state(
        self, account: ProviderAccount, *, now: int
    ) -> LimitDetectionResult | None:
        """Probe *account* and normalise the result, or ``None`` on error."""
        outcome = await self._probe(account, now=now)
        if outcome is None:
            return None
        return LimitDetectionResult(
            credential_id=account.id,
            is_limited=outcome.rate_limit.is_limited(outcome.status_code),
            source="poller",
            observed_at=now,
            windows=outcome.rate_limit.windows,
        )

    async def _probe(self, account: ProviderAccount, *, now: int) -> ProbeOutcome | None:
        """Resolve credentials and dispatch to the matching provider probe."""
        async with self._client_factory() as client:
            if account.family == "anthropic":
                if account.is_subscription:
                    token = self._token_loader(account)
                    if not token:
                        return None
                    return await probe_anthropic(client, now=now, oauth_token=token)
                key = self._api_key_loader(account)
                if not key:
                    return None
                return await probe_anthropic(client, now=now, api_key=key)

            bearer = (
                self._token_loader(account)
                if account.is_subscription
                else self._api_key_loader(account)
            )
            if not bearer:
                return None
            return await probe_openai(client, now=now, bearer_token=bearer)
