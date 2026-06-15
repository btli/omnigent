"""Integration facade between omnigent's existing seams and cswap.

The existing launch / detection / cost / server code calls a handful of
small, **always-safe** functions here instead of touching the cswap
internals directly. Every function:

* is a no-op when no ``pools:`` block is configured (backward compatible);
* never raises — any error is logged and swallowed so multi-subscription
  problems can never break a launch, a turn, or the server;
* lazily builds a :class:`CswapContainer` over the machine-global omnigent
  DB (the same ``chat.db`` the server and ``omnigent run`` share), or uses
  the container the server explicitly activates at startup.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import replace

from sqlalchemy import select

from omnigent.cswap.container import CswapContainer, build_container
from omnigent.cswap.domain.entities.credential_pool import CredentialPool
from omnigent.cswap.domain.entities.provider_account import ProviderAccount
from omnigent.cswap.domain.value_objects.enums import Family
from omnigent.cswap.domain.value_objects.limit_state import LimitDetectionResult, LimitState
from omnigent.db.utils import now_epoch, utc_day

logger = logging.getLogger(__name__)

DATABASE_URI_ENV = "OMNIGENT_DATABASE_URI"

# Default auto-recovery cooldown for a reactive limit with no known reset
# time (Claude's rolling 5h window). Prevents a permanent lockout when the
# proactive poller is disabled.
_DEFAULT_LIMIT_COOLDOWN_S = 5 * 3600

# Server-activated container (set by the lifespan). When None, the lazy
# machine-global path is used instead.
_container: CswapContainer | None = None
_pools: dict[str, CredentialPool] = {}
_lazy_attempted = False
# Guards lazy init against concurrent first-callers (the reactive hook may
# run in a worker thread via asyncio.to_thread; multiple session forwarders
# can race the first build).
_init_lock = threading.Lock()


def activate(container: CswapContainer, pools: dict[str, CredentialPool]) -> None:
    """Install the server-built container + parsed pools as the active pair."""
    global _container, _pools
    _container = container
    _pools = pools
    logger.info("cswap activated with %d pool(s)", len(pools))


def deactivate() -> None:
    """Clear the active container (for tests / shutdown)."""
    global _container, _pools, _lazy_attempted
    _container = None
    _pools = {}
    _lazy_attempted = False


def _resolve_db_uri() -> str:
    """Resolve the omnigent DB URI (env override, else machine-global)."""
    override = os.environ.get(DATABASE_URI_ENV)
    if override:
        return override
    from omnigent.host.local_server import _local_data_dir

    return f"sqlite:///{_local_data_dir() / 'chat.db'}"


def _ensure_container() -> CswapContainer | None:
    """Return the active container, lazily building one if needed.

    The lazy build is attempted at most once per process; failures (no DB,
    no pools) leave the facade inert.
    """
    global _container, _pools, _lazy_attempted
    if _container is not None:
        return _container if _pools else None
    if _lazy_attempted:
        return None
    # Double-checked locking: only one thread runs the (heavyweight, migration-
    # running) build; others wait and see the result.
    with _init_lock:
        if _container is not None:
            return _container if _pools else None
        if _lazy_attempted:
            return None
        _lazy_attempted = True
        try:
            from omnigent.cswap.config.pool_config import load_pools
            from omnigent.cswap.config.pool_config_syncer import sync_pools
            from omnigent.db.utils import get_or_create_engine, make_managed_session_maker
            from omnigent.onboarding.provider_config import load_config

            pools = load_pools(load_config())
            if not pools:
                return None
            engine = get_or_create_engine(_resolve_db_uri())
            session_maker = make_managed_session_maker(engine)
            sync_pools(session_maker, pools)
            _container = build_container(session_maker)
            _pools = pools
            logger.info("cswap lazily initialised with %d pool(s)", len(pools))
            return _container
        except Exception:
            logger.exception("cswap lazy initialisation failed; multi-subscription disabled")
            return None


def is_active() -> bool:
    """Whether multi-subscription routing is configured and available."""
    return _ensure_container() is not None


_CONFIG_DIR_ENV = {"anthropic": "CLAUDE_CONFIG_DIR", "openai": "CODEX_HOME"}
_API_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def select_launch_env_for_family(
    family: Family, *, session_id: str | None = None
) -> dict[str, str]:
    """Select an account for *family* and return the env to launch it with.

    For a subscription account this is its isolated config dir
    (``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME``); for a tier-fallback api_key
    account it is the resolved key (``ANTHROPIC_API_KEY`` /
    ``OPENAI_API_KEY``). Binds the session to the chosen account when
    *session_id* is given (so reactive failover and cost attribution can
    find it).

    :returns: Env vars to merge into the launched process, or ``{}`` when
        no pool is configured or on any error.
    """
    container = _ensure_container()
    if container is None:
        return {}
    try:
        account = container.select_credential.execute(family, now_epoch()).account
        if account is None:
            return {}
        env: dict[str, str] = {}
        if account.is_subscription:
            config_dir = account.config_dir()
            if config_dir:
                env = {_CONFIG_DIR_ENV[family]: os.path.expanduser(config_dir)}
        else:
            from omnigent.cswap.infrastructure.detection.credentials import (
                resolve_account_api_key,
            )

            key = resolve_account_api_key(account)
            if key:
                env = {_API_KEY_ENV[family]: key}
        # Bind whenever the chosen account is actually the one the process will
        # use: a subscription always (a dir-less subscription runs on its
        # default login, which IS this account); an api_key only when its key
        # resolved (otherwise the process falls back to ambient creds and
        # attributing this account would mis-route failover/cost).
        if session_id and (account.is_subscription or env):
            container.registry.bind(session_id, account.id, family)
        return env
    except Exception:
        logger.exception("cswap account selection failed for family %s", family)
        return {}


def record_reactive_text(text: str, *, family: Family, session_id: str) -> None:
    """Scan agent *text* for a usage-limit signal and run track + failover.

    Called per forwarded transcript item, so it parses **first** (a cheap
    regex) and only touches the container / DB on a positive match — when
    no pool is configured or the text shows no limit, this is regex-only.
    """
    if not session_id:
        return
    try:
        from omnigent.cswap.infrastructure.detection.reactive_output_detector import (
            ReactiveOutputDetector,
        )

        parsed = ReactiveOutputDetector.parse(text, family=family)
        if not parsed.is_limited:
            return
        container = _ensure_container()
        if container is None:
            return
        credential_id = container.registry.active_credential(session_id)
        if not credential_id:
            return
        detection = ReactiveOutputDetector.to_detection(credential_id, parsed, now_epoch())
        if detection is None:
            return
        _track_and_failover(container, detection, session_id=session_id, family=family)
    except Exception:
        logger.exception("cswap reactive detection failed")


def _with_recovery(detection: LimitDetectionResult) -> LimitDetectionResult:
    """Ensure a limited detection has a recovery time so it auto-recovers.

    A reactive limit often carries no reset (a 429 with no headers, or
    "usage limit reached" text without the header lines). Without a
    ``limited_until`` the account would stay limited until the poller probes
    it or it is manually cleared — a permanent lockout when polling is off.
    Default a ``5h`` cooldown (Claude's rolling window).
    """
    if not detection.is_limited or detection.limited_until is not None:
        return detection
    return replace(detection, limited_until=detection.observed_at + _DEFAULT_LIMIT_COOLDOWN_S)


def _track_and_failover(
    container: CswapContainer,
    detection: LimitDetectionResult,
    *,
    session_id: str,
    family: Family,
) -> None:
    """Persist *detection*; fire one-shot failover when newly limited."""
    detection = _with_recovery(detection)
    result = container.track_usage_limit.execute(detection)
    if result.was_newly_limited:
        container.failover_on_limit.execute(
            session_id=session_id,
            exhausted_credential_id=detection.credential_id,
            family=family,
            now=detection.observed_at,
        )


async def run_poll_sweep_once() -> int:
    """Probe every active account once and persist the results.

    Proactive only — does not trigger failover (no active session to fail
    over); the refreshed state steers the next launch selection. No-op
    unless ``OMNIGENT_CSWAP_POLL_ENABLED`` is set.

    :returns: The number of accounts whose state was refreshed.
    """
    container = _ensure_container()
    if container is None:
        return 0
    from omnigent.cswap.infrastructure.detection.usage_endpoint_poller import is_poll_enabled

    if not is_poll_enabled():
        return 0

    accounts = [m for pool in _pools.values() for m in pool.members if m.is_active]

    async def _probe_one(account: ProviderAccount) -> int:
        """Probe one account and persist; never raises (per-account isolated)."""
        try:
            detection = await container.gateway.fetch_limit_state(account, now=now_epoch())
            if detection is not None:
                # Recovery default so a probe that only learns "limited" (e.g.
                # 429 + retry-after, no window) still auto-recovers.
                container.track_usage_limit.execute(_with_recovery(detection))
                return 1
        except Exception:
            logger.exception("cswap probe/track failed for account %s", account)
        return 0

    # Probe all accounts concurrently — independent network calls.
    results = await asyncio.gather(*(_probe_one(a) for a in accounts))
    return sum(results)


async def poll_loop(*, interval_s: float = 1800.0) -> None:
    """Background loop running :func:`run_poll_sweep_once` every *interval_s*.

    Started from the server lifespan; cancel the task to stop it. Inert
    when polling is disabled or no pool is configured.
    """
    while True:
        try:
            await run_poll_sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cswap poll sweep failed")
        await asyncio.sleep(interval_s)


def status_snapshot() -> list[dict[str, object]]:
    """Return the current pools, accounts, limit states, and today's cost.

    Shape (one entry per pool)::

        {"name", "family", "failover_mode",
         "accounts": [{"id", "name", "kind", "priority", "is_active",
                       "limit_status", "limited_until", "windows",
                       "earliest_reset_at", "cost_today_usd"}]}

    :returns: A list of pool dicts, or ``[]`` when no pool is configured.
    """
    container = _ensure_container()
    if container is None:
        return []
    try:
        now = now_epoch()
        all_ids = [m.id for pool in _pools.values() for m in pool.members]
        states = container.state_repo.find_many(all_ids)
        costs = _costs_today(container, all_ids, utc_day(now))
        return [
            {
                "name": pool.name,
                "family": pool.family,
                "failover_mode": pool.failover_mode,
                "accounts": [
                    _account_status(m, states.get(m.id), costs.get(m.id, 0.0), now)
                    for m in pool.members
                ],
            }
            for pool in _pools.values()
        ]
    except Exception:
        logger.exception("cswap status_snapshot failed")
        return []


def _costs_today(
    container: CswapContainer, credential_ids: list[str], day_utc: str
) -> dict[str, float]:
    """Batch-fetch today's per-account cost in a single query."""
    from omnigent.db.db_models import SqlProviderAccountCost

    if not credential_ids:
        return {}
    with container.session_maker() as session:
        rows = session.execute(
            select(SqlProviderAccountCost).where(
                SqlProviderAccountCost.credential_id.in_(credential_ids),
                SqlProviderAccountCost.day_utc == day_utc,
            )
        ).scalars()
        return {row.credential_id: row.cost_usd for row in rows}


def _account_status(
    member: ProviderAccount, state: LimitState | None, cost_today: float, now: int
) -> dict[str, object]:
    """Build one account's status entry for :func:`status_snapshot`."""
    windows = {w.label: w.utilization_pct for w in state.windows} if state else {}
    return {
        "id": member.id,
        "name": member.name,
        "kind": member.kind,
        "priority": member.priority,
        "is_active": member.is_active,
        "limit_status": state.to_status(now) if state else "unknown",
        "limited_until": state.limited_until if state and state.is_limited else None,
        "windows": windows,
        "earliest_reset_at": state.earliest_reset_at() if state else None,
        "cost_today_usd": cost_today,
    }


def mark_available(credential_id: str) -> bool:
    """Manually clear an account's limited state (bypasses staleness).

    :returns: ``True`` when applied, ``False`` when cswap is inactive.
    """
    container = _ensure_container()
    if container is None:
        return False
    try:
        container.track_usage_limit.execute(
            LimitDetectionResult(
                credential_id=credential_id,
                is_limited=False,
                source="manual",
                observed_at=now_epoch(),
            )
        )
        return True
    except Exception:
        logger.exception("cswap mark_available failed for %s", credential_id)
        return False


def attribute_cost(
    session_id: str,
    *,
    cost_usd: float,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Attribute a turn's cost to the session's active account."""
    container = _ensure_container()
    if container is None or not session_id:
        return
    try:
        credential_id = container.registry.active_credential(session_id)
        if not credential_id:
            return
        container.cost_sink.record_credential_cost(
            credential_id,
            utc_day(now_epoch()),
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except Exception:
        logger.exception("cswap cost attribution failed")
