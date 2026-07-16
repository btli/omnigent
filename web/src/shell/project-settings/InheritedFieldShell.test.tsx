import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InheritedFieldShell } from "./InheritedFieldShell";

afterEach(cleanup);

describe("InheritedFieldShell", () => {
  it("renders inherited provenance without a Reset action", () => {
    render(
      <InheritedFieldShell
        field="model"
        label="Model"
        provenance="inherited"
        onReset={vi.fn()}
        hint="Harness default"
      >
        <input id="project-default-model-control" />
      </InheritedFieldShell>,
    );

    expect(screen.getByTestId("project-default-model-field")).toBeInTheDocument();
    expect(screen.getByTestId("project-default-model-provenance")).toHaveAttribute(
      "data-provenance",
      "inherited",
    );
    expect(screen.getByTestId("project-default-model-provenance")).toHaveTextContent(
      "Inherited",
    );
    expect(screen.queryByTestId("project-default-model-reset")).toBeNull();
    expect(screen.getByTestId("project-default-model-hint")).toHaveTextContent(
      "Harness default",
    );
  });

  it("renders overridden provenance and calls Reset", () => {
    const onReset = vi.fn();
    render(
      <InheritedFieldShell
        field="repo_url"
        label="Repository"
        provenance="overridden"
        onReset={onReset}
      >
        <input id="project-default-repo_url-control" />
      </InheritedFieldShell>,
    );

    const field = screen.getByTestId("project-default-repo_url-field");
    expect(field).toHaveClass("border-primary/40", "bg-primary/5");
    expect(screen.getByTestId("project-default-repo_url-provenance")).toHaveAttribute(
      "data-provenance",
      "overridden",
    );
    fireEvent.click(screen.getByTestId("project-default-repo_url-reset"));
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("renders an invalid saved value with destructive error text", () => {
    render(
      <InheritedFieldShell
        field="host_type"
        label="Host type"
        provenance="invalid"
        onReset={vi.fn()}
        error="Saving repairs this value."
      >
        <select id="project-default-host_type-control" />
      </InheritedFieldShell>,
    );

    expect(screen.getByTestId("project-default-host_type-provenance")).toHaveTextContent(
      "Invalid saved value",
    );
    expect(screen.getByTestId("project-default-host_type-error")).toHaveClass(
      "text-destructive",
    );
  });
});
