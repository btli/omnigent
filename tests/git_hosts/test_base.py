"""Tests for :mod:`omnigent.git_hosts.base`."""

from __future__ import annotations

from omnigent.git_hosts.base import (
    CloneAuthBinding,
    ClonePlan,
    GitHostProvider,
    HostConfig,
)


class _StubProvider(GitHostProvider):
    provider = "stub"
    default_clone_username = "x-access-token"

    def matches(self, host: str) -> bool:
        return host == "stub.example.com"

    def default_api_base(self, web_host: str) -> str:
        return f"https://{web_host}/api/v1"


def test_clone_binding_uses_default_username() -> None:
    binding = _StubProvider().clone_binding()
    assert binding == CloneAuthBinding(scheme="basic", username="x-access-token")


def test_normalize_repo_url_is_identity_by_default() -> None:
    url = "https://stub.example.com/org/repo"
    assert _StubProvider().normalize_repo_url(url) == url


def test_host_config_and_clone_plan_are_frozen_value_types() -> None:
    cfg = HostConfig(
        id="acme",
        provider="stub",
        web_host="stub.example.com",
        api_base="https://stub.example.com/api/v1",
        credential_source="env:ACME_TOKEN",
    )
    plan = ClonePlan(
        provider="stub",
        host_id="acme",
        canonical_host="stub.example.com",
        normalized_url="https://stub.example.com/org/repo",
        api_base=cfg.api_base,
        auth=CloneAuthBinding(scheme="basic", username="x-access-token"),
        credential_source=cfg.credential_source,
    )
    assert plan.credential_source == "env:ACME_TOKEN"
    assert plan.ssh_host is None
