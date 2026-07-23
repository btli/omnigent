import { useEffect, useRef } from "react";
import { PlusIcon, SquareTerminalIcon } from "lucide-react";

import type { BangMenuRow } from "@/hooks/useBangShellMenu";
import { cn } from "@/lib/utils";

/**
 * Floating `!`-shell-command suggestions panel, rendered above the
 * composer while the user types the bang token. Mirrors
 * `SlashCommandMenu`'s shell: `absolute bottom-full` panel, section
 * headers, `data-active` + `scrollIntoView`, `onMouseDown` preventDefault
 * so clicking never blurs the textarea. Two sections in flat keyboard
 * order: "Running shells" on top, the visually distinct "New shell…"
 * type list below (present only when the agent declares 2+ types).
 */

interface BangShellMenuProps {
  /** Flat row list from `useBangShellMenu`, already filtered. */
  rows: BangMenuRow[];
  /** Index of the highlighted row in `rows` (-1 = none). */
  activeIndex: number;
  /** Called when the user selects a completable row (click or keyboard). */
  onSelect: (row: BangMenuRow) => void;
}

function RowButton({
  row,
  flatIndex,
  active,
  onSelect,
}: {
  row: BangMenuRow;
  flatIndex: number;
  active: boolean;
  onSelect: (row: BangMenuRow) => void;
}) {
  const unavailable = !row.completable;
  const exited = row.kind === "shell" && row.running === false;
  return (
    <button
      type="button"
      role="option"
      aria-selected={active}
      aria-disabled={unavailable || undefined}
      disabled={unavailable}
      data-testid={`bang-menu-item-${row.token}`}
      data-active={active ? "true" : undefined}
      data-bang-row-index={flatIndex}
      className={cn(
        "flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[13px] text-foreground",
        unavailable ? "cursor-default opacity-50" : "hover:bg-accent",
        active && "bg-accent",
      )}
      // preventDefault keeps the textarea focused while the user clicks.
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => {
        if (row.completable) onSelect(row);
      }}
    >
      {row.kind === "shell" ? (
        <SquareTerminalIcon
          className={cn(
            "size-3.5 shrink-0",
            exited ? "text-muted-foreground" : "text-emerald-500 dark:text-emerald-400",
          )}
        />
      ) : (
        <PlusIcon className="size-3.5 shrink-0 text-muted-foreground" />
      )}
      <span className="truncate font-mono">{row.token}</span>
      <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
        {row.kind === "shell" ? (exited ? "exited" : row.name) : row.isDefault ? "(default)" : ""}
      </span>
    </button>
  );
}

export function BangShellMenu({ rows, activeIndex, onSelect }: BangShellMenuProps) {
  const listRef = useRef<HTMLDivElement>(null);
  // Keep the keyboard-highlighted row visible in the capped, scrollable
  // list — same data-active + scrollIntoView pattern as the slash menu.
  useEffect(() => {
    if (activeIndex < 0 || !listRef.current) return;
    listRef.current.querySelector('[data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);
  if (rows.length === 0) return null;

  // The hook builds shells before types, so the partition is contiguous
  // and rendering the sections in order preserves visual = keyboard order.
  const shellRows = rows
    .map((row, flatIndex) => ({ row, flatIndex }))
    .filter(({ row }) => row.kind === "shell");
  const typeRows = rows
    .map((row, flatIndex) => ({ row, flatIndex }))
    .filter(({ row }) => row.kind === "type");

  const sectionHeader = (label: string) => (
    <div className="px-2 pb-0.5 pt-1.5 text-[11px] font-medium text-muted-foreground">{label}</div>
  );

  return (
    <div
      data-testid="bang-shell-menu"
      className="absolute bottom-full left-0 z-10 mb-2 w-72 overflow-hidden rounded-xl border border-border bg-popover shadow-lg"
    >
      <div
        ref={listRef}
        role="listbox"
        aria-label="Shells"
        className="max-h-80 overflow-y-auto p-1"
      >
        {shellRows.length > 0 && sectionHeader("Running shells")}
        {shellRows.map(({ row, flatIndex }) => (
          <RowButton
            key={`shell:${row.token}`}
            row={row}
            flatIndex={flatIndex}
            active={flatIndex === activeIndex}
            onSelect={onSelect}
          />
        ))}
        {typeRows.length > 0 && sectionHeader("New shell…")}
        {typeRows.map(({ row, flatIndex }) => (
          <RowButton
            key={`type:${row.token}`}
            row={row}
            flatIndex={flatIndex}
            active={flatIndex === activeIndex}
            onSelect={onSelect}
          />
        ))}
      </div>
    </div>
  );
}
