"""Smoke test that the DI container wires an end-to-end select/track flow."""

from __future__ import annotations

from omnigent.cswap.config.pool_config import account_id_for, load_pools
from omnigent.cswap.config.pool_config_syncer import sync_pools
from omnigent.cswap.container import build_container
from omnigent.cswap.domain.value_objects.limit_state import LimitDetectionResult
from omnigent.cswap.domain.value_objects.usage_window import UsageWindow
from omnigent.db.utils import ManagedSessionMaker


def test_container_select_track_failover_round_trip(session_maker: ManagedSessionMaker) -> None:
    sync_pools(
        session_maker,
        load_pools(
            {
                "pools": {
                    "claude-pool": {
                        "family": "anthropic",
                        "failover": "auto",
                        "members": [
                            {"name": "c1", "claude_config_dir": "~/.c1", "priority": 0},
                            {"name": "c2", "claude_config_dir": "~/.c2", "priority": 1},
                        ],
                    }
                }
            }
        ),
    )
    container = build_container(session_maker)
    c1 = account_id_for("claude-pool", "c1")
    c2 = account_id_for("claude-pool", "c2")

    # Fresh: launch selection returns a subscription account.
    selected = container.select_credential.execute("anthropic", now=1000)
    assert selected.account is not None
    assert selected.account.id in {c1, c2}

    # c1 hits a limit (reactive) -> newly limited -> auto failover to c2.
    tracked = container.track_usage_limit.execute(
        LimitDetectionResult(
            credential_id=c1,
            is_limited=True,
            source="reactive",
            observed_at=1000,
            windows=(UsageWindow("5h", 100, 99999),),
        )
    )
    assert tracked.was_newly_limited is True

    event = container.failover_on_limit.execute(
        session_id="sess-1", exhausted_credential_id=c1, family="anthropic", now=1000
    )
    assert event is not None
    assert event.switched is True
    assert event.next_credential_id == c2
    assert container.registry.active_credential("sess-1") == c2
