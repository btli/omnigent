import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { TerminalSurface } from "./ChatPage";

afterEach(cleanup);

describe("TerminalSurface", () => {
  it("keeps a hidden terminal laid out without exposing visible descendants", () => {
    const { rerender } = render(
      <TerminalSurface isShown={false}>
        <div className="visible" />
      </TerminalSurface>,
    );
    const surface = screen.getByTestId("terminal-surface");

    expect(surface).toHaveClass("opacity-0", "pointer-events-none");
    expect(surface).not.toHaveClass("invisible");
    expect(surface).toHaveAttribute("inert");
    expect(surface).toHaveAttribute("aria-hidden", "true");

    rerender(
      <TerminalSurface isShown>
        <div className="visible" />
      </TerminalSurface>,
    );

    expect(surface).not.toHaveClass("opacity-0", "pointer-events-none", "invisible");
    expect(surface).not.toHaveAttribute("inert");
    expect(surface).toHaveAttribute("aria-hidden", "false");
  });
});
