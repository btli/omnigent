"""Unit tests for the cswap pure domain layer.

These exercise the value objects and entities with no I/O: usage windows,
limit-state availability/headroom, rate-limit header parsing for all three
provider header families, and the headroom-plus-reset rotation policy
(including subscription→api_key tier fallback).
"""

from __future__ import annotations

from omnigent.cswap.domain.entities.credential_pool import CredentialPool
from omnigent.cswap.domain.entities.provider_account import ProviderAccount
from omnigent.cswap.domain.value_objects.limit_state import (
    LimitDetectionResult,
    LimitState,
)
from omnigent.cswap.domain.value_objects.rate_limit_headers import RateLimitHeaders
from omnigent.cswap.domain.value_objects.rotation_policy import (
    RotationCandidate,
    RotationPolicy,
)
from omnigent.cswap.domain.value_objects.usage_window import UsageWindow


def _state(
    credential_id: str,
    *,
    is_limited: bool = False,
    limited_until: int | None = None,
    windows: tuple[UsageWindow, ...] = (),
    last_checked_at: int | None = None,
) -> LimitState:
    # Mirror how detections set limited_until: the soonest known window reset.
    if is_limited and limited_until is None and windows:
        resets = [w.reset_at for w in windows if w.reset_at is not None]
        limited_until = min(resets) if resets else None
    return LimitState(
        credential_id=credential_id,
        is_limited=is_limited,
        limited_until=limited_until,
        windows=windows,
        source="poller" if (windows or is_limited) else None,
        last_checked_at=last_checked_at,
    )


# ── UsageWindow ────────────────────────────────────────────


def test_usage_window_clamps_and_reports_remaining() -> None:
    assert UsageWindow("5h", 150, None).utilization_pct == 100
    assert UsageWindow("5h", -5, None).utilization_pct == 0
    assert UsageWindow("5h", 75, None).remaining_pct() == 25
    assert UsageWindow("5h", None, None).remaining_pct() is None
    assert UsageWindow("5h", 100, None).is_exhausted() is True
    assert UsageWindow("5h", 99, None).is_exhausted() is False


# ── LimitState ─────────────────────────────────────────────


def test_limit_state_available_when_not_limited() -> None:
    state = _state("a", windows=(UsageWindow("5h", 50, 2000),))
    assert state.is_available_now(now=1000) is True


def test_limit_state_limited_until_reset_passes() -> None:
    state = _state("a", is_limited=True, windows=(UsageWindow("5h", 100, 2000),))
    assert state.is_available_now(now=1999) is False
    assert state.is_available_now(now=2000) is True


def test_limit_state_limited_without_reset_is_unavailable() -> None:
    state = _state("a", is_limited=True, windows=(UsageWindow("5h", 100, None),))
    assert state.is_available_now(now=10**12) is False


def test_limit_state_headroom_is_minimum_across_windows() -> None:
    state = _state(
        "a",
        windows=(UsageWindow("5h", 90, 2000), UsageWindow("7d", 40, 3000)),
    )
    # 5h has 10% remaining, 7d has 60% — binding constraint is 10%.
    assert state.remaining_headroom_pct() == 10
    assert state.earliest_reset_at() == 2000


def test_limit_state_status_unknown_then_available_then_limited() -> None:
    assert LimitState("a").to_status(now=1000) == "unknown"
    assert _state("a", windows=(UsageWindow("5h", 10, 2000),)).to_status(1000) == "available"
    limited = _state("a", is_limited=True, windows=(UsageWindow("5h", 100, 2000),))
    assert limited.to_status(now=1000) == "limited"


def test_detection_result_projects_to_limit_state() -> None:
    result = LimitDetectionResult(
        credential_id="a",
        is_limited=True,
        source="reactive",
        observed_at=1234,
        windows=(UsageWindow("5h", 100, 5000),),
    )
    state = result.to_limit_state()
    assert state.credential_id == "a"
    assert state.is_limited is True
    assert state.last_checked_at == 1234
    assert state.source == "reactive"


# ── RateLimitHeaders ───────────────────────────────────────


def test_parse_anthropic_unified_subscription_headers() -> None:
    headers = {
        "anthropic-ratelimit-unified-5h-limit": "100",
        "anthropic-ratelimit-unified-5h-remaining": "25",
        "anthropic-ratelimit-unified-5h-reset": "1700000000",
        "anthropic-ratelimit-unified-7d-limit": "1000",
        "anthropic-ratelimit-unified-7d-remaining": "900",
        "anthropic-ratelimit-unified-7d-reset": "1700500000",
    }
    parsed = RateLimitHeaders.parse_anthropic(headers, now=1699999000)
    by_label = {w.label: w for w in parsed.windows}
    assert by_label["5h"].utilization_pct == 75
    assert by_label["5h"].reset_at == 1700000000
    assert by_label["7d"].utilization_pct == 10
    assert parsed.is_limited(200) is False


def test_parse_anthropic_api_key_headers_rfc3339_and_retry_after() -> None:
    headers = {
        "anthropic-ratelimit-requests-limit": "50",
        "anthropic-ratelimit-requests-remaining": "0",
        "anthropic-ratelimit-requests-reset": "2026-06-14T00:00:10Z",
        "retry-after": "10",
    }
    parsed = RateLimitHeaders.parse_anthropic(headers, now=1000)
    req = next(w for w in parsed.windows if w.label == "requests")
    assert req.utilization_pct == 100
    assert req.is_exhausted() is True
    assert parsed.retry_after_at == 1010
    assert parsed.is_limited(429) is True


def test_parse_openai_duration_resets() -> None:
    headers = {
        "x-ratelimit-limit-requests": "60",
        "x-ratelimit-remaining-requests": "59",
        "x-ratelimit-reset-requests": "1s",
        "x-ratelimit-limit-tokens": "1000",
        "x-ratelimit-remaining-tokens": "500",
        "x-ratelimit-reset-tokens": "6m0s",
    }
    parsed = RateLimitHeaders.parse_openai(headers, now=1000)
    by_label = {w.label: w for w in parsed.windows}
    assert by_label["requests"].utilization_pct == 2
    assert by_label["requests"].reset_at == 1001
    assert by_label["tokens"].utilization_pct == 50
    assert by_label["tokens"].reset_at == 1360


def test_parse_empty_headers_yields_no_windows() -> None:
    parsed = RateLimitHeaders.parse_anthropic({}, now=1000)
    assert parsed.windows == ()
    assert parsed.is_limited(200) is False


def test_retry_after_parses_seconds_rfc3339_and_http_date() -> None:
    # Delta seconds.
    assert RateLimitHeaders.parse_anthropic({"retry-after": "30"}, now=1000).retry_after_at == 1030
    # RFC 3339.
    rfc = RateLimitHeaders.parse_anthropic(
        {"retry-after": "2026-06-14T00:00:10Z"}, now=1000
    ).retry_after_at
    assert rfc is not None
    # RFC 7231 HTTP date.
    http = RateLimitHeaders.parse_anthropic(
        {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}, now=1000
    ).retry_after_at
    assert http == 1445412480


def test_recovery_at_waits_for_latest_exhausted_window() -> None:
    # Both windows exhausted: recover only when the LATER one resets (avoids
    # re-selecting while the 7d window is still at 100%).
    headers = RateLimitHeaders(
        windows=(UsageWindow("5h", 100, 2000), UsageWindow("7d", 100, 5000)),
        retry_after_at=None,
    )
    assert headers.recovery_at() == 5000
    # retry-after takes precedence when present.
    headers2 = RateLimitHeaders(windows=(UsageWindow("5h", 100, 2000),), retry_after_at=9999)
    assert headers2.recovery_at() == 9999
    # No exhausted window → soonest reset (best guess); none → None.
    headers3 = RateLimitHeaders(
        windows=(UsageWindow("5h", 50, 2000), UsageWindow("7d", 30, 5000)),
        retry_after_at=None,
    )
    assert headers3.recovery_at() == 2000
    assert RateLimitHeaders(windows=(), retry_after_at=None).recovery_at() is None


# ── RotationPolicy ─────────────────────────────────────────


def _candidate(cid: str, priority: int, kind: str, state: LimitState) -> RotationCandidate:
    return RotationCandidate(credential_id=cid, priority=priority, kind=kind, limit_state=state)  # type: ignore[arg-type]


def test_rotation_prefers_most_headroom_among_available() -> None:
    a = _candidate("a", 0, "subscription", _state("a", windows=(UsageWindow("5h", 80, 9999),)))
    b = _candidate("b", 1, "subscription", _state("b", windows=(UsageWindow("5h", 40, 9999),)))
    # b has more headroom (60 vs 20) despite lower priority number on a.
    assert RotationPolicy.select([a, b], now=1000) == "b"


def test_rotation_prefers_subscription_tier_over_api_key() -> None:
    sub = _candidate(
        "sub", 5, "subscription", _state("sub", windows=(UsageWindow("5h", 90, 9999),))
    )
    api = _candidate("api", 0, "api_key", _state("api", windows=(UsageWindow("5h", 1, 9999),)))
    # api has way more headroom, but subscription tier wins outright.
    assert RotationPolicy.select([sub, api], now=1000) == "sub"


def test_rotation_tier_fallback_to_api_key_when_all_subs_limited() -> None:
    sub = _candidate(
        "sub",
        0,
        "subscription",
        _state("sub", is_limited=True, windows=(UsageWindow("5h", 100, 5000),)),
    )
    api = _candidate("api", 9, "api_key", _state("api", windows=(UsageWindow("5h", 10, 9999),)))
    assert RotationPolicy.select([sub, api], now=1000) == "api"
    # With tier fallback disabled, no subscription available -> best-effort sub.
    assert RotationPolicy.select([sub, api], now=1000, allow_tier_fallback=False) == "sub"


def test_rotation_best_effort_picks_soonest_reset_when_none_available() -> None:
    a = _candidate(
        "a",
        0,
        "subscription",
        _state("a", is_limited=True, windows=(UsageWindow("5h", 100, 2000),)),
    )
    b = _candidate(
        "b",
        1,
        "subscription",
        _state("b", is_limited=True, windows=(UsageWindow("5h", 100, 1500),)),
    )
    assert RotationPolicy.select([a, b], now=1000, best_effort=True) == "b"
    assert RotationPolicy.select([a, b], now=1000, best_effort=False) is None


def test_rotation_excludes_current_account() -> None:
    a = _candidate("a", 0, "subscription", _state("a", windows=(UsageWindow("5h", 10, 9999),)))
    b = _candidate("b", 1, "subscription", _state("b", windows=(UsageWindow("5h", 20, 9999),)))
    assert RotationPolicy.select([a, b], now=1000, exclude_credential_id="b") == "a"


def test_rotation_returns_none_for_empty() -> None:
    assert RotationPolicy.select([], now=1000) is None


def test_rotation_best_effort_all_api_keys_no_tier_fallback_does_not_crash() -> None:
    # All api_key, all limited, tier fallback disabled: best-effort must still
    # return a candidate (soonest reset) rather than raising on an empty list.
    a = _candidate(
        "a", 0, "api_key", _state("a", is_limited=True, windows=(UsageWindow("5h", 100, 2000),))
    )
    b = _candidate(
        "b", 1, "api_key", _state("b", is_limited=True, windows=(UsageWindow("5h", 100, 1500),))
    )
    chosen = RotationPolicy.select([a, b], now=1000, allow_tier_fallback=False, best_effort=True)
    assert chosen == "b"  # soonest reset


# ── Entities ───────────────────────────────────────────────


def test_provider_account_config_dir_by_family() -> None:
    claude = ProviderAccount(
        id="pacct_1",
        name="c1",
        family="anthropic",
        kind="subscription",
        priority=0,
        claude_config_dir="~/.claude-1",
    )
    codex = ProviderAccount(
        id="pacct_2",
        name="x1",
        family="openai",
        kind="subscription",
        priority=0,
        codex_config_dir="~/.codex-1",
    )
    api = ProviderAccount(
        id="pacct_3",
        name="k1",
        family="anthropic",
        kind="api_key",
        priority=9,
        api_key_ref="env:ANTHROPIC_API_KEY",
    )
    assert claude.config_dir() == "~/.claude-1"
    assert codex.config_dir() == "~/.codex-1"
    assert api.config_dir() is None
    assert api.is_subscription is False


def test_credential_pool_to_candidates_skips_inactive_and_defaults_state() -> None:
    active = ProviderAccount(id="a", name="a", family="anthropic", kind="subscription", priority=0)
    inactive = ProviderAccount(
        id="b", name="b", family="anthropic", kind="subscription", priority=1, is_active=False
    )
    pool = CredentialPool(
        id="pool_1",
        name="p",
        family="anthropic",
        failover_mode="auto",
        members=(active, inactive),
    )
    candidates = pool.to_candidates(states={})
    assert [c.credential_id for c in candidates] == ["a"]
    # Missing state defaults to an available, unknown-headroom candidate.
    assert candidates[0].headroom() == 100
