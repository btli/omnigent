import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { TerminalSurface } from "./ChatPage";

afterEach(cleanup);

describe("TerminalSurface", () => {
  it("keeps a hidden terminal laid out without exposing visible descendants", () => {
    const { container, rerender } = render(<TerminalSurface isShown={false} />);
    const surface = container.firstChild as HTMLElement;

    expect(surface).toHaveClass("opacity-0", "pointer-events-none");
    expect(surface).not.toHaveClass("invisible");
    expect(surface).toHaveAttribute("inert");
    expect(surface).toHaveAttribute("aria-hidden", "true");

    rerender(<TerminalSurface isShown />);

    expect(surface).not.toHaveClass("opacity-0", "pointer-events-none", "invisible");
    expect(surface).not.toHaveAttribute("inert");
    expect(surface).toHaveAttribute("aria-hidden", "false");
  });
});
