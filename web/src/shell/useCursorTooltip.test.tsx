import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { setEmbedRoot } from "@/lib/host";
import { useCursorTooltip } from "./useCursorTooltip";

afterEach(() => {
  cleanup();
  setEmbedRoot(null);
  document.body.replaceChildren();
});

// Mirrors a virtualized FolderTree row: the wrapper's transform would become
// the containing block for a fixed tooltip rendered inside it.
function TransformedRow() {
  const { handlers, tooltip } = useCursorTooltip("folder/file.ts");

  return (
    <div data-testid="row" style={{ transform: "translateY(600px)" }}>
      <span {...handlers}>file.ts</span>
      {tooltip}
    </div>
  );
}

describe("useCursorTooltip", () => {
  it("portals the hovered tooltip to the body and removes it on mouse leave", () => {
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
});
