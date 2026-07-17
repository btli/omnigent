import { Input } from "@/components/ui/input";

import { InheritedFieldShell } from "./InheritedFieldShell";
import { fieldControlIds } from "./ProjectDefaultPicker";
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
  const { prefix, describedBy } = fieldControlIds(field, hint, error);

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
        aria-describedby={describedBy}
        aria-invalid={error ? true : undefined}
        className="h-11 w-full min-w-0"
        onChange={(event) => onChange(event.target.value)}
      />
    </InheritedFieldShell>
  );
}
