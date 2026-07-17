import type { ModelOption } from "@/lib/harnessCatalog";

import { ProjectDefaultPicker, withCurrentOption } from "./ProjectDefaultPicker";
import type { FieldProvenance } from "./projectDefaultsDraft";

export function DefaultModelPicker({
  value,
  provenance,
  harness,
  catalog,
  error,
  onChange,
  onReset,
}: {
  value: string;
  provenance: FieldProvenance;
  harness: string | null;
  catalog: readonly ModelOption[];
  error?: string;
  onChange: (value: string) => void;
  onReset: () => void;
}) {
  const { options, selected } = withCurrentOption(
    catalog.map((option) => ({ value: option.id, label: option.label })),
    value,
    `${value} (not in current catalog)`,
  );

  const hint =
    harness === null
      ? "Choose a project harness to select a compatible model. Without one, the future session's agent decides."
      : catalog.length === 0
        ? "No project-wide model catalog available. The future session's agent decides."
        : "Harness default";

  return (
    <ProjectDefaultPicker
      field="model"
      label="Model"
      value={value}
      provenance={provenance}
      options={options}
      triggerLabel={value ? (selected?.label ?? value) : "Harness default"}
      disabled={catalog.length === 0}
      hint={hint}
      error={error}
      onChange={onChange}
      onReset={onReset}
    />
  );
}
