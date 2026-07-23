"""E2E: `!` bang shell commands in the chat composer.

This tests the FRONTEND half of the bang-command feature (T3 grammar +
T4 composer integration from the multi-PR sequence). The T1 runner
`/terminals/{id}/input` route and T2 server `shell_command` event
handler are on separate branches not yet merged to main, so full
cross-stack execution (command actually running + receipt from the
server) is NOT tested here — instead, for flows that depend on those
backend pieces, we use Playwright ROUTE INTERCEPTION to stub the
server response and assert the CLIENT contract (session-scoped POST
URL, nested event-data shape, and receipt rendering from the stubbed
event).

Against the real server on this branch we CAN test:
- The `!` autocomplete menu appearing + ordering (user shells top,
  New shell types bottom)
- Suppression of the types section when ≤1 type is declared
- First-option activation + Tab completion
- `!`-prefixed input being intercepted (never sent as a chat message)
- The bang+attachment hard error

Backend-gated flows tested via stubbing:
- Terminal creation: the T4 client sends the remediation contract
  (`terminal` + `attempt_id`), while this isolated branch still carries
  the pre-T2 server route requiring a client-generated `session_key`.
  The stub asserts the approved request and returns the sequenced T2 shape.
- `! cmd` / `!u-xxxx cmd` actually executing: we stub the
  shell_command event response and assert the correct POST body is
  sent (`data.attempt_id` present, no `session_key` per the wire
  contract in §2 of BANG_SHELL_REMEDIATION_SPEC.md) and the receipt
  card renders from the stubbed event.

The function-scoped `terminal_session` fixture (from conftest.py)
registers a zsh-declaring agent and creates a runner-bound session —
no LLM turn needed, since the bang UX is agent-independent.

**Caveat from test_new_shell.py**: xterm renders to a WebGL canvas,
so DO NOT assert on shell stdout in the DOM. Assert on menu DOM state,
composer behavior, POST payloads, and receipt-card rendering instead.

Post-merge validation (once all backend PRs land): the full command
execution + real receipt flow will be tested by backend pytest (the
runner input route tests) and the existing e2e terminal suites.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page, Request, Route, expect

from tests.e2e_ui.conftest import open_right_rail

# Tab key prefix for a user-created `zsh` shell
_USER_ZSH_KEY_RE = re.compile(r"^terminal:terminal_zsh_u-")
_UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_USER_MESSAGE_SELECTOR = '[data-testid="message-bubble"][data-role="user"]'
_STUB_SEQUENCE = 1_000_000_000


def _composer(page: Page) -> Locator:
    """Return the real chat composer textarea."""
    return page.get_by_role("textbox", name="Message the agent")


def _session_key(terminal_id: str) -> str:
    """Extract the zsh session key from a terminal resource ID."""
    prefix = "terminal_zsh_"
    assert terminal_id.startswith(prefix)
    return terminal_id.removeprefix(prefix)


def _stub_terminal_create(
    page: Page,
    base_url: str,
    session_id: str,
    session_key: str,
) -> tuple[str, list[Request]]:
    """Stub the sequenced T2 create contract missing from this T4 branch."""
    terminal_id = f"terminal_zsh_{session_key}"
    create_url = f"{base_url}/v1/sessions/{session_id}/resources/terminals"
    create_posts: list[Request] = []
    resource: dict[str, Any] = {
        "id": terminal_id,
        "object": "session.resource",
        "type": "terminal",
        "session_id": session_id,
        "name": f"zsh:{session_key}",
        "metadata": {
            "terminal_name": "zsh",
            "session_key": session_key,
            "running": True,
        },
        "sequence": _STUB_SEQUENCE,
    }

    def fulfill_create(route: Route) -> None:
        request = route.request
        if request.method != "POST":
            route.continue_()
            return
        create_posts.append(request)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "terminal": resource,
                    "sequence": _STUB_SEQUENCE,
                }
            ),
        )

    page.route(create_url, fulfill_create)
    return terminal_id, create_posts


def _assert_terminal_create_post(
    posts: list[Request],
    base_url: str,
    session_id: str,
) -> None:
    """Assert the approved T4-to-T2 direct-create request contract."""
    assert len(posts) == 1
    request = posts[0]
    assert request.method == "POST"
    assert request.url == f"{base_url}/v1/sessions/{session_id}/resources/terminals"
    body = request.post_data_json
    assert isinstance(body, dict)
    assert set(body) == {"terminal", "attempt_id"}
    assert body["terminal"] == "zsh"
    assert isinstance(body["attempt_id"], str)
    assert _UUID_V4_RE.fullmatch(body["attempt_id"])
    assert "session_key" not in body


def _open_shells_tab(page: Page) -> None:
    """Open the right rail's Shells tab.

    :param page: Playwright page already navigated to `/c/{id}`.
    """
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("Shells")).click()


def _create_shell_via_rail(page: Page) -> str:
    """Create a new shell via the "+ New shell" button in the rail.

    Returns the created terminal's resource ID (e.g., `terminal_zsh_u-abc123`)
    by extracting it from the focused tab key.

    :param page: Playwright page with Shells tab open.
    :returns: Terminal resource ID.
    """
    _open_shells_tab(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="New shell").click()

    # Wait for the shell to be created and focused
    main_terminal = page.get_by_test_id("main-terminal-view")
    expect(main_terminal).to_be_visible(timeout=60_000)
    expect(main_terminal).to_have_attribute("data-active-terminal", _USER_ZSH_KEY_RE)

    # Extract the terminal ID from the active key: `terminal:terminal_zsh_u-abc123`
    active_key = main_terminal.get_attribute("data-active-terminal")
    assert active_key and active_key.startswith("terminal:")
    return active_key.removeprefix("terminal:")


def test_bang_autocomplete_menu_shows_running_shells_and_types(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """Typing `!` shows the autocomplete menu with running shells and new-shell types.

    The menu should list:
    - Running shells section (top) — one row per user shell
    - New shell section (bottom) — one row per declared shell type

    With a single declared type (zsh), the types section should be suppressed
    (≤1 type rule). After creating a shell, the running shells section appears.
    """
    base_url, session_id = terminal_session
    stub_terminal_id, create_posts = _stub_terminal_create(page, base_url, session_id, "u-menu01")
    page.goto(f"{base_url}/c/{session_id}")

    # The agent declares only `zsh`, so initially no menu appears (≤1 type,
    # zero user-shell rows). Exited rows would still render as disabled context.
    composer = _composer(page)
    composer.type("!")

    # With ≤1 declared type and no user-shell rows, the menu should be suppressed
    menu = page.locator('[data-testid="bang-shell-menu"]')
    expect(menu).to_have_count(0, timeout=2_000)

    # Clear and create a shell via the rail
    composer.clear()
    terminal_id = _create_shell_via_rail(page)
    assert terminal_id == stub_terminal_id
    _assert_terminal_create_post(create_posts, base_url, session_id)
    session_key = _session_key(terminal_id)

    # Return to chat and type `!` again
    page.get_by_role("button", name="Close shell").click()
    composer.type("!")

    # Now the menu should appear with the running shell
    expect(menu).to_be_visible(timeout=5_000)

    # The running shells section should be present
    expect(menu.get_by_text("Running shells")).to_be_visible()

    # The exact shell created above should be the only running row.
    expect(menu.get_by_test_id(f"bang-menu-item-{session_key}")).to_be_visible()

    # With single declared type, the "New shell" section should be suppressed
    expect(menu.get_by_text("New shell…", exact=True)).to_have_count(0)


def test_bang_menu_keyboard_navigation_and_completion(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """The first bang-menu option is active and Tab completes it.

    Creates a shell first so the menu is populated, then tests completion.
    """
    base_url, session_id = terminal_session
    stub_terminal_id, create_posts = _stub_terminal_create(page, base_url, session_id, "u-tab001")
    page.goto(f"{base_url}/c/{session_id}")

    # Create a shell so the menu will appear
    terminal_id = _create_shell_via_rail(page)
    assert terminal_id == stub_terminal_id
    _assert_terminal_create_post(create_posts, base_url, session_id)
    session_key = _session_key(terminal_id)

    # Return to chat
    page.get_by_role("button", name="Close shell").click()
    composer = _composer(page)
    composer.type("!")

    # Menu should appear
    menu = page.locator('[data-testid="bang-shell-menu"]')
    expect(menu).to_be_visible(timeout=5_000)

    # First item should be highlighted (data-active="true")
    first_item = menu.locator('[role="option"]').first
    expect(first_item).to_have_attribute("data-active", "true")

    # Press Tab to complete
    page.keyboard.press("Tab")

    # The composer should now contain the completed token: `!u-abc123 `
    expect(composer).to_have_value(f"!{session_key} ")

    # Menu should close after completion
    expect(menu).to_have_count(0)


def test_bang_command_intercepted_not_sent_as_chat_message(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """`!` commands are intercepted and never sent as normal chat messages.

    Submitting a `!` command should NOT trigger the normal chat send path
    (no agent turn, no chat bubble).
    """
    base_url, session_id = terminal_session
    stub_terminal_id, create_posts = _stub_terminal_create(page, base_url, session_id, "u-chat01")
    page.goto(f"{base_url}/c/{session_id}")

    composer = _composer(page)

    # Type a bang command (bare `!zsh` to create a shell without executing)
    composer.type("!zsh")
    page.keyboard.press("Enter")

    # The create succeeds and focuses the new shell in the main view.
    main_terminal = page.get_by_test_id("main-terminal-view")
    expect(main_terminal).to_be_visible(timeout=60_000)
    expect(main_terminal).to_have_attribute("data-active-terminal", _USER_ZSH_KEY_RE)
    active_key = main_terminal.get_attribute("data-active-terminal")
    assert active_key and active_key.startswith("terminal:")
    terminal_id = active_key.removeprefix("terminal:")
    assert terminal_id == stub_terminal_id
    _assert_terminal_create_post(create_posts, base_url, session_id)
    session_key = _session_key(terminal_id)

    # Return to chat before checking composer and transcript DOM.
    page.get_by_role("button", name="Close shell").click()
    expect(_composer(page)).to_have_value("")

    # But NO chat bubble should appear (the bang was intercepted)
    # Wait a moment to ensure no bubble appears
    page.wait_for_timeout(1000)
    chat_messages = page.locator(_USER_MESSAGE_SELECTOR)
    expect(chat_messages).to_have_count(0)

    # The shell should be created instead (rail should show it)
    _open_shells_tab(page)
    rail = page.get_by_role("complementary", name="Workspace")
    shell_row = rail.get_by_role("button").filter(has_text=session_key)
    expect(shell_row).to_have_count(1)
    expect(shell_row).to_contain_text("zsh")


def test_bang_with_attachment_shows_hard_error(
    page: Page, terminal_session: tuple[str, str], tmp_path: Path
) -> None:
    """Bang commands with attachments show an inline error.

    Per the spec, bang + attachment is a hard error: nothing is sent to
    agent or shell, and an inline error appears.
    """
    base_url, session_id = terminal_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = _composer(page)
    sample = tmp_path / "bang_attachment.txt"
    sample.write_text("bang attachment must not be sent\n")

    # Drive the same hidden input used by the paperclip picker.
    file_input = page.locator('input[type="file"][accept*="image/"]')
    file_input.set_input_files(str(sample))
    remove_button = page.get_by_role("button", name=f"Remove {sample.name}")
    expect(remove_button).to_be_visible(timeout=10_000)

    composer.fill("! echo BANG_ATTACHMENT")
    events_url = f"{base_url}/v1/sessions/{session_id}/events"
    event_posts: list[Request] = []

    def record_event_post(request: Request) -> None:
        if request.method == "POST" and request.url == events_url:
            event_posts.append(request)

    page.on("request", record_event_post)
    page.keyboard.press("Enter")

    # The approved composer uses an inline text row (not role="alert").
    error = page.locator("form.chat-composer-form").get_by_text(
        "Shell commands can't carry attachments",
        exact=True,
    )
    expect(error).to_be_visible(timeout=5_000)
    expect(composer).to_have_value("! echo BANG_ATTACHMENT")
    expect(remove_button).to_be_visible()
    expect(page.locator(_USER_MESSAGE_SELECTOR)).to_have_count(0)
    assert event_posts == [], "bang+attachment must not POST an agent or shell event"


def test_bare_bang_creates_new_shell(page: Page, terminal_session: tuple[str, str]) -> None:
    """Bare `!` (no command) creates a new shell of the default type.

    This tests the create-only client path against the sequenced T2 response
    contract. It is equivalent to clicking "+ New shell".
    """
    base_url, session_id = terminal_session
    stub_terminal_id, create_posts = _stub_terminal_create(page, base_url, session_id, "u-bare01")
    page.goto(f"{base_url}/c/{session_id}")

    composer = _composer(page)
    composer.type("!")
    page.keyboard.press("Enter")

    # A new shell should appear in the main view
    main_terminal = page.get_by_test_id("main-terminal-view")
    expect(main_terminal).to_be_visible(timeout=60_000)
    expect(main_terminal).to_have_attribute("data-active-terminal", f"terminal:{stub_terminal_id}")
    _assert_terminal_create_post(create_posts, base_url, session_id)

    # The shell view replaces the composer; close it and assert the remounted
    # chat draft stayed cleared.
    page.get_by_role("button", name="Close shell").click()
    expect(_composer(page)).to_have_value("")


def test_bang_command_with_stub_sends_correct_post_and_renders_receipt(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """Bang command execution sends the correct POST shape and renders a receipt.

    This tests the CLIENT contract when backend support exists:
    - POST to `/v1/sessions/{id}/events` with `type: "shell_command"`
    - Receipt card renders from the stubbed server response

    We stub the shell_command event response since the backend routes
    (T1 runner input + T2 server handler) aren't on main yet.
    """
    base_url, session_id = terminal_session
    events_url = f"{base_url}/v1/sessions/{session_id}/events"
    stub_terminal: dict[str, Any] = {
        "id": "terminal_zsh_u-test01",
        "object": "session.resource",
        "type": "terminal",
        "session_id": session_id,
        "name": "zsh:u-test01",
        "metadata": {
            "terminal_name": "zsh",
            "session_key": "u-test01",
            "running": True,
        },
        "sequence": _STUB_SEQUENCE,
    }

    def stub_shell_command_post(route: Route) -> None:
        """Fulfill the session-scoped shell-command route."""
        request = route.request
        if request.method != "POST":
            route.continue_()
            return
        body = request.post_data_json
        body_data = body.get("data") if isinstance(body, dict) else None
        attempt_id = body_data.get("attempt_id") if isinstance(body_data, dict) else None
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "item_id": "item_test_bang_001",
                    "item": {
                        "id": "item_test_bang_001",
                        "response_id": "turn_test_bang_001",
                        "type": "terminal_command",
                        "status": "ok",
                        "kind": "input",
                        "input": "echo BANG_TEST",
                        "action": "spawn",
                        "terminal_id": "terminal_zsh_u-test01",
                        "terminal_name": "zsh",
                        "session_key": "u-test01",
                        "attempt_id": attempt_id,
                        "sequence": _STUB_SEQUENCE,
                        "terminal": stub_terminal,
                        "http_status": 200,
                    },
                    "terminal": stub_terminal,
                    "sequence": _STUB_SEQUENCE,
                }
            ),
        )

    page.route(events_url, stub_shell_command_post)
    page.goto(f"{base_url}/c/{session_id}")
    composer = _composer(page)
    composer.type("! echo BANG_TEST")
    with page.expect_request(
        lambda request: request.method == "POST" and request.url == events_url
    ) as request_info:
        page.keyboard.press("Enter")

    request = request_info.value
    assert request.method == "POST"
    assert request.url == events_url
    post_body = request.post_data_json
    assert isinstance(post_body, dict)
    assert set(post_body) == {"type", "data"}
    assert post_body["type"] == "shell_command"

    data = post_body["data"]
    assert isinstance(data, dict)
    assert set(data) == {"action", "terminal", "command", "attempt_id"}
    assert data["action"] == "spawn"
    assert data["terminal"] == "zsh"
    assert data["command"] == "echo BANG_TEST"
    assert isinstance(data["attempt_id"], str)
    assert _UUID_V4_RE.fullmatch(data["attempt_id"])
    assert "attempt_id" not in post_body
    assert "session_key" not in data
    assert "session_key" not in post_body

    # A successful command focuses its shell, so return to chat before
    # asserting on the transcript receipt.
    close_shell = page.get_by_role("button", name="Close shell")
    expect(close_shell).to_be_visible(timeout=10_000)
    close_shell.click()
    receipt = page.get_by_test_id("terminal-command-card").filter(has_text="$ echo BANG_TEST")
    expect(receipt).to_have_count(1, timeout=5_000)
    expect(receipt).to_be_visible()
    expect(receipt.get_by_test_id("terminal-command-chip")).to_have_text("→ new zsh · u-test01")


def test_bang_target_existing_shell_stub(page: Page, terminal_session: tuple[str, str]) -> None:
    """Targeting an existing shell `!u-xxxx cmd` sends the correct POST.

    Creates a shell first, then sends a command to it via `!u-xxxx`.
    Stubs the shell_command response.
    """
    base_url, session_id = terminal_session
    stub_terminal_id, create_posts = _stub_terminal_create(page, base_url, session_id, "u-send001")
    page.goto(f"{base_url}/c/{session_id}")

    terminal_id = _create_shell_via_rail(page)
    assert terminal_id == stub_terminal_id
    _assert_terminal_create_post(create_posts, base_url, session_id)
    session_key = _session_key(terminal_id)
    page.get_by_role("button", name="Close shell").click()

    events_url = f"{base_url}/v1/sessions/{session_id}/events"
    stub_terminal: dict[str, Any] = {
        "id": terminal_id,
        "object": "session.resource",
        "type": "terminal",
        "session_id": session_id,
        "name": f"zsh:{session_key}",
        "metadata": {
            "terminal_name": "zsh",
            "session_key": session_key,
            "running": True,
        },
    }

    def stub_send_command_post(route: Route) -> None:
        """Fulfill the existing-shell shell-command route."""
        request = route.request
        if request.method != "POST":
            route.continue_()
            return
        body = request.post_data_json
        body_data = body.get("data") if isinstance(body, dict) else None
        attempt_id = body_data.get("attempt_id") if isinstance(body_data, dict) else None
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "item_id": "item_test_send_001",
                    "item": {
                        "id": "item_test_send_001",
                        "response_id": "turn_test_send_001",
                        "type": "terminal_command",
                        "status": "ok",
                        "kind": "input",
                        "input": "pwd",
                        "action": "send",
                        "terminal_id": terminal_id,
                        "terminal_name": "zsh",
                        "session_key": session_key,
                        "attempt_id": attempt_id,
                        "terminal": stub_terminal,
                        "http_status": 200,
                    },
                    "terminal": stub_terminal,
                }
            ),
        )

    page.route(events_url, stub_send_command_post)
    composer = _composer(page)
    composer.type(f"!{session_key} pwd")
    with page.expect_request(
        lambda request: request.method == "POST" and request.url == events_url
    ) as request_info:
        page.keyboard.press("Enter")

    request = request_info.value
    assert request.method == "POST"
    assert request.url == events_url
    post_body = request.post_data_json
    assert isinstance(post_body, dict)
    assert set(post_body) == {"type", "data"}
    assert post_body["type"] == "shell_command"

    data = post_body["data"]
    assert isinstance(data, dict)
    assert set(data) == {"action", "terminal_id", "command", "attempt_id"}
    assert data["action"] == "send"
    assert data["terminal_id"] == terminal_id
    assert data["command"] == "pwd"
    assert isinstance(data["attempt_id"], str)
    assert _UUID_V4_RE.fullmatch(data["attempt_id"])
    assert "attempt_id" not in post_body
    assert "terminal" not in data
    assert "session_key" not in data
    assert "session_key" not in post_body

    close_shell = page.get_by_role("button", name="Close shell")
    expect(close_shell).to_be_visible(timeout=10_000)
    close_shell.click()
    receipt = page.get_by_test_id("terminal-command-card").filter(has_text="$ pwd")
    expect(receipt).to_have_count(1, timeout=5_000)
    expect(receipt).to_be_visible()
    expect(receipt.get_by_test_id("terminal-command-chip")).to_have_text(f"→ zsh · {session_key}")


def test_bang_unknown_target_shows_error(page: Page, terminal_session: tuple[str, str]) -> None:
    """Bang with an unknown target shows an inline error.

    Per the spec, `!nosuch echo hi` shows the exact "No shell" feedback
    and preserves the input.
    """
    base_url, session_id = terminal_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = _composer(page)
    composer.type("!nosuch echo hi")
    page.keyboard.press("Enter")

    # Command feedback is an inline text row, not an ARIA alert.
    error = page.locator("form.chat-composer-form").get_by_text(
        "No shell `nosuch` — press ! to see running shells",
        exact=True,
    )
    expect(error).to_be_visible(timeout=5_000)

    # The input should be preserved
    expect(composer).to_have_value("!nosuch echo hi")

    # No chat message should be sent
    page.wait_for_timeout(1000)
    chat_messages = page.locator(_USER_MESSAGE_SELECTOR)
    expect(chat_messages).to_have_count(0)
