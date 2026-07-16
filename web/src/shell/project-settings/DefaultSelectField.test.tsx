import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DefaultSelectField } from "./DefaultSelectField";

afterEach(cleanup);

describe("DefaultSelectField", () => {
  it("renders options and emits a changed selection", () => {
    const onChange = vi.fn();
    render(
      <DefaultSelectField
        field="host_type"
        label="Host type"
        value="external"
        provenance="inherited"
        options={[
          { value: "external", label: "External" },
          { value: "managed", label: "Managed" },
        ]}
        onChange={onChange}
        onReset={vi.fn()}
      />,
    );

    const select = screen.getByTestId("project-default-host_type-control");
    expect(select).toHaveAccessibleName("Host type");
    expect(screen.getByRole("option", { name: "External" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Managed" })).toBeInTheDocument();
    fireEvent.change(select, { target: { value: "managed" } });
    expect(onChange).toHaveBeenCalledWith("managed");
  });

  it("delegates Reset for an overridden select", () => {
    const onReset = vi.fn();
    render(
      <DefaultSelectField
        field="host_type"
        label="Host type"
        value="managed"
        provenance="overridden"
        options={[{ value: "managed", label: "Managed" }]}
        onChange={vi.fn()}
        onReset={onReset}
      />,
    );

    fireEvent.click(screen.getByTestId("project-default-host_type-reset"));
    expect(onReset).toHaveBeenCalledTimes(1);
  });
});
