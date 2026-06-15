"""Reactive usage-limit detection from agent output text.

The cheapest signal that an account is exhausted is the agent printing a
limit message. This pure, family-aware parser is fed scrollback / stream
text by the forwarder wiring; it has no I/O so it is exhaustively testable.

Patterns are anchored per provider to avoid mistaking an unrelated tool's
output for a limit:

* **anthropic** — Claude Code's "claude … usage limit reached" (and the
  ``anthropic-ratelimit-unified-*-reset`` epoch headers it prints).
* **openai** — Codex/OpenAI's quota errors (``insufficient_quota`` /
  ``rate_limit_exceeded`` / "rate limit reached"); OpenAI does not surface
  reset epochs in transcript text, so recovery falls back to a cooldown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from omnigent.cswap.domain.value_objects.enums import Family
from omnigent.cswap.domain.value_objects.limit_state import LimitDetectionResult
from omnigent.cswap.domain.value_objects.usage_window import UsageWindow

# ── anthropic ──────────────────────────────────────────────
_CLAUDE_LIMIT_RE = re.compile(r"claude\s+(?:ai\s+)?usage\s+limit\s+reached", re.IGNORECASE)
_GENERIC_LIMIT_RE = re.compile(
    r"usage\s+limit\s+reached|you'?ve\s+(?:hit|reached)\s+your\s+usage\s+limit",
    re.IGNORECASE,
)
_MENTIONS_CLAUDE_RE = re.compile(r"claude", re.IGNORECASE)
_RESET_5H_RE = re.compile(r"anthropic-ratelimit-unified-5h-reset[:\s]+(\d{9,})", re.IGNORECASE)
_RESET_7D_RE = re.compile(r"anthropic-ratelimit-unified-7d-reset[:\s]+(\d{9,})", re.IGNORECASE)

# ── openai / codex ─────────────────────────────────────────
# Provider-specific error codes are unambiguous on their own.
_OPENAI_CODE_RE = re.compile(r"insufficient_quota|rate_limit_exceeded", re.IGNORECASE)
# Generic phrasing only when the text also names openai/gpt/codex.
_OPENAI_GENERIC_RE = re.compile(
    r"rate\s+limit\s+reached|exceeded\s+your\s+current\s+quota", re.IGNORECASE
)
_MENTIONS_OPENAI_RE = re.compile(r"openai|gpt|codex", re.IGNORECASE)


@dataclass(frozen=True)
class ReactiveParseResult:
    """The outcome of scanning a chunk of agent output.

    :param is_limited: Whether a usage-limit signal was detected.
    :param reset_at_5h: Parsed 5h reset epoch seconds, if present.
    :param reset_at_7d: Parsed 7d reset epoch seconds, if present.
    """

    is_limited: bool
    reset_at_5h: int | None = None
    reset_at_7d: int | None = None

    def soonest_reset(self) -> int | None:
        """Return the soonest parsed reset, or ``None``."""
        resets = [r for r in (self.reset_at_5h, self.reset_at_7d) if r is not None]
        return min(resets) if resets else None


def _parse_anthropic(output: str) -> ReactiveParseResult:
    mentions_claude = bool(_MENTIONS_CLAUDE_RE.search(output))
    is_limited = bool(_CLAUDE_LIMIT_RE.search(output)) or (
        mentions_claude and bool(_GENERIC_LIMIT_RE.search(output))
    )
    reset_5h = _RESET_5H_RE.search(output)
    reset_7d = _RESET_7D_RE.search(output)
    return ReactiveParseResult(
        is_limited=is_limited,
        reset_at_5h=int(reset_5h.group(1)) if reset_5h else None,
        reset_at_7d=int(reset_7d.group(1)) if reset_7d else None,
    )


def _parse_openai(output: str) -> ReactiveParseResult:
    is_limited = bool(_OPENAI_CODE_RE.search(output)) or (
        bool(_MENTIONS_OPENAI_RE.search(output)) and bool(_OPENAI_GENERIC_RE.search(output))
    )
    return ReactiveParseResult(is_limited=is_limited)


class ReactiveOutputDetector:
    """Stateless, family-aware detector for usage-limit signals."""

    @staticmethod
    def parse(output: str, *, family: Family = "anthropic") -> ReactiveParseResult:
        """Scan *output* for a *family* usage-limit signal.

        :param output: A chunk of agent/scrollback text.
        :param family: ``"anthropic"`` or ``"openai"``.
        :returns: A :class:`ReactiveParseResult`.
        """
        return _parse_anthropic(output) if family == "anthropic" else _parse_openai(output)

    @staticmethod
    def to_detection(
        credential_id: str, parsed: ReactiveParseResult, observed_at: int
    ) -> LimitDetectionResult | None:
        """Project a positive parse into a :class:`LimitDetectionResult`.

        :returns: A reactive detection for *credential_id*, or ``None``
            when ``parsed`` indicates no limit. ``limited_until`` is the
            soonest parsed reset (the facade defaults a cooldown when no
            reset is known).
        """
        if not parsed.is_limited:
            return None
        windows: list[UsageWindow] = []
        if parsed.reset_at_5h is not None:
            windows.append(UsageWindow("5h", None, parsed.reset_at_5h))
        if parsed.reset_at_7d is not None:
            windows.append(UsageWindow("7d", None, parsed.reset_at_7d))
        return LimitDetectionResult(
            credential_id=credential_id,
            is_limited=True,
            source="reactive",
            observed_at=observed_at,
            limited_until=parsed.soonest_reset(),
            windows=tuple(windows),
        )
