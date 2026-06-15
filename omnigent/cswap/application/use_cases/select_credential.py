"""Use case: pick the account to route a family's next launch to."""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.cswap.application.ports.ports import CredentialSelectionPolicy
from omnigent.cswap.domain.entities.provider_account import ProviderAccount


@dataclass(frozen=True)
class SelectCredentialResult:
    """Outcome of a selection.

    :param account: The chosen account, or ``None`` when none could be
        selected (no pool, or — in non-best-effort mode — none available).
    :param used_tier_fallback: ``True`` when the chosen account is an
        api_key (i.e. subscriptions were exhausted and tier fallback fired).
    """

    account: ProviderAccount | None
    used_tier_fallback: bool


class SelectCredentialUseCase:
    """Select a credential for a family via the selection policy."""

    def __init__(self, policy: CredentialSelectionPolicy) -> None:
        """:param policy: The selection policy adapter."""
        self._policy = policy

    def execute(
        self,
        family: str,
        now: int,
        *,
        exclude_credential_id: str | None = None,
        best_effort: bool = True,
    ) -> SelectCredentialResult:
        """Select an account for *family*.

        :param family: ``"anthropic"`` or ``"openai"``.
        :param now: Current Unix epoch seconds.
        :param exclude_credential_id: Account to skip (failover).
        :param best_effort: When ``True`` (launch), never return ``None``
            if any account exists. When ``False`` (failover), ``None`` when
            nothing is available.
        :returns: A :class:`SelectCredentialResult`.
        """
        account = self._policy.select_for_family(
            family,
            now,
            exclude_credential_id=exclude_credential_id,
            best_effort=best_effort,
        )
        return SelectCredentialResult(
            account=account,
            used_tier_fallback=account is not None and account.kind == "api_key",
        )
