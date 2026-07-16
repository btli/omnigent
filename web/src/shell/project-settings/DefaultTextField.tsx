import { Input } from "@/components/ui/input";

import { InheritedFieldShell } from "./InheritedFieldShell";
import type { DefaultField, FieldProvenance } from "./projectDefaultsDraft";

export function DefaultTextField({
  field,
  label,
  value,
  provenance,
  placeholder,
  hint,
  error,
  onChange,
  onReset,
}: {
  field: DefaultField;
  label: string;
  value: string;
  provenance: FieldProvenance;
  placeholder: string;
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
      <Input
        id={`${prefix}-control`}
        data-testid={`${prefix}-control`}
        value={value}
        placeholder={placeholder}
        title={value || placeholder}
        aria-describedby={describedBy || undefined}
        aria-invalid={error ? true : undefined}
        className="h-11 w-full min-w-0"
        onChange={(event) => onChange(event.target.value)}
      />
    </InheritedFieldShell>
  );
}
