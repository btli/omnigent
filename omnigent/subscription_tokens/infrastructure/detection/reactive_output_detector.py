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

from omnigent.subscription_tokens.domain.value_objects.enums import Family
from omnigent.subscription_tokens.domain.value_objects.limit_state import LimitDetectionResult
from omnigent.subscription_tokens.domain.value_objects.usage_window import UsageWindow


def message_text(data: object) -> str:
    """Extract the human-readable text from a transcript message item.

    Both forwarders hand us the message's ``data`` dict; scanning its
    ``str()`` would match Python ``repr`` punctuation/keys, not the actual
    content. This pulls the ``content`` text (a plain string, or the
    concatenated ``text`` of content blocks).

    :param data: A message item's ``data`` (expected to be a mapping).
    :returns: The message's text, or ``""`` when none can be extracted.
    """
    if not isinstance(data, dict):
        return ""
    content = data.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return ""


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
# OpenAI quota/limit phrasing. All openai matching also requires a provider
# mention (openai/gpt/codex) so an assistant *quoting* an unrelated tool's
# "rate_limit_exceeded" error text does not trigger a bogus failover.
_OPENAI_LIMIT_RE = re.compile(
    r"insufficient_quota|rate_limit_exceeded|rate\s+limit\s+reached"
    r"|exceeded\s+your\s+current\s+quota",
    re.IGNORECASE,
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

    def recovery_after(self, now: int) -> int | None:
        """Return the soonest parsed reset still in the future, or ``None``.

        The reactive text says the account is limited but not *which* window
        was hit. Using the soonest reset that is still ahead of *now* is the
        least-bad choice: it recovers at the 5h reset for the common 5h limit,
        but never picks an already-elapsed reset — which would mark a still-
        limited account available and cause a failover/re-limit loop. When no
        parsed reset is in the future, returns ``None`` so the facade applies a
        cooldown instead.
        """
        future = [r for r in (self.reset_at_5h, self.reset_at_7d) if r is not None and r > now]
        return min(future) if future else None


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
    is_limited = bool(_MENTIONS_OPENAI_RE.search(output)) and bool(_OPENAI_LIMIT_RE.search(output))
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
            limited_until=parsed.recovery_after(observed_at),
            windows=tuple(windows),
        )
