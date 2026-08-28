"""Browser coverage for project-folder context-menu actions."""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Browser, Locator, Page, expect


def _create_project(page: Page, name: str) -> None:
    """Create an empty project from the Projects header action."""
    page.get_by_test_id("new-project").click()
    page.get_by_placeholder("Project name…").fill(name)
    page.get_by_test_id("new-project-confirm").click()


def _folder_header(page: Page, project: str) -> Locator:
    """Locate a project folder's collapse-toggle button."""
    return page.get_by_role("button", name=project, exact=True)


def _expanded(header: Locator) -> str:
    """Read the raw expanded state for comparison across an action."""
    value = header.get_attribute("aria-expanded")
    assert value is not None, "folder header is missing aria-expanded"
    return value


@pytest.fixture
def project_page(page: Page, seeded_session: tuple[str, str]) -> tuple[Page, str]:
    """Open a desktop sidebar containing a fresh project folder."""
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"

    page.goto(f"{base_url}/c/{session_id}")
    _create_project(page, project)
    expect(_folder_header(page, project)).to_be_visible()

    return page, project


def test_right_click_opens_project_folder_menu(project_page: tuple[Page, str]) -> None:
    """Right-click opens the shared actions without toggling the folder."""
    page, project = project_page
    header = _folder_header(page, project)
    before = _expanded(header)

    header.click(button="right")

    expect(page.get_by_test_id("rename-project")).to_be_visible()
    expect(page.get_by_test_id("project-settings")).to_be_visible()
    expect(page.get_by_test_id("delete-project")).to_be_visible()
    expect(header).to_have_attribute("aria-expanded", before)

    page.get_by_test_id("rename-project").click()
    expect(page.get_by_test_id("rename-project-confirm")).to_be_visible()


def test_left_click_still_toggles_the_folder(project_page: tuple[Page, str]) -> None:
    """Plain left-click still expands and collapses the folder."""
    page, project = project_page
    header = _folder_header(page, project)
    before = _expanded(header)
    flipped = "false" if before == "true" else "true"

    expect(header).to_have_attribute("data-slot", "context-menu-trigger")
    header.click()
    expect(header).to_have_attribute("aria-expanded", flipped)
    header.click()
    expect(header).to_have_attribute("aria-expanded", before)


def test_keyboard_opens_project_folder_menu(project_page: tuple[Page, str]) -> None:
    """The Menu key opens the folder actions from the focused header."""
    page, project = project_page
    header = _folder_header(page, project)
    before = _expanded(header)

    header.focus()
    header.press("ContextMenu")

    expect(page.get_by_test_id("rename-project")).to_be_visible()
    expect(header).to_have_attribute("aria-expanded", before)


def test_touch_long_press_opens_project_folder_menu(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A stationary touch hold opens the folder actions."""
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    context = browser.new_context(
        has_touch=True,
        viewport={"width": 1280, "height": 720},
    )
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}")
        _create_project(page, project)
        header = _folder_header(page, project)
        expect(header).to_be_visible()

        bounds = header.bounding_box()
        assert bounds is not None, "folder header has no touch target bounds"
        point = {
            "x": bounds["x"] + bounds["width"] / 2,
            "y": bounds["y"] + bounds["height"] / 2,
        }
        cdp = context.new_cdp_session(page)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
        try:
            page.wait_for_timeout(750)
            expect(page.get_by_test_id("rename-project")).to_be_visible()
        finally:
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    finally:
        context.close()
