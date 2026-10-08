"""Android shell: the Workspace rail's embedded file viewer must not re-add the OS inset.

The Android shell (``web/android``) injects its own ``!important`` safe-area
stylesheet from ``NativeBridgeScript.kt`` on top of the SPA's CSS. Like the SPA's
``index.css`` rule, it must exempt panels nested inside the already padded
Workspace rail, or the frameless viewer inside the rail at ``md+`` is padded a
second time.

Plain Chromium stands in for the WebView: the Kotlin-embedded script is
extracted and injected before any app script runs, and the OS insets are written
into the page the way ``MainActivity.emitInsets()`` does.
"""

from __future__ import annotations

import re
import shutil
import textwrap
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import FloatRect, Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BRIDGE_SOURCE = (
    _REPO_ROOT / "web/android/app/src/main/java/ai/omnigent/android/NativeBridgeScript.kt"
)
# Kotlin templates inside the raw string: the bridge transport object name and
# the escaped literal dollar sign.
_KOTLIN_SUBSTITUTIONS = {
    "${OmnigentBridgeListener.JS_OBJECT_NAME}": "omnigentNativeBridge",
    "${'$'}": "$",
}

# Unfolded foldable / tablet class (>= the 768px ``md`` breakpoint, where the
# Workspace rail docks) and a phone, where the viewer is a standalone overlay.
_UNFOLDED_VIEWPORT = {"width": 1024, "height": 768}
_PHONE_VIEWPORT = {"width": 390, "height": 844}
_STATUS_BAR_PX = 52
_NAV_BAR_PX = 20

_SEEDED_FILE = "android_workspace_viewer_inset.txt"
_SEEDED_CONTENT = "Body of the file opened in the Workspace rail."


def android_bridge_script() -> str:
    """Return the JavaScript the Android shell injects, taken from the Kotlin source.

    :returns: The bridge script with Kotlin template placeholders substituted.
    """
    source = _BRIDGE_SOURCE.read_text()
    match = re.search(r'val source: String =\s*"""\n(.*?)""".trimIndent\(\)', source, re.S)
    assert match, f"bridge raw string not found in {_BRIDGE_SOURCE}"
    script = textwrap.dedent(match.group(1))
    for placeholder, value in _KOTLIN_SUBSTITUTIONS.items():
        script = script.replace(placeholder, value)
    assert "${" not in script, "unsubstituted Kotlin template in the bridge script"
    return script


def emit_android_insets(page: Page, top: int, bottom: int) -> None:
    """Push the OS insets into the page the way ``MainActivity.emitInsets()`` does.

    :param page: Page running under the injected Android bridge.
    :param top: Status-bar inset in CSS px.
    :param bottom: Navigation-bar inset in CSS px.
    """
    page.evaluate(
        """([top, bottom]) => {
          const s = document.documentElement.style;
          s.setProperty('--omnigent-safe-top', top + 'px');
          s.setProperty('--omnigent-safe-bottom', bottom + 'px');
          s.setProperty('--omnigent-android-safe-area-top', top + 'px');
          s.setProperty('--omnigent-android-safe-area-bottom', bottom + 'px');
          s.setProperty('--omnigent-android-safe-area-left', '0px');
          s.setProperty('--omnigent-android-safe-area-right', '0px');
        }""",
        [top, bottom],
    )


@pytest.fixture
def session_with_workspace_file(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed one text file into the session workspace so the Files tab has a row to open.

    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :returns: The same ``(base_url, session_id)`` pair.
    """
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_SEEDED_FILE}",
        json={"content": f"{_SEEDED_CONTENT}\n", "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield seeded_session
    finally:
        # With ``os_env.cwd: .`` a spawned runner writes under <repo-root>/<session_id>/.
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def _open_android_session(request: pytest.FixtureRequest, base_url: str, session_id: str) -> Page:
    """Create the recorded page after setup and open the session under the Android shell.

    :param request: Pytest request used to create the ``page`` fixture lazily.
    :param base_url: Base URL of the e2e server.
    :param session_id: Session to open.
    :returns: The page, with the shell marker live and the OS insets applied.
    """
    page: Page = request.getfixturevalue("page")
    page.add_init_script(android_bridge_script())
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator(".app-shell")).to_have_attribute("data-android-native", "true")
    expect(page.locator("style#omnigent-android-insets")).to_have_count(1)
    emit_android_insets(page, _STATUS_BAR_PX, _NAV_BAR_PX)
    return page


def _box(locator: Locator) -> FloatRect:
    box = locator.bounding_box()
    assert box is not None, f"element {locator} has no bounding box"
    return box


def _vertical_padding(locator: Locator) -> tuple[str, str]:
    top, bottom = locator.evaluate(
        "el => { const cs = getComputedStyle(el); return [cs.paddingTop, cs.paddingBottom]; }"
    )
    return (top, bottom)


@pytest.mark.browser_context_args(viewport=_UNFOLDED_VIEWPORT)
def test_rail_file_viewer_keeps_single_status_bar_inset(
    request: pytest.FixtureRequest,
    session_with_workspace_file: tuple[str, str],
) -> None:
    """A file opened in the Workspace rail starts right under the tab strip.

    :param request: Pytest request (the page is created after the file is seeded).
    :param session_with_workspace_file: ``(base_url, session_id)`` with one seeded file.
    """
    base_url, session_id = session_with_workspace_file
    page = _open_android_session(request, base_url, session_id)

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    # The rail owns the status-bar clearance; a nested panel has nothing left to clear.
    expect(rail).to_have_css("padding-top", f"{_STATUS_BAR_PX}px")

    rail.get_by_role("tab", name=re.compile("^Files")).click()
    row = rail.get_by_role("button", name=re.compile(re.escape(_SEEDED_FILE))).filter(
        has_text=_SEEDED_FILE
    )
    expect(row).to_be_visible(timeout=30_000)
    row.click()

    viewer = rail.get_by_test_id("file-viewer")
    expect(viewer).to_be_visible()
    expect(viewer.get_by_text(_SEEDED_CONTENT).first).to_be_visible(timeout=20_000)
    strip = rail.locator(".workspace-tab-strip")
    header = viewer.locator(":scope > div").first
    expect(header).to_contain_text(_SEEDED_FILE)
    page.wait_for_timeout(1_500)

    strip_box = _box(strip)
    header_box = _box(header)
    gap = header_box["y"] - (strip_box["y"] + strip_box["height"])
    padding_top, padding_bottom = _vertical_padding(viewer)
    assert gap <= 1.0 and padding_top == "0px" and padding_bottom == "0px", (
        f"viewer header sits {gap:.0f}px below the Workspace tab strip; "
        f"nested viewer padding top={padding_top} bottom={padding_bottom}"
    )


@pytest.mark.browser_context_args(viewport=_PHONE_VIEWPORT)
def test_standalone_file_viewer_keeps_one_inset_on_a_phone(
    request: pytest.FixtureRequest,
    session_with_workspace_file: tuple[str, str],
) -> None:
    """Below ``md`` the full-screen viewer is not nested in the rail and keeps one inset each.

    :param request: Pytest request (the page is created after the file is seeded).
    :param session_with_workspace_file: ``(base_url, session_id)`` with one seeded file.
    """
    base_url, session_id = session_with_workspace_file
    page = _open_android_session(request, base_url, session_id)

    page.get_by_role("button", name="Conversation actions").click()
    page.get_by_role("menuitem", name="Files", exact=True).click()
    expect(page.get_by_test_id("files-panel-drawer")).to_have_attribute("data-state", "open")
    row = page.get_by_role("button", name=re.compile(rf"^{re.escape(_SEEDED_FILE)}"))
    expect(row).to_be_visible(timeout=30_000)
    row.click()

    viewer = page.locator('aside[data-testid="file-viewer"]')
    expect(viewer).to_be_visible()
    expect(viewer.get_by_text(_SEEDED_CONTENT).first).to_be_visible(timeout=20_000)
    page.wait_for_timeout(1_500)

    assert _vertical_padding(viewer) == (f"{_STATUS_BAR_PX}px", f"{_NAV_BAR_PX}px")
