import { describe, expect, it } from "vitest";

import {
  buildDraft,
  fieldDisplayValue,
  fieldProvenance,
  isDirty,
  resetField,
  serializeBundle,
  setFieldValue,
  type ResolvedBaselines,
} from "./projectDefaultsDraft";

const BASELINES: ResolvedBaselines = {
  host_type: "external",
  repo_url: null,
  default_branch: "main",
  host_id: "host-1",
  workspace: "/workspace",
  harness: "claude-native",
  model: "claude-sonnet-4-5",
  reasoning_effort: "high",
};

describe("projectDefaultsDraft", () => {
  it("shows an absent field as inherited with its resolved baseline", () => {
    const state = buildDraft({}, BASELINES);

    expect(fieldProvenance(state, "model")).toBe("inherited");
    expect(fieldDisplayValue(state, "model")).toBe(BASELINES.model);
    expect(state.fields.model.value).toBe(BASELINES.model);
  });

  it("serializes an absent field edited away from its baseline as an override", () => {
    const state = setFieldValue(buildDraft({}, BASELINES), "model", "claude-opus-4-1");

    expect(fieldProvenance(state, "model")).toBe("overridden");
    expect(serializeBundle(state)).toEqual({ model: "claude-opus-4-1" });
  });

  it("keeps an absent field inherited when edited to its baseline", () => {
    const state = setFieldValue(buildDraft({}, BASELINES), "model", BASELINES.model ?? "");

    expect(fieldProvenance(state, "model")).toBe("inherited");
    expect(serializeBundle(state)).toEqual({});
  });

  it("keeps an untouched stored value overridden and serialized", () => {
    const state = buildDraft({ model: "stored-model" }, BASELINES);

    expect(fieldProvenance(state, "model")).toBe("overridden");
    expect(serializeBundle(state)).toEqual({ model: "stored-model" });
  });

  it("keeps a stored value overridden when edited to the baseline", () => {
    const state = setFieldValue(
      buildDraft({ model: "stored-model" }, BASELINES),
      "model",
      BASELINES.model ?? "",
    );

    expect(fieldProvenance(state, "model")).toBe("overridden");
    expect(serializeBundle(state)).toEqual({ model: BASELINES.model });
  });

  it("omits a stored value after it is emptied", () => {
    const state = setFieldValue(buildDraft({ model: "stored-model" }, BASELINES), "model", "");

    expect(serializeBundle(state)).toEqual({});
  });

  it("normalizes a legacy null to inherited and omits it", () => {
    const state = buildDraft({ model: null }, { ...BASELINES, model: null });

    expect(fieldProvenance(state, "model")).toBe("inherited");
    expect(fieldDisplayValue(state, "model")).toBe("");
    expect(serializeBundle(state)).toEqual({});
  });

  it("omits a whitespace-only edit", () => {
    const state = setFieldValue(buildDraft({}, BASELINES), "model", "   \n  ");

    expect(serializeBundle(state)).toEqual({});
  });

  it("marks a null host type invalid and repairs it by omission", () => {
    const state = buildDraft({ host_type: null }, BASELINES);

    expect(state.invalidHostTypeNull).toBe(true);
    expect(fieldProvenance(state, "host_type")).toBe("invalid");
    expect(fieldDisplayValue(state, "host_type")).toBe("external");
    expect(serializeBundle(state)).toEqual({});
  });

  it("resets a stored value to inherited and omits it", () => {
    const state = resetField(buildDraft({ model: "stored-model" }, BASELINES), "model");

    expect(fieldProvenance(state, "model")).toBe("inherited");
    expect(fieldDisplayValue(state, "model")).toBe(BASELINES.model);
    expect(serializeBundle(state)).toEqual({});
  });

  it("compares serialized state with a null-normalized original bundle", () => {
    const original = { model: null };
    const state = buildDraft(original, { ...BASELINES, model: null });

    expect(isDirty(state, original)).toBe(false);
    expect(isDirty(setFieldValue(state, "model", "new-model"), original)).toBe(true);
  });
});
