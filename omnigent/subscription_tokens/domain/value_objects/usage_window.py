"""The :class:`UsageWindow` value object.

A usage window is one rate-limit dimension reported by a provider — for a
Claude subscription the rolling ``5h`` and ``7d`` windows; for an API key
the ``requests`` / ``tokens`` budgets. Each window carries how much of it
is consumed (:attr:`utilization_pct`) and when it next resets
(:attr:`reset_at`). Routing ranks accounts by the *remaining headroom* of
their most-constrained window, so a single uniform shape across both
account kinds keeps the selection logic simple.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UsageWindow:
    """One rate-limit dimension for an account.

    :param label: A short identifier for the window, e.g. ``"5h"``,
        ``"7d"``, ``"requests"``, or ``"tokens"``. Free-form so the same
        value object describes both subscription rolling windows and
        API-key request/token budgets.
    :param utilization_pct: Percent of the window consumed, ``0``–``100``,
        or ``None`` when unknown. Values are clamped into range at
        construction.
    :param reset_at: Unix epoch seconds at which the window resets, or
        ``None`` when unknown.
    """

    label: str
    utilization_pct: int | None
    reset_at: int | None

    def __post_init__(self) -> None:
        """Clamp :attr:`utilization_pct` into the ``0``–``100`` range."""
        if self.utilization_pct is not None:
            clamped = max(0, min(100, self.utilization_pct))
            if clamped != self.utilization_pct:
                object.__setattr__(self, "utilization_pct", clamped)

    def remaining_pct(self) -> int | None:
        """Return the unused percentage of this window, or ``None``.

        :returns: ``100 - utilization_pct`` (never negative), or ``None``
            when :attr:`utilization_pct` is unknown.
        """
        if self.utilization_pct is None:
            return None
        return 100 - self.utilization_pct

    def is_exhausted(self) -> bool:
        """Whether this window is fully consumed.

        :returns: ``True`` only when :attr:`utilization_pct` is known and
            at ``100``.
        """
        return self.utilization_pct is not None and self.utilization_pct >= 100
