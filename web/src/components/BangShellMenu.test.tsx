// Rendering tests for the `!`-shell suggestions panel: section headers,
// ordering (shells above types), exited-row styling, the "(default)"
// type tag, listbox roles, active-row highlight, and click selection.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BangMenuRow } from "@/hooks/useBangShellMenu";
import { BangShellMenu } from "./BangShellMenu";

afterEach(cleanup);

const ROWS: BangMenuRow[] = [
  { kind: "shell", token: "u-ab12cd", name: "zsh", running: true, completable: true },
  { kind: "shell", token: "u-dead00", name: "bash", running: false, completable: false },
  { kind: "type", token: "zsh", name: "zsh", isDefault: true, completable: true },
  { kind: "type", token: "bash", name: "bash", isDefault: false, completable: true },
];

describe("BangShellMenu", () => {
  it("renders nothing for an empty row list", () => {
    render(<BangShellMenu rows={[]} activeIndex={-1} onSelect={() => {}} />);
    expect(screen.queryByTestId("bang-shell-menu")).toBeNull();
  });

  it("renders both section headers with shells above types", () => {
    render(<BangShellMenu rows={ROWS} activeIndex={0} onSelect={() => {}} />);
    const menu = screen.getByTestId("bang-shell-menu");
    const text = menu.textContent ?? "";
    expect(text.indexOf("Running shells")).toBeGreaterThanOrEqual(0);
    expect(text.indexOf("Running shells")).toBeLessThan(text.indexOf("New shell…"));
    const options = screen.getAllByRole("option");
    expect(options.map((o) => o.getAttribute("data-testid"))).toEqual([
      "bang-menu-item-u-ab12cd",
      "bang-menu-item-u-dead00",
      "bang-menu-item-zsh",
      "bang-menu-item-bash",
    ]);
  });

  it("omits a section header when that section has no rows", () => {
    render(
      <BangShellMenu
        rows={ROWS.filter((r) => r.kind === "shell")}
        activeIndex={0}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText("Running shells")).toBeDefined();
    expect(screen.queryByText("New shell…")).toBeNull();
  });

  it("dims exited shells, labels them, and blocks their selection", () => {
    const onSelect = vi.fn();
    render(<BangShellMenu rows={ROWS} activeIndex={0} onSelect={onSelect} />);
    const exited = screen.getByTestId("bang-menu-item-u-dead00");
    expect(exited.getAttribute("aria-disabled")).toBe("true");
    expect(exited.textContent).toContain("exited");
    fireEvent.click(exited);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("click-disables a non-round-trippable type row", () => {
    const onSelect = vi.fn();
    const unavailable: BangMenuRow = {
      kind: "type",
      token: "zsh",
      name: "zsh",
      completable: false,
    };
    render(<BangShellMenu rows={[unavailable]} activeIndex={-1} onSelect={onSelect} />);
    const row = screen.getByTestId("bang-menu-item-zsh") as HTMLButtonElement;
    expect(row.disabled).toBe(true);
    expect(row.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(row);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("tags the default type row", () => {
    render(<BangShellMenu rows={ROWS} activeIndex={0} onSelect={() => {}} />);
    expect(screen.getByTestId("bang-menu-item-zsh").textContent).toContain("(default)");
    expect(screen.getByTestId("bang-menu-item-bash").textContent).not.toContain("(default)");
  });

  it("marks the active row and calls onSelect with the clicked row", () => {
    const onSelect = vi.fn();
    render(<BangShellMenu rows={ROWS} activeIndex={2} onSelect={onSelect} />);
    expect(screen.getByTestId("bang-menu-item-zsh").getAttribute("data-active")).toBe("true");
    expect(screen.getByTestId("bang-menu-item-u-ab12cd").getAttribute("data-active")).toBeNull();
    fireEvent.click(screen.getByTestId("bang-menu-item-u-ab12cd"));
    expect(onSelect).toHaveBeenCalledWith(ROWS[0]);
  });

  it("shows the shell type as the running row's secondary label", () => {
    render(<BangShellMenu rows={ROWS} activeIndex={0} onSelect={() => {}} />);
    expect(screen.getByTestId("bang-menu-item-u-ab12cd").textContent).toContain("zsh");
  });
});
