import { cn } from "@/lib/utils";

import { InheritedFieldShell } from "./InheritedFieldShell";
import type { DefaultField, FieldProvenance } from "./projectDefaultsDraft";

export function DefaultSelectField({
  field,
  label,
  value,
  provenance,
  options,
  hint,
  error,
  onChange,
  onReset,
}: {
  field: DefaultField;
  label: string;
  value: string;
  provenance: FieldProvenance;
  options: readonly { value: string; label: string }[];
  hint?: string;
  error?: string;
  onChange: (value: string) => void;
  onReset: () => void;
}) {
  const prefix = `project-default-${field}`;
  const describedBy = [hint && `${prefix}-hint`, error && `${prefix}-error`]
    .filter(Boolean)
    .join(" ");

  return (
    <InheritedFieldShell
      field={field}
      label={label}
      provenance={provenance}
      onReset={onReset}
      hint={hint}
      error={error}
    >
      <select
        id={`${prefix}-control`}
        data-testid={`${prefix}-control`}
        value={value}
        title={options.find((option) => option.value === value)?.label ?? value}
        aria-describedby={describedBy || undefined}
        aria-invalid={error ? true : undefined}
        className={cn(
          "h-11 w-full min-w-0 rounded-lg border border-input bg-background px-2.5 text-sm outline-none",
          "focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50",
          "aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20",
        )}
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </InheritedFieldShell>
  );
}
