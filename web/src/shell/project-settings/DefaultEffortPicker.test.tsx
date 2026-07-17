import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { effortOptionsForHarness } from "@/lib/harnessCatalog";

import { DefaultEffortPicker } from "./DefaultEffortPicker";

afterEach(cleanup);

describe("DefaultEffortPicker", () => {
  it("renders effort options for the harness and model and emits a selection", () => {
    const onChange = vi.fn();
    render(
      <DefaultEffortPicker
        value="high"
        provenance="inherited"
        harness="claude-native"
        model="sonnet"
        catalog={effortOptionsForHarness("claude-native")}
        onChange={onChange}
        onReset={vi.fn()}
      />,
    );

    fireEvent.pointerDown(screen.getByTestId("project-default-reasoning_effort-control"), {
      button: 0,
    });
    expect(
      screen.getByTestId("project-default-reasoning_effort-option-xhigh"),
    ).toHaveTextContent("xHigh");
    fireEvent.click(screen.getByTestId("project-default-reasoning_effort-option-xhigh"));
    expect(onChange).toHaveBeenCalledWith("xhigh");
  });

  it("gates selection until a model is chosen", () => {
    render(
      <DefaultEffortPicker
        value=""
        provenance="inherited"
        harness="claude-native"
        model={null}
        catalog={effortOptionsForHarness("claude-native")}
        onChange={vi.fn()}
        onReset={vi.fn()}
      />,
    );

    expect(screen.getByTestId("project-default-reasoning_effort-control")).toBeDisabled();
    expect(screen.getByTestId("project-default-reasoning_effort-hint")).toHaveTextContent(
      "Choose a project model to select compatible effort",
    );
  });

  it("preserves a stored effort when no catalog is available", () => {
    render(
      <DefaultEffortPicker
        value="legacy-effort"
        provenance="overridden"
        harness="codex-native"
        model="gpt-5.4"
        catalog={[]}
        onChange={vi.fn()}
        onReset={vi.fn()}
      />,
    );

    expect(screen.getByTestId("project-default-reasoning_effort-control")).toBeDisabled();
    expect(screen.getByTestId("project-default-reasoning_effort-control")).toHaveTextContent(
      "legacy-effort (not in current catalog)",
    );
  });
});
