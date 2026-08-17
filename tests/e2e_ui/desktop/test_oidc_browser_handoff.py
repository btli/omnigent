"""Browser coverage for the desktop OIDC handoff status window.

The Electron main process opens the system browser and owns ticket polling;
this shell page keeps that wait cancellable and makes a failed attempt
retryable. Playwright drives the shipped HTML with the isolated preload API
stubbed before its script runs, exercising the actual user-visible controls.
"""

from pathlib import Path

from playwright.sync_api import Page, expect

_OIDC_LOGIN_PAGE = (
    Path(__file__).resolve().parents[3] / "web" / "electron" / "src" / "oidc_login.html"
)

_PRELOAD_STUB = """
window.__oidc = { calls: [], listener: null };
window.omnigentOidcLogin = {
  onState: (listener) => {
    window.__oidc.listener = listener;
    return () => { window.__oidc.listener = null; };
  },
  cancel: () => window.__oidc.calls.push("cancel"),
  retry: () => window.__oidc.calls.push("retry"),
};
"""


def test_oidc_handoff_wait_error_retry_and_cancel(page: Page) -> None:
    """The browser handoff clearly reports progress and offers safe recovery."""
    page.add_init_script(_PRELOAD_STUB)
    page.goto(_OIDC_LOGIN_PAGE.as_uri())

    page.evaluate(
        """() => window.__oidc.listener({
          phase: "waiting",
          message: "Waiting for sign-in…",
          host: "accounts.example.com",
        })"""
    )
    expect(page.get_by_role("heading")).to_have_text("Continue sign-in in your browser")
    expect(page.locator("#message")).to_have_text("Waiting for sign-in…")
    expect(page.locator("#host")).to_have_text("accounts.example.com")
    expect(page.get_by_role("button", name="Retry")).to_be_hidden()

    page.evaluate(
        """() => window.__oidc.listener({
          phase: "error",
          message: "The sign-in ticket expired.",
          host: "accounts.example.com",
        })"""
    )
    expect(page.get_by_role("heading")).to_have_text("Sign-in did not finish")
    expect(page.locator("#message")).to_have_text("The sign-in ticket expired.")

    page.get_by_role("button", name="Retry").click()
    page.get_by_role("button", name="Cancel").click()
    assert page.evaluate("() => window.__oidc.calls") == ["retry", "cancel"]
