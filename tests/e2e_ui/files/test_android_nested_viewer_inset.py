"""Browser regression coverage for the Android injected safe-area styles."""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Page

from tests.e2e_ui.mobile._android_bridge import android_bridge_script, emit_android_insets

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WEB_CSS = _REPO_ROOT / "web/src/index.css"


def native_panel_rule() -> str:
    """Return the SPA rule that pads full-height panels on native shells, from ``index.css``.

    :returns: The complete rule, selector through closing brace, comments stripped.
    """
    css = re.sub(r"/\*.*?\*/", "", _WEB_CSS.read_text(encoding="utf-8"), flags=re.S)
    match = re.search(
        r":is\(\[data-ios-native\], \[data-android-native\]\)\s*"
        r':is\(\s*aside\[aria-label="Workspace"\],[^{}]*\{[^{}]*\}',
        css,
    )
    assert match, f"native panel rule not found in {_WEB_CSS}"
    return match.group(0)


def test_android_insets_skip_viewer_nested_in_workspace(page: Page) -> None:
    """The Workspace owns its insets; standalone viewers keep theirs."""
    page.set_viewport_size({"width": 1000, "height": 700})
    page.set_content(
        """<!doctype html>
        <html data-android-native>
          <head><style>
            body { margin: 0; font: 16px sans-serif; }
            #open-file { height: 36px; }
            [role=tablist] { height: 44px; background: #ddd; }
            [data-testid=file-viewer] { height: 260px; background: #fff; }
            [data-testid=file-viewer] header { height: 48px; background: #eee; }
          </style></head>
          <body>
            <button
              id="open-file"
              onclick="document.querySelector('[data-testid=file-viewer]').hidden=false"
            >Open report.md</button>
            <aside aria-label="Workspace">
              <div id="workspace-tabs" role="tablist"><button role="tab">Files</button></div>
              <section data-testid="file-viewer" hidden>
                <header data-testid="file-viewer-header">report.md · Download · Comment</header>
              </section>
            </aside>
          </body>
        </html>"""
    )

    web_inset_style = page.add_style_tag(content=native_panel_rule())
    page.add_script_tag(content=android_bridge_script())
    emit_android_insets(page, 52, 20)

    page.locator("#open-file").click()

    wide = page.evaluate(
        """() => {
          const workspace = document.querySelector('aside[aria-label="Workspace"]');
          const tabs = document.querySelector('#workspace-tabs');
          const viewer = workspace.querySelector('[data-testid="file-viewer"]');
          const header = viewer.querySelector('[data-testid="file-viewer-header"]');
          return {
            headerTop: header.getBoundingClientRect().top,
            tabsBottom: tabs.getBoundingClientRect().bottom,
            workspaceTop: getComputedStyle(workspace).paddingTop,
            workspaceBottom: getComputedStyle(workspace).paddingBottom,
            viewerTop: getComputedStyle(viewer).paddingTop,
            viewerBottom: getComputedStyle(viewer).paddingBottom,
          };
        }"""
    )
    assert wide["headerTop"] == wide["tabsBottom"]
    assert wide["workspaceTop"] == "52px"
    assert wide["workspaceBottom"] == "20px"
    assert wide["viewerTop"] == "0px"
    assert wide["viewerBottom"] == "0px"

    page.set_viewport_size({"width": 390, "height": 844})
    page.locator('aside[aria-label="Workspace"]').evaluate("el => el.remove()")
    web_inset_style.evaluate("style => style.remove()")
    page.locator("body").evaluate(
        "body => body.insertAdjacentHTML('beforeend', "
        '\'<section data-testid="file-viewer"><header '
        'data-testid="file-viewer-header">report.md</header></section>\')'
    )
    narrow = page.evaluate(
        """() => {
          const viewer = document.querySelector('[data-testid="file-viewer"]');
          const header = viewer.querySelector('[data-testid="file-viewer-header"]');
          return {
            headerOffset: header.getBoundingClientRect().top - viewer.getBoundingClientRect().top,
            top: getComputedStyle(viewer).paddingTop,
            bottom: getComputedStyle(viewer).paddingBottom,
          };
        }"""
    )
    assert narrow["headerOffset"] == 52
    assert narrow["top"] == "52px"
    assert narrow["bottom"] == "20px"
