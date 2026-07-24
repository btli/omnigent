"""E2E: the rail's "+ New shell" affordance and typing into the shell.

The right rail's Shells tab shows by default whenever the session agent
declares a non-empty ``terminals:`` block — its empty state carries a
virtual "+ New shell" row (``NewTerminalButton`` in
``web/src/shell/NewTerminalButton.tsx``). With a single declared
terminal name the row creates the shell directly on click (no dropdown),
POSTing ``/resources/terminals`` and handing the new terminal's tab key
to ``onOpenShell``. For a chat-first, non-wrapper terminal session (an
``openai-agents`` harness with a ``terminals:`` block and no
``omnigent.wrapper``), that hosts the shell INLINE in the workspace rail
— the Shells tab swaps its list for the shell's terminal, mirroring the
Files tab's inline viewer — instead of taking over the center. The
chat-replacing full-screen center view (``MainTerminalView``) is the
native-wrapper / mobile path only. None of this needs an LLM turn — the
user, not the agent, launches the shell — so these tests never send a
chat message.

Three behaviors are covered:

1. **"+ New shell" launches and hosts the shell inline in the rail.**
   Clicking the row creates a ``zsh`` shell and opens it inside the rail's
   Shells tab: an inline terminal whose xterm connects, with the rail
   naming the shell. The center is untouched — no ``main-terminal-view``
   mounts — and "Back to shells" collapses the inline terminal back to the
   list without closing the PTY.

2. **The user can type a command into the shell.** We type ``pwd`` into
   the connected inline shell and assert it keeps running — the keystrokes
   are accepted and the bridge does not error or close. We deliberately do
   NOT assert on the command's output: xterm renders to a WebGL canvas,
   so stdout is not in the DOM (the same reason
   ``files/test_right_panel.py`` only checks ``data-state``), and reading
   it back via a file side-effect proved environment-fragile (the shell's
   cwd and the filesystem-API root coincide locally but not on CI).

3. **The workspace rail preserves its redesigned top inset.** The rail floats
   beside the chat header at the 8px outer inset instead of clearing the
   header like the main-column surfaces.

Both use the function-scoped ``terminal_session`` fixture (registers the
``zsh``-declaring agent and a runner-bound session), so each test gets an
independent session.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail


def _open_new_shell(page: Page) -> None:
    """Open the Shells tab and click the "+ New shell" row.

    Leaves the rail's Shells tab active with the create POST fired. Scopes
    every lookup to the desktop "Workspace" rail so it never matches the
    hidden mobile drawer that mirrors the same controls.

    :param page: Playwright page already navigated to ``/c/{id}``.
    """
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    # Shells is present by default — the agent declares a ``zsh`` terminal,
    # so the tab shows before any shell exists with the "+ New shell"
    # affordance as its whole content.
    rail.get_by_role("tab", name=re.compile("Shells")).click()
    # Single declared name → the row creates directly on click (no dropdown).
    rail.get_by_role("button", name="New shell").click()


def test_new_shell_launches_and_opens(page: Page, terminal_session: tuple[str, str]) -> None:
    """Clicking "+ New shell" launches a shell hosted inline in the rail.

    The create is user-driven (no chat message), so the only wait is for
    the runner to spin the PTY up and the xterm to connect. For a chat-first
    non-wrapper session the created shell hosts INLINE in the rail's Shells
    tab — not the center: its xterm mounts inside the "Workspace" rail and
    connects, the rail names the freshly-created ``zsh`` shell (a fall-back
    to the agent's ``tui`` REPL would mean the new key was dropped), and no
    center ``main-terminal-view`` takes over. "Back to shells" collapses the
    inline terminal back to the list without closing the PTY.
    """
    base_url, session_id = terminal_session

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)

    rail = page.get_by_role("complementary", name="Workspace")
    # The new shell hosts INLINE in the rail: its xterm mounts inside the
    # Shells tab and connects.
    terminal_view = rail.get_by_test_id("terminal-view")
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)
    # The rail names the CLICKED shell (zsh), not the agent's REPL (tui).
    expect(rail).to_contain_text("zsh")

    # No center takeover: the chat-replacing full-screen shell view is the
    # native-wrapper / mobile path only, so a chat-first session never mounts
    # ``main-terminal-view`` here.
    expect(page.get_by_test_id("main-terminal-view")).to_have_count(0)

    # "Back to shells" collapses the inline terminal back to the shell list
    # (the "+ New shell" row is visible again) without closing the PTY.
    rail.get_by_role("button", name="Back to shells").click()
    expect(rail.get_by_role("button", name="New shell")).to_be_visible()


def test_new_shell_accepts_typed_command(page: Page, terminal_session: tuple[str, str]) -> None:
    """The user can type a command into a freshly created inline shell.

    Types ``pwd`` into the connected inline shell and asserts the bridge keeps
    running: the keystrokes are accepted and the PTY neither errors nor
    closes. We do NOT assert on the command's output — xterm renders to a
    WebGL canvas, so stdout never reaches the DOM, and capturing it via a
    file side-effect proved environment-fragile (the shell cwd and the
    filesystem-API root line up locally but not on CI). Verifying the shell
    stays healthy after input is the portable signal.
    """
    base_url, session_id = terminal_session

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)

    # Wait for the rail's inline shell xterm to connect before sending
    # keystrokes — input typed before the WS attach opens is dropped.
    rail = page.get_by_role("complementary", name="Workspace")
    terminal_view = rail.get_by_test_id("terminal-view")
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)

    # Focus xterm's hidden input (a plain container click doesn't reliably
    # focus the WebGL canvas in headless Chromium), then type a command.
    textarea = terminal_view.locator("textarea.xterm-helper-textarea")
    textarea.focus()
    page.keyboard.type("pwd")
    page.keyboard.press("Enter")

    # The shell accepted the command and stays live — no bridge error, no
    # "terminal session ended". A regression that drops user input or kills
    # the PTY on first keystroke would flip this out of ``connected``.
    expect(terminal_view).to_have_attribute("data-state", "connected")


def test_workspace_rail_preserves_outer_top_inset(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """The workspace rail starts at the shell's 8px outer inset.

    The old rail cleared the absolute chat header and aligned with expanded
    main-column surfaces. The redesign deliberately extends it beside the
    header, matching the sidebar's outer inset. Assert that geometry directly;
    shell launch behavior remains covered by the two tests above.
    """
    base_url, session_id = terminal_session

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    header = page.get_by_role("banner")
    expect(rail).to_be_visible()
    expect(header).to_be_visible()

    rail_top = rail.evaluate("el => el.getBoundingClientRect().top")
    header_bottom = header.evaluate("el => el.getBoundingClientRect().bottom")
    assert abs(rail_top - 8) <= 2, (
        f"workspace rail top {rail_top}px — expected the 8px outer inset"
    )
    assert rail_top < header_bottom, (
        f"workspace rail top {rail_top}px vs header bottom {header_bottom}px "
        "— expected the rail to extend beside the header"
    )
