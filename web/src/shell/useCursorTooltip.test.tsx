import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setEmbedRoot } from "@/lib/host";
import { useCursorTooltip } from "./useCursorTooltip";

afterEach(() => {
  cleanup();
  setEmbedRoot(null);
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// Mirrors a virtualized FolderTree row: the wrapper's transform would become
// the containing block for a fixed tooltip rendered inside it.
function TransformedRow({ text = "folder/file.ts" }: { text?: string }) {
  const { handlers, tooltip } = useCursorTooltip(text);

  return (
    <div data-testid="row" style={{ transform: "translateY(600px)" }}>
      <span {...handlers}>file.ts</span>
      {tooltip}
    </div>
  );
}

// jsdom has no layout: fix the viewport (the root's client box) and give only
// the element holding exactly the tooltip text a box, so measuring any other
// node reads 0x0. Mutate the returned size to simulate a re-wrap.
function stubLayout(viewport: [number, number], tooltip: [number, number]) {
  vi.spyOn(document.documentElement, "clientWidth", "get").mockReturnValue(viewport[0]);
  vi.spyOn(document.documentElement, "clientHeight", "get").mockReturnValue(viewport[1]);
  const size = { width: tooltip[0], height: tooltip[1] };
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
    this: HTMLElement,
  ) {
    const { width, height } =
      this.textContent === "folder/file.ts" ? size : { width: 0, height: 0 };
    return {
      width,
      height,
      top: 0,
      left: 0,
      right: width,
      bottom: height,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect;
  });
  return size;
}

function hoverAt(clientX: number, clientY: number) {
  render(<TransformedRow />);
  fireEvent.mouseMove(screen.getByText("file.ts"), { clientX, clientY });
  return screen.getByText("folder/file.ts");
}

describe("useCursorTooltip", () => {
  it("portals the hovered tooltip to the body and removes it on mouse leave", () => {
    stubLayout([1000, 800], [200, 30]);
    render(<TransformedRow />);

    const row = screen.getByTestId("row");
    const label = screen.getByText("file.ts");
    expect(screen.queryByText("folder/file.ts")).not.toBeInTheDocument();

    fireEvent.mouseMove(label, { clientX: 120, clientY: 200 });

    const tooltip = screen.getByText("folder/file.ts");
    expect(tooltip.parentElement).toBe(document.body);
    expect(row).not.toContainElement(tooltip);
    expect(tooltip).toHaveStyle({ position: "fixed", left: "120px", top: "214px" });

    fireEvent.mouseLeave(label);
    expect(screen.queryByText("folder/file.ts")).not.toBeInTheDocument();
  });

  it("portals into the embed root when one is registered", () => {
    const embedRoot = document.createElement("div");
    document.body.appendChild(embedRoot);
    setEmbedRoot(embedRoot);
    render(<TransformedRow />);

    fireEvent.mouseMove(screen.getByText("file.ts"), { clientX: 120, clientY: 200 });

    expect(screen.getByText("folder/file.ts").parentElement).toBe(embedRoot);
  });

  // Viewport 1000x800 and a 200x30 box unless noted; margin 8, 14 below, 8 above.
  it.each([
    { name: "above the pointer near the bottom edge", pointer: [120, 780], left: 120, top: 742 },
    { name: "left of the pointer near the right edge", pointer: [900, 200], left: 700, top: 214 },
    { name: "above and left in the bottom-right corner", pointer: [900, 780], left: 700, top: 742 },
  ])("flips $name", ({ pointer, left, top }) => {
    stubLayout([1000, 800], [200, 30]);

    expect(hoverAt(pointer[0], pointer[1])).toHaveStyle({ left: `${left}px`, top: `${top}px` });
  });

  it("never starts past the left edge when it fits on neither side", () => {
    stubLayout([300, 800], [250, 30]);

    expect(hoverAt(150, 200)).toHaveStyle({ left: "8px" });
  });

  it("never starts past the top edge when it fits neither above nor below", () => {
    stubLayout([1000, 40], [200, 30]);

    expect(hoverAt(120, 20)).toHaveStyle({ top: "8px" });
  });

  it("sizes to its text, capped at the viewport, so it does not squeeze near an edge", () => {
    stubLayout([1000, 800], [200, 30]);

    const tooltip = hoverAt(900, 200);

    expect(tooltip).toHaveClass("w-max");
    expect(tooltip).not.toHaveClass("w-fit");
    expect(tooltip).toHaveStyle({ maxWidth: "984px" });
    // A path wider than the cap has no break points; it must still wrap inside the box.
    expect(tooltip).toHaveClass("wrap-anywhere");
  });

  it("measures the viewport without a classic scrollbar", () => {
    stubLayout([1000, 800], [200, 30]);
    // A 17px classic scrollbar: the window is wider than the usable viewport.
    vi.stubGlobal("innerWidth", 1017);

    const tooltip = hoverAt(795, 200);

    expect(tooltip).toHaveStyle({ left: "595px", maxWidth: "984px" });
  });

  it("re-measures while shown, so a re-wrap moves it off the edge", () => {
    const size = stubLayout([1000, 800], [200, 30]);
    const tooltip = hoverAt(700, 200);
    expect(tooltip).toHaveStyle({ left: "700px" });

    // Same text, bigger box (e.g. after a font loads): the next render must flip.
    size.width = 400;
    fireEvent.mouseMove(screen.getByText("file.ts"), { clientX: 701, clientY: 200 });

    expect(tooltip).toHaveStyle({ left: "301px" });
  });

  it("re-measures when the text changes under a still pointer", () => {
    stubLayout([1000, 800], [200, 30]);
    const { rerender } = render(<TransformedRow text="a.ts" />);
    fireEvent.mouseMove(screen.getByText("file.ts"), { clientX: 850, clientY: 200 });
    expect(screen.getByText("a.ts")).toHaveStyle({ left: "850px" });

    // Only "folder/file.ts" has a box in the stub, so the longer text must flip it.
    rerender(<TransformedRow text="folder/file.ts" />);

    expect(screen.getByText("folder/file.ts")).toHaveStyle({ left: "650px" });
  });
});
