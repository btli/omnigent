import { modelOptionsForHarness } from "@/lib/harnessCatalog";

import {
  ProjectDefaultPicker,
  type ProjectDefaultPickerOption,
} from "./DefaultHarnessPicker";
import type { FieldProvenance } from "./projectDefaultsDraft";

export function DefaultModelPicker({
  value,
  provenance,
  harness,
  error,
  onChange,
  onReset,
}: {
  value: string;
  provenance: FieldProvenance;
  harness: string | null;
  error?: string;
  onChange: (value: string) => void;
  onReset: () => void;
}) {
  const catalog = modelOptionsForHarness(harness);
  const known = catalog.some((option) => option.id === value);
  const options: ProjectDefaultPickerOption[] = catalog.map((option) => ({
    value: option.id,
    label: option.label,
  }));
  if (value && !known) {
    options.unshift({ value, label: `${value} (not in current catalog)` });
  }

  const noHarnessHint =
    "Choose a project harness to select a compatible model. Without one, the future session's agent decides.";
  const noCatalogHint =
    "No project-wide model catalog available. The future session's agent decides.";
  const hint = harness === null ? noHarnessHint : catalog.length === 0 ? noCatalogHint : "Harness default";
  const triggerLabel = value
    ? (catalog.find((option) => option.id === value)?.label ??
      `${value} (not in current catalog)`)
    : "Harness default";

  return (
    <ProjectDefaultPicker
      field="model"
      label="Model"
      value={value}
      provenance={provenance}
      options={options}
      triggerLabel={triggerLabel}
      disabled={catalog.length === 0}
      hint={hint}
      error={error}
      onChange={onChange}
      onReset={onReset}
    />
  );
}
