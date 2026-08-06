"""Browser e2e coverage for touch gestures on draggable session rows.

The sidebar uses dnd-kit for touch dragging and Radix for its context menu.
These tests drive Chromium through CDP so the page receives a genuine touch
sequence; synthetic pointer events do not arm dnd-kit's ``TouchSensor``.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, CDPSession, Locator, Page, expect

_MOBILE_VIEWPORT = {"width": 390, "height": 844}


@pytest.fixture
def touch_page(browser: Browser) -> Iterator[tuple[Page, CDPSession]]:
    """Page plus CDP session in a mobile browser context with touch enabled."""
    context = browser.new_context(
        has_touch=True,
        is_mobile=True,
        viewport=_MOBILE_VIEWPORT,
    )
    page = context.new_page()
    cdp = context.new_cdp_session(page)
    try:
        yield page, cdp
    finally:
        cdp.detach()
        context.close()


def _touch(cdp: CDPSession, event: str, *points: tuple[float, float]) -> None:
    """Dispatch a native touch event at ``points`` (empty for touchEnd)."""
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": event, "touchPoints": [{"x": x, "y": y} for x, y in points]},
    )


def _center(locator: Locator) -> tuple[float, float]:
    """Midpoint of ``locator``'s bounding box."""
    box = locator.bounding_box()
    assert box is not None, "element has no touchable bounding box"
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give the test session a unique, visible sidebar label."""
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    response.raise_for_status()


def _create_project(base_url: str, name: str) -> str:
    """Create an empty project for a drag target; returns its id."""
    response = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
    response.raise_for_status()
    return response.json()["id"]


def _row_link(page: Page, session_id: str) -> Locator:
    """Locate the sidebar link for ``session_id``."""
    return page.locator(f'a[href="/c/{session_id}"]')


def _section(page: Page, title: str) -> Locator:
    """Locate the sidebar section headed by ``title``."""
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def test_touch_drag_does_not_open_session_context_menu(
    touch_page: tuple[Page, CDPSession],
    seeded_session: tuple[str, str],
) -> None:
    """A still touch activates drag without opening Radix's menu over it."""
    base_url, session_id = seeded_session
    page, cdp = touch_page
    _set_title(base_url, session_id, f"e2e-touch-drag-{uuid.uuid4().hex[:8]}")

    page.goto(f"{base_url}/c/{session_id}?sidebar=open")
    link = _row_link(page, session_id)
    expect(link).to_be_visible()
    row = link.locator("xpath=ancestor::li[1]")

    _touch(cdp, "touchStart", _center(link))
    try:
        # dnd-kit's unchanged 250ms delay must activate the row first.
        expect(row).to_have_class(re.compile(r"\bopacity-40\b"), timeout=600)

        # Hold past Radix's ~700ms timer while the drag remains active.
        page.wait_for_timeout(550)
        expect(row).to_have_class(re.compile(r"\bopacity-40\b"))
        expect(page.get_by_test_id("rename-conversation")).to_have_count(0)
    finally:
        _touch(cdp, "touchEnd")


def test_vertical_touch_scroll_still_works_on_session_row(
    touch_page: tuple[Page, CDPSession],
    seeded_session: tuple[str, str],
) -> None:
    """Moving before the hold delay scrolls the sidebar instead of dragging."""
    base_url, session_id = seeded_session
    page, cdp = touch_page
    _set_title(base_url, session_id, f"e2e-touch-scroll-{uuid.uuid4().hex[:8]}")

    page.goto(f"{base_url}/c/{session_id}?sidebar=open")
    link = _row_link(page, session_id)
    expect(link).to_be_visible()
    row = link.locator("xpath=ancestor::li[1]")
    scroll_area = page.locator("aside nav")
    conversation_list = page.get_by_test_id("sidebar-conversation-list")

    # One seeded row does not naturally overflow. Extend the real list's
    # layout so Chromium has native scroll range without adding fake rows.
    conversation_list.evaluate("element => { element.style.minHeight = '1800px'; }")
    scroll_area.evaluate("element => { element.scrollTop = 0; }")
    assert scroll_area.evaluate("element => element.scrollHeight > element.clientHeight")

    x, y = _center(link)
    _touch(cdp, "touchStart", (x, y))
    for offset in (20, 45, 70, 95, 120):
        _touch(cdp, "touchMove", (x, y - offset))
        page.wait_for_timeout(20)
    _touch(cdp, "touchEnd")

    page.wait_for_function(
        "element => element.scrollTop > 20",
        arg=scroll_area.element_handle(),
        timeout=3_000,
    )
    assert "opacity-40" not in (row.get_attribute("class") or "")
    expect(page.get_by_test_id("rename-conversation")).to_have_count(0)


def test_touch_drag_moves_session_into_project(
    touch_page: tuple[Page, CDPSession],
    seeded_session: tuple[str, str],
) -> None:
    """A touch drag still drops the session into a project folder."""
    base_url, session_id = seeded_session
    page, cdp = touch_page
    _set_title(base_url, session_id, f"e2e-touch-drop-{uuid.uuid4().hex[:8]}")
    project = f"Project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, project)

    try:
        page.goto(f"{base_url}/c/{session_id}?sidebar=open")
        link = _row_link(page, session_id)
        header = page.get_by_role("button", name=project, exact=True)
        expect(link).to_be_visible()
        expect(header).to_be_visible()
        row = link.locator("xpath=ancestor::li[1]")

        start_x, start_y = _center(link)
        end_x, end_y = _center(header)

        _touch(cdp, "touchStart", (start_x, start_y))
        expect(row).to_have_class(re.compile(r"\bopacity-40\b"), timeout=600)

        for step in range(1, 6):
            progress = step / 5
            _touch(
                cdp,
                "touchMove",
                (start_x + (end_x - start_x) * progress, start_y + (end_y - start_y) * progress),
            )
            page.wait_for_timeout(20)
        with page.expect_response(
            lambda response: (
                response.request.method == "PATCH"
                and response.url.endswith(f"/v1/sessions/{session_id}")
            ),
            timeout=5_000,
        ) as move_response:
            _touch(cdp, "touchEnd")
        assert move_response.value.ok, "server rejected the project membership update"

        expect(_section(page, project).locator(f'a[href="/c/{session_id}"]')).to_be_visible()
        expect(_section(page, "Sessions").locator(f'a[href="/c/{session_id}"]')).to_have_count(0)

        # A full navigation drops the optimistic React Query state. Re-checking
        # after reload proves the server persisted the project membership.
        page.goto(f"{base_url}/c/{session_id}?sidebar=open")
        reloaded_header = page.get_by_role("button", name=project, exact=True)
        expect(reloaded_header).to_be_visible()
        if reloaded_header.get_attribute("aria-expanded") != "true":
            reloaded_header.click()
        expect(_section(page, project).locator(f'a[href="/c/{session_id}"]')).to_be_visible()
        expect(_section(page, "Sessions").locator(f'a[href="/c/{session_id}"]')).to_have_count(0)
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/projects/{project_id}", timeout=10.0)
