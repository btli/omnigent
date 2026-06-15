"""Application ports (Protocols) for the subscription-token package.

These define the boundaries the use-cases depend on; infrastructure
provides the concrete adapters. Keeping them as :class:`typing.Protocol`
classes means use-cases can be unit-tested against trivial in-memory
fakes with no DB or network.

Convention: everything is synchronous **except**
:meth:`UsageLimitGateway.fetch_limit_state`, which performs a network
probe. omnigent's stores are sync (short SQLite/PG transactions), so the
use-cases stay sync and call them directly; only the probe and the
background poller are async.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from omnigent.subscription_tokens.domain.entities.credential_pool import CredentialPool
from omnigent.subscription_tokens.domain.entities.provider_account import ProviderAccount
from omnigent.subscription_tokens.domain.value_objects.enums import AccountKind, FailoverMode
from omnigent.subscription_tokens.domain.value_objects.limit_state import (
    LimitDetectionResult,
    LimitState,
)


class UsageLimitStateRepository(Protocol):
    """Persistence for per-account :class:`LimitState` observations."""

    def find(self, credential_id: str) -> LimitState | None:
        """Return the stored state for *credential_id*, or ``None``."""
        ...

    def find_many(self, credential_ids: list[str]) -> dict[str, LimitState]:
        """Return stored states keyed by id (absent ids omitted)."""
        ...

    def upsert(self, state: LimitState, *, enforce_staleness: bool = True) -> bool:
        """Write *state*, honouring the staleness guard.

        :param state: The observation to persist; its
            :attr:`LimitState.last_checked_at` is the observation time.
        :param enforce_staleness: When ``True``, skip the write if a
            strictly-newer observation is already stored (so a slow poll
            cannot clobber a fresh reactive signal). ``manual`` overrides
            pass ``False``.
        :returns: ``True`` if a row was written/updated, ``False`` if the
            staleness guard skipped it.
        """
        ...

    def observe(self, state: LimitState, *, enforce_staleness: bool = True) -> tuple[bool, bool]:
        """Atomically write *state* and report ``(wrote, prior_was_available)``.

        The prior read and the write happen in one serialized transaction so
        two writers reporting the same limit cannot both see the account as
        available and both decide it was newly limited.

        :returns: ``(wrote, prior_was_available)`` — ``wrote`` per
            :meth:`upsert`; ``prior_was_available`` is whether the account
            was available *before* this observation.
        """
        ...


class CredentialPoolRepository(Protocol):
    """Read access to synced pools and accounts."""

    def find_pool_for_family(self, family: str) -> CredentialPool | None:
        """Return the active pool serving *family* (with members), or ``None``."""
        ...

    def find_account(self, credential_id: str) -> ProviderAccount | None:
        """Return the account for *credential_id*, or ``None``."""
        ...

    def accounts_for_family(self, family: str) -> list[ProviderAccount]:
        """Return all active accounts serving *family* (for the poll sweep)."""
        ...


class SessionCredentialRegistry(Protocol):
    """Tracks which account is active for each session."""

    def bind(self, session_id: str, credential_id: str, family: str) -> None:
        """Record (or rebind) the active account for *session_id*."""
        ...

    def active_credential(self, session_id: str) -> str | None:
        """Return the active account id for *session_id*, or ``None``."""
        ...


class CostAttributionSink(Protocol):
    """Records per-account cost, extending the existing cost pipeline."""

    def record_credential_cost(
        self,
        credential_id: str,
        day_utc: str,
        *,
        cost_usd: float,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Increment the per-account, per-day cost rollup."""
        ...


class UsageLimitGateway(Protocol):
    """Probes an account's live usage headroom (async, network)."""

    def supports(self, kind: AccountKind) -> bool:
        """Whether this gateway can probe accounts of *kind*."""
        ...

    async def fetch_limit_state(
        self, account: ProviderAccount, *, now: int
    ) -> LimitDetectionResult | None:
        """Probe *account* and return an observation, or ``None`` on error.

        Implementations never raise — a failed probe yields ``None`` and
        leaves the stored state untouched.
        """
        ...


class CredentialSelectionPolicy(Protocol):
    """Selects the account to route a family to."""

    def select_for_family(
        self,
        family: str,
        now: int,
        *,
        exclude_credential_id: str | None = None,
        best_effort: bool = True,
    ) -> ProviderAccount | None:
        """Return the chosen account for *family*, or ``None``.

        :param best_effort: When ``True`` (launch), never return ``None``
            if any account exists — fall back to soonest-reset. When
            ``False`` (failover), return ``None`` if nothing is available.
        """
        ...


@dataclass(frozen=True)
class FailoverEvent:
    """Describes a failover outcome for notification.

    The in-flight process keeps running on the exhausted account (it was
    launched with that account's credentials); failover records the limit
    so the **next launch** rotates to :attr:`next_credential_id`. The event
    is what surfaces that recommendation to the user.

    :param session_id: The affected session.
    :param exhausted_credential_id: The account that hit its limit.
    :param next_credential_id: The account the next launch should use, or
        ``None`` when none is currently available.
    :param mode: The pool's failover mode.
    """

    session_id: str
    exhausted_credential_id: str
    next_credential_id: str | None
    mode: FailoverMode


class FailoverNotifier(Protocol):
    """Surfaces failover events to the user (e.g. an SSE notification)."""

    def notify(self, event: FailoverEvent) -> None:
        """Deliver *event*. Must not raise; fire-and-forget is fine."""
        ...
