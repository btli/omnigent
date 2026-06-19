"""Tests for the reactive usage-limit output detector (transcript scanning)."""

from __future__ import annotations

from omnigent.subscription_tokens.infrastructure.detection.reactive_output_detector import (
    ReactiveOutputDetector,
    ReactiveParseResult,
    message_text,
)


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


def test_reactive_recovery_skips_elapsed_reset() -> None:
    # 5h reset already elapsed, 7d reset in the future → recover at 7d, NOT the
    # past 5h (which would mark a still-limited account available → fail loop).
    parsed = ReactiveParseResult(is_limited=True, reset_at_5h=900, reset_at_7d=5000)
    det = ReactiveOutputDetector.to_detection("c", parsed, observed_at=1000)
    assert det is not None
    assert det.limited_until == 5000
    # All resets elapsed → None (the facade then applies a cooldown).
    parsed2 = ReactiveParseResult(is_limited=True, reset_at_5h=800, reset_at_7d=900)
    det2 = ReactiveOutputDetector.to_detection("c", parsed2, observed_at=1000)
    assert det2 is not None
    assert det2.limited_until is None


def test_message_text_extracts_real_content_not_repr() -> None:
    # Plain-string content.
    assert message_text({"role": "assistant", "content": "Claude usage limit reached"}) == (
        "Claude usage limit reached"
    )
    # Content blocks are concatenated by their text.
    blocks = {"content": [{"type": "text", "text": "hit a"}, {"type": "text", "text": "limit"}]}
    assert message_text(blocks) == "hit a limit"
    # No content / non-dict → empty (nothing to scan, no repr noise).
    assert message_text({"role": "user"}) == ""
    assert message_text("not a dict") == ""


def test_reactive_openai_requires_provider_mention() -> None:
    # A quota signal fires only alongside a provider mention (openai/gpt/codex),
    # so an assistant quoting an unrelated tool's error does not failover.
    assert ReactiveOutputDetector.parse("openai: insufficient_quota", family="openai").is_limited
    assert ReactiveOutputDetector.parse("codex rate_limit_exceeded", family="openai").is_limited
    assert ReactiveOutputDetector.parse("OpenAI: rate limit reached", family="openai").is_limited
    # Without a provider mention, even an error-code token does not fire.
    assert not ReactiveOutputDetector.parse("insufficient_quota", family="openai").is_limited
    assert not ReactiveOutputDetector.parse("rate limit reached", family="openai").is_limited
    # A Claude limit must NOT match under the openai family.
    assert not ReactiveOutputDetector.parse(
        "claude usage limit reached", family="openai"
    ).is_limited


def test_reactive_openai_detection_has_no_reset() -> None:
    # OpenAI surfaces no reset epoch in transcript text → limited_until None
    # (the facade applies a cooldown default).
    parsed = ReactiveOutputDetector.parse("openai insufficient_quota", family="openai")
    detection = ReactiveOutputDetector.to_detection("c", parsed, observed_at=100)
    assert detection is not None
    assert detection.limited_until is None
    assert detection.windows == ()
