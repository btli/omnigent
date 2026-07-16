import type { KeyboardEventHandler, RefObject } from "react";

interface ProjectCreateRowProps {
  inputRef: RefObject<HTMLInputElement | null>;
  value: string;
  onChange: (value: string) => void;
  onKeyDown: KeyboardEventHandler<HTMLInputElement>;
  disabled: boolean;
  error: string | null;
  errorId: string;
}

/** Shared project-name input and inline creation error. */
export function ProjectCreateRow({
  inputRef,
  value,
  onChange,
  onKeyDown,
  disabled,
  error,
  errorId,
}: ProjectCreateRowProps) {
  return (
    <>
      <input
        ref={inputRef}
        className="w-full bg-transparent text-xs outline-none"
        placeholder="Project name…"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={onKeyDown}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? errorId : undefined}
      />
      {error && (
        <p id={errorId} className="pt-1 text-destructive text-xs" role="alert">
          {error}
        </p>
      )}
    </>
  );
}
