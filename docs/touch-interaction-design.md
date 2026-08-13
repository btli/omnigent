# Touch Interaction Design

Status: proposed. Architecture and phasing for
[touch-interaction-spec.md](touch-interaction-spec.md). Requirements are
referenced as `TR-n`.

## Shape of the solution

Three layers, built bottom-up. Each layer ships independently and is useful
without the ones above it.

```
Phase 2   Left rail (single mobile nav surface)          TR-20..25
             │ consumes
Phase 1   Gesture dispatcher (one owner per surface)     TR-9..19, 26..27
             │ consumes
Phase 0   Input capability + pointer-event foundation    TR-1..8
```

## Phase 0 — foundation (no behavior redesign)

### `useInputCapabilities`

One hook (plus a matching CSS custom-media/utility story) wrapping:

```ts
interface InputCapabilities {
  coarsePrimary: boolean;   // (pointer: coarse)
  anyCoarse: boolean;       // (any-pointer: coarse)
  hoverPrimary: boolean;    // (hover: hover)
}
```

Backed by `matchMedia` with change listeners, so convertibles and
mouse-attach events update live (TR-1). All existing touch-conditional
logic migrates from width checks to this hook (TR-2). The abandoned-train
commit that moved row gestures to coarse-pointer gating is the prototype;
resurrect its approach, not its diff.

### Breakpoint consolidation

One module exports the canonical breakpoints; `useIsMobileViewport` and the
JS `MD_BREAKPOINT` constants consume it; the stray `767px`/`767.98px`
variants are unified (TR-3). The Android `NativeBridgeScript` width probe is
replaced by reading the web layer's signal (TR-4).

### Resize hooks → pointer events

Mechanical migration of the five hooks to
`pointerdown`/`pointermove`/`pointerup` + `setPointerCapture`, with
`touch-action: none` on handles (TR-5, TR-8). Coarse-pointer hit-target
padding on handles (TR-6); `useResizableColumn` gains the keyboard path the
other hooks already have (TR-7). No API change for consumers — this is five
small, independently reviewable PRs.

## Phase 1 — the gesture dispatcher

### Core: `useGestureDispatcher`

Generalizes the train's `useRowGesture` (commit `8dd70928c` and successors
on `feature/mobile-slide-actions-v2` / `train/polly/session-row-gesture-fix`
— recover the hardening: drift tolerance, disjoint-region thresholds,
WebView haptic hook). One instance owns one surface's pointer stream and
awards it to a single intent (TR-9):

```
pointerdown
  └─ undecided ──(axis-locked horizontal ≥ swipe threshold)──► swipe
       │        ──(vertical movement first)────────────────► release to scroll
       │        ──(hold ≥ holdMs, still within tolerance)──► hold
       │              └─(then moves past drag tolerance)───► drag
       │              └─(released without drag)────────────► context menu
       └─(fine pointer)──► mouse semantics unchanged
```

Thresholds live in one exported constants module with the disjointness
documented (TR-10). While undecided the dispatcher buffers via refs — no
React state churn, passive listeners where possible (TR-15).

Integration points, in order:

1. **Session rows** — replaces the direct dnd-kit TouchSensor + Radix
   long-press contention: the dispatcher decides, then *drives* dnd-kit
   (activate drag programmatically) and the context menu (controlled open)
   rather than letting their timers race (TR-11, TR-12, TR-17). #3985's
   swipe actions become the first swipe consumer (TR-16), with the #3154
   review defects encoded as regression tests (TR-28).
2. **Resize handles** — Phase 0's pointer hooks register as `resize` intent
   claimants so an active resize excludes other consumers (TR-8).
3. **Edge ownership table** — a small module declaring who owns each edge
   and the back-dismissal stack (TR-13). `__omnigentNativeHandleBack`
   (Android) and the iOS edge-pan handoff both resolve through it (TR-26).
   Per-surface `touch-action`/`overscroll-behavior` claims are declared
   alongside (TR-27).

### What the dispatcher is *not*

Not a global event bus and not a wrapper around every click. Plain taps,
buttons, and inputs never route through it. It exists only where ≥2 gesture
consumers contend for the same pointer stream (rows, edges, handles).

## Phase 2 — the left rail (gated)

**Go/no-go gate:** Phase 2 starts only after Phase 1's row arbitration has
landed and survived on main, and mobile-nav pain still justifies it. If
cancelled, Phases 0–1 stand alone (this is the debate's decided asymmetry:
the dispatcher is designed against the rail topology from day one via the
edge-ownership table, so the rail is a consumer, not a rewrite).

### Model

Adapt, don't invent: the desktop `WorkspacePanel` Radix `Tabs` rail is
already the navigation model (TR-20). Mobile gets:

- a narrow persistent rail anchor (icon column) instead of the full-screen
  sidebar overlay + top-right FAB (TR-21);
- one destination set shared with desktop via the existing `railTabs`
  contract — the six hand-maintained FAB→tab mappings in `AppShell` and the
  per-tool `MobilePanelDrawer` states collapse into one open/close machine;
- expansion by tap / edge-swipe (dispatcher client, TR-13) / keyboard, with
  `WorkspacePanel`'s ARIA + roving tabindex inherited (TR-23);
- safe-area insets applied once at the rail boundary from the existing
  `--omnigent-safe-*` vars (TR-24);
- Android back and iOS edge-pan dismiss rail layers per the ownership table
  (TR-22); displaced header controls (the #3589/#4551 strip) get rail homes
  (TR-25).

Migration runs the rail behind a temporary fallback alongside the FAB/drawer
path, then removes the duplicates once task coverage is proven.

## Delivery mechanics

- Every PR cites the TR-ns it satisfies; reviewers judge against the spec.
- Phase 0 is parallelizable (capability hook, breakpoint consolidation, five
  hook migrations ≈ seven small PRs).
- Phase 1 lands the dispatcher core with the session-row integration as its
  proving consumer, then #3985 rebases onto it instead of shipping a bespoke
  swipe.
- Salvage before rewrite: audit `feature/mobile-slide-actions-v2` and
  `train/polly/session-row-gesture-fix` for reusable recognizer/test code;
  cherry-pick with history noted in PR descriptions.
- UI-visible PRs carry demo media and `tests/e2e_ui` coverage per repo
  policy (TR-29).

## Risks

| Risk | Mitigation |
|---|---|
| Dispatcher becomes a framework nobody else adopts | Scope it to contended surfaces only; three named integration points, no speculative API |
| Phase 2 stalls like #1395 | Hard gate + fallback path; Phases 0–1 are complete deliverables on their own |
| Threshold tuning churn | Constants in one module, values + rationale documented, changed only with device-matrix retest |
| Native shells drift from the ownership table | Table is the single source; shell PRs that add gesture heuristics are rejected per TR-26 |
| Scroll perf regression on long session lists | TR-15 gates; profile on low-end Android in review |
