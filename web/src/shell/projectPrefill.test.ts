import { describe, expect, it } from "vitest";
import { initialPrefillState, prefillDone } from "./projectPrefill";

describe("project prefill identity", () => {
  it("anchors a project visit on its stable project id", () => {
    expect(initialPrefillState("proj_sprint_42")).toEqual({
      projectId: "proj_sprint_42",
      phase: "host",
      agentSeeded: false,
      seededWorkspace: null,
    });
  });

  it("starts a plain visit settled", () => {
    expect(prefillDone(initialPrefillState(""))).toBe(true);
  });
});
