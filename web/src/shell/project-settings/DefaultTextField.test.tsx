import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DefaultTextField } from "./DefaultTextField";

afterEach(cleanup);

describe("DefaultTextField", () => {
  it("shows a behavioral placeholder without overriding on focus", () => {
    const onChange = vi.fn();
    render(
      <DefaultTextField
        field="workspace"
        label="Workspace"
        value=""
        provenance="inherited"
        placeholder="No project workspace"
        onChange={onChange}
        onReset={vi.fn()}
      />,
    );

    const input = screen.getByTestId("project-default-workspace-control");
    expect(input).toHaveAttribute("placeholder", "No project workspace");
    fireEvent.focus(input);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("emits changed text and delegates Reset", () => {
    const onChange = vi.fn();
    const onReset = vi.fn();
    render(
      <DefaultTextField
        field="repo_url"
        label="Repository"
        value="https://example.com/old.git"
        provenance="overridden"
        placeholder="No repository"
        onChange={onChange}
        onReset={onReset}
      />,
    );

    fireEvent.change(screen.getByTestId("project-default-repo_url-control"), {
      target: { value: "https://example.com/new.git" },
    });
    expect(onChange).toHaveBeenCalledWith("https://example.com/new.git");
    fireEvent.click(screen.getByTestId("project-default-repo_url-reset"));
    expect(onReset).toHaveBeenCalledTimes(1);
  });
});
