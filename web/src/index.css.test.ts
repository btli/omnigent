/// <reference types="node" />
// Node types via explicit reference: the app tsconfig is browser-only, and
// importing index.css?raw instead yields "" under vitest's CSS stubbing.
import { readFileSync } from "node:fs";
// lightningcss is the minifier @tailwindcss/vite runs during `vite build`
// (resolved from its dependency tree, so we test the version the build uses).
import { createElement } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { transform } from "lightningcss";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { ChangedSort } from "./shell/FlatFileList";
import { WorkspacePanel } from "./shell/WorkspacePanel";

vi.mock("./shell/FileViewer", () => ({ FileViewer: () => null }));
vi.mock("./shell/FilesPanel", () => ({ FilesPanel: () => null }));
vi.mock("./shell/InlineTerminalsSection", () => ({
  InlineTerminalsSection: () => null,
}));
vi.mock("./shell/SubagentsPanel", () => ({ SubagentsPanel: () => null }));
vi.mock("./shell/TodoPanel", () => ({ TodoPanel: () => null }));
vi.mock("@/components/BrowserPane/BrowserPane", () => ({
  BrowserPane: () => null,
}));

afterEach(cleanup);

// Relative to the vitest root (web/) — import.meta.url is not a file://
// URL inside vitest's module graph, so it can't locate the file.
const cssSource = readFileSync("src/index.css", "utf8");
const nativeBridgeSource = readFileSync(
  "android/app/src/main/java/ai/omnigent/android/NativeBridgeScript.kt",
  "utf8",
);

/* Regression test for the "transparent dropdown in prod" bug.
 *
 * Dark mode renders popovers/cards with a semi-transparent background that
 * relies on `backdrop-filter` glass rules in index.css. LightningCSS
 * collapses an unprefixed + `-webkit-` declaration pair into a single
 * logical declaration, keeping only the LAST one written. With the
 * unprefixed property first, the built CSS ended up with only
 * `-webkit-backdrop-filter` — which Chrome ignores — so menus turned
 * see-through in `npm run build` output while `npm run dev` looked fine.
 *
 * This test minifies the actual glass rules from index.css the same way
 * the build does and fails if either form of backdrop-filter is lost.
 */

// Tailwind v4 browser baseline (Safari 16.4, Chrome 111, Firefox 128),
// mirroring the targets the build minifies against. Safari <18 needs the
// -webkit- prefix for backdrop-filter; Chrome/Firefox need it unprefixed.
const TARGETS = {
  safari: (16 << 16) | (4 << 8),
  chrome: 111 << 16,
  firefox: 128 << 16,
};

// Matches `backdrop-filter:` declarations but not `-webkit-backdrop-filter:`.
const UNPREFIXED_DECL = /(?<![-\w])backdrop-filter\s*:/;
const WEBKIT_DECL = /-webkit-backdrop-filter\s*:/;

/** Innermost `selector { ... }` blocks that declare backdrop-filter. */
function extractBackdropFilterRules(css: string): string[] {
  const blocks = css.match(/[^{}]+\{[^{}]*\}/g) ?? [];
  // Require a `:` so blocks that merely mention backdrop-filter in a
  // comment (e.g. the dark-token block) are not treated as glass rules.
  return blocks.filter((block) => UNPREFIXED_DECL.test(block));
}

describe("index.css backdrop-filter glass rules", () => {
  const rules = extractBackdropFilterRules(cssSource);

  it("has the glass rules this test exists to protect", () => {
    // 2 today: the bg-card frosted surfaces and the popover/menu rule.
    // 0 or 1 means a rule was removed/renamed — update or delete this test.
    expect(rules.length).toBeGreaterThanOrEqual(2);
  });

  it.each(rules.map((rule) => [rule.trim().slice(0, 60), rule] as const))(
    "keeps both backdrop-filter forms after build minification: %s",
    (_label, rule) => {
      const minified = new TextDecoder().decode(
        transform({
          filename: "index.css",
          code: new TextEncoder().encode(rule),
          minify: true,
          targets: TARGETS,
        }).code,
      );

      // Chrome/Firefox only honor the unprefixed property. Losing it is the
      // exact prod-only transparency bug: LightningCSS keeps the last of a
      // prefixed/unprefixed pair, so `-webkit-` must be declared FIRST.
      expect(minified, "unprefixed backdrop-filter was dropped by minification").toMatch(
        UNPREFIXED_DECL,
      );
      // Safari 16.4-17 only honor the -webkit- form; it must survive too.
      expect(minified, "-webkit-backdrop-filter was dropped by minification").toMatch(WEBKIT_DECL);
    },
  );
});

/* Regression test for the "page gets wider when the kebab menu opens" bug.
 *
 * The bg-card glass rule used to exclude `[aria-hidden="true"]` to skip
 * visually collapsed panels. But Radix's modal a11y hiding sets
 * aria-hidden="true" on the OPEN sidebar while a menu/dialog is up, which
 * dropped the rule's 1px border and reflowed every sidebar row 2px wider
 * (titles gained a character). The rule now keys on `data-collapsed`,
 * which only the panels themselves set. This test runs the actual selector
 * from index.css against a real DOM to pin that contract.
 */
describe("index.css bg-card glass rule selector", () => {
  // The selector of the rule declaring the bg-card glass border/blur.
  const cardRule = extractBackdropFilterRules(cssSource).find((rule) => rule.includes(".bg-card"))!;
  // Strip comments preceding the selector in the extracted block.
  const selector = cardRule
    .slice(0, cardRule.indexOf("{"))
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .trim();

  function makeAside(): HTMLElement {
    const dark = document.createElement("div");
    dark.className = "dark";
    const aside = document.createElement("aside");
    aside.className = "conversations-sidebar flex flex-col bg-card";
    dark.appendChild(aside);
    document.body.appendChild(dark);
    return aside;
  }

  it("matches an open bg-card panel even while Radix marks it aria-hidden", () => {
    const aside = makeAside();
    // Open panel: glass border applies.
    expect(aside.matches(selector)).toBe(true);
    // Radix hideOthers sets aria-hidden="true" on open panels whenever a
    // modal menu/dialog is up. The glass styling must NOT react to it —
    // if this fails, opening the session kebab menu drops the sidebar's
    // 1px border again and every row reflows 2px wider.
    aside.setAttribute("aria-hidden", "true");
    expect(aside.matches(selector)).toBe(true);
    aside.remove();
  });

  it("stops matching when the panel marks itself collapsed", () => {
    const aside = makeAside();
    // Closed panels (w-0) set data-collapsed; the glass border/shadow must
    // not paint them as a glowing strip along the screen edge.
    aside.setAttribute("data-collapsed", "true");
    expect(aside.matches(selector)).toBe(false);
    aside.remove();
  });
});

const cssBlocks = [...cssSource.matchAll(/[^{}]+\{[^{}]*\}/g)];
const workspaceAttribute = /\[\s*aria-label\s*=\s*("Workspace"|'Workspace'|Workspace)\s*\]/;
const workspaceRules = cssBlocks.filter(([block]) => workspaceAttribute.test(block));
const workspaceSafeAreaRules = workspaceRules.filter(([block]) =>
  block.includes("--omnigent-safe-top"),
);
const workspaceSafeAreaRule = workspaceSafeAreaRules[0]?.[0] ?? "";
const workspaceSelector = workspaceSafeAreaRule
  .slice(0, workspaceSafeAreaRule.indexOf("{"))
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .trim();
const workspaceCss = workspaceRules.map(([block]) => block).join("\n");
const fullHeightPanelRule =
  cssBlocks.find(
    ([block]) => block.includes(".conversations-sidebar") && block.includes("--omnigent-safe-top"),
  )?.[0] ?? "";
const rootSafeAreaRule =
  cssBlocks.find(
    ([block]) => block.includes(":root") && block.includes("--omnigent-safe-top"),
  )?.[0] ?? "";

const nativeInsetMatch = nativeBridgeSource.match(
  /internal val insetStyles: String\s*=\s*"""([\s\S]*?)"""\.trimIndent\(\)/,
);
const nativeInsetCss = trimIndent(nativeInsetMatch?.[1] ?? "");

const FULL_HEIGHT_PANEL_TARGETS = [
  { className: "conversations-sidebar" },
  { testId: "execution-logs-panel" },
  { testId: "file-viewer" },
  { testId: "files-panel-drawer" },
  { testId: "terminals-panel" },
  { testId: "subagents-panel-drawer" },
  { testId: "todos-panel-drawer" },
] as const;

function trimIndent(value: string): string {
  const lines = value
    .replace(/^\n/, "")
    .replace(/\n\s*$/, "")
    .split("\n");
  const indents = lines
    .filter((line) => line.trim())
    .map((line) => line.match(/^\s*/)?.[0].length ?? 0);
  const indent = Math.min(...indents);
  return lines.map((line) => line.slice(indent)).join("\n");
}

function renderWorkspace(platform: "android" | "ios"): HTMLElement {
  render(
    createElement(
      "div",
      { [`data-${platform}-native`]: "" },
      createElement(
        TooltipProvider,
        { delayDuration: 0 },
        createElement(WorkspacePanel, {
          conversationId: "safe-area-test",
          width: 360,
          handleProps: { tabIndex: 0 },
          rightRailTab: "files",
          onRightRailTabChange: vi.fn(),
          showFilesPanel: true,
          showBrowserTab: false,
          changedCount: 0,
          showShellsTab: false,
          terminalsLength: 0,
          subagentsWorking: 0,
          agentCount: 1,
          todosSupported: false,
          todosCompleted: 0,
          todosTotal: 0,
          rootSessionId: null,
          selectedFilePath: null,
          openFiles: [],
          openFileViewer: vi.fn(),
          onCloseFile: vi.fn(),
          onShowScopeView: vi.fn(),
          onCommentsOpenChange: vi.fn(),
          openTerminalsPanel: vi.fn(),
          permissionLevel: null,
          filesPanelSort: "recent" as ChangedSort,
          onSortChange: vi.fn(),
          filesPanelFlatView: false,
          onFlatViewChange: vi.fn(),
          filesPanelShowHidden: false,
          onShowHiddenChange: vi.fn(),
        }),
      ),
    ),
  );
  return screen.getByRole("complementary", { name: "Workspace" });
}

function assertWorkspacePadding(
  css: string,
  platform: "android" | "ios",
  variableFallback = "",
): void {
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);
  try {
    const computed = getComputedStyle(renderWorkspace(platform));
    expect(computed.paddingTop).toBe(`var(--omnigent-safe-top${variableFallback})`);
    expect(computed.paddingBottom).toBe(`var(--omnigent-safe-bottom${variableFallback})`);
    expect(computed.paddingLeft).toBe(`var(--omnigent-safe-left${variableFallback})`);
    expect(computed.paddingRight).toBe(`var(--omnigent-safe-right${variableFallback})`);
  } finally {
    style.remove();
    cleanup();
  }
}

function assertFullHeightPanelPadding(css: string, variableFallback = ""): void {
  const style = document.createElement("style");
  style.textContent = css;
  const shell = document.createElement("div");
  shell.setAttribute("data-android-native", "");
  document.head.appendChild(style);
  document.body.appendChild(shell);
  try {
    for (const target of FULL_HEIGHT_PANEL_TARGETS) {
      const panel = document.createElement("div");
      if ("className" in target) panel.className = target.className;
      if ("testId" in target) panel.dataset.testid = target.testId;
      shell.appendChild(panel);
      const computed = getComputedStyle(panel);
      expect(computed.paddingTop).toBe(`var(--omnigent-safe-top${variableFallback})`);
      expect(computed.paddingBottom).toBe(`var(--omnigent-safe-bottom${variableFallback})`);
      expect(computed.paddingLeft).toBe(`var(--omnigent-safe-left${variableFallback})`);
      expect(computed.paddingRight).toBe(`var(--omnigent-safe-right${variableFallback})`);
    }
  } finally {
    shell.remove();
    style.remove();
  }
}

/** Brace nesting depth at a source index; 0 is top level. */
function braceDepthAt(sourceIndex: number): number {
  let depth = 0;
  let quote: '"' | "'" | undefined;

  for (let index = 0; index < sourceIndex; index += 1) {
    const character = cssSource[index];
    if (quote) {
      if (character === "\\") index += 1;
      else if (character === quote) quote = undefined;
    } else if (character === "/" && cssSource[index + 1] === "*") {
      const commentEnd = cssSource.indexOf("*/", index + 2);
      index = commentEnd === -1 ? sourceIndex : commentEnd + 1;
    } else if (character === '"' || character === "'") {
      quote = character;
    } else if (character === "{") depth += 1;
    else if (character === "}") depth -= 1;
  }
  return depth;
}

describe("index.css native safe-area layout", () => {
  it("folds Android and browser safe areas on both lateral edges", () => {
    const style = document.createElement("style");
    style.textContent = rootSafeAreaRule;
    document.head.appendChild(style);
    try {
      const computed = getComputedStyle(document.documentElement);
      expect(computed.getPropertyValue("--omnigent-safe-left")).toContain(
        "--omnigent-android-safe-area-left",
      );
      expect(computed.getPropertyValue("--omnigent-safe-right")).toContain(
        "--omnigent-android-safe-area-right",
      );
    } finally {
      style.remove();
    }
  });

  it("keeps the Workspace rule at the top level", () => {
    expect(workspaceSafeAreaRule, "the Workspace safe-area rule is gone").not.toBe("");
    expect(workspaceSafeAreaRules).toHaveLength(1);
    expect(braceDepthAt(workspaceSafeAreaRules[0]?.index ?? -1)).toBe(0);
  });

  it.each(["android", "ios"] as const)(
    "computes four-edge padding on the real Workspace panel in the %s shell",
    (platform) => assertWorkspacePadding(workspaceCss, platform),
  );

  it("computes four-edge padding on every full-height native panel", () => {
    assertFullHeightPanelPadding(fullHeightPanelRule);
  });

  it("fails its layout contract when a later padding shorthand wins", () => {
    expect(workspaceSelector).not.toBe("");
    expect(() =>
      assertWorkspacePadding(`${workspaceCss}\n${workspaceSelector}{padding:0}`, "android"),
    ).toThrow();
  });

  it("does not double-count app-owned bar footprints", () => {
    expect(workspaceSafeAreaRule).not.toMatch(/--omnigent-(?:inset|native)-/);
  });
});

describe("Android injected safe-area layout", () => {
  it("exposes the production inset stylesheet to the layout test", () => {
    expect(nativeInsetMatch, "NativeBridgeScript.insetStyles is gone").not.toBeNull();
    expect(nativeInsetCss).not.toBe("");
  });

  it("computes four-edge padding on the real Workspace panel", () => {
    assertWorkspacePadding(nativeInsetCss, "android", ", 0px");
  });

  it("computes four-edge padding on every full-height panel", () => {
    assertFullHeightPanelPadding(nativeInsetCss, ", 0px");
  });
});

/* Regression test for the "table link column collapses to ~2ch" bug.
 *
 * Streamdown styles links with `wrap-anywhere`, which also drops the
 * element's min-content width to one character. Inside its auto-layout
 * table that let a link-only column ("#3090") be squeezed to ~2ch and
 * stack one character per line. index.css narrows links in table cells
 * back to `break-word`; this pins the selector so the override keeps
 * applying to cells only, and never leaks into prose links.
 */
describe("index.css table link wrapping rule", () => {
  const rule = (cssSource.match(/[^{}]+\{[^{}]*\}/g) ?? []).find(
    (block) => block.includes('[data-streamdown="table-cell"]') && /overflow-wrap\s*:/.test(block),
  );

  // Derived lazily: a missing rule must fail the assertions below with a
  // readable message, not crash at collection time.
  const selector = (rule ?? "")
    .slice(0, rule ? rule.indexOf("{") : 0)
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .trim();

  it("has the rule this test exists to protect", () => {
    expect(rule, "the table-cell link wrapping rule is gone from index.css").toBeDefined();
    expect(rule).toMatch(/overflow-wrap\s*:\s*break-word/);
  });

  function makeLink(cellAttr: string | null): HTMLElement {
    const host = document.createElement("div");
    if (cellAttr) host.setAttribute("data-streamdown", cellAttr);
    const link = document.createElement("a");
    link.setAttribute("data-streamdown", "link");
    link.className = "wrap-anywhere";
    host.appendChild(link);
    document.body.appendChild(host);
    return link;
  }

  it.each(["table-cell", "table-header-cell"])("targets links inside a %s", (cellAttr) => {
    const link = makeLink(cellAttr);
    expect(link.matches(selector)).toBe(true);
    link.parentElement?.remove();
  });

  it("leaves links outside table cells on Streamdown's wrap-anywhere", () => {
    // Prose links must keep `anywhere` so a bare overlong URL in a
    // paragraph still breaks mid-token instead of overflowing.
    const link = makeLink(null);
    expect(link.matches(selector)).toBe(false);
    link.parentElement?.remove();
  });
});
