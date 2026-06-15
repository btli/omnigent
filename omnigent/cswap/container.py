"""Dependency-injection container for the cswap package.

Assembles the repositories, gateway, selection policy, and use cases from
a single :data:`~omnigent.db.utils.ManagedSessionMaker`. The server builds
one container at startup and hands its use cases to the auth-resolution,
detection, and cost-attribution seams.
"""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.cswap.application.ports.ports import (
    CostAttributionSink,
    CredentialPoolRepository,
    FailoverNotifier,
    SessionCredentialRegistry,
    UsageLimitGateway,
    UsageLimitStateRepository,
)
from omnigent.cswap.application.use_cases.failover_on_limit import FailoverOnLimitUseCase
from omnigent.cswap.application.use_cases.select_credential import SelectCredentialUseCase
from omnigent.cswap.application.use_cases.track_usage_limit import TrackUsageLimitUseCase
from omnigent.cswap.infrastructure.detection.composite_usage_limit_gateway import (
    CompositeUsageLimitGateway,
)
from omnigent.cswap.infrastructure.detection.usage_endpoint_poller import UsageEndpointPoller
from omnigent.cswap.infrastructure.notification.notifiers import LoggingFailoverNotifier
from omnigent.cswap.infrastructure.repositories.sqlalchemy_repositories import (
    SqlCostAttributionSink,
    SqlCredentialPoolRepository,
    SqlSessionCredentialRegistry,
    SqlUsageLimitStateRepository,
)
from omnigent.cswap.infrastructure.selection.priority_credential_selection_policy import (
    PriorityCredentialSelectionPolicy,
)
from omnigent.db.utils import ManagedSessionMaker


@dataclass(frozen=True)
class CswapContainer:
    """Wired cswap collaborators shared across the server."""

    session_maker: ManagedSessionMaker
    pool_repo: CredentialPoolRepository
    state_repo: UsageLimitStateRepository
    registry: SessionCredentialRegistry
    cost_sink: CostAttributionSink
    gateway: UsageLimitGateway
    notifier: FailoverNotifier
    track_usage_limit: TrackUsageLimitUseCase
    select_credential: SelectCredentialUseCase
    failover_on_limit: FailoverOnLimitUseCase


def build_container(
    session_maker: ManagedSessionMaker,
    *,
    notifier: FailoverNotifier | None = None,
    gateway: UsageLimitGateway | None = None,
) -> CswapContainer:
    """Build a :class:`CswapContainer` over *session_maker*.

    :param session_maker: Managed session factory bound to the omnigent DB.
    :param notifier: Failover notifier; defaults to a logging notifier.
    :param gateway: Usage-limit gateway; defaults to the composite poller.
    :returns: A fully-wired container.
    """
    pool_repo = SqlCredentialPoolRepository(session_maker)
    state_repo = SqlUsageLimitStateRepository(session_maker)
    registry = SqlSessionCredentialRegistry(session_maker)
    cost_sink = SqlCostAttributionSink(session_maker)
    selection_policy = PriorityCredentialSelectionPolicy(pool_repo, state_repo)
    resolved_gateway = gateway or CompositeUsageLimitGateway([UsageEndpointPoller()])
    resolved_notifier = notifier or LoggingFailoverNotifier()

    return CswapContainer(
        session_maker=session_maker,
        pool_repo=pool_repo,
        state_repo=state_repo,
        registry=registry,
        cost_sink=cost_sink,
        gateway=resolved_gateway,
        notifier=resolved_notifier,
        track_usage_limit=TrackUsageLimitUseCase(state_repo),
        select_credential=SelectCredentialUseCase(selection_policy),
        failover_on_limit=FailoverOnLimitUseCase(
            pool_repo, selection_policy, registry, resolved_notifier
        ),
    )
