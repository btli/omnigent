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

/** Shared testid/label ids for a default-field control and its hint/error. */
export function fieldControlIds(field: DefaultField, hint?: string, error?: string) {
  const prefix = `project-default-${field}`;
  const describedBy =
    [hint && `${prefix}-hint`, error && `${prefix}-error`].filter(Boolean).join(" ") || undefined;
  return { prefix, describedBy };
}

/**
 * Resolve the picker's current selection, prepending a synthetic option when
 * the stored value is missing from the catalog (legacy id, unavailable host)
 * so it stays visible and resettable rather than vanishing.
 */
export function withCurrentOption(
  options: readonly ProjectDefaultPickerOption[],
  value: string,
  unknownLabel: string,
): {
  options: readonly ProjectDefaultPickerOption[];
  selected?: ProjectDefaultPickerOption;
  unknown: boolean;
} {
  const selected = options.find((option) => option.value === value);
  if (!value || selected) return { options, selected, unknown: false };
  const synthetic = { value, label: unknownLabel };
  return { options: [synthetic, ...options], selected: synthetic, unknown: true };
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
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            id={`${prefix}-control`}
            type="button"
            variant="outline"
            data-testid={`${prefix}-control`}
            disabled={disabled}
            title={triggerLabel}
            aria-describedby={describedBy}
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
                <span className="min-w-0 flex-1 truncate">{option.content ?? option.label}</span>
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </InheritedFieldShell>
  );
}
