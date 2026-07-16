"""Tests for :func:`omnigent.git_hosts.url.split_host`."""

from __future__ import annotations

import pytest

from omnigent.git_hosts.url import split_host


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/org/repo", "github.com"),
        ("https://GitHub.com/org/repo.git", "github.com"),
        ("https://git.acme.com/team/proj#main", "git.acme.com"),
        ("git@git.acme.com:team/proj.git", "git.acme.com"),
    ],
)
def test_split_host_extracts_canonical_lowercase_host(url: str, expected: str) -> None:
    assert split_host(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://user@git.acme.com/team/proj",
        "https://user:tok@git.acme.com/team/proj",
        "ftp://git.acme.com/team/proj",
        "team/proj",
        "https:///team/proj",
        "git@user@evil.com:team/proj.git",
        "git@user:pass@evil.com:team/proj.git",
        "https://git.acme.com:8443/team/proj",
        "https://[::1]:8443/team/proj",
    ],
)
def test_split_host_rejects_unsupported_or_userinfo(url: str) -> None:
    with pytest.raises(ValueError):
        split_host(url)
