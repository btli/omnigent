"""Tests for cswap detection: reactive parser, probes, poller, gateway."""

from __future__ import annotations

import httpx
import pytest

from omnigent.cswap.domain.entities.provider_account import ProviderAccount
from omnigent.cswap.infrastructure.detection.composite_usage_limit_gateway import (
    CompositeUsageLimitGateway,
)
from omnigent.cswap.infrastructure.detection.probes import probe_anthropic, probe_openai
from omnigent.cswap.infrastructure.detection.reactive_output_detector import (
    ReactiveOutputDetector,
)
from omnigent.cswap.infrastructure.detection.usage_endpoint_poller import (
    POLL_ENABLED_ENV,
    UsageEndpointPoller,
    is_poll_enabled,
)

# ── Reactive detector ──────────────────────────────────────


def test_reactive_detects_claude_anchored_phrase() -> None:
    result = ReactiveOutputDetector.parse("Error: Claude AI usage limit reached. Try later.")
    assert result.is_limited is True


def test_reactive_generic_phrase_requires_claude_mention() -> None:
    assert ReactiveOutputDetector.parse("usage limit reached").is_limited is False
    assert ReactiveOutputDetector.parse("claude: usage limit reached").is_limited is True


def test_reactive_extracts_reset_headers_sets_limited_until() -> None:
    text = (
        "claude usage limit reached\n"
        "anthropic-ratelimit-unified-5h-reset: 1700000000\n"
        "anthropic-ratelimit-unified-7d-reset 1700500000\n"
    )
    result = ReactiveOutputDetector.parse(text)
    assert result.reset_at_5h == 1700000000
    assert result.reset_at_7d == 1700500000

    detection = ReactiveOutputDetector.to_detection("cred-1", result, observed_at=1699999999)
    assert detection is not None
    assert detection.is_limited is True
    assert detection.source == "reactive"
    assert detection.observed_at == 1699999999
    # limited_until is the soonest parsed reset.
    assert detection.limited_until == 1700000000
    assert {w.label for w in detection.windows} == {"5h", "7d"}


def test_reactive_to_detection_none_when_not_limited() -> None:
    result = ReactiveOutputDetector.parse("all good")
    assert ReactiveOutputDetector.to_detection("c", result, observed_at=1) is None


def test_reactive_openai_detects_quota_codes() -> None:
    # Provider-specific error codes match on their own.
    assert ReactiveOutputDetector.parse("Error: insufficient_quota", family="openai").is_limited
    assert ReactiveOutputDetector.parse("rate_limit_exceeded", family="openai").is_limited
    # Generic phrasing needs an openai/gpt/codex mention.
    assert not ReactiveOutputDetector.parse("rate limit reached", family="openai").is_limited
    assert ReactiveOutputDetector.parse("OpenAI: rate limit reached", family="openai").is_limited
    # A Claude limit must NOT match under the openai family.
    assert not ReactiveOutputDetector.parse(
        "claude usage limit reached", family="openai"
    ).is_limited


def test_reactive_openai_detection_has_no_reset() -> None:
    # OpenAI surfaces no reset epoch in transcript text → limited_until None
    # (the facade applies a cooldown default).
    parsed = ReactiveOutputDetector.parse("insufficient_quota", family="openai")
    detection = ReactiveOutputDetector.to_detection("c", parsed, observed_at=100)
    assert detection is not None
    assert detection.limited_until is None
    assert detection.windows == ()


# ── Probes (httpx.MockTransport) ───────────────────────────


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def test_probe_anthropic_subscription_reads_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        assert request.headers["anthropic-beta"] == "oauth-2025-04-20"
        return httpx.Response(
            200,
            headers={
                "anthropic-ratelimit-unified-5h-limit": "100",
                "anthropic-ratelimit-unified-5h-remaining": "10",
                "anthropic-ratelimit-unified-5h-reset": "1700000000",
            },
        )

    async with _client(handler) as client:
        outcome = await probe_anthropic(client, now=1699999000, oauth_token="tok")
    assert outcome is not None
    assert outcome.status_code == 200
    assert outcome.rate_limit.windows[0].utilization_pct == 90
    assert outcome.rate_limit.is_limited(200) is False


async def test_probe_anthropic_429_is_limited() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "sk-key"
        return httpx.Response(429, headers={"retry-after": "30"})

    async with _client(handler) as client:
        outcome = await probe_anthropic(client, now=1000, api_key="sk-key")
    assert outcome is not None
    assert outcome.rate_limit.is_limited(outcome.status_code) is True
    assert outcome.rate_limit.retry_after_at == 1030


async def test_probe_openai_reads_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-oai"
        return httpx.Response(
            200,
            headers={
                "x-ratelimit-limit-requests": "100",
                "x-ratelimit-remaining-requests": "100",
                "x-ratelimit-reset-requests": "2s",
            },
        )

    async with _client(handler) as client:
        outcome = await probe_openai(client, now=500, bearer_token="sk-oai")
    assert outcome is not None
    assert outcome.rate_limit.windows[0].reset_at == 502


async def test_probe_returns_none_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with _client(handler) as client:
        assert await probe_anthropic(client, now=1, oauth_token="t") is None


# ── Poller ─────────────────────────────────────────────────


def _account(family: str, kind: str) -> ProviderAccount:
    return ProviderAccount(
        id=f"{family}-{kind}",
        name="acct",
        family=family,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        priority=0,
        claude_config_dir="~/.c" if family == "anthropic" else None,
        codex_config_dir="~/.x" if family == "openai" else None,
        api_key_ref="env:K" if kind == "api_key" else None,
    )


async def test_poller_subscription_anthropic_builds_detection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "60"})

    poller = UsageEndpointPoller(
        client_factory=lambda: _client(handler),
        token_loader=lambda acct: "oauth-tok",
        api_key_loader=lambda acct: None,
    )
    detection = await poller.fetch_limit_state(_account("anthropic", "subscription"), now=1000)
    assert detection is not None
    assert detection.is_limited is True
    assert detection.source == "poller"
    # Recovery time comes from retry-after (1000 + 60).
    assert detection.limited_until == 1060


async def test_poller_api_key_anthropic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "sk-resolved"
        return httpx.Response(200, headers={})

    poller = UsageEndpointPoller(
        client_factory=lambda: _client(handler),
        token_loader=lambda acct: None,
        api_key_loader=lambda acct: "sk-resolved",
    )
    detection = await poller.fetch_limit_state(_account("anthropic", "api_key"), now=1000)
    assert detection is not None
    assert detection.is_limited is False


async def test_poller_openai_subscription() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.openai.com"
        return httpx.Response(200, headers={})

    poller = UsageEndpointPoller(
        client_factory=lambda: _client(handler),
        token_loader=lambda acct: "chatgpt-tok",
        api_key_loader=lambda acct: None,
    )
    detection = await poller.fetch_limit_state(_account("openai", "subscription"), now=1000)
    assert detection is not None


async def test_poller_returns_none_when_no_credential() -> None:
    poller = UsageEndpointPoller(
        client_factory=lambda: _client(lambda r: httpx.Response(200)),
        token_loader=lambda acct: None,
        api_key_loader=lambda acct: None,
    )
    assert await poller.fetch_limit_state(_account("anthropic", "subscription"), now=1) is None


def test_is_poll_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(POLL_ENABLED_ENV, raising=False)
    assert is_poll_enabled() is False
    monkeypatch.setenv(POLL_ENABLED_ENV, "1")
    assert is_poll_enabled() is True


# ── Composite gateway ──────────────────────────────────────


async def test_composite_dispatches_to_first_supporting_adapter() -> None:
    poller = UsageEndpointPoller(
        client_factory=lambda: _client(lambda r: httpx.Response(200, headers={})),
        token_loader=lambda acct: "tok",
        api_key_loader=lambda acct: None,
    )
    gateway = CompositeUsageLimitGateway([poller])
    assert gateway.supports("subscription") is True
    detection = await gateway.fetch_limit_state(_account("anthropic", "subscription"), now=1)
    assert detection is not None
