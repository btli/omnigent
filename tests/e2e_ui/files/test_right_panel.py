"""E2E: right rail Shells tab and file viewer.

Execution-logs coverage was dropped: that surface used to be a
``SessionRail`` card but the rail is now tabbed (Agents/Files/Shells)
and the only entry point left is the ``md:hidden`` mobile session menu —
unreachable on the desktop viewport these tests run at.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _TERMINAL_PANEL_FILE,
    _TERMINAL_PANEL_FILE_CONTENT,
    open_right_rail,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("tab_name", "tooltip", "expected_state"),
    [
        ("Files", "Files", "active"),
        ("Agents", "Agents", "inactive"),
    ],
)
def test_workspace_tab_hover_tooltip(
    page: Page,
    terminal_session: tuple[str, str],
    tab_name: str,
    tooltip: str,
    expected_state: str,
) -> None:
    """Explain fixed workspace tabs on hover without changing selection."""
    base_url, session_id = terminal_session
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)

    rail = page.get_by_role("complementary", name="Workspace")
    tab = rail.get_by_role("tab", name=re.compile(f"^{tab_name}"))

    expect(tab).to_have_attribute("data-state", expected_state)
    tab.hover()
    expect(page.get_by_role("tooltip")).to_have_text(tooltip)
    expect(tab).to_have_attribute("data-state", expected_state)


def test_right_panel_terminals_and_file_viewer(
    page: Page,
    terminal_session: tuple[str, str],
) -> None:
    """Launch a terminal and exercise the Terminals tab + file viewer.

    The terminal session fixture registers the test agent and creates a
    session bound to it. We send ``spin up zsh``, then verify:

    - the right rail's Terminals tab lists the launched terminal, its
      xterm connects inline, and the maximize button opens the full
      terminals push panel;
    - the Files tab can open a workspace file and the file viewer renders
      its contents.

    The file is seeded via the filesystem API rather than relying on the
    agent's terminal ``printf`` to write it: the agent's workspace cwd
    differs by environment (empty on CI), and the changed-files panel only
    refreshes on turn active→idle, so a late PTY write races the refresh.
    The terminal launch above is the real terminal coverage; the file
    viewer just needs a deterministic file present in the scanned workspace.
    """
    base_url, session_id = terminal_session
    test_file = _REPO_ROOT / _TERMINAL_PANEL_FILE
    if test_file.exists():
        test_file.unlink()

    # Seed the file into the session workspace (same API the markdown tests
    # use) so it deterministically appears in the Files panel.
    seed_resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_TERMINAL_PANEL_FILE}",
        json={"content": f"{_TERMINAL_PANEL_FILE_CONTENT}\n", "encoding": "utf-8"},
        timeout=10.0,
    )
    seed_resp.raise_for_status()

    # Scope rail-content lookups to the desktop "Workspace" rail so they
    # don't match the hidden mobile drawer that mirrors the same testids.
    rail = page.get_by_role("complementary", name="Workspace")

    try:
        page.goto(f"{base_url}/c/{session_id}")
        # The rail defaults open but is remembered per session; ensure it is
        # open so the Shells tab and Files panel below are reachable.
        open_right_rail(page)

        composer = page.get_by_placeholder("Ask the agent anything…")
        expect(composer).to_be_visible()
        composer.fill("spin up zsh")
        page.get_by_role("button", name="Send", exact=True).click()

        # Open the Shells tab (present by default — the agent declares
        # terminals). The launched shell's row shows once the agent's
        # sys_terminal_launch completes (LLM turn → up to 60s); keep
        # main's generous click timeout for slow CI turns.
        rail.get_by_role("tab", name=re.compile("Shells")).click(timeout=60_000)
        terminal_row = rail.get_by_role("button").filter(has_text="zsh").filter(has_text="main")
        expect(terminal_row.first).to_be_visible(timeout=60_000)

        # Clicking a shell row hosts it INLINE in the rail for a chat-first,
        # non-wrapper session (the runner-hosted SDK REPL keeps the center),
        # mirroring the Files tab's inline viewer — chat stays visible on the
        # left. The full-screen center takeover (MainTerminalView) is the
        # native-wrapper / mobile path only.
        terminal_row.first.click()
        # The shell's xterm mounts inline in the rail and connects. The rail
        # names the CLICKED shell (zsh); a fall-back to the agent's embedded
        # ``tui`` REPL would mean the explicit key was dropped.
        terminal_view = rail.get_by_test_id("terminal-view")
        expect(terminal_view).to_be_visible(timeout=20_000)
        expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)
        expect(rail).to_contain_text("zsh")
        expect(rail).not_to_contain_text("tui")
        # No center takeover: chat stays visible (the Chat/Terminal pill
        # remains — inline hosting is precisely the not-chat-replacing path)
        # and no ``main-terminal-view`` mounts.
        expect(page.get_by_role("button", name="Chat", exact=True)).to_be_visible()
        expect(page.get_by_test_id("main-terminal-view")).to_have_count(0)
        # "Back to shells" collapses the inline terminal back to the list
        # without closing the PTY, before the Files steps below.
        rail.get_by_role("button", name="Back to shells").click()
        expect(rail.get_by_role("button", name="New shell")).to_be_visible()

        # Switch to the Files tab and open the seeded file. The
        # changed-file row renders two buttons carrying the filename: the
        # file-open button (visible text) and an icon-only Download button
        # (aria-label "Download <name>"). Filter to the open button by its
        # visible text so the locator stays single-element under strict mode.
        rail.get_by_role("tab", name=re.compile("Files")).click()
        file_button = rail.get_by_role(
            "button", name=re.compile(re.escape(_TERMINAL_PANEL_FILE))
        ).filter(has_text=_TERMINAL_PANEL_FILE)
        expect(file_button).to_be_visible(timeout=60_000)
        file_button.click()

        # Two FileViewer instances mount (hidden mobile drawer + desktop
        # rail); scope to the rail so the locator is single-element and the
        # visible desktop instance — the bare-page ``.last`` can resolve to
        # the hidden drawer once other push panels have mounted.
        file_viewer = rail.get_by_test_id("file-viewer")
        expect(file_viewer).to_be_visible()
        # The open file is identified by its tab (the desktop viewer header no
        # longer repeats a top-level filename — it's redundant with the tab).
        # The tab's close button carries an "Close <name>" label.
        expect(
            rail.get_by_role("button", name=f"Close {_TERMINAL_PANEL_FILE}", exact=True)
        ).to_be_visible()
        # The viewer renders the file body; scope inside the file viewer and
        # pick the first match — its presence proves the content landed.
        expect(file_viewer.get_by_text(_TERMINAL_PANEL_FILE_CONTENT).first).to_be_visible(
            timeout=20_000
        )
    finally:
        if test_file.exists():
            test_file.unlink()
