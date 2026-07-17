"""Browser e2e for the pinned-row project subtitle.

Pinning lifts a session out of its project folder into the flat "Pinned"
section (see ``test_sidebar_pin_unpin`` / ``test_sidebar_projects``), which
drops the visual cue for which project the session came from. To restore it,
a pinned, project-owned row renders a subtitle
(``data-testid="pinned-project-subtitle"``) with a folder icon and the
project name (``ConversationRow`` in Sidebar.tsx) — visible on every
viewport, no hover needed.

This drives the real chain the ``Sidebar`` unit tests mock out: the live
create-project + move flow → ``project_id`` on the refreshed
``GET /v1/sessions`` list → the pinned peel keeping the membership → the
subtitle resolving the project name from the owner's project list.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give a session a unique title via ``PATCH /v1/sessions/{id}`` so its row
    is easy to spot among other tests' sessions in the shared server."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def _section(page: Page, title: str) -> Locator:
    """Locate the sidebar ``<section>`` whose collapse-header button reads
    *title* (e.g. "Pinned" or a project name)."""
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _move_to_new_project(page: Page, row: Locator, name: str) -> None:
    """Drive the row kebab → "Add to project" → "Create new project" flow,
    typing *name* and committing with Enter."""
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("move-to-project").click()
    page.get_by_role("menuitem", name="Create new project").click()
    new_input = page.get_by_placeholder("Project name…")
    new_input.fill(name)
    new_input.press("Enter")


def test_pinned_project_row_shows_project_subtitle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A pinned, project-owned row names its project in a visible subtitle.

    Files the session into a fresh project, pins it (which lifts it into the
    flat "Pinned" section, away from the project folder), then asserts the
    pinned row carries the project-name subtitle. Catches a regression where
    the pinned peel drops the membership or the subtitle stops resolving the
    project's display name.
    """
    base_url, session_id = seeded_session
    title = f"e2e-flyout-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)
    project = f"Project {uuid.uuid4().hex[:6]}"

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()

    # File it into a new project first, then pin it out of that folder.
    _move_to_new_project(page, row, project)
    expect(_section(page, project).locator(f'a[href="/c/{session_id}"]')).to_be_visible()

    project_row = (
        _section(page, project)
        .locator("li")
        .filter(has=page.locator(f'a[href="/c/{session_id}"]'))
    )
    project_row.hover()
    project_row.get_by_test_id("quick-pin-conversation").click()

    # Now under "Pinned" — the project folder no longer conveys its project.
    pinned_row = (
        _section(page, "Pinned")
        .locator("li")
        .filter(has=page.locator(f'a[href="/c/{session_id}"]'))
    )
    expect(pinned_row).to_be_visible()

    # The pinned row names its project in an always-visible subtitle — the
    # folder no longer conveys it, and there is no hover-only affordance.
    subtitle = pinned_row.get_by_test_id("pinned-project-subtitle")
    expect(subtitle).to_be_visible()
    expect(subtitle).to_contain_text(project)
    expect(pinned_row).to_contain_text(title)
