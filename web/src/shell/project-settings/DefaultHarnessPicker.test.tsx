import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DefaultHarnessPicker } from "./DefaultHarnessPicker";

afterEach(cleanup);

describe("DefaultHarnessPicker", () => {
  it("renders catalog labels and emits a selection", () => {
    const onChange = vi.fn();
    render(
      <DefaultHarnessPicker
        value="claude-native"
        provenance="inherited"
        harnessOptions={[
          { id: "claude-native", label: "Claude Code" },
          { id: "codex-native", label: "Codex" },
        ]}
        onChange={onChange}
        onReset={vi.fn()}
      />,
    );

    fireEvent.pointerDown(screen.getByTestId("project-default-harness-control"), {
      button: 0,
    });
    expect(screen.getByTestId("project-default-harness-option-claude-native")).toHaveTextContent(
      "Claude Code",
    );
    fireEvent.click(screen.getByTestId("project-default-harness-option-codex-native"));
    expect(onChange).toHaveBeenCalledWith("codex-native");
  });

  it("preserves and resets an unknown harness id", () => {
    const onReset = vi.fn();
    render(
      <DefaultHarnessPicker
        value="legacy-harness"
        provenance="overridden"
        harnessOptions={[{ id: "claude-native", label: "Claude Code" }]}
        onChange={vi.fn()}
        onReset={onReset}
      />,
    );

    expect(screen.getByTestId("project-default-harness-control")).toHaveTextContent(
      "legacy-harness (not in current catalog)",
    );
    fireEvent.click(screen.getByTestId("project-default-harness-reset"));
    expect(onReset).toHaveBeenCalledTimes(1);
  });
});
