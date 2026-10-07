"""Browser regression coverage for the Android injected safe-area styles."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

from playwright.sync_api import Page

_REPO_ROOT = Path(__file__).resolve().parents[3]
_NATIVE_SCRIPT = (
    _REPO_ROOT / "web/android/app/src/main/java/ai/omnigent/android/NativeBridgeScript.kt"
)
_WEB_CSS = _REPO_ROOT / "web/src/index.css"


def test_android_insets_skip_viewer_nested_in_workspace(page: Page) -> None:
    """The Workspace owns its insets; standalone viewers keep theirs."""
    page.set_viewport_size({"width": 1000, "height": 700})
    page.set_content(
        """<!doctype html>
        <html data-android-native>
          <head><style>
            :root { --omnigent-safe-top: 52px; --omnigent-safe-bottom: 20px; }
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

    css = _WEB_CSS.read_text(encoding="utf-8")
    rule_start = css.index("/* Full-height native surfaces")
    selector_start = css.index(":is([data-ios-native], [data-android-native])", rule_start)
    rule_end = css.index("}", selector_start) + 1
    web_inset_style = page.add_style_tag(content=css[selector_start:rule_end])

    kotlin = _NATIVE_SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r"val source: String =\s*\"\"\"\n(?P<script>.*?)\n\s*\"\"\"\.trimIndent\(\)",
        kotlin,
        re.DOTALL,
    )
    assert match is not None, "could not extract NativeBridgeScript.source"
    script = textwrap.dedent(match.group("script"))
    script = script.replace("${OmnigentBridgeListener.JS_OBJECT_NAME}", "omnigentNativeBridge")
    script = script.replace("${'$'}", "$")
    page.add_script_tag(content=script)
    page.evaluate("window.__omnigentNativeEmitInsets(52, 20)")

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
