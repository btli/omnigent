"""Tests for the subscription-token integration facade (explicit activation)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import httpx
import pytest

from omnigent.db.utils import ManagedSessionMaker
from omnigent.subscription_tokens import integration
from omnigent.subscription_tokens.config.pool_config import account_id_for, load_pools
from omnigent.subscription_tokens.config.pool_config_syncer import sync_pools
from omnigent.subscription_tokens.container import build_container
from omnigent.subscription_tokens.domain.value_objects.limit_state import LimitState
from omnigent.subscription_tokens.labels import (
    CREDENTIAL_ACCOUNT_LABEL,
    CREDENTIAL_FAMILY_LABEL,
    CREDENTIAL_KIND_LABEL,
)


def _accounts(snapshot: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return the first pool's typed per-account dicts from a status snapshot."""
    accounts = snapshot[0]["accounts"]
    assert isinstance(accounts, list)
    return accounts


def _bound_family(session_maker: ManagedSessionMaker, session_id: str) -> str | None:
    """Return the persisted ``family`` of *session_id*'s binding, or ``None``."""
    from omnigent.db.db_models import SqlSessionCredentialBinding

    with session_maker() as session:
        row = session.get(SqlSessionCredentialBinding, session_id)
        return row.family if row is not None else None


def test_resolve_db_uri_prefers_explicit_then_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The facade shares the server/host DB: explicit override, else DATABASE_URL.

    Both are normalised to the psycopg3 dialect so the resolved string matches
    the server's engine cache key (one shared engine, one shared database).
    """
    monkeypatch.setenv(integration.DATABASE_URI_ENV, "postgresql://u:p@h:5432/explicit")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/fallback")
    # Explicit OMNIGENT_DATABASE_URI wins, normalised to +psycopg.
    assert integration._resolve_db_uri() == "postgresql+psycopg://u:p@h:5432/explicit"
    # Without it, DATABASE_URL (what the server entrypoint sets) is used.
    monkeypatch.delenv(integration.DATABASE_URI_ENV, raising=False)
    assert integration._resolve_db_uri() == "postgresql+psycopg://u:p@h:5432/fallback"
    # Neither → the machine-global sqlite chat.db (laptop/dev path).
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert integration._resolve_db_uri().startswith("sqlite:///")


@pytest.fixture
def active_facade(session_maker: ManagedSessionMaker) -> Iterator[ManagedSessionMaker]:
    """Activate the facade over a seeded two-subscription claude pool."""
    pools = load_pools(
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
    )
    sync_pools(session_maker, pools)
    integration.activate(build_container(session_maker), pools)
    try:
        yield session_maker
    finally:
        integration.deactivate()


@pytest.fixture
def active_openai_facade(session_maker: ManagedSessionMaker) -> Iterator[ManagedSessionMaker]:
    """Activate the facade over an OpenAI pool: two Codex subs + an api_key."""
    pools = load_pools(
        {
            "pools": {
                "codex-pool": {
                    "family": "openai",
                    "failover": "auto",
                    "members": [
                        {"name": "x1", "codex_config_dir": "~/.codex-x1", "priority": 0},
                        {"name": "x2", "codex_config_dir": "~/.codex-x2", "priority": 1},
                        {
                            "name": "xkey",
                            "kind": "api_key",
                            "api_key_ref": "env:OAI_TEST_KEY",
                            "priority": 9,
                        },
                    ],
                }
            }
        }
    )
    sync_pools(session_maker, pools)
    integration.activate(build_container(session_maker), pools)
    try:
        yield session_maker
    finally:
        integration.deactivate()


def test_inactive_facade_is_noop() -> None:
    integration.deactivate()
    assert integration.is_active() is False
    assert integration.select_launch_env_for_family("anthropic") == {}
    assert integration.select_codex_launch(session_id="s") == integration.CodexLaunchSelection()
    assert integration.status_snapshot() == []
    assert integration.mark_available("whatever") is False


def test_select_codex_launch_subscription_returns_source_and_binds(
    active_openai_facade: ManagedSessionMaker,
) -> None:
    # Priority-0 subscription is selected; its CODEX_HOME is the bridge source.
    selection = integration.select_codex_launch(session_id="sess-x")
    assert selection.config_source == os.path.expanduser("~/.codex-x1")
    assert selection.api_key is None
    # Session is bound to x1 so reactive failover + cost attribution resolve.
    bound = build_container(active_openai_facade).registry.active_credential("sess-x")
    assert bound == account_id_for("codex-pool", "x1")


def test_select_codex_launch_api_key_when_subscriptions_limited(
    active_openai_facade: ManagedSessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OAI_TEST_KEY", "sk-oai-xyz")
    # Limit both subscriptions → tier fallback to the api_key account.
    repo = build_container(active_openai_facade).state_repo
    for name in ("x1", "x2"):
        repo.upsert(
            LimitState(
                account_id_for("codex-pool", name),
                is_limited=True,
                limited_until=10**12,
                source="manual",
                last_checked_at=1,
            )
        )
    selection = integration.select_codex_launch(session_id="sess-y")
    assert selection.config_source is None
    assert selection.api_key == "sk-oai-xyz"
    bound = build_container(active_openai_facade).registry.active_credential("sess-y")
    assert bound == account_id_for("codex-pool", "xkey")


def test_transfer_session_binding_carries_account_across_session_ids(
    active_openai_facade: ManagedSessionMaker,
) -> None:
    # The launch binds the parent; a native /clear rotation or a sub-agent child
    # gets a new session id that must resolve the same account.
    integration.select_codex_launch(session_id="sess-parent")
    registry = build_container(active_openai_facade).registry
    parent_cred = registry.active_credential("sess-parent")
    assert parent_cred == account_id_for("codex-pool", "x1")

    integration.transfer_session_binding("sess-parent", "sess-child", family="openai")
    assert registry.active_credential("sess-child") == parent_cred
    assert _bound_family(active_openai_facade, "sess-child") == "openai"  # family pinned, not lost

    # No-op when the source has no binding — never invents one.
    integration.transfer_session_binding("sess-unbound", "sess-target", family="openai")
    assert registry.active_credential("sess-target") is None


async def test_codex_child_registration_copies_binding_to_child_session(
    active_openai_facade: ManagedSessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Wiring test for the sub-agent path: when the forwarder registers a Codex
    # child thread, it must copy the parent's launched OpenAI account onto the
    # new child session id (so reactive detection on child events resolves it).
    from omnigent import codex_native_forwarder as fwd

    integration.select_codex_launch(session_id="conv-parent")
    registry = build_container(active_openai_facade).registry
    parent_cred = registry.active_credential("conv-parent")
    assert parent_cred == account_id_for("codex-pool", "x1")

    async def _fake_register(
        client: httpx.AsyncClient,
        *,
        parent_session_id: str,
        parent_thread_id: str | None,
        child_thread_id: str,
        item: dict[str, object],
    ) -> str:
        return "conv-child"

    monkeypatch.setattr(fwd, "_register_child_session", _fake_register)
    state = fwd._CodexForwarderState()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        base_url="http://ap",
    ) as client:
        await fwd._ensure_child_session(
            client,
            parent_session_id="conv-parent",
            parent_thread_id=None,
            child_thread_id="thread-child",
            item={},
            forwarder_state=state,
        )

    assert registry.active_credential("conv-child") == parent_cred
    # Pin the family the call site passes (a regression to a non-openai family
    # would still satisfy the credential-id assertion above, but not this one).
    assert _bound_family(active_openai_facade, "conv-child") == "openai"


def test_select_launch_env_returns_config_dir_and_binds(
    active_facade: ManagedSessionMaker,
) -> None:
    env = integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    assert env == {"CLAUDE_CONFIG_DIR": os.path.expanduser("~/.c1")}  # priority 0, available
    # Session is bound to the selected account.
    snapshot = integration.status_snapshot()
    assert snapshot[0]["name"] == "claude-pool"


def test_reactive_limit_without_reset_applies_cooldown(
    active_facade: ManagedSessionMaker,
) -> None:
    # Bind the session to c1 via a launch selection.
    integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    c1 = account_id_for("claude-pool", "c1")

    # A limit signal with NO reset headers.
    integration.record_reactive_text(
        "Claude usage limit reached.", family="anthropic", session_id="sess-1"
    )

    # c1 is now limited; the next launch selection avoids it.
    env = integration.select_launch_env_for_family("anthropic", session_id="sess-2")
    assert env == {"CLAUDE_CONFIG_DIR": os.path.expanduser("~/.c2")}

    # Failover does NOT rebind the running session: sess-1 was launched on c1
    # and keeps running on it (the next launch is what rotates to c2).
    container = build_container(active_facade)
    assert container.registry.active_credential("sess-1") == c1

    # No header reset, but the facade applied a default cooldown — limited with
    # a concrete limited_until (auto-recovers, not a permanent lockout).
    snapshot = integration.status_snapshot()
    c1_account = next(a for a in _accounts(snapshot) if a["id"] == c1)
    assert c1_account["limit_status"] == "limited"
    assert c1_account["limited_until"] is not None


def test_reactive_text_usage_limit_triggers_failover(active_facade: ManagedSessionMaker) -> None:
    integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    c1 = account_id_for("claude-pool", "c1")
    c2 = account_id_for("claude-pool", "c2")

    # A Claude "usage limit reached" line in forwarded agent output.
    integration.record_reactive_text(
        "Claude AI usage limit reached. Resets later.",
        family="anthropic",
        session_id="sess-1",
    )

    # c1 limited → next launch avoids it; the running sess-1 stays on c1.
    env = integration.select_launch_env_for_family("anthropic", session_id="sess-2")
    assert env == {"CLAUDE_CONFIG_DIR": os.path.expanduser("~/.c2")}
    assert build_container(active_facade).registry.active_credential("sess-1") == c1
    assert c2  # referenced for clarity


def test_reactive_text_non_limit_is_noop(active_facade: ManagedSessionMaker) -> None:
    integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    # Ordinary output (no limit signal) leaves everything available.
    integration.record_reactive_text(
        "Here is the answer to your question.", family="anthropic", session_id="sess-1"
    )
    snapshot = integration.status_snapshot()
    statuses = {a["id"]: a["limit_status"] for a in _accounts(snapshot)}
    assert all(s != "limited" for s in statuses.values())


def test_attribute_cost_and_status_snapshot(active_facade: ManagedSessionMaker) -> None:
    integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    integration.attribute_cost("sess-1", cost_usd=1.25, input_tokens=100, output_tokens=20)
    # No-op posts (zero spend, or a negative cumulative glitch) must not
    # create a row or inflate the turn count.
    integration.attribute_cost("sess-1", cost_usd=0.0, input_tokens=0, output_tokens=0)
    integration.attribute_cost("sess-1", cost_usd=-5.0, input_tokens=-3, output_tokens=0)

    snapshot = integration.status_snapshot()
    # The pool surfaces its rotation mode (default here) for operator visibility.
    assert snapshot[0]["rotation_mode"] == "max_headroom"
    accounts = {a["name"]: a for a in _accounts(snapshot)}
    assert accounts["c1"]["cost_today_usd"] == pytest.approx(1.25)  # unchanged by no-ops
    # Never observed (no probe/limit) → unknown, not available.
    assert accounts["c1"]["limit_status"] == "unknown"


def test_mark_available_clears_limit(active_facade: ManagedSessionMaker) -> None:
    integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    integration.record_reactive_text(
        "Claude usage limit reached.", family="anthropic", session_id="sess-1"
    )
    c1 = account_id_for("claude-pool", "c1")

    assert integration.mark_available(c1) is True
    snapshot = integration.status_snapshot()
    accounts = {a["id"]: a for a in _accounts(snapshot)}
    assert accounts[c1]["limit_status"] == "available"


def test_active_credential_for_session_returns_bound_account(
    active_openai_facade: ManagedSessionMaker,
) -> None:
    integration.select_codex_launch(session_id="sess-x")  # binds priority-0 x1
    assert integration.active_credential_for_session("sess-x") == {
        "id": account_id_for("codex-pool", "x1"),
        "name": "x1",
        "kind": "subscription",
        "family": "openai",
        "limit_status": "unknown",  # never observed
    }


def test_active_credential_for_session_reflects_limit_state(
    active_openai_facade: ManagedSessionMaker,
) -> None:
    integration.select_codex_launch(session_id="sess-x")
    build_container(active_openai_facade).state_repo.upsert(
        LimitState(
            account_id_for("codex-pool", "x1"),
            is_limited=True,
            limited_until=10**12,
            source="manual",
            last_checked_at=1,
        )
    )
    info = integration.active_credential_for_session("sess-x")
    assert info is not None
    assert info["limit_status"] == "limited"  # mid-session: stays on the limited account


def test_active_credential_for_session_api_key_tier(
    active_openai_facade: ManagedSessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OAI_TEST_KEY", "sk-oai-xyz")
    repo = build_container(active_openai_facade).state_repo
    for name in ("x1", "x2"):
        repo.upsert(
            LimitState(
                account_id_for("codex-pool", name),
                is_limited=True,
                limited_until=10**12,
                source="manual",
                last_checked_at=1,
            )
        )
    integration.select_codex_launch(session_id="sess-y")  # tier-falls to api_key xkey
    info = integration.active_credential_for_session("sess-y")
    assert info is not None
    assert info["name"] == "xkey"
    assert info["kind"] == "api_key"
    assert info["family"] == "openai"


def test_active_credential_for_session_unbound_is_none(
    active_openai_facade: ManagedSessionMaker,
) -> None:
    # Never invents an account for a session the launch never bound.
    assert integration.active_credential_for_session("never-launched") is None


def test_active_credential_for_session_inactive_is_none() -> None:
    integration.deactivate()
    assert integration.active_credential_for_session("s") is None


def test_credential_labels_for_session_projects_bound_account(
    active_openai_facade: ManagedSessionMaker,
) -> None:
    """The policy-engine seed facade projects the bound account to the three
    engine-owned labels."""
    integration.select_codex_launch(session_id="sess-x")  # binds priority-0 x1
    assert integration.credential_labels_for_session("sess-x") == {
        CREDENTIAL_KIND_LABEL: "subscription",
        CREDENTIAL_FAMILY_LABEL: "openai",
        CREDENTIAL_ACCOUNT_LABEL: account_id_for("codex-pool", "x1"),
    }


def test_credential_labels_for_session_unbound_is_empty(
    active_openai_facade: ManagedSessionMaker,
) -> None:
    """A session the launch never bound projects to no labels."""
    assert integration.credential_labels_for_session("never-launched") == {}


def test_credential_labels_for_session_inactive_is_empty() -> None:
    """No pool configured → no labels (and no container build)."""
    integration.deactivate()
    assert integration.credential_labels_for_session("s") == {}


def test_credential_labels_for_session_skips_limit_state_read(
    active_openai_facade: ManagedSessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-build label facade resolves the binding directly and does NOT
    read limit state — keeping that DB read off the build_policy_engine hot
    path. (The chip's active_credential_for_session DOES read it — the contrast
    proves the skip is real, not just an unbound no-op.)"""
    integration.select_codex_launch(session_id="sess-x")
    container = integration._ensure_container()
    assert container is not None
    calls: list[object] = []
    real_find_many = container.state_repo.find_many

    def spy(ids: list[str]) -> object:
        calls.append(ids)
        return real_find_many(ids)

    monkeypatch.setattr(container.state_repo, "find_many", spy)

    labels = integration.credential_labels_for_session("sess-x")
    assert labels[CREDENTIAL_KIND_LABEL] == "subscription"
    assert calls == []  # the label path never touched limit state

    integration.active_credential_for_session("sess-x")
    assert calls  # contrast: the chip path does read it


def test_credential_labels_for_session_swallows_errors(
    active_openai_facade: ManagedSessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Always-safe contract: a lookup failure returns ``{}`` (logged), so it can
    never break a policy-engine build / a turn."""
    integration.select_codex_launch(session_id="sess-x")

    def boom(_credential_id: str) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(integration, "_find_member", boom)
    assert integration.credential_labels_for_session("sess-x") == {}


def test_sessions_for_credentials_returns_bound_sessions(
    active_openai_facade: ManagedSessionMaker,
) -> None:
    integration.select_codex_launch(session_id="sess-a")
    integration.select_codex_launch(session_id="sess-b")
    x1 = account_id_for("codex-pool", "x1")
    x2 = account_id_for("codex-pool", "x2")
    result = integration.sessions_for_credentials([x1, x2])
    assert set(result[x1]) == {"sess-a", "sess-b"}
    assert result[x2] == []  # an account no session launched on → empty (key present)


def test_sessions_for_credentials_inactive_is_empty() -> None:
    integration.deactivate()
    assert integration.sessions_for_credentials(["whatever"]) == {}


def test_registry_sessions_for_credentials_orders_newest_first_and_caps(
    active_openai_facade: ManagedSessionMaker,
) -> None:
    from omnigent.db.db_models import SqlSessionCredentialBinding

    x1 = account_id_for("codex-pool", "x1")
    registry = build_container(active_openai_facade).registry
    with active_openai_facade() as session:
        for i in range(5):
            session.add(
                SqlSessionCredentialBinding(
                    session_id=f"c{i}", credential_id=x1, family="openai", bound_at=2000 + i
                )
            )
        session.commit()
    # Newest bound_at first, capped per credential at the requested limit.
    assert registry.sessions_for_credentials([x1], limit_per=2) == {x1: ["c4", "c3"]}


def test_registry_sessions_for_credentials_live_filter_beats_the_cap(
    active_openai_facade: ManagedSessionMaker,
) -> None:
    # A long-running session must not be hidden behind newer dead bindings: the
    # only_session_ids filter is applied in SQL *before* the per-credential cap.
    from omnigent.db.db_models import SqlSessionCredentialBinding

    x1 = account_id_for("codex-pool", "x1")
    registry = build_container(active_openai_facade).registry
    with active_openai_facade() as session:
        for i in range(200):  # 200 newer, dead bindings
            session.add(
                SqlSessionCredentialBinding(
                    session_id=f"dead{i}", credential_id=x1, family="openai", bound_at=5000 + i
                )
            )
        session.add(  # one older, still-live binding
            SqlSessionCredentialBinding(
                session_id="live-old", credential_id=x1, family="openai", bound_at=1
            )
        )
        session.commit()
    result = registry.sessions_for_credentials([x1], only_session_ids={"live-old"}, limit_per=200)
    assert result == {x1: ["live-old"]}


@pytest.fixture
def active_oauth_facade(session_maker: ManagedSessionMaker) -> Iterator[ManagedSessionMaker]:
    """Activate a claude pool whose subscriptions auth by a headless OAuth token."""
    pools = load_pools(
        {
            "pools": {
                "claude-pool": {
                    "family": "anthropic",
                    "failover": "auto",
                    "members": [
                        {
                            "name": "sub-a",
                            "kind": "subscription",
                            "oauth_token_ref": "env:CLAUDE_OAUTH_A",
                            "priority": 0,
                        },
                        {
                            "name": "sub-b",
                            "kind": "subscription",
                            "oauth_token_ref": "env:CLAUDE_OAUTH_B",
                            "priority": 1,
                        },
                    ],
                }
            }
        }
    )
    sync_pools(session_maker, pools)
    integration.activate(build_container(session_maker), pools)
    try:
        yield session_maker
    finally:
        integration.deactivate()


def test_select_launch_env_injects_oauth_token_and_binds(
    active_oauth_facade: ManagedSessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_OAUTH_A", "sk-ant-oat-aaa")
    env = integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-aaa"}  # priority-0 sub-a's token
    bound = build_container(active_oauth_facade).registry.active_credential("sess-1")
    assert bound == account_id_for("claude-pool", "sub-a")


def test_select_launch_env_oauth_unresolved_does_not_bind(
    active_oauth_facade: ManagedSessionMaker,
) -> None:
    # sub-a declares an oauth_token_ref but CLAUDE_OAUTH_A is unset → the token
    # can't resolve → empty env AND no binding (the process falls back to ambient
    # creds, so attributing sub-a would mis-route failover/cost).
    env = integration.select_launch_env_for_family("anthropic", session_id="sess-2")
    assert env == {}
    assert build_container(active_oauth_facade).registry.active_credential("sess-2") is None


def test_select_codex_launch_injects_access_token_and_binds(
    session_maker: ManagedSessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_OAUTH_A", "codex-oat-aaa")
    pools = load_pools(
        {
            "pools": {
                "codex-pool": {
                    "family": "openai",
                    "failover": "auto",
                    "members": [
                        {
                            "name": "cx-a",
                            "kind": "subscription",
                            "oauth_token_ref": "env:CODEX_OAUTH_A",
                            "priority": 0,
                        },
                    ],
                }
            }
        }
    )
    sync_pools(session_maker, pools)
    integration.activate(build_container(session_maker), pools)
    try:
        selection = integration.select_codex_launch(session_id="sess-cx")
        assert selection.access_token == "codex-oat-aaa"  # CODEX_ACCESS_TOKEN source
        assert selection.config_source is None
        assert selection.api_key is None
        bound = build_container(session_maker).registry.active_credential("sess-cx")
        assert bound == account_id_for("codex-pool", "cx-a")
    finally:
        integration.deactivate()
