import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import type { DefaultField, FieldProvenance } from "./projectDefaultsDraft";

export function InheritedFieldShell({
  field,
  label,
  provenance,
  onReset,
  hint,
  error,
  children,
}: {
  field: DefaultField;
  label: string;
  provenance: FieldProvenance;
  onReset: () => void;
  hint?: string;
  error?: string;
  children: ReactNode;
}) {
  const prefix = `project-default-${field}`;
  const provenanceLabel =
    provenance === "invalid"
      ? "Invalid saved value"
      : provenance === "overridden"
        ? "Overridden"
        : "Inherited";

  return (
    <div
      data-testid={`${prefix}-field`}
      className={cn(
        "min-w-0 space-y-2 rounded-lg border border-border p-3",
        provenance === "overridden" && "border-primary/40 bg-primary/5",
        provenance === "invalid" && "border-destructive/50 bg-destructive/5",
        error && "border-destructive/50 bg-destructive/5",
      )}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <label
          htmlFor={`${prefix}-control`}
          className="min-w-0 flex-1 text-sm font-medium leading-tight"
        >
          {label}
        </label>
        <Badge
          variant={provenance === "invalid" ? "destructive" : "outline"}
          data-testid={`${prefix}-provenance`}
          data-provenance={provenance}
          className={cn(
            provenance === "inherited" && "text-muted-foreground",
            provenance === "overridden" && "border-primary/40 bg-primary/5 text-primary",
          )}
        >
          {provenanceLabel}
        </Badge>
        {provenance !== "inherited" && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            data-testid={`${prefix}-reset`}
            className="min-h-11 px-3"
            onClick={onReset}
          >
            Reset
          </Button>
        )}
      </div>
      {children}
      {hint && (
        <p
          id={`${prefix}-hint`}
          data-testid={`${prefix}-hint`}
          className="text-xs leading-relaxed text-muted-foreground"
        >
          {hint}
        </p>
      )}
      {error && (
        <p
          id={`${prefix}-error`}
          data-testid={`${prefix}-error`}
          className="text-xs leading-relaxed text-destructive"
        >
          {error}
        </p>
      )}
    </div>
  );
}
