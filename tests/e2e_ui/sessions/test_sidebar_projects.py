"""Browser e2e coverage for first-class sidebar projects."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect


@dataclass
class ProjectApi:
    """Seed projects and sessions through their public REST APIs."""

    base_url: str
    agent_id: str
    session_ids: list[str] = field(default_factory=list)

    def create_project(
        self,
        name: str,
        defaults: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name}
        if defaults is not None:
            payload["defaults_json"] = defaults
        response = httpx.post(
            f"{self.base_url}/v1/projects",
            json=payload,
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()

    def create_session(self, project_id: str, title: str) -> str:
        response = httpx.post(
            f"{self.base_url}/v1/sessions",
            json={
                "agent_id": self.agent_id,
                "project_id": project_id,
                "title": title,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        session_id = response.json()["id"]
        self.session_ids.append(session_id)
        return session_id


@pytest.fixture
def project_api(seeded_session: tuple[str, str]) -> Iterator[ProjectApi]:
    """Reuse the seeded session's agent for project-bound JSON creates."""
    base_url, source_session_id = seeded_session
    response = httpx.get(
        f"{base_url}/v1/sessions/{source_session_id}/agent",
        timeout=10.0,
    )
    response.raise_for_status()
    api = ProjectApi(base_url=base_url, agent_id=response.json()["id"])
    try:
        yield api
    finally:
        for session_id in reversed(api.session_ids):
            httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)


def _unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


def _section(page: Page, project_name: str) -> Locator:
    return (
        page.locator("section")
        .filter(has=page.get_by_role("button", name=project_name, exact=True))
        .last
    )


def _session_href(session_id: str) -> str:
    return f'a[href="/c/{session_id}"]'


def _session_link(section: Locator, session_id: str) -> Locator:
    return section.locator(_session_href(session_id))


def _row(page: Page, section: Locator, session_id: str) -> Locator:
    return section.locator("li").filter(has=page.locator(_session_href(session_id)))


def _open_project_kebab(page: Page, project_name: str) -> None:
    section = _section(page, project_name)
    section.hover()
    page.get_by_role("button", name=f"Project actions for {project_name}").click()


def _expect_project_menu(page: Page, project_id: str) -> None:
    new_session = page.get_by_role("menuitem", name="New session", exact=True)
    expect(new_session).to_be_visible()
    expect(new_session).to_have_attribute("href", f"/?project_id={project_id}")
    for name in ("Project settings", "Rename project", "Delete project"):
        expect(page.get_by_role("menuitem", name=name, exact=True)).to_be_visible()


def _open_project_settings(page: Page, project_name: str) -> Locator:
    _open_project_kebab(page, project_name)
    page.get_by_role("menuitem", name="Project settings", exact=True).click()
    dialog = page.get_by_role("dialog", name="Project settings")
    expect(dialog).to_be_visible()
    return dialog


def test_api_seeded_project_shows_sessions_and_new_session_link(
    page: Page,
    project_api: ProjectApi,
) -> None:
    project_name = _unique("Project folder")
    title = _unique("Filed session")
    project = project_api.create_project(project_name)
    session_id = project_api.create_session(project["id"], title)

    page.goto(f"{project_api.base_url}/c/{session_id}")

    header = page.get_by_role("button", name=project_name, exact=True)
    expect(header).to_be_visible()
    expect(header).to_have_attribute("aria-expanded", "true")
    expect(_session_link(_section(page, project_name), session_id)).to_contain_text(title)

    _section(page, project_name).hover()
    new_session = page.get_by_role("link", name=f"New session in {project_name}")
    expect(new_session).to_be_visible()
    expect(new_session).to_have_attribute("href", f"/?project_id={project['id']}")


def test_project_right_click_and_kebab_offer_the_same_actions(
    page: Page,
    project_api: ProjectApi,
) -> None:
    project_name = _unique("Project actions")
    project = project_api.create_project(project_name)
    session_id = project_api.create_session(project["id"], _unique("Action session"))
    page.goto(f"{project_api.base_url}/c/{session_id}")

    page.get_by_role("button", name=project_name, exact=True).click(button="right")
    _expect_project_menu(page, project["id"])
    page.keyboard.press("Escape")

    _open_project_kebab(page, project_name)
    _expect_project_menu(page, project["id"])


def test_inline_rename_commits_and_keeps_collision_in_edit(
    page: Page,
    project_api: ProjectApi,
) -> None:
    original_name = _unique("Rename source")
    existing_name = _unique("Rename collision")
    renamed_name = _unique("Rename committed")
    project = project_api.create_project(original_name)
    project_api.create_project(existing_name)
    session_id = project_api.create_session(project["id"], _unique("Rename session"))
    page.goto(f"{project_api.base_url}/c/{session_id}")

    page.get_by_role("button", name=original_name, exact=True).click(button="right")
    page.get_by_role("menuitem", name="Rename project", exact=True).click()
    rename_input = page.get_by_role("textbox", name="Rename project")
    expect(rename_input).to_be_visible()
    rename_input.fill(renamed_name)
    rename_input.press("Enter")

    renamed_header = page.get_by_role("button", name=renamed_name, exact=True)
    expect(renamed_header).to_be_visible()
    expect(renamed_header).to_have_attribute("aria-expanded", "true")

    renamed_header.click(button="right")
    page.get_by_role("menuitem", name="Rename project", exact=True).click()
    rename_input = page.get_by_role("textbox", name="Rename project")
    rename_input.fill(existing_name)
    rename_input.press("Enter")

    alert = page.get_by_role("alert")
    expect(alert).to_contain_text("already exists")
    expect(rename_input).to_be_visible()
    expect(rename_input).to_have_value(existing_name)
    expect(rename_input).to_have_attribute("aria-invalid", "true")
    editing_section = page.locator("section").filter(has=rename_input)
    expect(_session_link(editing_section, session_id)).to_be_visible()


def test_move_session_between_projects_from_row_kebab(
    page: Page,
    project_api: ProjectApi,
) -> None:
    source_name = _unique("Move source")
    target_name = _unique("Move target")
    source = project_api.create_project(source_name)
    project_api.create_project(target_name)
    session_id = project_api.create_session(source["id"], _unique("Moved session"))
    page.goto(f"{project_api.base_url}/c/{session_id}")

    source_row = _row(page, _section(page, source_name), session_id)
    expect(source_row).to_be_visible()
    source_row.hover()
    source_row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("move-to-project").click()
    page.get_by_role("menuitem", name=target_name, exact=True).click()

    expect(_session_link(_section(page, source_name), session_id)).to_have_count(0)
    target_header = page.get_by_role("button", name=target_name, exact=True)
    expect(target_header).to_have_attribute("aria-expanded", "true")
    expect(_session_link(_section(page, target_name), session_id)).to_be_visible()


def test_archived_filter_lists_and_filters_by_project(
    page: Page,
    project_api: ProjectApi,
) -> None:
    project_name = _unique("Archived project")
    title = _unique("Archived filed session")
    project = project_api.create_project(project_name)
    session_id = project_api.create_session(project["id"], title)
    page.goto(f"{project_api.base_url}/c/{session_id}")

    row = _row(page, _section(page, project_name), session_id)
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    with page.expect_response(
        lambda response: (
            response.url.endswith(f"/v1/sessions/{session_id}")
            and response.request.method == "PATCH"
        )
    ) as archive_response:
        page.get_by_test_id("archive-conversation").click()
    assert archive_response.value.ok

    page.get_by_test_id("settings-button").click()
    page.get_by_test_id("settings-nav-archived").click()
    expect(page.get_by_role("heading", name="Archived sessions", exact=True)).to_be_visible()

    project_filter = page.get_by_role("combobox", name="Filter archived sessions by project")
    expect(project_filter).to_be_visible()
    project_filter.click()
    options = page.get_by_role("option")
    expect(options).to_have_text(["All projects", project_name])
    page.get_by_role("option", name=project_name, exact=True).click()

    archived_rows = page.get_by_test_id("archived-row")
    expect(archived_rows).to_have_count(1)
    expect(archived_rows).to_contain_text(title)


def test_project_settings_resolved_defaults_save_and_reset(
    page: Page,
    project_api: ProjectApi,
) -> None:
    project_name = _unique("Defaults editor")
    project = project_api.create_project(project_name, defaults={"model": None})
    session_id = project_api.create_session(project["id"], _unique("Defaults session"))
    page.goto(f"{project_api.base_url}/c/{session_id}")

    dialog = _open_project_settings(page, project_name)
    host_type = dialog.get_by_test_id("project-default-host_type-control")
    expect(host_type).to_have_value("external")
    expect(dialog.get_by_test_id("project-default-host_type-provenance")).to_have_attribute(
        "data-provenance", "inherited"
    )
    expect(dialog.get_by_test_id("project-default-host_id-field")).to_be_visible()

    host_type.select_option("managed")
    expect(dialog.get_by_test_id("project-default-host_type-provenance")).to_have_attribute(
        "data-provenance", "overridden"
    )
    expect(dialog.get_by_test_id("project-default-host_id-field")).to_have_count(0)
    repo = dialog.get_by_test_id("project-default-repo_url-control")
    branch = dialog.get_by_test_id("project-default-default_branch-control")
    expect(repo).to_be_visible()
    expect(branch).to_be_visible()

    repo_url = "https://github.com/example/project-defaults.git"
    repo.fill(repo_url)
    branch.fill("main")
    with page.expect_response(
        lambda response: (
            response.url.endswith(f"/v1/projects/{project['id']}")
            and response.request.method == "PATCH"
        )
    ) as save_response:
        dialog.get_by_role("button", name="Save settings", exact=True).click()
    assert save_response.value.ok
    expect(dialog).to_have_count(0)

    project_response = httpx.get(
        f"{project_api.base_url}/v1/projects/{project['id']}",
        timeout=10.0,
    )
    project_response.raise_for_status()
    assert project_response.json()["defaults_json"] == {
        "host_type": "managed",
        "repo_url": repo_url,
        "default_branch": "main",
    }

    dialog = _open_project_settings(page, project_name)
    expect(dialog.get_by_test_id("project-default-host_type-control")).to_have_value("managed")
    expect(dialog.get_by_test_id("project-default-repo_url-provenance")).to_have_attribute(
        "data-provenance", "overridden"
    )
    expect(dialog.get_by_test_id("project-default-default_branch-provenance")).to_have_attribute(
        "data-provenance", "overridden"
    )
    dialog.get_by_test_id("project-default-default_branch-reset").click()
    with page.expect_response(
        lambda response: (
            response.url.endswith(f"/v1/projects/{project['id']}")
            and response.request.method == "PATCH"
        )
    ) as reset_response:
        dialog.get_by_role("button", name="Save settings", exact=True).click()
    assert reset_response.value.ok

    project_response = httpx.get(
        f"{project_api.base_url}/v1/projects/{project['id']}",
        timeout=10.0,
    )
    project_response.raise_for_status()
    assert project_response.json()["defaults_json"] == {
        "host_type": "managed",
        "repo_url": repo_url,
    }


def test_project_settings_remains_usable_at_360px(
    page: Page,
    project_api: ProjectApi,
) -> None:
    page.set_viewport_size({"width": 360, "height": 780})
    project_name = _unique("Narrow defaults")
    project = project_api.create_project(project_name, defaults={})
    session_id = project_api.create_session(project["id"], _unique("Narrow session"))
    page.goto(f"{project_api.base_url}/c/{session_id}")

    dialog = _open_project_settings(page, project_name)
    dialog_box = dialog.bounding_box()
    assert dialog_box is not None
    assert dialog_box["x"] >= 0
    assert dialog_box["y"] >= 0
    assert dialog_box["x"] + dialog_box["width"] <= 360
    assert dialog_box["y"] + dialog_box["height"] <= 780
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    host_field = dialog.get_by_test_id("project-default-host_type-field")
    header = host_field.locator(":scope > div").first
    assert header.evaluate("element => getComputedStyle(element).flexWrap") == "wrap"

    harness_trigger = dialog.get_by_test_id("project-default-harness-control")
    harness_trigger.click()
    menu = page.get_by_test_id("project-default-harness-options")
    expect(menu).to_be_visible()
    menu_box = menu.bounding_box()
    assert menu_box is not None
    assert menu_box["x"] >= 0
    assert menu_box["x"] + menu_box["width"] <= 360
    page.keyboard.press("Escape")

    save = dialog.get_by_role("button", name="Save settings", exact=True)
    expect(save).to_be_visible()
    save_box = save.bounding_box()
    assert save_box is not None
    assert save_box["y"] + save_box["height"] <= 780


def test_delete_project_archives_sessions_and_removes_folder(
    page: Page,
    project_api: ProjectApi,
) -> None:
    project_name = _unique("Delete project")
    project = project_api.create_project(project_name)
    session_id = project_api.create_session(project["id"], _unique("Delete project session"))
    page.goto(f"{project_api.base_url}/c/{session_id}")

    _open_project_kebab(page, project_name)
    page.get_by_role("menuitem", name="Delete project", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("all of its sessions")
    with page.expect_response(
        lambda response: (
            response.url.endswith(f"/v1/projects/{project['id']}/archive?include_sessions=true")
            and response.request.method == "POST"
        )
    ) as delete_response:
        dialog.get_by_role("button", name="Delete project", exact=True).click()
    assert delete_response.value.ok

    expect(page.get_by_role("button", name=project_name, exact=True)).to_have_count(0)
    session_response = httpx.get(
        f"{project_api.base_url}/v1/sessions/{session_id}",
        timeout=10.0,
    )
    session_response.raise_for_status()
    assert session_response.json()["archived"] is True
    project_response = httpx.get(
        f"{project_api.base_url}/v1/projects/{project['id']}",
        timeout=10.0,
    )
    project_response.raise_for_status()
    assert project_response.json()["archived_at"] is not None
