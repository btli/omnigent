import type { ReactNode } from "react";
import { ChevronDownIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

import { InheritedFieldShell } from "./InheritedFieldShell";
import type { DefaultField, FieldProvenance } from "./projectDefaultsDraft";

export interface ProjectDefaultPickerOption {
  value: string;
  label: string;
  content?: ReactNode;
}

export function ProjectDefaultPicker({
  field,
  label,
  value,
  provenance,
  options,
  triggerLabel,
  triggerContent,
  disabled = false,
  hint,
  error,
  onChange,
  onReset,
}: {
  field: DefaultField;
  label: string;
  value: string;
  provenance: FieldProvenance;
  options: readonly ProjectDefaultPickerOption[];
  triggerLabel: string;
  triggerContent?: ReactNode;
  disabled?: boolean;
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
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            id={`${prefix}-control`}
            type="button"
            variant="outline"
            data-testid={`${prefix}-control`}
            disabled={disabled}
            title={triggerLabel}
            aria-describedby={describedBy || undefined}
            aria-invalid={error ? true : undefined}
            className="h-11 w-full min-w-0 justify-between px-3 font-normal"
          >
            <span className="min-w-0 flex-1 truncate text-left">
              {triggerContent ?? triggerLabel}
            </span>
            <ChevronDownIcon className="size-4 shrink-0 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          data-testid={`${prefix}-options`}
          className="max-h-[var(--radix-dropdown-menu-content-available-height)] max-w-[calc(100vw-2rem)] overflow-y-auto"
        >
          <DropdownMenuRadioGroup value={value} onValueChange={onChange}>
            {options.map((option) => (
              <DropdownMenuRadioItem
                key={option.value}
                value={option.value}
                data-testid={`${prefix}-option-${option.value}`}
                className="min-h-11 min-w-0 text-xs"
                title={option.label}
              >
                <span className="min-w-0 flex-1 truncate">
                  {option.content ?? option.label}
                </span>
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </InheritedFieldShell>
  );
}

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
  const knownLabel = harnessOptions.find((option) => option.id === value)?.label;
  const options: ProjectDefaultPickerOption[] = harnessOptions.map((option) => ({
    value: option.id,
    label: option.label,
  }));
  if (value && knownLabel === undefined) {
    options.unshift({
      value,
      label: `${value} (not in current catalog)`,
    });
  }

  const triggerLabel = value
    ? (knownLabel ?? `${value} (not in current catalog)`)
    : "Agent default";

  return (
    <ProjectDefaultPicker
      field="harness"
      label="Harness"
      value={value}
      provenance={provenance}
      options={options}
      triggerLabel={triggerLabel}
      disabled={options.length === 0}
      hint={
        value && knownLabel === undefined
          ? "This saved harness is not in the current catalog. Reset it or choose a replacement."
          : "Agent default"
      }
      onChange={onChange}
      onReset={onReset}
    />
  );
}
