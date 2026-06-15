"""The :class:`RateLimitHeaders` value object.

This is the one place that knows the *shape* of provider rate-limit
response headers — the volatile seam most likely to drift as providers
change. It normalises three header families into a uniform tuple of
:class:`~omnigent.cswap.domain.value_objects.usage_window.UsageWindow`:

* Anthropic **subscription** (OAuth): ``anthropic-ratelimit-unified-5h-*``
  and ``-7d-*`` with epoch-second ``reset`` values (undocumented but used
  by claude-swap / remote-dev).
* Anthropic **API key**: ``anthropic-ratelimit-requests-*`` /
  ``-tokens-*`` with RFC 3339 ``reset`` values, plus ``retry-after``.
* OpenAI: ``x-ratelimit-*-requests`` / ``-tokens`` with Go-style duration
  ``reset`` values (e.g. ``"6m0s"``), plus ``retry-after``.

Keeping the brittle parsing isolated here means the routing and detection
logic above it stay provider-agnostic.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from omnigent.cswap.domain.value_objects.usage_window import UsageWindow

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def _lower_keys(headers: Mapping[str, str]) -> dict[str, str]:
    """Return *headers* with lower-cased keys for case-insensitive lookup."""
    return {str(k).lower(): str(v) for k, v in headers.items()}


def _to_int(value: str | None) -> int | None:
    """Parse *value* as an int, returning ``None`` on failure/absence."""
    if value is None:
        return None
    try:
        return int(float(value.strip()))
    except (ValueError, AttributeError):
        return None


def _utilization_pct(limit: int | None, remaining: int | None) -> int | None:
    """Return percent consumed from *limit* / *remaining*, or ``None``.

    :returns: ``round((limit - remaining) / limit * 100)`` clamped to
        ``0``–``100``, or ``None`` when either input is missing or
        ``limit`` is not positive.
    """
    if limit is None or remaining is None or limit <= 0:
        return None
    used = max(0, limit - remaining)
    return max(0, min(100, round(used / limit * 100)))


def _parse_epoch_or_rfc3339(value: str | None) -> int | None:
    """Parse a reset value that is either epoch seconds or RFC 3339.

    Anthropic's unified (subscription) headers give epoch seconds; its
    API-key headers give RFC 3339 timestamps. This accepts either.

    :returns: Unix epoch seconds, or ``None`` on failure/absence.
    """
    if value is None:
        return None
    value = value.strip()
    epoch = _to_int(value)
    # Heuristic: a bare integer (no date punctuation) is epoch seconds.
    if epoch is not None and not any(c in value for c in "-:T"):
        return epoch
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return epoch


def _parse_duration_to_epoch(value: str | None, now: int) -> int | None:
    """Parse a Go-style duration (e.g. ``"6m0s"``) to an absolute epoch.

    :param value: Duration string such as ``"1s"``, ``"6m0s"``,
        ``"1h2m3s"``, ``"100ms"``.
    :param now: Current Unix epoch seconds, added to the parsed duration.
    :returns: ``now + duration`` in epoch seconds, or ``None`` when the
        string parses to no recognised units.
    """
    if value is None:
        return None
    matches = _DURATION_RE.findall(value.strip().lower())
    if not matches:
        return None
    seconds = sum(float(amount) * _DURATION_UNITS[unit] for amount, unit in matches)
    return now + round(seconds)


def _retry_after_at(raw: str | None, now: int) -> int | None:
    """Parse a ``retry-after`` header to epoch seconds.

    Handles all three forms the spec allows: a delta in seconds, an RFC 3339
    timestamp, and an RFC 7231 HTTP date (``Wed, 21 Oct 2015 07:28:00 GMT``).

    :returns: Absolute epoch seconds to retry after, or ``None``.
    """
    if raw is None:
        return None
    secs = _to_int(raw)
    if secs is not None and "-" not in raw and ":" not in raw:
        # A bare integer is a delta-seconds value per RFC 7231 — unless it is
        # implausibly large for a delay (>~115 days), in which case a provider
        # has put an absolute epoch here; treat it as such rather than as a
        # ~decades-in-the-future delay.
        return secs if secs >= 10**8 else now + secs
    epoch = _parse_epoch_or_rfc3339(raw)
    if epoch is not None:
        return epoch
    try:
        http_date = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if http_date.tzinfo is None:
        http_date = http_date.replace(tzinfo=timezone.utc)
    return int(http_date.timestamp())


@dataclass(frozen=True)
class RateLimitHeaders:
    """Normalised rate-limit headers from a provider response.

    :param windows: Usage windows parsed from the headers, uniform across
        provider/account kinds.
    :param retry_after_at: Absolute epoch seconds from a ``retry-after``
        header, or ``None`` when absent.
    """

    windows: tuple[UsageWindow, ...]
    retry_after_at: int | None

    @classmethod
    def _windows_from(
        cls,
        headers: dict[str, str],
        specs: tuple[tuple[str, str, str | None], ...],
        *,
        reset_parser: str,
        now: int,
    ) -> list[UsageWindow]:
        """Build windows from (label, header-prefix, reset-header) specs.

        :param headers: Lower-cased header map.
        :param specs: Tuples of ``(label, prefix, reset_header)``; the
            limit/remaining headers are ``f"{prefix}-limit"`` /
            ``f"{prefix}-remaining"``. ``reset_header`` may be ``None``.
        :param reset_parser: ``"epoch_or_rfc3339"`` or ``"duration"``.
        :param now: Current epoch seconds (for duration resets).
        """
        windows: list[UsageWindow] = []
        for label, prefix, reset_header in specs:
            limit = _to_int(headers.get(f"{prefix}-limit"))
            remaining = _to_int(headers.get(f"{prefix}-remaining"))
            reset_raw = headers.get(reset_header) if reset_header else None
            if limit is None and remaining is None and reset_raw is None:
                continue
            reset_at = (
                _parse_epoch_or_rfc3339(reset_raw)
                if reset_parser == "epoch_or_rfc3339"
                else _parse_duration_to_epoch(reset_raw, now)
            )
            windows.append(
                UsageWindow(
                    label=label,
                    utilization_pct=_utilization_pct(limit, remaining),
                    reset_at=reset_at,
                )
            )
        return windows

    @classmethod
    def parse_anthropic(cls, headers: Mapping[str, str], *, now: int) -> RateLimitHeaders:
        """Parse Anthropic headers (subscription unified or API-key).

        Reads whichever of the unified ``5h``/``7d`` or the API-key
        ``requests``/``tokens`` families are present.
        """
        h = _lower_keys(headers)
        specs = (
            ("5h", "anthropic-ratelimit-unified-5h", "anthropic-ratelimit-unified-5h-reset"),
            ("7d", "anthropic-ratelimit-unified-7d", "anthropic-ratelimit-unified-7d-reset"),
            ("requests", "anthropic-ratelimit-requests", "anthropic-ratelimit-requests-reset"),
            ("tokens", "anthropic-ratelimit-tokens", "anthropic-ratelimit-tokens-reset"),
            (
                "input-tokens",
                "anthropic-ratelimit-input-tokens",
                "anthropic-ratelimit-input-tokens-reset",
            ),
            (
                "output-tokens",
                "anthropic-ratelimit-output-tokens",
                "anthropic-ratelimit-output-tokens-reset",
            ),
        )
        windows = cls._windows_from(h, specs, reset_parser="epoch_or_rfc3339", now=now)
        return cls(
            windows=tuple(windows), retry_after_at=_retry_after_at(h.get("retry-after"), now)
        )

    @classmethod
    def parse_openai(cls, headers: Mapping[str, str], *, now: int) -> RateLimitHeaders:
        """Parse OpenAI ``x-ratelimit-*`` headers (duration-style resets)."""
        h = _lower_keys(headers)
        specs = (
            ("requests", "x-ratelimit-requests", "x-ratelimit-reset-requests"),
            ("tokens", "x-ratelimit-tokens", "x-ratelimit-reset-tokens"),
        )
        # OpenAI spells limit/remaining as ``x-ratelimit-limit-requests`` etc.,
        # i.e. ``{stat}-{dimension}`` not ``{dimension}-{stat}``. Remap.
        remapped: dict[str, str] = dict(h)
        for dim in ("requests", "tokens"):
            for stat in ("limit", "remaining"):
                src = f"x-ratelimit-{stat}-{dim}"
                if src in h:
                    remapped[f"x-ratelimit-{dim}-{stat}"] = h[src]
        windows = cls._windows_from(remapped, specs, reset_parser="duration", now=now)
        return cls(
            windows=tuple(windows), retry_after_at=_retry_after_at(h.get("retry-after"), now)
        )

    def is_limited(self, status_code: int) -> bool:
        """Whether these headers / status indicate the account is limited.

        :param status_code: HTTP status of the probe response.
        :returns: ``True`` on a 429, a present ``retry-after``, or any
            fully-exhausted window.
        """
        if status_code == 429 or self.retry_after_at is not None:
            return True
        return any(w.is_exhausted() for w in self.windows)

    def recovery_at(self) -> int | None:
        """Return the epoch when the limit lifts, or ``None`` if unknown.

        The account is unblocked only once **every** blocking constraint has
        cleared, so this is the *latest* of: an explicit ``retry-after`` and
        each exhausted window's reset. (Using the soonest would re-select the
        account while a longer window is still at 100%, causing flapping.)
        When nothing is exhausted and there is no ``retry-after``, falls back
        to the soonest known reset.
        """
        blocking = [
            w.reset_at for w in self.windows if w.is_exhausted() and w.reset_at is not None
        ]
        if self.retry_after_at is not None:
            blocking.append(self.retry_after_at)
        if blocking:
            return max(blocking)
        resets = [w.reset_at for w in self.windows if w.reset_at is not None]
        return min(resets) if resets else None
