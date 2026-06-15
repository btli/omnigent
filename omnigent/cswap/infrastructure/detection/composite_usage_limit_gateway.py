"""Compose multiple usage-limit gateways, dispatching by account kind."""

from __future__ import annotations

from omnigent.cswap.application.ports.ports import UsageLimitGateway
from omnigent.cswap.domain.entities.provider_account import ProviderAccount
from omnigent.cswap.domain.value_objects.enums import AccountKind
from omnigent.cswap.domain.value_objects.limit_state import LimitDetectionResult


class CompositeUsageLimitGateway(UsageLimitGateway):
    """Routes a probe to the first adapter that supports the account kind."""

    def __init__(self, adapters: list[UsageLimitGateway]) -> None:
        """:param adapters: Ordered gateways; the first supporting an
        account's kind handles it.
        """
        self._adapters = adapters

    def supports(self, kind: AccountKind) -> bool:
        """Whether any composed adapter supports *kind*."""
        return any(adapter.supports(kind) for adapter in self._adapters)

    async def fetch_limit_state(
        self, account: ProviderAccount, *, now: int
    ) -> LimitDetectionResult | None:
        """Probe *account* via the first supporting adapter, or ``None``."""
        for adapter in self._adapters:
            if adapter.supports(account.kind):
                return await adapter.fetch_limit_state(account, now=now)
        return None
