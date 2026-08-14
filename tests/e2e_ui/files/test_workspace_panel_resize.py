"""E2E coverage for the workspace push-panel resize seam."""

from __future__ import annotations

from playwright.sync_api import Page, expect


def _open_execution_logs_panel(page: Page) -> None:
    expect(page.get_by_test_id("execution-logs-card")).to_be_visible(timeout=30_000)
    page.get_by_test_id("execution-log-row-main").click()
    expect(page.get_by_test_id("execution-logs-panel")).to_be_visible()


def test_workspace_panel_pointer_resize_persists_without_annexing_chat(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Resize the workspace panel while adjacent chat input stays inert."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}?debug=1")
    _open_execution_logs_panel(page)

    panel = page.get_by_test_id("execution-logs-panel")
    handle = page.get_by_role("separator", name="Resize panel")
    initial_width = panel.bounding_box()["width"]
    handle_box = handle.bounding_box()

    page.mouse.move(handle_box["x"] + handle_box["width"] / 2, handle_box["y"] + 100)
    page.mouse.down()
    page.mouse.move(handle_box["x"] - 80, handle_box["y"] + 100, steps=4)
    page.mouse.up()

    resized_width = panel.bounding_box()["width"]
    assert resized_width >= initial_width + 70

    chat = page.locator("main").first
    chat_box = chat.bounding_box()
    page.mouse.move(chat_box["x"] + chat_box["width"] / 2, chat_box["y"] + 200)
    page.mouse.wheel(0, 200)
    page.mouse.click(chat_box["x"] + chat_box["width"] / 2, chat_box["y"] + 200)
    expect(panel).to_have_js_property("offsetWidth", round(resized_width))

    page.reload()
    _open_execution_logs_panel(page)
    persisted_width = page.get_by_test_id("execution-logs-panel").bounding_box()["width"]
    assert abs(persisted_width - resized_width) <= 1
