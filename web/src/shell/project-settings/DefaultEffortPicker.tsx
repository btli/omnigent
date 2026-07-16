import { effortOptionsForHarness } from "@/lib/harnessCatalog";

import {
  ProjectDefaultPicker,
  type ProjectDefaultPickerOption,
} from "./DefaultHarnessPicker";
import type { FieldProvenance } from "./projectDefaultsDraft";

export function DefaultEffortPicker({
  value,
  provenance,
  harness,
  model,
  error,
  onChange,
  onReset,
}: {
  value: string;
  provenance: FieldProvenance;
  harness: string | null;
  model: string | null;
  error?: string;
  onChange: (value: string) => void;
  onReset: () => void;
}) {
  const catalog = effortOptionsForHarness(harness, model);
  const known = catalog.some((option) => option.value === value);
  const options: ProjectDefaultPickerOption[] = catalog.map((option) => ({
    value: option.value,
    label: option.label,
  }));
  if (value && !known) {
    options.unshift({ value, label: `${value} (not in current catalog)` });
  }

  const hint =
    harness === null
      ? "Choose a project harness to select compatible effort. Without one, the future session's agent decides."
      : model === null || model === ""
        ? "Choose a project model to select compatible effort. Without one, the harness decides."
        : catalog.length === 0
          ? "No project-wide effort catalog available. The future session's agent decides."
          : "Harness default";
  const triggerLabel = value
    ? (catalog.find((option) => option.value === value)?.label ??
      `${value} (not in current catalog)`)
    : "Harness default";

  return (
    <ProjectDefaultPicker
      field="reasoning_effort"
      label="Effort"
      value={value}
      provenance={provenance}
      options={options}
      triggerLabel={triggerLabel}
      disabled={model === null || model === "" || catalog.length === 0}
      hint={hint}
      error={error}
      onChange={onChange}
      onReset={onReset}
    />
  );
}
