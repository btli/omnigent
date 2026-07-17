import type { EffortOption } from "@/lib/harnessCatalog";

import { ProjectDefaultPicker, withCurrentOption } from "./ProjectDefaultPicker";
import type { FieldProvenance } from "./projectDefaultsDraft";

export function DefaultEffortPicker({
  value,
  provenance,
  harness,
  model,
  catalog,
  error,
  onChange,
  onReset,
}: {
  value: string;
  provenance: FieldProvenance;
  harness: string | null;
  model: string | null;
  catalog: readonly EffortOption[];
  error?: string;
  onChange: (value: string) => void;
  onReset: () => void;
}) {
  const { options, selected } = withCurrentOption(
    catalog.map((option) => ({ value: option.value, label: option.label })),
    value,
    `${value} (not in current catalog)`,
  );

  const hint =
    harness === null
      ? "Choose a project harness to select compatible effort. Without one, the future session's agent decides."
      : model === null || model === ""
        ? "Choose a project model to select compatible effort. Without one, the harness decides."
        : catalog.length === 0
          ? "No project-wide effort catalog available. The future session's agent decides."
          : "Harness default";

  return (
    <ProjectDefaultPicker
      field="reasoning_effort"
      label="Effort"
      value={value}
      provenance={provenance}
      options={options}
      triggerLabel={value ? (selected?.label ?? value) : "Harness default"}
      disabled={model === null || model === "" || catalog.length === 0}
      hint={hint}
      error={error}
      onChange={onChange}
      onReset={onReset}
    />
  );
}
