"""Tests for :mod:`omnigent.git_hosts.url`."""

from __future__ import annotations

import pytest

from omnigent.git_hosts.url import managed_repo_path, managed_repo_path_allows, split_host


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


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://git.acme.com/team/proj.git", "/team/proj"),
        ("https://git.acme.com/team/proj.git/", "/team/proj"),
        ("https://git.acme.com/team/proj", "/team/proj"),
        ("https://git.acme.com", ""),
    ],
)
def test_managed_repo_path_derives_credential_scope(url: str, expected: str) -> None:
    assert managed_repo_path(url) == expected


def test_managed_repo_path_allows_vectors() -> None:
    base = "/team/proj"
    # Legit git paths + exact-prefix roots -> attached.
    assert managed_repo_path_allows("/team/proj/info/refs", base)
    assert managed_repo_path_allows("/team/proj.git/git-receive-pack", base)
    assert managed_repo_path_allows("/team/proj/git-receive-pack?x=1", base)  # query stripped
    assert managed_repo_path_allows("/team/proj", base)  # exact bare root
    assert managed_repo_path_allows("/team/proj.git", base)  # exact .git root
    # Escape vectors the allowlist rejects in one rule.
    assert not managed_repo_path_allows("/team/proj/../secret/info/refs", base)  # literal ..
    assert not managed_repo_path_allows("/team/proj/%2e%2e/secret", base)  # single-encoded
    assert not managed_repo_path_allows("/team/proj/%252e%252e/other", base)  # double-encoded
    assert not managed_repo_path_allows("/team/proj/..\\other", base)  # literal backslash
    assert not managed_repo_path_allows("/team/proj/..;x/other", base)  # matrix param
    assert not managed_repo_path_allows("/team/proj/%2fother", base)  # any percent-encoding
    assert not managed_repo_path_allows("/team/proj/a\\b", base)  # any backslash
    # Prefix-not-boundary and cross-repo.
    assert not managed_repo_path_allows("/team/project/info/refs", base)  # prefix, not boundary
    assert not managed_repo_path_allows("/team/proj2/info/refs", base)  # sibling sharing a prefix
    assert not managed_repo_path_allows("/other/repo.git/info/refs", base)
    # Trailing-dot / case variants decline (fail-closed, not a wrong-repo match).
    assert not managed_repo_path_allows("/team/proj./info", base)
    assert not managed_repo_path_allows("/team/PROJ/info", base)
    # An empty repo prefix must never blanket-match every path.
    assert not managed_repo_path_allows("/anything/at/all", "")


def test_managed_repo_path_predicate_is_shared_by_both_callers() -> None:
    from omnigent.inner.egress import proxy as egress_proxy
    from omnigent.server.routes import sessions

    assert egress_proxy.managed_repo_path_allows is managed_repo_path_allows
    assert sessions.managed_repo_path_allows is managed_repo_path_allows
