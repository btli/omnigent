import { afterEach, describe, expect, it } from "vitest";
import { restoreFontPreferences } from "./restoreFontPreferences";

afterEach(() => {
  localStorage.clear();
  document.documentElement.style.removeProperty("--ui-font-scale");
  document.documentElement.style.removeProperty("--ui-font-family");
});

describe("restoreFontPreferences", () => {
  it("applies the saved UI size + family to the document root on boot", () => {
    localStorage.setItem("omnigent:ui-font-size", JSON.stringify(20));
    localStorage.setItem("omnigent:ui-font-family", JSON.stringify("Inter"));

    restoreFontPreferences();

    // 20 / 16 base = 1.25.
    expect(document.documentElement.style.getPropertyValue("--ui-font-scale")).toBe("1.25");
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe(
      "Inter, var(--font-sans)",
    );
  });

  it("applies defaults when nothing is stored (no throw)", () => {
    expect(() => restoreFontPreferences()).not.toThrow();
    // Default size 16 → scale 1; family unset → no override.
    expect(document.documentElement.style.getPropertyValue("--ui-font-scale")).toBe("1");
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe("");
  });
});
