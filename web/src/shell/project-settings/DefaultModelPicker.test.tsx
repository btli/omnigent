import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { modelOptionsForHarness } from "@/lib/harnessCatalog";

import { DefaultModelPicker } from "./DefaultModelPicker";

afterEach(cleanup);

describe("DefaultModelPicker", () => {
  it("renders only the selected harness catalog and emits a model", () => {
    const onChange = vi.fn();
    render(
      <DefaultModelPicker
        value="sonnet"
        provenance="inherited"
        harness="claude-native"
        catalog={modelOptionsForHarness("claude-native")}
        onChange={onChange}
        onReset={vi.fn()}
      />,
    );

    fireEvent.pointerDown(screen.getByTestId("project-default-model-control"), { button: 0 });
    expect(screen.getByTestId("project-default-model-option-opus")).toHaveTextContent("Opus");
    fireEvent.click(screen.getByTestId("project-default-model-option-opus"));
    expect(onChange).toHaveBeenCalledWith("opus");
  });

  it("gates selection until a project harness is chosen", () => {
    render(
      <DefaultModelPicker
        value=""
        provenance="inherited"
        harness={null}
        catalog={[]}
        onChange={vi.fn()}
        onReset={vi.fn()}
      />,
    );

    expect(screen.getByTestId("project-default-model-control")).toBeDisabled();
    expect(screen.getByTestId("project-default-model-hint")).toHaveTextContent(
      "Choose a project harness to select a compatible model",
    );
  });

  it("preserves a stored value when the harness has no project catalog", () => {
    const onReset = vi.fn();
    render(
      <DefaultModelPicker
        value="legacy-model"
        provenance="overridden"
        harness="codex-native"
        catalog={[]}
        onChange={vi.fn()}
        onReset={onReset}
      />,
    );

    expect(screen.getByTestId("project-default-model-control")).toBeDisabled();
    expect(screen.getByTestId("project-default-model-control")).toHaveTextContent(
      "legacy-model (not in current catalog)",
    );
    fireEvent.click(screen.getByTestId("project-default-model-reset"));
    expect(onReset).toHaveBeenCalledTimes(1);
  });
});
