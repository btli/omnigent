import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const createProject = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false }));

vi.mock("./useConversations", () => ({
  useCreateProject: () => createProject,
}));

import { useProjectPickerState } from "./useProjectPickerState";

describe("useProjectPickerState", () => {
  beforeEach(() => {
    createProject.mutate.mockReset();
  });

  it("selects a newly created project and resets inline-create state", () => {
    const onSelect = vi.fn();
    const { result } = renderHook(() => useProjectPickerState([], onSelect));

    act(() => {
      result.current.beginCreatingProject();
      result.current.setNewProjectName("Docs");
    });
    act(() => result.current.commitNewProject());

    const options = createProject.mutate.mock.calls[0][1];
    act(() => options.onSuccess({ id: "proj_docs", name: "Docs" }));

    expect(onSelect).toHaveBeenCalledWith("proj_docs");
    expect(result.current.creatingNew).toBe(false);
    expect(result.current.newProjectName).toBe("");
    expect(result.current.createError).toBeNull();
  });

  it("keeps the create row open and exposes an inline error on failure", () => {
    const { result } = renderHook(() => useProjectPickerState([], vi.fn()));

    act(() => {
      result.current.beginCreatingProject();
      result.current.setNewProjectName("Docs");
    });
    act(() => result.current.commitNewProject());

    const options = createProject.mutate.mock.calls[0][1];
    act(() => options.onError(new Error("500 Internal Server Error")));

    expect(result.current.creatingNew).toBe(true);
    expect(result.current.newProjectName).toBe("Docs");
    expect(result.current.createError).toContain("Couldn't create project");
  });
});
