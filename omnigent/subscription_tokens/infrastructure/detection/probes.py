"""Async headroom probes against provider APIs.

Each probe sends the cheapest possible request (``max_tokens=1``) purely
to read the rate-limit response **headers** — the proactive equivalent of
waiting to hit a limit. The HTTP client is injected so tests can drive a
``httpx.MockTransport`` with no network. Probes never raise: any error
returns ``None`` and the account's stored state is left untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from omnigent.subscription_tokens.domain.value_objects.rate_limit_headers import RateLimitHeaders

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"
ANTHROPIC_PROBE_MODEL = "claude-haiku-4-5"

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_PROBE_MODEL = "gpt-4o-mini"

PROBE_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class ProbeOutcome:
    """A probe response reduced to what detection needs.

    :param rate_limit: Parsed rate-limit headers.
    :param status_code: HTTP status of the probe response (429 => limited).
    """

    rate_limit: RateLimitHeaders
    status_code: int


async def probe_anthropic(
    client: httpx.AsyncClient,
    *,
    now: int,
    oauth_token: str | None = None,
    api_key: str | None = None,
) -> ProbeOutcome | None:
    """Probe Anthropic and return its rate-limit headers, or ``None``.

    :param client: Injected async HTTP client.
    :param now: Current epoch seconds (for relative reset parsing).
    :param oauth_token: Subscription OAuth bearer token, if applicable.
    :param api_key: API key, if applicable. Exactly one of ``oauth_token``
        / ``api_key`` should be set.
    """
    headers = {"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    if oauth_token:
        headers["authorization"] = f"Bearer {oauth_token}"
        headers["anthropic-beta"] = ANTHROPIC_OAUTH_BETA
    elif api_key:
        headers["x-api-key"] = api_key
    else:
        return None
    body = {
        "model": ANTHROPIC_PROBE_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }
    try:
        resp = await client.post(
            ANTHROPIC_MESSAGES_URL, json=body, headers=headers, timeout=PROBE_TIMEOUT_S
        )
    except httpx.HTTPError:
        return None
    return ProbeOutcome(
        rate_limit=RateLimitHeaders.parse_anthropic(resp.headers, now=now),
        status_code=resp.status_code,
    )


async def probe_openai(
    client: httpx.AsyncClient, *, now: int, bearer_token: str
) -> ProbeOutcome | None:
    """Probe OpenAI and return its rate-limit headers, or ``None``.

    :param client: Injected async HTTP client.
    :param now: Current epoch seconds (for duration reset parsing).
    :param bearer_token: API key or subscription token (OpenAI uses
        ``Authorization: Bearer`` for both).
    """
    if not bearer_token:
        return None
    headers = {"authorization": f"Bearer {bearer_token}", "content-type": "application/json"}
    body = {
        "model": OPENAI_PROBE_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }
    try:
        resp = await client.post(
            OPENAI_CHAT_URL, json=body, headers=headers, timeout=PROBE_TIMEOUT_S
        )
    except httpx.HTTPError:
        return None
    return ProbeOutcome(
        rate_limit=RateLimitHeaders.parse_openai(resp.headers, now=now),
        status_code=resp.status_code,
    )
