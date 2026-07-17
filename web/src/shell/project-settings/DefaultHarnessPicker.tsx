import { ProjectDefaultPicker, withCurrentOption } from "./ProjectDefaultPicker";
import type { FieldProvenance } from "./projectDefaultsDraft";

export function DefaultHarnessPicker({
  value,
  provenance,
  harnessOptions,
  onChange,
  onReset,
}: {
  value: string;
  provenance: FieldProvenance;
  harnessOptions: readonly { id: string; label: string }[];
  onChange: (value: string) => void;
  onReset: () => void;
}) {
  const { options, selected, unknown } = withCurrentOption(
    harnessOptions.map((option) => ({ value: option.id, label: option.label })),
    value,
    `${value} (not in current catalog)`,
  );

  return (
    <ProjectDefaultPicker
      field="harness"
      label="Harness"
      value={value}
      provenance={provenance}
      options={options}
      triggerLabel={value ? (selected?.label ?? value) : "Agent default"}
      disabled={options.length === 0}
      hint={
        unknown
          ? "This saved harness is not in the current catalog. Reset it or choose a replacement."
          : "Agent default"
      }
      onChange={onChange}
      onReset={onReset}
    />
  );
}
