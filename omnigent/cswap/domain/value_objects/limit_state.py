"""The :class:`LimitState` value object and :class:`LimitDetectionResult`.

:class:`LimitState` is the authoritative, persisted view of one account's
usage-limit status. It separates two concerns deliberately:

* **Availability** — ``is_limited`` plus an explicit ``limited_until``
  recovery epoch. Selection reads only these to decide whether to route to
  the account. Modelling recovery as a single timestamp (rather than
  inferring it from the *earliest* window reset) avoids re-selecting an
  account the moment its soonest window resets while a longer window is
  still exhausted.
* **Headroom** — the :class:`UsageWindow` tuple, kept purely for ranking
  (prefer the account with the most remaining capacity) and display. Any
  number of arbitrary-label windows is fine.

:class:`LimitDetectionResult` is the lighter-weight shape produced by the
reactive detector and the proactive probe; the track-usage-limit use case
folds it into a persisted :class:`LimitState`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from omnigent.cswap.domain.value_objects.enums import DetectionSource, LimitStatus
from omnigent.cswap.domain.value_objects.usage_window import UsageWindow


@dataclass(frozen=True)
class LimitState:
    """The persisted usage-limit state for one provider account.

    :param credential_id: The ``ProviderAccount`` id this state belongs to.
    :param is_limited: ``True`` when the account is currently rate-limited.
    :param limited_until: Unix epoch seconds at which the limit lifts, or
        ``None`` when unknown. Only meaningful while ``is_limited``.
    :param windows: Known usage windows (informational — headroom + display).
        Empty when never observed.
    :param source: How the most recent observation was learned, or ``None``.
    :param last_checked_at: Unix epoch seconds of the most recent
        observation, or ``None``. The staleness guard so a slow poll cannot
        clobber a fresher reactive signal.
    """

    credential_id: str
    is_limited: bool = False
    limited_until: int | None = None
    windows: tuple[UsageWindow, ...] = field(default_factory=tuple)
    source: DetectionSource | None = None
    last_checked_at: int | None = None

    def is_available_now(self, now: int) -> bool:
        """Whether this account can be routed to at *now*.

        Available when not limited, or when limited and its
        :attr:`limited_until` recovery time has passed. A limited account
        with no known recovery time is treated as unavailable.

        :param now: Current Unix epoch seconds.
        """
        if not self.is_limited:
            return True
        return self.limited_until is not None and now >= self.limited_until

    def earliest_reset_at(self) -> int | None:
        """Return the soonest known reset, for best-effort ranking.

        Prefers the soonest window reset; falls back to
        :attr:`limited_until` when no window declares one. Used only to
        order fully-limited candidates (pick the one recovering soonest) —
        availability uses :meth:`is_available_now`.
        """
        resets = [w.reset_at for w in self.windows if w.reset_at is not None]
        if resets:
            return min(resets)
        return self.limited_until

    def remaining_headroom_pct(self) -> int | None:
        """Return the headroom of the most-constrained known window.

        :returns: ``0``–``100`` remaining percent (minimum across windows
            that report utilization), or ``None`` when headroom is unknown.
        """
        remaining = [pct for w in self.windows if (pct := w.remaining_pct()) is not None]
        return min(remaining) if remaining else None

    def to_status(self, now: int) -> LimitStatus:
        """Return the coarse :data:`LimitStatus` at *now*.

        :returns: ``"unknown"`` when never observed, otherwise
            ``"available"`` / ``"limited"`` per :meth:`is_available_now`.
        """
        if self.source is None and not self.is_limited and not self.windows:
            return "unknown"
        return "available" if self.is_available_now(now) else "limited"


@dataclass(frozen=True)
class LimitDetectionResult:
    """A single usage-limit observation from a detector or probe.

    Distinct from :class:`LimitState` (the persisted aggregate): one raw
    observation the track-usage-limit use case reconciles against stored
    state (staleness guard + newly-limited computation).

    :param credential_id: The account observed.
    :param is_limited: Whether the observation indicates a limit was hit.
    :param source: How the observation was made.
    :param observed_at: Unix epoch seconds the observation was made.
    :param limited_until: Recovery epoch when limited, or ``None``.
    :param windows: Usage windows parsed from the observation, if any.
    """

    credential_id: str
    is_limited: bool
    source: DetectionSource
    observed_at: int
    limited_until: int | None = None
    windows: tuple[UsageWindow, ...] = field(default_factory=tuple)

    def to_limit_state(self) -> LimitState:
        """Project this observation into a :class:`LimitState`."""
        return LimitState(
            credential_id=self.credential_id,
            is_limited=self.is_limited,
            limited_until=self.limited_until,
            windows=self.windows,
            source=self.source,
            last_checked_at=self.observed_at,
        )
