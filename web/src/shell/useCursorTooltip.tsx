import { useState } from "react";
import { createPortal } from "react-dom";
import { getEmbedRoot } from "@/lib/host";

/**
 * Returns mouse event handlers for a trigger element and a fixed-positioned
 * tooltip node that follows the cursor. The node is portalled to the embed root
 * (or `document.body` standalone), so a transformed ancestor such as a
 * virtualized row can't become its containing block.
 */
export function useCursorTooltip(text: string): {
  handlers: {
    onMouseMove: (e: React.MouseEvent) => void;
    onMouseLeave: () => void;
  };
  tooltip: React.ReactNode;
} {
  const [cursorPos, setCursorPos] = useState<{ x: number; y: number } | null>(null);

  const handlers = {
    onMouseMove: (e: React.MouseEvent) => setCursorPos({ x: e.clientX, y: e.clientY }),
    onMouseLeave: () => setCursorPos(null),
  };

  const tooltip = cursorPos
    ? createPortal(
        <div
          style={{
            position: "fixed",
            left: cursorPos.x,
            top: cursorPos.y + 14,
            pointerEvents: "none",
          }}
          className="z-50 inline-flex w-fit items-center rounded-md border border-border bg-popover px-3 py-1.5 text-sm text-popover-foreground shadow-tooltip"
        >
          {text}
        </div>,
        getEmbedRoot() ?? document.body,
      )
    : null;

  return { handlers, tooltip };
}
