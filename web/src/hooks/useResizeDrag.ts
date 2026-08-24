import { useCallback, useEffect, useRef } from "react";

interface ResizeDragOptions<T extends Element> {
  captureRequired?: boolean;
  enabled?: boolean;
  onMove: (event: React.PointerEvent<T>) => void;
  onCommit?: () => void;
  overlay?: boolean;
  observeHandleRemoval?: boolean;
  releaseCaptureOnFinish?: boolean;
}

const OVERLAY_STYLE =
  "position:fixed;inset:0;z-index:2147483647;cursor:col-resize;background:transparent;";

/** Shared pointer lifecycle for resize handles; callers provide only sizing policy. */
export function useResizeDrag<T extends Element = Element>({
  captureRequired = true,
  enabled = true,
  onMove,
  onCommit,
  overlay = false,
  observeHandleRemoval = false,
  releaseCaptureOnFinish = false,
}: ResizeDragOptions<T>) {
  const activePointerId = useRef<number | null>(null);
  const activeHandle = useRef<T | null>(null);
  const cleanup = useRef<(() => void) | null>(null);
  const overlayElement = useRef<HTMLDivElement | null>(null);
  const onMoveRef = useRef(onMove);
  const onCommitRef = useRef(onCommit);
  const releaseCaptureOnFinishRef = useRef(releaseCaptureOnFinish);
  onMoveRef.current = onMove;
  onCommitRef.current = onCommit;
  releaseCaptureOnFinishRef.current = releaseCaptureOnFinish;

  const finishDrag = useCallback((commit: boolean, releaseCapture = false) => {
    const pointerId = activePointerId.current;
    if (pointerId === null) return;

    const handle = activeHandle.current;
    activePointerId.current = null;
    activeHandle.current = null;
    cleanup.current?.();
    cleanup.current = null;
    overlayElement.current?.remove();
    overlayElement.current = null;
    if (commit) onCommitRef.current?.();

    if (releaseCapture) {
      try {
        if (handle?.hasPointerCapture?.(pointerId)) handle.releasePointerCapture(pointerId);
      } catch {
        // The handle may have detached before the drag ended.
      }
    }

    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }, []);

  const cancelDrag = useCallback(
    () => finishDrag(false, releaseCaptureOnFinishRef.current),
    [finishDrag],
  );

  const onPointerDown = useCallback(
    (event: React.PointerEvent<T>) => {
      if (!enabled || event.button !== 0 || activePointerId.current !== null) return;

      const capture = event.currentTarget.setPointerCapture;
      if (capture) {
        try {
          capture.call(event.currentTarget, event.pointerId);
        } catch {
          return;
        }
      } else if (captureRequired) {
        return;
      }

      event.preventDefault();
      activePointerId.current = event.pointerId;
      activeHandle.current = event.currentTarget;

      const onDocumentPointerUp = (documentEvent: PointerEvent) => {
        if (documentEvent.pointerId === activePointerId.current) {
          finishDrag(true, releaseCaptureOnFinishRef.current);
        }
      };
      const onDocumentPointerCancel = (documentEvent: PointerEvent) => {
        if (documentEvent.pointerId === activePointerId.current) {
          finishDrag(false, releaseCaptureOnFinishRef.current);
        }
      };
      const observer = observeHandleRemoval
        ? new MutationObserver(() => {
            if (activeHandle.current && !activeHandle.current.isConnected) cancelDrag();
          })
        : null;
      observer?.observe(document.documentElement, { childList: true, subtree: true });
      document.addEventListener("pointerup", onDocumentPointerUp);
      document.addEventListener("pointercancel", onDocumentPointerCancel);
      cleanup.current = () => {
        observer?.disconnect();
        document.removeEventListener("pointerup", onDocumentPointerUp);
        document.removeEventListener("pointercancel", onDocumentPointerCancel);
      };

      if (overlay) {
        const element = document.createElement("div");
        element.style.cssText = OVERLAY_STYLE;
        document.body.appendChild(element);
        overlayElement.current = element;
      }
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [cancelDrag, captureRequired, enabled, finishDrag, observeHandleRemoval, overlay],
  );

  const onPointerMove = useCallback((event: React.PointerEvent<T>) => {
    if (event.pointerId === activePointerId.current) onMoveRef.current(event);
  }, []);

  const onPointerUp = useCallback(
    (event: React.PointerEvent<T>) => {
      if (event.pointerId === activePointerId.current) finishDrag(true, true);
    },
    [finishDrag],
  );

  const onPointerCancel = useCallback(
    (event: React.PointerEvent<T>) => {
      if (event.pointerId === activePointerId.current) {
        finishDrag(false, releaseCaptureOnFinishRef.current);
      }
    },
    [finishDrag],
  );

  useEffect(() => {
    if (!enabled) cancelDrag();
  }, [cancelDrag, enabled]);
  useEffect(() => cancelDrag, [cancelDrag]);

  return {
    cancelDrag,
    handleProps: {
      onPointerDown,
      onPointerMove,
      onPointerUp,
      onPointerCancel,
      onLostPointerCapture: onPointerCancel,
    },
  };
}
