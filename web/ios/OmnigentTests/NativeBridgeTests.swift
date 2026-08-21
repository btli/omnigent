import WebKit
import XCTest

@testable import Omnigent

@MainActor
final class NativeBridgeTests: XCTestCase {
  func testGeneratedBridgeExecutesLivePickerRoundTripInWKWebView() async throws {
    let handler = MessageHandler()
    let configuration = WKWebViewConfiguration()
    configuration.userContentController.add(handler, name: "omnigentNative")
    let webView = WKWebView(frame: .zero, configuration: configuration)
    webView.loadHTMLString("<html><head></head><body></body></html>", baseURL: nil)
    try await Task.sleep(for: .milliseconds(100))

    _ = try await webView.evaluateJavaScript(OmnigentWebView.nativeBridgeScript())
    _ = try await webView.evaluateJavaScript("window.omnigentNative.nativeWebReady(1)")
    let promise = Task {
      try await webView.callAsyncJavaScript(
        "return await window.omnigentNative.getServerPicker();",
        arguments: [:],
        contentWorld: .page
      )
    }

    let request = try await handler.nextMessage(method: "getServerPicker")
    let requestID = try XCTUnwrap(request["requestId"] as? Int)
    _ = try await webView.evaluateJavaScript(
      "window.__omnigentNativeEmitServerPicker(\(requestID), {currentOrigin:'https://current.example.com',currentServerUrl:'https://current.example.com/app',managedServers:['https://managed.example.com'],recentServers:['https://recent.example.com']})"
    )
    let result = try await promise.value as? [String: Any]

    XCTAssertEqual(result?["currentServerUrl"] as? String, "https://current.example.com/app")
    XCTAssertTrue(handler.messages.contains { $0["method"] as? String == "nativeWebReady" })
  }
}

@MainActor
private final class MessageHandler: NSObject, WKScriptMessageHandler {
  var messages: [[String: Any]] = []

  func userContentController(
    _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
  ) {
    if let body = message.body as? [String: Any] { messages.append(body) }
  }

  func nextMessage(method: String) async throws -> [String: Any] {
    for _ in 0..<100 {
      if let message = messages.first(where: { $0["method"] as? String == method }) {
        return message
      }
      try await Task.sleep(for: .milliseconds(10))
    }
    throw CocoaError(.coderReadCorrupt)
  }
}
