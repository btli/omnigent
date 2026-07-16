"""Tests for :func:`omnigent.git_hosts.config.load_git_hosts`."""

from __future__ import annotations

import pytest

from omnigent.git_hosts.base import HostConfig
from omnigent.git_hosts.config import load_git_hosts


def test_none_and_empty_yield_no_hosts() -> None:
    assert load_git_hosts(None) == ()
    assert load_git_hosts([]) == ()


def test_parses_a_host_and_defaults_api_base_from_provider() -> None:
    hosts = load_git_hosts(
        [
            {
                "id": "acme-forgejo",
                "provider": "forgejo",
                "web_host": "Git.Acme.com",
                "credential_source": "env:ACME_FORGEJO_TOKEN",
            }
        ]
    )
    assert hosts == (
        HostConfig(
            id="acme-forgejo",
            provider="forgejo",
            web_host="git.acme.com",
            api_base="https://git.acme.com/api/v1",
            credential_source="env:ACME_FORGEJO_TOKEN",
        ),
    )


def test_explicit_api_base_and_optional_fields_are_kept() -> None:
    (host,) = load_git_hosts(
        [
            {
                "id": "ghe",
                "provider": "ghe",
                "web_host": "ghe.acme.com",
                "credential_source": "env:GHE_TOKEN",
                "api_base": "https://ghe.acme.com/api/v3",
                "ssh_host": "ssh.ghe.acme.com",
                "ssh_port": 2222,
                "ca_bundle": "/etc/ssl/acme-ca.pem",
            }
        ]
    )
    assert host.api_base == "https://ghe.acme.com/api/v3"
    assert host.ssh_host == "ssh.ghe.acme.com"
    assert host.ssh_port == 2222
    assert host.ca_bundle == "/etc/ssl/acme-ca.pem"


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ({"not": "a list"}, "must be a list"),
        ([{"provider": "forgejo", "web_host": "x", "credential_source": "env:X"}], "id"),
        ([{"id": "a", "web_host": "x", "credential_source": "env:X"}], "provider"),
        (
            [{"id": "a", "provider": "svn", "web_host": "x", "credential_source": "env:X"}],
            "unknown git host provider",
        ),
        (
            [
                {
                    "id": "a",
                    "provider": "forgejo",
                    "web_host": "dup.com",
                    "credential_source": "env:A",
                },
                {
                    "id": "b",
                    "provider": "gitea",
                    "web_host": "Dup.com",
                    "credential_source": "env:B",
                },
            ],
            "duplicate",
        ),
        (
            [
                {
                    "id": "a",
                    "provider": "forgejo",
                    "web_host": "x",
                    "credential_source": "env:X",
                    "ssh_port": True,
                }
            ],
            "ssh_port",
        ),
        (
            [
                {
                    "id": "a",
                    "provider": "forgejo",
                    "web_host": "x",
                    "credential_source": "env:X",
                    "ssh_host": 2222,
                }
            ],
            "ssh_host",
        ),
        (
            [
                {
                    "id": "a",
                    "provider": "forgejo",
                    "web_host": "x",
                    "credential_source": "env:X",
                    "ca_bundle": ["a"],
                }
            ],
            "ca_bundle",
        ),
        (
            [
                {
                    "id": "a",
                    "provider": "forgejo",
                    "web_host": "git.acme.com:8443",
                    "credential_source": "env:X",
                }
            ],
            "web_host",
        ),
        (
            [
                {
                    "id": "a",
                    "provider": "forgejo",
                    "web_host": "git.acme.com/evil",
                    "credential_source": "env:X",
                }
            ],
            "web_host",
        ),
        (
            [
                {
                    "id": "a",
                    "provider": "forgejo",
                    "web_host": "u@git.acme.com",
                    "credential_source": "env:X",
                }
            ],
            "web_host",
        ),
        (
            [
                {
                    "id": "a",
                    "provider": "forgejo",
                    "web_host": "git.acme.com",
                    "credential_source": "env:X",
                    "ssh-port": 2222,
                }
            ],
            "unknown keys",
        ),
    ],
)
def test_fail_closed_on_malformed_config(raw: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_git_hosts(raw)
