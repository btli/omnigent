"""The :class:`LimitState` value object and :class:`LimitDetectionResult`.

:class:`LimitState` is the authoritative, persisted view of one account's
usage-limit status: whether it is limited, its per-window headroom, and
when it resets. Selection asks two questions of it —
:meth:`LimitState.is_available_now` (can I route to this account?) and
:meth:`LimitState.remaining_headroom_pct` (how much room is left?) — which
together implement the headroom-plus-reset routing policy.

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
    :param is_limited: ``True`` when the account is currently rate-limited
        (a 429 / "usage limit reached" signal, or an exhausted window).
    :param windows: The known usage windows (e.g. ``5h`` / ``7d`` for a
        subscription). Empty when never observed.
    :param source: How the most recent observation was learned, or
        ``None`` when never observed.
    :param last_checked_at: Unix epoch seconds of the most recent
        observation, or ``None`` when never observed. Used as the
        staleness guard so a slow poll cannot clobber a fresher reactive
        signal.
    """

    credential_id: str
    is_limited: bool = False
    windows: tuple[UsageWindow, ...] = field(default_factory=tuple)
    source: DetectionSource | None = None
    last_checked_at: int | None = None

    def earliest_reset_at(self) -> int | None:
        """Return the soonest reset across all known windows, or ``None``.

        :returns: The minimum :attr:`UsageWindow.reset_at` over windows
            that declare one, or ``None`` when no window has a known reset.
        """
        resets = [w.reset_at for w in self.windows if w.reset_at is not None]
        return min(resets) if resets else None

    def is_available_now(self, now: int) -> bool:
        """Whether this account can be routed to at *now*.

        An account is available when it is not limited, or when it is
        limited but its earliest window reset has already passed. A
        limited account with no known reset is treated as unavailable.

        :param now: Current Unix epoch seconds.
        :returns: ``True`` when the account may be selected.
        """
        if not self.is_limited:
            return True
        earliest = self.earliest_reset_at()
        return earliest is not None and now >= earliest

    def remaining_headroom_pct(self) -> int | None:
        """Return the headroom of the most-constrained known window.

        The binding constraint is the window with the *least* remaining
        capacity, so headroom is the minimum ``remaining_pct`` across
        windows that report utilization.

        :returns: ``0``–``100`` remaining percent, or ``None`` when no
            window reports utilization (headroom unknown).
        """
        remaining = [w.remaining_pct() for w in self.windows if w.remaining_pct() is not None]
        return min(r for r in remaining if r is not None) if remaining else None

    def to_status(self, now: int) -> LimitStatus:
        """Return the coarse :data:`LimitStatus` at *now*.

        :param now: Current Unix epoch seconds.
        :returns: ``"unknown"`` when never observed, otherwise
            ``"available"`` / ``"limited"`` per :meth:`is_available_now`.
        """
        if self.source is None and not self.is_limited and not self.windows:
            return "unknown"
        return "available" if self.is_available_now(now) else "limited"


@dataclass(frozen=True)
class LimitDetectionResult:
    """A single usage-limit observation from a detector or probe.

    Distinct from :class:`LimitState` (the persisted aggregate): this is
    one raw observation that the track-usage-limit use case reconciles
    against stored state (applying the staleness guard and computing
    whether the account was *newly* limited).

    :param credential_id: The account observed.
    :param is_limited: Whether the observation indicates a limit was hit.
    :param windows: Usage windows parsed from the observation, if any.
    :param source: How the observation was made.
    :param observed_at: Unix epoch seconds the observation was made.
    """

    credential_id: str
    is_limited: bool
    source: DetectionSource
    observed_at: int
    windows: tuple[UsageWindow, ...] = field(default_factory=tuple)

    def to_limit_state(self) -> LimitState:
        """Project this observation into a :class:`LimitState`.

        :returns: A :class:`LimitState` carrying the observation's fields,
            with :attr:`LimitState.last_checked_at` set to
            :attr:`observed_at`.
        """
        return LimitState(
            credential_id=self.credential_id,
            is_limited=self.is_limited,
            windows=self.windows,
            source=self.source,
            last_checked_at=self.observed_at,
        )
