"""E2E coverage for the mobile header pill's focused kebab geometry."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle

_VIEWPORT = {"width": 390, "height": 844}


@pytest.fixture
def chat_first_session(
    live_server: str,
) -> Iterator[tuple[str, str]]:
    """Create an owner-managed session without terminal-first metadata."""
    create = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                _build_hello_world_bundle(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


_FOCUSED_RING_GEOMETRY_JS = r"""
() => {
  const trigger = document.querySelector('[data-testid="header-conversation-actions"]');
  if (!(trigger instanceof HTMLElement) || !trigger.parentElement) return null;

  const pill = trigger.parentElement;
  const pillRect = pill.getBoundingClientRect();
  const triggerRect = trigger.getBoundingClientRect();
  const pillRadius = Math.min(pillRect.width, pillRect.height) / 2;
  const triggerRadius = Math.min(
    parseFloat(getComputedStyle(trigger).borderTopLeftRadius),
    triggerRect.width / 2,
    triggerRect.height / 2,
  );
  const shadow = getComputedStyle(trigger).boxShadow;
  const ringLayer = shadow
    .split(/,(?![^()]*\))/)
    .map((layer) => ({
      layer,
      lengths: [...layer.matchAll(/(-?[0-9.]+)px/g)].map((match) => Number(match[1])),
    }))
    .find(({ lengths }) => lengths.length >= 4 && lengths[3] > 0);
  if (!ringLayer) return { shadow, error: 'focus ring shadow was not painted' };

  const ringWidth = ringLayer.lengths[3];
  const inset = /\binset\b/.test(ringLayer.layer);
  const paintedRadius = triggerRadius + (inset ? 0 : ringWidth);
  const cx = (triggerRect.left + triggerRect.right) / 2;
  const cy = (triggerRect.top + triggerRect.bottom) / 2;
  const capCx = pillRect.right - pillRadius;
  const capCy = (pillRect.top + pillRect.bottom) / 2;
  let worst = -Infinity;
  for (let degree = 0; degree < 360; degree += 1) {
    const angle = degree * Math.PI / 180;
    const x = cx + paintedRadius * Math.cos(angle);
    const y = cy + paintedRadius * Math.sin(angle);
    const outside = Math.hypot(x - capCx, y - capCy) - pillRadius;
    worst = Math.max(worst, outside);
  }

  return {
    focused: document.activeElement === trigger,
    focusVisible: trigger.matches(':focus-visible'),
    inset,
    ringWidth,
    shadow,
    worst,
  };
}
"""


def test_focused_kebab_ring_stays_inside_mobile_pill(
    browser: Browser,
    chat_first_session: tuple[str, str],
) -> None:
    """Outside-dismiss refocuses the kebab without crossing the pill cap."""
    base_url, session_id = chat_first_session
    context = browser.new_context(
        viewport=_VIEWPORT,
        device_scale_factor=3,
        has_touch=True,
    )
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}")
        trigger = page.get_by_role("button", name="Conversation actions")
        expect(trigger).to_be_visible(timeout=30_000)
        expect(page.get_by_test_id("view-mode-toggle")).to_have_count(0)

        trigger.tap()
        menu = page.get_by_role("menu")
        expect(menu).to_be_visible()
        page.touchscreen.tap(20, 200)
        expect(menu).to_be_hidden()
        expect(trigger).to_be_focused()
        page.wait_for_timeout(250)

        result = page.evaluate(_FOCUSED_RING_GEOMETRY_JS)
        assert result is not None, "the mobile conversation kebab did not render"
        assert "error" not in result, result.get("error")
        assert result["focused"] and result["focusVisible"], (
            "outside-dismiss did not leave a visible focus indicator on the kebab"
        )
        assert result["ringWidth"] >= 3, "the focus indicator became narrower than 3px"
        assert result["worst"] <= 0.25, (
            f"the {result['ringWidth']:.1f}px focused kebab ring crosses the pill's "
            f"rounded trailing cap by {result['worst']:.1f}px "
            f"(inset={result['inset']}, shadow={result['shadow']})"
        )
    finally:
        context.close()
