"""E2E: the mobile header glass pill contains its own controls.

``ChatHeader.test.tsx`` asserts the pill's Tailwind classes, but jsdom
performs no layout, so it cannot prove the *geometric* contract: the
Chat/Terminal ``ViewModeToggle`` paints its own background, and the pill
is a ``rounded-full`` stadium, so that painted track must stay inside the
pill's rounded caps — not just its rectangular bounds. A real browser at
a phone viewport is the only place that geometry runs.

The fixture stamps ``omnigent.ui: terminal`` on a plain runner-bound
session (no wrapper label), which is all the SPA needs to render the
``ViewModeToggle`` without booting a native CLI. Two cluster shapes are
covered:

  - toggle **+ owner kebab** (a top-level terminal-first session), and
  - toggle **alone** — the shape a terminal-first *sub-agent* shows on a
    phone, where there is no owner kebab to cover the pill's trailing cap.
    The single-user e2e server only mints owner sessions, so the test
    drops the trailing kebab to reach that cluster shape.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, ViewportSize, expect

from tests.e2e_ui.conftest import (
    _build_hello_world_bundle,
    _ensure_runner_online,
    _server_state,
)

# Two phone widths below the Tailwind ``md`` breakpoint (768px): 360px is a
# common Android portrait width and 390px an iPhone-12-class one. A ~1080px
# device screenshot reports as 360 CSS px at devicePixelRatio 3.
_WIDTHS = (360, 390)


@pytest.fixture
def terminal_first_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound, owner-managed session flagged terminal-first.

    The ``omnigent.ui: terminal`` label makes the SPA render the header's
    Chat/Terminal ``ViewModeToggle``; being owner-managed (a top-level
    session in the sidebar list) makes it render the "Conversation
    actions" kebab.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    bundle = _build_hello_world_bundle()
    create = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({"labels": {"omnigent.ui": "terminal"}})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]
    httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    ).raise_for_status()
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            respawned.wait(timeout=5)


# For every direct child of the pill that paints a background (the
# ViewModeToggle track — ghost icon buttons stay transparent), report how
# far each corner of its border box sits outside the pill's ``rounded-full``
# stadium. A positive ``worst`` means visible overflow past the rounded edge.
_CONTAINMENT_JS = """
() => {
  const toggle = document.querySelector('[data-testid="view-mode-toggle"]');
  if (!toggle) return null;
  const pill = toggle.parentElement;
  const p = pill.getBoundingClientRect();
  const r = Math.min(p.width, p.height) / 2;   // rounded-full cap radius
  const cy = (p.top + p.bottom) / 2;
  // Signed distance of a point outside the stadium (>0 means outside).
  const outside = (x, y) => {
    const leftCx = p.left + r;
    const rightCx = p.right - r;
    let cx = x;
    if (x < leftCx) cx = leftCx;
    else if (x > rightCx) cx = rightCx;
    if (cx === x) {
      // Flat middle band: only the top/bottom edges bound it.
      return Math.max(p.top - y, y - p.bottom);
    }
    return Math.hypot(x - cx, y - cy) - r;
  };
  let worst = -Infinity;
  let culprit = null;
  for (const c of pill.children) {
    const bg = getComputedStyle(c).backgroundColor;
    const painted = bg && bg !== 'transparent' && !/rgba?\\([^)]*,\\s*0\\s*\\)$/.test(bg);
    const cr = c.getBoundingClientRect();
    if (!painted || cr.width === 0) continue;
    for (const [x, y] of [
      [cr.left, cr.top], [cr.right, cr.top],
      [cr.left, cr.bottom], [cr.right, cr.bottom],
    ]) {
      const d = outside(x, y);
      if (d > worst) {
        worst = d;
        culprit = c.getAttribute('data-testid') || c.tagName;
      }
    }
  }
  return { worst, culprit, height: p.height };
}
"""


def _assert_pill_contains_painted_controls(page: Page, width: int, *, drop_kebab: bool) -> None:
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=30_000)
    if drop_kebab:
        # The single-user server only mints owner sessions, so drop the owner
        # kebab to reach the terminal-first sub-agent cluster shape (a lone
        # ViewModeToggle with no control covering the pill's trailing cap).
        expect(page.get_by_role("button", name="Conversation actions")).to_be_visible()
        page.get_by_role("button", name="Conversation actions").evaluate(
            "el => el.remove()"
        )
    else:
        expect(page.get_by_role("button", name="Conversation actions")).to_be_visible()

    result = page.evaluate(_CONTAINMENT_JS)
    assert result is not None, "the terminal-first Chat/Terminal toggle did not render"
    # 0.75px absorbs sub-pixel rounding; a real overflow is several pixels.
    assert result["worst"] <= 0.75, (
        f"a painted control ({result['culprit']}) overflows the pill's rounded "
        f"edge at {width}px by {result['worst']:.1f}px (pill height {result['height']:.1f})"
    )


@pytest.mark.parametrize("width", _WIDTHS, ids=[f"{w}px" for w in _WIDTHS])
def test_pill_contains_toggle_and_kebab(
    page: Page,
    terminal_first_session: tuple[str, str],
    width: int,
) -> None:
    """The Chat/Terminal track stays inside the pill next to the owner kebab."""
    base_url, session_id = terminal_first_session
    viewport: ViewportSize = {"width": width, "height": 844}
    page.set_viewport_size(viewport)
    page.goto(f"{base_url}/c/{session_id}")
    _assert_pill_contains_painted_controls(page, width, drop_kebab=False)


@pytest.mark.parametrize("width", _WIDTHS, ids=[f"{w}px" for w in _WIDTHS])
def test_pill_contains_lone_toggle(
    page: Page,
    terminal_first_session: tuple[str, str],
    width: int,
) -> None:
    """A lone Chat/Terminal track (sub-agent shape) stays inside the pill.

    This is the case the trailing-edge inset guards: with no kebab covering
    the pill's trailing cap, an un-inset track paints past the rounded edge.
    """
    base_url, session_id = terminal_first_session
    viewport: ViewportSize = {"width": width, "height": 844}
    page.set_viewport_size(viewport)
    page.goto(f"{base_url}/c/{session_id}")
    _assert_pill_contains_painted_controls(page, width, drop_kebab=True)
