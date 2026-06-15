"""Use case: react when the active account hits its usage limit.

Resolves the pool's failover mode, picks the account the **next launch**
should rotate to (excluding the exhausted one), and notifies. It does NOT
rebind the running session: the in-flight process was launched with the
exhausted account's credentials and keeps using them until it relaunches —
rebinding mid-flight would mis-attribute that session's subsequent cost and
limit signals. Should be invoked only when
:class:`~omnigent.cswap.application.use_cases.track_usage_limit.TrackUsageLimitResult`
reports ``was_newly_limited`` so it fires at most once per limit episode.
"""

from __future__ import annotations

from omnigent.cswap.application.ports.ports import (
    CredentialPoolRepository,
    CredentialSelectionPolicy,
    FailoverEvent,
    FailoverNotifier,
)


class FailoverOnLimitUseCase:
    """Recommend an alternate account and apply the pool's failover mode."""

    def __init__(
        self,
        pool_repo: CredentialPoolRepository,
        selection_policy: CredentialSelectionPolicy,
        notifier: FailoverNotifier,
    ) -> None:
        """:param pool_repo: Resolves the pool (and its failover mode).
        :param selection_policy: Picks the next available account.
        :param notifier: Surfaces the outcome to the user.
        """
        self._pool_repo = pool_repo
        self._selection_policy = selection_policy
        self._notifier = notifier

    def execute(
        self,
        *,
        session_id: str,
        exhausted_credential_id: str,
        family: str,
        now: int,
    ) -> FailoverEvent | None:
        """Run failover for a newly-limited account.

        :returns: The :class:`FailoverEvent` describing the outcome, or
            ``None`` when the pool's mode is ``disabled``.
        """
        pool = self._pool_repo.find_pool_for_family(family)
        mode = pool.failover_mode if pool is not None else "notify"
        if mode == "disabled":
            return None

        alternate = self._selection_policy.select_for_family(
            family, now, exclude_credential_id=exhausted_credential_id, best_effort=False
        )
        event = FailoverEvent(
            session_id=session_id,
            exhausted_credential_id=exhausted_credential_id,
            next_credential_id=alternate.id if alternate is not None else None,
            mode=mode,
        )
        self._notifier.notify(event)
        return event
