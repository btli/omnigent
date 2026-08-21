import XCTest

@testable import Omnigent

final class WorkspaceChromeScriptTests: XCTestCase {
  /// The rule must key on Omnigent's own embed root, not on the monolith-owned
  /// workspace nav markup, and must win over it (fixed + full-inset + top layer).
  func testCSSPromotesTheEmbedRootToAFullViewportOverlay() {
    XCTAssertEqual(
      WorkspaceChromeScript.css,
      """
      .omnigent-app {
        position: fixed !important;
        inset: 0 !important;
        z-index: 2147483647 !important;
      }
      """,
      "Keep this identical to WORKSPACE_CHROME_HIDE_CSS in "
        + "web/electron/src/workspace-chrome.js and to WorkspaceChromeScript.kt."
    )
  }

  /// The script is injected on every finished load, so re-running it on a document
  /// that already has the stylesheet must not stack duplicate <style> nodes.
  func testSourceInstallsTheStylesheetAtMostOncePerDocument() {
    let source = WorkspaceChromeScript.source

    XCTAssertTrue(
      source.contains(
        """
        if (document.querySelector("style[data-omnigent-workspace-chrome]")) return;
        """))
    XCTAssertTrue(source.contains("document.documentElement.appendChild(style)"))
  }

  /// The CSS reaches the page as a JSON-encoded string literal, so a quote or
  /// newline in it can't break out of the surrounding script.
  func testSourceEmbedsTheCSSAsAnEscapedStringLiteral() {
    XCTAssertTrue(
      WorkspaceChromeScript.source.contains(
        WebViewModel.javascriptString(WorkspaceChromeScript.css))
    )
  }
}

final class NativeBridgeScriptTests: XCTestCase {
  func testServerPickerBridgeExposesOfferedServersAndNativeActions() {
    let source = OmnigentWebView.nativeBridgeScript(
      currentOrigin: "https://current.example.com",
      recentServers: [
        "https://managed.example.com/path",
        "https://quote.example.com/\"quoted\"",
      ]
    )

    XCTAssertTrue(source.contains("getServerPicker()"))
    XCTAssertTrue(source.contains("switchServer(url)"))
    XCTAssertTrue(source.contains("openServerSetup()"))
    XCTAssertTrue(source.contains("currentOrigin: pickerCurrentOrigin"))
    XCTAssertTrue(source.contains("recentServers: [...pickerRecentServers]"))
    XCTAssertTrue(source.contains("return Promise.reject(new Error("))
    XCTAssertTrue(source.contains(#"method: "switchServer""#))
    XCTAssertTrue(source.contains(#"method: "openServerSetup""#))
    XCTAssertTrue(source.contains("current.example.com"))
    XCTAssertTrue(source.contains(#"\"quoted\""#))
    XCTAssertFalse(source.contains("setServerSwitcherHidden"))
  }
}
