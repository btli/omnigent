"""Tests for typed project defaults and host-specific resolution."""

from __future__ import annotations

import pytest

from omnigent.errors import ProjectInputError
from omnigent.projects.defaults import ProjectDefaultsBundle
from omnigent.projects.resolver import resolve_project_defaults


def test_resolver_applies_field_precedence_and_explicit_null_clears() -> None:
    resolved = resolve_project_defaults(
        server_defaults=ProjectDefaultsBundle(
            host_type="external",
            workspace="/server",
            harness="server-harness",
            model="server-model",
            reasoning_effort="low",
        ),
        project_defaults={
            "workspace": "/project",
            "harness": "project-harness",
            "model": None,
            "reasoning_effort": "medium",
        },
        defaults_schema_version=1,
        session_overrides=ProjectDefaultsBundle(
            harness="session-harness",
            reasoning_effort=None,
        ),
    )

    assert resolved.workspace == "/project"
    assert resolved.harness_override == "session-harness"
    assert resolved.model_override is None
    assert resolved.reasoning_effort is None


def test_resolver_maps_managed_repo_and_branch_without_git() -> None:
    resolved = resolve_project_defaults(
        server_defaults=ProjectDefaultsBundle(host_type="external"),
        project_defaults={
            "host_type": "managed",
            "repo_url": "https://github.com/acme/widgets.git",
            "default_branch": "release",
        },
        defaults_schema_version=1,
        session_overrides=ProjectDefaultsBundle(),
    )

    assert resolved.host_type == "managed"
    assert resolved.host_id is None
    assert resolved.workspace == "https://github.com/acme/widgets.git#release"
    assert resolved.git is None


def test_resolver_managed_repo_without_branch_uses_bare_workspace() -> None:
    resolved = resolve_project_defaults(
        server_defaults=ProjectDefaultsBundle(host_type="external"),
        project_defaults={
            "host_type": "managed",
            "repo_url": "https://github.com/acme/widgets.git",
            "default_branch": None,
        },
        defaults_schema_version=1,
        session_overrides=ProjectDefaultsBundle(),
    )

    assert resolved.workspace == "https://github.com/acme/widgets.git"
    assert resolved.git is None


def test_resolver_managed_explicit_project_workspace_wins_over_repo_url() -> None:
    resolved = resolve_project_defaults(
        server_defaults=ProjectDefaultsBundle(host_type="external"),
        project_defaults={
            "host_type": "managed",
            "repo_url": "https://github.com/acme/widgets.git",
            "default_branch": "release",
            "workspace": "https://github.com/acme/explicit.git#main",
        },
        defaults_schema_version=1,
        session_overrides=ProjectDefaultsBundle(),
    )

    assert resolved.workspace == "https://github.com/acme/explicit.git#main"


def test_resolver_maps_external_base_branch_and_mints_branch_per_session() -> None:
    minted = iter(("omnigent/session-one", "omnigent/session-two"))
    arguments = {
        "server_defaults": ProjectDefaultsBundle(host_type="external"),
        "project_defaults": {
            "host_type": "external",
            "host_id": "host_pinned",
            "workspace": "/srv/widgets",
            "default_branch": "main",
        },
        "defaults_schema_version": 1,
        "session_overrides": ProjectDefaultsBundle(),
        "branch_name_factory": lambda: next(minted),
    }

    first = resolve_project_defaults(**arguments)
    second = resolve_project_defaults(**arguments)

    assert first.host_type == "external"
    assert first.host_id == "host_pinned"
    assert first.workspace == "/srv/widgets"
    assert first.git is not None
    assert first.git.base_branch == "main"
    assert first.git.branch_name == "omnigent/session-one"
    assert second.git is not None
    assert second.git.branch_name == "omnigent/session-two"


def test_resolver_external_default_branch_requires_pinned_host() -> None:
    with pytest.raises(ProjectInputError, match="requires a pinned host_id"):
        resolve_project_defaults(
            server_defaults=ProjectDefaultsBundle(host_type="external"),
            project_defaults={"default_branch": "main"},
            defaults_schema_version=1,
            session_overrides=ProjectDefaultsBundle(),
        )


def test_resolver_external_without_default_branch_has_no_git_options() -> None:
    resolved = resolve_project_defaults(
        server_defaults=ProjectDefaultsBundle(host_type="external"),
        project_defaults={"host_id": None, "default_branch": None},
        defaults_schema_version=1,
        session_overrides=ProjectDefaultsBundle(),
    )

    assert resolved.git is None


def test_resolver_revalidates_schema_version_and_managed_host_id() -> None:
    with pytest.raises(ProjectInputError, match="Unsupported defaults_schema_version"):
        resolve_project_defaults(
            server_defaults=ProjectDefaultsBundle(host_type="external"),
            project_defaults={},
            defaults_schema_version=2,
            session_overrides=ProjectDefaultsBundle(),
        )

    with pytest.raises(ProjectInputError, match="prohibit host_id"):
        resolve_project_defaults(
            server_defaults=ProjectDefaultsBundle(host_type="external"),
            project_defaults={"host_type": "managed", "host_id": "host_bad"},
            defaults_schema_version=1,
            session_overrides=ProjectDefaultsBundle(),
        )


@pytest.mark.parametrize(
    "project_defaults",
    [
        {"host_type": "managed", "repo_url": "github.com/acme/widgets"},
        {
            "host_type": "managed",
            "repo_url": "https://github.com/acme/widgets.git",
            "default_branch": "a" * 40,
        },
    ],
)
def test_resolver_rejects_invalid_managed_repo_workspace(
    project_defaults: dict[str, str],
) -> None:
    with pytest.raises(ProjectInputError, match="Invalid project defaults"):
        resolve_project_defaults(
            server_defaults=ProjectDefaultsBundle(host_type="external"),
            project_defaults=project_defaults,
            defaults_schema_version=1,
            session_overrides=ProjectDefaultsBundle(),
        )
