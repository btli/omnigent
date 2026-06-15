"""Dependency-injection container for subscription-aware token management.

Assembles the repositories, gateway, selection policy, and use cases from
a single :data:`~omnigent.db.utils.ManagedSessionMaker`. The server builds
one container at startup and hands its use cases to the auth-resolution,
detection, and cost-attribution seams.
"""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.db.utils import ManagedSessionMaker
from omnigent.subscription_tokens.application.ports.ports import (
    CostAttributionSink,
    CredentialPoolRepository,
    FailoverNotifier,
    SessionCredentialRegistry,
    UsageLimitGateway,
    UsageLimitStateRepository,
)
from omnigent.subscription_tokens.application.use_cases.failover_on_limit import (
    FailoverOnLimitUseCase,
)
from omnigent.subscription_tokens.application.use_cases.select_credential import (
    SelectCredentialUseCase,
)
from omnigent.subscription_tokens.application.use_cases.track_usage_limit import (
    TrackUsageLimitUseCase,
)
from omnigent.subscription_tokens.infrastructure.detection.composite_usage_limit_gateway import (
    CompositeUsageLimitGateway,
)
from omnigent.subscription_tokens.infrastructure.detection.usage_endpoint_poller import (
    UsageEndpointPoller,
)
from omnigent.subscription_tokens.infrastructure.notification.notifiers import (
    LoggingFailoverNotifier,
)
from omnigent.subscription_tokens.infrastructure.repositories.sqlalchemy_repositories import (
    SqlCostAttributionSink,
    SqlCredentialPoolRepository,
    SqlSessionCredentialRegistry,
    SqlUsageLimitStateRepository,
)
from omnigent.subscription_tokens.infrastructure.selection.priority_selection_policy import (
    PriorityCredentialSelectionPolicy,
)


@dataclass(frozen=True)
class SubscriptionTokenContainer:
    """Wired subscription-token collaborators shared across the server."""

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
    immediate_session_maker: ManagedSessionMaker | None = None,
    notifier: FailoverNotifier | None = None,
    gateway: UsageLimitGateway | None = None,
) -> SubscriptionTokenContainer:
    """Build a :class:`SubscriptionTokenContainer` over *session_maker*.

    :param session_maker: Managed session factory bound to the omnigent DB.
    :param immediate_session_maker: A ``BEGIN IMMEDIATE`` factory used for the
        atomic newly-limited transition (see
        :meth:`~omnigent.subscription_tokens.application.ports.ports.UsageLimitStateRepository.observe`).
        Defaults to *session_maker*.
    :param notifier: Failover notifier; defaults to a logging notifier.
    :param gateway: Usage-limit gateway; defaults to the composite poller.
    :returns: A fully-wired container.
    """
    pool_repo = SqlCredentialPoolRepository(session_maker)
    state_repo = SqlUsageLimitStateRepository(session_maker, immediate_session_maker)
    registry = SqlSessionCredentialRegistry(session_maker)
    cost_sink = SqlCostAttributionSink(session_maker)
    selection_policy = PriorityCredentialSelectionPolicy(pool_repo, state_repo)
    resolved_gateway = gateway or CompositeUsageLimitGateway([UsageEndpointPoller()])
    resolved_notifier = notifier or LoggingFailoverNotifier()

    return SubscriptionTokenContainer(
        session_maker=session_maker,
        pool_repo=pool_repo,
        state_repo=state_repo,
        registry=registry,
        cost_sink=cost_sink,
        gateway=resolved_gateway,
        notifier=resolved_notifier,
        track_usage_limit=TrackUsageLimitUseCase(state_repo),
        select_credential=SelectCredentialUseCase(selection_policy),
        failover_on_limit=FailoverOnLimitUseCase(pool_repo, selection_policy, resolved_notifier),
    )
