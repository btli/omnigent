"""E2E: a session search that outlives the client deadline says it timed out.

The palette's server search (``GET /v1/sessions?search_query=``) is bounded by
``SEARCH_FETCH_TIMEOUT_MS`` (10 s) in ``useConversations.ts``. When a slow
content scan runs past it, the palette must show "Search timed out" rather than
the generic "Couldn't load sessions." (a timeout is not a failed load) or
"No results found" (the scan never finished).

The slow server is simulated by holding every search request unanswered, so the
browser's own ``AbortSignal.timeout`` fires exactly as it does against a slow
deployment. Releasing the hold and pressing Retry proves the state recovers.
"""

from __future__ import annotations

import uuid

from playwright.sync_api import Page, Route, expect

# The client deadline is 10 s; leave headroom for the debounce and CI jitter.
_TIMEOUT_MESSAGE_WAIT_MS = 25_000


def _is_session_search(url: str) -> bool:
    return "/v1/sessions?" in url and "search_query=" in url


def test_slow_session_search_shows_timed_out_state(page: Page, live_server: str) -> None:
    """A held search settles to "Search timed out"; Retry recovers once it answers."""
    base_url = live_server
    held: list[Route] = []

    def hold(route: Route) -> None:
        held.append(route)

    page.route(_is_session_search, hold)
    try:
        page.goto(f"{base_url}/")
        search_button = page.get_by_test_id("sidebar-search-button")
        expect(search_button).to_be_visible(timeout=30_000)
        search_button.click()

        palette_input = page.get_by_test_id("command-palette-input")
        expect(palette_input).to_be_visible()
        palette_input.fill(f"zzz-rare-{uuid.uuid4().hex[:12]}")

        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_text("Searching…")).to_be_visible()
        status = dialog.get_by_role("status")
        expect(status).to_contain_text("Search timed out", timeout=_TIMEOUT_MESSAGE_WAIT_MS)
        expect(status).not_to_contain_text("Couldn't load sessions")
        expect(dialog.get_by_text("No results found")).to_have_count(0)
        # A timeout is terminal: exactly one search request, no retry storm.
        assert len(held) == 1

        # Once the server answers, Retry replaces the timeout with real results.
        # Unrouting disposes the held (already client-aborted) request.
        page.unroute(_is_session_search, hold)
        held.clear()
        dialog.get_by_role("button", name="Retry").click()
        expect(dialog.get_by_text("No results found")).to_be_visible()
        expect(dialog.get_by_text("Search timed out")).to_have_count(0)
    finally:
        for route in held:
            route.abort()
