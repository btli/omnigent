"""Unit tests for the ``omnigent project`` CLI."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import respx
from click.testing import CliRunner

from omnigent.cli import cli

_BASE = "http://localhost:6767"
_PROJECT_ID = "proj_abc123"
_PROJECT = {
    "id": _PROJECT_ID,
    "owner_principal_id": "alice@example.com",
    "name": "Omnigent",
    "description": "Agent workspace",
    "normalized_name": "omnigent",
    "normalized_name_checksum": "checksum",
    "storage_key": "storage-key",
    "defaults_json": {"model": "gpt-5", "workspace": "/workspace"},
    "defaults_schema_version": 1,
    "row_version": 3,
    "created_at": 1_700_000_000,
    "updated_at": 1_700_000_100,
    "archived_at": None,
}


def _patch_server(base_url: str = _BASE) -> Any:
    """Patch the CLI so it uses *base_url* without starting a server."""
    return patch("omnigent.cli._resolve_attach_server", return_value=base_url)


def _json_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


@respx.mock
def test_project_list_table_and_json() -> None:
    route = respx.get(f"{_BASE}/v1/projects").mock(
        return_value=httpx.Response(200, json=[_PROJECT])
    )
    runner = CliRunner()

    with _patch_server():
        table_result = runner.invoke(cli, ["project", "list", "--archived-members"])
        json_result = runner.invoke(cli, ["project", "list", "--json"])

    assert table_result.exit_code == 0, table_result.output
    assert "ID" in table_result.output
    assert "NAME" in table_result.output
    assert "UPDATED" in table_result.output
    assert _PROJECT_ID in table_result.output
    assert "Omnigent" in table_result.output
    assert route.calls[0].request.url.params["archived_members"] == "true"
    assert json.loads(json_result.output) == [_PROJECT]


@respx.mock
def test_project_create_with_description_and_defaults() -> None:
    route = respx.post(f"{_BASE}/v1/projects").mock(
        return_value=httpx.Response(201, json=_PROJECT)
    )

    with _patch_server():
        result = CliRunner().invoke(
            cli,
            [
                "project",
                "create",
                "Omnigent",
                "--description",
                "Agent workspace",
                "--set",
                "repo_url=https://github.com/acme/omnigent.git",
                "--set",
                "model=gpt-5",
            ],
        )

    assert result.exit_code == 0, result.output
    assert _PROJECT_ID in result.output
    assert _json_body(route.calls.last.request) == {
        "name": "Omnigent",
        "description": "Agent workspace",
        "defaults_json": {
            "repo_url": "https://github.com/acme/omnigent.git",
            "model": "gpt-5",
        },
    }


def test_project_create_rejects_unknown_default_key() -> None:
    with _patch_server():
        result = CliRunner().invoke(
            cli,
            ["project", "create", "Omnigent", "--set", "credential_ref=secret"],
        )

    assert result.exit_code == 1
    assert "Unknown project default key 'credential_ref'" in result.output
    assert "repo_url" in result.output


@respx.mock
def test_project_show_full_row_and_json() -> None:
    respx.get(f"{_BASE}/v1/projects/{_PROJECT_ID}").mock(
        return_value=httpx.Response(200, json=_PROJECT, headers={"ETag": '"3"'})
    )
    runner = CliRunner()

    with _patch_server():
        show_result = runner.invoke(cli, ["project", "show", _PROJECT_ID])
        json_result = runner.invoke(cli, ["project", "show", _PROJECT_ID, "--json"])

    assert show_result.exit_code == 0, show_result.output
    assert "defaults_json" in show_result.output
    assert "gpt-5" in show_result.output
    assert "/workspace" in show_result.output
    assert json.loads(json_result.output) == _PROJECT


@respx.mock
def test_project_rename_gets_etag_then_mutates() -> None:
    get_route = respx.get(f"{_BASE}/v1/projects/{_PROJECT_ID}").mock(
        return_value=httpx.Response(200, json=_PROJECT, headers={"ETag": '"3"'})
    )
    rename_route = respx.post(f"{_BASE}/v1/projects/{_PROJECT_ID}/rename").mock(
        return_value=httpx.Response(200, json={**_PROJECT, "name": "New name"})
    )

    with _patch_server():
        result = CliRunner().invoke(cli, ["project", "rename", _PROJECT_ID, "New name"])

    assert result.exit_code == 0, result.output
    assert get_route.called
    assert rename_route.calls.last.request.headers["If-Match"] == '"3"'
    assert _json_body(rename_route.calls.last.request) == {"name": "New name"}


@respx.mock
def test_project_update_sets_and_unsets_defaults() -> None:
    respx.get(f"{_BASE}/v1/projects/{_PROJECT_ID}").mock(
        return_value=httpx.Response(200, json=_PROJECT, headers={"ETag": '"3"'})
    )
    update_route = respx.patch(f"{_BASE}/v1/projects/{_PROJECT_ID}").mock(
        return_value=httpx.Response(200, json={**_PROJECT, "row_version": 4})
    )

    with _patch_server():
        result = CliRunner().invoke(
            cli,
            [
                "project",
                "update",
                _PROJECT_ID,
                "--description",
                "Updated",
                "--set",
                "model=gpt-5.1",
                "--set",
                "harness=claude",
                "--unset",
                "workspace",
            ],
        )

    assert result.exit_code == 0, result.output
    request = update_route.calls.last.request
    assert request.headers["If-Match"] == '"3"'
    assert _json_body(request) == {
        "description": "Updated",
        "defaults_json": {
            "model": "gpt-5.1",
            "workspace": None,
            "harness": "claude",
        },
    }


@respx.mock
def test_project_archive_includes_sessions() -> None:
    respx.get(f"{_BASE}/v1/projects/{_PROJECT_ID}").mock(
        return_value=httpx.Response(200, json=_PROJECT, headers={"ETag": '"3"'})
    )
    route = respx.post(f"{_BASE}/v1/projects/{_PROJECT_ID}/archive").mock(
        return_value=httpx.Response(
            200,
            json={**_PROJECT, "archived_at": 1_700_000_200, "archived_sessions": 2},
        )
    )

    with _patch_server():
        result = CliRunner().invoke(cli, ["project", "archive", _PROJECT_ID, "--include-sessions"])

    assert result.exit_code == 0, result.output
    assert route.calls.last.request.url.params["include_sessions"] == "true"
    assert route.calls.last.request.headers["If-Match"] == '"3"'
    assert "2 session" in result.output


@respx.mock
def test_project_restore() -> None:
    archived = {**_PROJECT, "archived_at": 1_700_000_200}
    respx.get(f"{_BASE}/v1/projects/{_PROJECT_ID}").mock(
        return_value=httpx.Response(200, json=archived, headers={"ETag": '"4"'})
    )
    route = respx.post(f"{_BASE}/v1/projects/{_PROJECT_ID}/restore").mock(
        return_value=httpx.Response(200, json=_PROJECT)
    )

    with _patch_server():
        result = CliRunner().invoke(cli, ["project", "restore", _PROJECT_ID])

    assert result.exit_code == 0, result.output
    assert route.calls.last.request.headers["If-Match"] == '"4"'


@respx.mock
def test_project_transfer() -> None:
    respx.get(f"{_BASE}/v1/projects/{_PROJECT_ID}").mock(
        return_value=httpx.Response(200, json=_PROJECT, headers={"ETag": '"3"'})
    )
    route = respx.post(f"{_BASE}/v1/projects/{_PROJECT_ID}/transfer").mock(
        return_value=httpx.Response(
            200, json={**_PROJECT, "owner_principal_id": "bob@example.com"}
        )
    )

    with _patch_server():
        result = CliRunner().invoke(cli, ["project", "transfer", _PROJECT_ID, "bob@example.com"])

    assert result.exit_code == 0, result.output
    assert route.calls.last.request.headers["If-Match"] == '"3"'
    assert _json_body(route.calls.last.request) == {"new_owner_principal_id": "bob@example.com"}


@respx.mock
def test_project_stale_etag_has_clear_error() -> None:
    respx.get(f"{_BASE}/v1/projects/{_PROJECT_ID}").mock(
        return_value=httpx.Response(200, json=_PROJECT, headers={"ETag": '"3"'})
    )
    respx.post(f"{_BASE}/v1/projects/{_PROJECT_ID}/rename").mock(
        return_value=httpx.Response(
            412,
            json={"error": {"code": "precondition_failed", "message": "stale"}},
        )
    )

    with _patch_server():
        result = CliRunner().invoke(cli, ["project", "rename", _PROJECT_ID, "New"])

    assert result.exit_code == 1
    assert "project changed on the server — retry" in result.output
    assert "stale" not in result.output


@respx.mock
def test_project_validation_error_is_rendered_cleanly() -> None:
    respx.post(f"{_BASE}/v1/projects").mock(
        return_value=httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "type": "value_error",
                        "loc": ["body", "defaults_json", "repo_url"],
                        "msg": "Value error, must be an absolute HTTPS URL",
                        "input": "github.com/acme/repo",
                    }
                ]
            },
        )
    )

    with _patch_server():
        result = CliRunner().invoke(
            cli,
            [
                "project",
                "create",
                "Bad",
                "--set",
                "host_type=managed",
                "--set",
                "repo_url=github.com/acme/repo",
            ],
        )

    assert result.exit_code == 1
    assert "repo_url: Value error, must be an absolute HTTPS URL" in result.output
    assert "{'type':" not in result.output
