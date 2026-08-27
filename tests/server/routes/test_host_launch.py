"""Tests for the host launch authorization helpers.

Tests ``resolve_host_owner`` and ``resolve_host_launch`` directly
(pure function tests, no HTTP).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import HTTPException

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes._host_launch import (
    resolve_host_launch,
    resolve_host_launch_allowlist,
    resolve_host_owner,
)
from omnigent.stores.host_store import now_epoch

_MAINTAINER = "alice"
_SA_IDENTITY = "system:serviceaccount:webhooks:webhook"


@dataclass
class _FakeHost:
    host_id: str = "host_1"
    name: str = "test-host"
    user_id: str = "alice"
    status: str = "online"
    updated_at: int = field(default_factory=now_epoch)


@dataclass
class _FakeHostStore:
    hosts: dict[str, _FakeHost] = field(default_factory=dict)

    def get_host(self, host_id: str) -> _FakeHost | None:
        return self.hosts.get(host_id)


@dataclass
class _FakeHostRegistry:
    conns: dict[str, object] = field(default_factory=dict)

    def get(self, host_id: str) -> object | None:
        return self.conns.get(host_id)


@dataclass
class _FakeConversationStore:
    convs: dict[str, Conversation] = field(default_factory=dict)

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self.convs.get(conversation_id)


# ── resolve_host_owner ───────────────────────────────────────────────


class TestResolveHostOwner:
    def test_unknown_host_404(self) -> None:
        store = _FakeHostStore()
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_owner(user_id="alice", host_id="host_x", host_store=store)
        assert exc_info.value.status_code == 404

    def test_wrong_owner_403(self) -> None:
        host = _FakeHost(host_id="host_1", user_id="bob")
        store = _FakeHostStore(hosts={"host_1": host})
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_owner(user_id="alice", host_id="host_1", host_store=store)
        assert exc_info.value.status_code == 403

    def test_correct_owner(self) -> None:
        host = _FakeHost(host_id="host_1", user_id="alice")
        store = _FakeHostStore(hosts={"host_1": host})
        result = resolve_host_owner(user_id="alice", host_id="host_1", host_store=store)
        assert result.host_id == "host_1"

    def test_no_auth_skips_owner_check(self) -> None:
        host = _FakeHost(host_id="host_1", user_id="bob")
        store = _FakeHostStore(hosts={"host_1": host})
        result = resolve_host_owner(user_id=None, host_id="host_1", host_store=store)
        assert result.host_id == "host_1"


# ── resolve_host_owner: service-identity launch allowlist carve-out ──


class TestResolveHostOwnerLaunchAllowlist:
    """The (host_id, identity) carve-out lets a non-owner service identity
    (e.g. an in-cluster webhook receiver authenticated as its own K8s
    ServiceAccount) launch on one specific host without becoming its owner.
    """

    def test_allowlisted_identity_permitted_though_not_owner(self) -> None:
        host = _FakeHost(host_id="server1", user_id=_MAINTAINER)
        store = _FakeHostStore(hosts={"server1": host})
        result = resolve_host_owner(
            user_id=_SA_IDENTITY,
            host_id="server1",
            host_store=store,
            launch_allowlist=frozenset({("server1", _SA_IDENTITY)}),
        )
        assert result.host_id == "server1"

    def test_non_allowlisted_identity_still_403(self) -> None:
        host = _FakeHost(host_id="server1", user_id=_MAINTAINER)
        store = _FakeHostStore(hosts={"server1": host})
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_owner(
                user_id="system:serviceaccount:other-ns:other-sa",
                host_id="server1",
                host_store=store,
                launch_allowlist=frozenset({("server1", _SA_IDENTITY)}),
            )
        assert exc_info.value.status_code == 403

    def test_near_miss_identity_still_403(self) -> None:
        """A prefix/near-miss of the allowlisted identity must not match."""
        host = _FakeHost(host_id="server1", user_id=_MAINTAINER)
        store = _FakeHostStore(hosts={"server1": host})
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_owner(
                user_id=_SA_IDENTITY + "-imposter",
                host_id="server1",
                host_store=store,
                launch_allowlist=frozenset({("server1", _SA_IDENTITY)}),
            )
        assert exc_info.value.status_code == 403

    def test_wrong_host_id_still_403(self) -> None:
        """The same identity, allowlisted for a DIFFERENT host, must not match here."""
        host = _FakeHost(host_id="server2", user_id=_MAINTAINER)
        store = _FakeHostStore(hosts={"server2": host})
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_owner(
                user_id=_SA_IDENTITY,
                host_id="server2",
                host_store=store,
                launch_allowlist=frozenset({("server1", _SA_IDENTITY)}),
            )
        assert exc_info.value.status_code == 403

    def test_maintainer_ownership_unaffected_by_allowlist(self) -> None:
        """The real owner is still permitted even with an allowlist configured
        (and even though they are not in it) — ownership is checked first."""
        host = _FakeHost(host_id="server1", user_id=_MAINTAINER)
        store = _FakeHostStore(hosts={"server1": host})
        result = resolve_host_owner(
            user_id=_MAINTAINER,
            host_id="server1",
            host_store=store,
            launch_allowlist=frozenset({("server1", _SA_IDENTITY)}),
        )
        assert result.host_id == "server1"
        assert result.user_id == _MAINTAINER

    def test_empty_allowlist_matches_current_403_behavior(self) -> None:
        host = _FakeHost(host_id="server1", user_id=_MAINTAINER)
        store = _FakeHostStore(hosts={"server1": host})
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_owner(
                user_id=_SA_IDENTITY,
                host_id="server1",
                host_store=store,
                launch_allowlist=frozenset(),
            )
        assert exc_info.value.status_code == 403

    def test_default_env_resolution_with_no_var_set_matches_current_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no explicit allowlist passed and the env var unset, behavior is
        unchanged from before this carve-out existed."""
        monkeypatch.delenv("OMNIGENT_HOST_LAUNCH_ALLOWLIST", raising=False)
        host = _FakeHost(host_id="server1", user_id=_MAINTAINER)
        store = _FakeHostStore(hosts={"server1": host})
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_owner(user_id=_SA_IDENTITY, host_id="server1", host_store=store)
        assert exc_info.value.status_code == 403

    def test_default_env_resolution_with_var_set_permits_launch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no explicit allowlist passed, the env var is consulted directly."""
        monkeypatch.setenv("OMNIGENT_HOST_LAUNCH_ALLOWLIST", f"server1={_SA_IDENTITY}")
        host = _FakeHost(host_id="server1", user_id=_MAINTAINER)
        store = _FakeHostStore(hosts={"server1": host})
        result = resolve_host_owner(user_id=_SA_IDENTITY, host_id="server1", host_store=store)
        assert result.host_id == "server1"


# ── resolve_host_launch_allowlist ─────────────────────────────────────


class TestResolveHostLaunchAllowlist:
    def test_unset_yields_empty_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OMNIGENT_HOST_LAUNCH_ALLOWLIST", raising=False)
        assert resolve_host_launch_allowlist() == frozenset()

    def test_blank_yields_empty_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNIGENT_HOST_LAUNCH_ALLOWLIST", "   ")
        assert resolve_host_launch_allowlist() == frozenset()

    def test_single_entry_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNIGENT_HOST_LAUNCH_ALLOWLIST", f"server1={_SA_IDENTITY}")
        assert resolve_host_launch_allowlist() == frozenset({("server1", _SA_IDENTITY)})

    def test_multiple_entries_parsed_with_whitespace_tolerance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "OMNIGENT_HOST_LAUNCH_ALLOWLIST",
            f" server1={_SA_IDENTITY} , server2=system:serviceaccount:ns2:sa2 ",
        )
        assert resolve_host_launch_allowlist() == frozenset(
            {
                ("server1", _SA_IDENTITY),
                ("server2", "system:serviceaccount:ns2:sa2"),
            }
        )

    def test_malformed_entry_fails_closed_to_empty_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing '=' anywhere in the list disables the WHOLE allowlist,
        rather than silently applying only the entries that did parse."""
        monkeypatch.setenv(
            "OMNIGENT_HOST_LAUNCH_ALLOWLIST", f"server1={_SA_IDENTITY},server2-no-equals-sign"
        )
        assert resolve_host_launch_allowlist() == frozenset()

    def test_empty_host_id_fails_closed_to_empty_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIGENT_HOST_LAUNCH_ALLOWLIST", f"={_SA_IDENTITY}")
        assert resolve_host_launch_allowlist() == frozenset()

    def test_empty_identity_fails_closed_to_empty_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIGENT_HOST_LAUNCH_ALLOWLIST", "server1=")
        assert resolve_host_launch_allowlist() == frozenset()


# ── resolve_host_launch ──────────────────────────────────────────────


class TestResolveHostLaunch:
    def test_host_offline_409(self) -> None:
        host = _FakeHost(host_id="host_1", user_id="alice", status="offline")
        store = _FakeHostStore(hosts={"host_1": host})
        registry = _FakeHostRegistry()  # empty = no connections
        conv_store = _FakeConversationStore()
        with pytest.raises(OmnigentError) as exc_info:
            resolve_host_launch(
                user_id="alice",
                host_id="host_1",
                session_id="s1",
                host_store=store,
                host_registry=registry,
                conversation_store=conv_store,
                permission_store=None,
            )
        assert exc_info.value.code == ErrorCode.CONFLICT

    def test_missing_session_404(self) -> None:
        host = _FakeHost(host_id="host_1", user_id="alice")
        conn = object()
        store = _FakeHostStore(hosts={"host_1": host})
        registry = _FakeHostRegistry(conns={"host_1": conn})
        conv_store = _FakeConversationStore()  # empty
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_launch(
                user_id="alice",
                host_id="host_1",
                session_id="s1",
                host_store=store,
                host_registry=registry,
                conversation_store=conv_store,
                permission_store=None,
            )
        assert exc_info.value.status_code == 404

    def test_success_no_auth(self) -> None:
        host = _FakeHost(host_id="host_1", user_id="alice")
        conn = object()
        conv = Conversation(
            id="s1",
            created_at=1,
            updated_at=1,
            root_conversation_id="s1",
            agent_id="ag_1",
        )
        store = _FakeHostStore(hosts={"host_1": host})
        registry = _FakeHostRegistry(conns={"host_1": conn})
        conv_store = _FakeConversationStore(convs={"s1": conv})
        result = resolve_host_launch(
            user_id=None,
            host_id="host_1",
            session_id="s1",
            host_store=store,
            host_registry=registry,
            conversation_store=conv_store,
            permission_store=None,
        )
        assert result.host.host_id == "host_1"
        assert result.conv.id == "s1"
