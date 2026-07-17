"""Tests for :mod:`omnigent.git_hosts.base`."""

from __future__ import annotations

import dataclasses

import pytest

import omnigent.git_hosts as git_hosts
from omnigent.git_hosts.base import (
    CloneAuthBinding,
    ClonePlan,
    HostConfig,
)


def test_provider_spec_is_a_frozen_value_type() -> None:
    spec = git_hosts.ProviderSpec(
        name="stub",
        clone_username="token-user",
        api_base_template="https://{host}/api/v1",
    )
    assert spec.default_api_base("stub.example.com") == "https://stub.example.com/api/v1"
    assert spec.clone_binding() == CloneAuthBinding(scheme="basic", username="token-user")
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "other"


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
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.id = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.provider = "x"
