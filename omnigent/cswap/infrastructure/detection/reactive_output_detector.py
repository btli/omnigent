"""Reactive usage-limit detection from agent output text.

The cheapest, most reliable signal that a Claude account is exhausted is
the agent itself printing "usage limit reached" (and Claude Code prints
the ``anthropic-ratelimit-unified-*-reset`` header values alongside).
This pure parser is fed scrollback / stream text by the executor and
native forwarder wiring; it has no I/O so it is exhaustively testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from omnigent.cswap.domain.value_objects.limit_state import LimitDetectionResult
from omnigent.cswap.domain.value_objects.usage_window import UsageWindow

# Claude-anchored phrase (high confidence).
_CLAUDE_LIMIT_RE = re.compile(r"claude\s+(?:ai\s+)?usage\s+limit\s+reached", re.IGNORECASE)
# Generic phrasing — only trusted when the text also mentions Claude, to
# avoid mistaking an unrelated tool's message for a Claude limit.
_GENERIC_LIMIT_RE = re.compile(
    r"usage\s+limit\s+reached|you'?ve\s+(?:hit|reached)\s+your\s+usage\s+limit",
    re.IGNORECASE,
)
_MENTIONS_CLAUDE_RE = re.compile(r"claude", re.IGNORECASE)
# Reset header lines Claude Code surfaces (epoch seconds).
_RESET_5H_RE = re.compile(r"anthropic-ratelimit-unified-5h-reset[:\s]+(\d{9,})", re.IGNORECASE)
_RESET_7D_RE = re.compile(r"anthropic-ratelimit-unified-7d-reset[:\s]+(\d{9,})", re.IGNORECASE)


@dataclass(frozen=True)
class ReactiveParseResult:
    """The outcome of scanning a chunk of agent output.

    :param is_limited: Whether a usage-limit signal was detected.
    :param reset_at_5h: Parsed 5h reset epoch seconds, if present.
    :param reset_at_7d: Parsed 7d reset epoch seconds, if present.
    """

    is_limited: bool
    reset_at_5h: int | None
    reset_at_7d: int | None


class ReactiveOutputDetector:
    """Stateless detector for usage-limit signals in agent output."""

    @staticmethod
    def parse(output: str) -> ReactiveParseResult:
        """Scan *output* for a Claude usage-limit signal.

        :param output: A chunk of agent/scrollback text.
        :returns: A :class:`ReactiveParseResult`; ``is_limited`` is ``True``
            only for a Claude-anchored phrase, or a generic phrase when the
            text also mentions Claude.
        """
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

    @staticmethod
    def to_detection(
        credential_id: str, parsed: ReactiveParseResult, observed_at: int
    ) -> LimitDetectionResult | None:
        """Project a positive parse into a :class:`LimitDetectionResult`.

        :returns: A reactive detection for *credential_id*, or ``None``
            when ``parsed`` indicates no limit.
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
            windows=tuple(windows),
        )
