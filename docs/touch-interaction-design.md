# Touch Interaction Design

Status: proposed. Architecture and phasing for
[touch-interaction-spec.md](touch-interaction-spec.md). Requirements are
referenced as `TR-n`.

## Shape of the solution

Three layers, built bottom-up. Each layer ships independently and is useful
without the ones above it.

```
Phase 2   Left rail (single mobile nav surface)          TR-22..27
             │ consumes
Phase 1   Gesture dispatcher (one owner per surface)     TR-10..21, 28
             │ consumes
Phase 0   Input capability + pointer-event foundation    TR-1..9
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
mouse-attach events update live (TR-1). Per TR-2 this hook gates
*affordances only* — persistent vs hover-revealed controls (TR-5), hit
targets, swipe hints. Gesture recognition never reads it: recognizers
branch on the live sequence's `PointerEvent.pointerType`, so a touch on a
fine-primary laptop still gets gesture semantics. The abandoned-train
commit that moved row gestures off viewport width is the prototype;
resurrect its approach, generalized from capability-gating to
pointerType-binding.

### Breakpoint consolidation

One module exports the canonical breakpoints; `useIsMobileViewport` and the
JS `MD_BREAKPOINT` constants consume it; the stray `767px`/`767.98px`
variants are unified (TR-3). The Android `NativeBridgeScript` back handler
stops hardcoding its own `innerWidth < 768` copy and consumes the web
layer's signal (TR-4).

### Hover-affordance fallback

The sidebar row/folder action buttons (`md:opacity-0
md:group-hover:opacity-100`) render persistently (or behind a visible
menu) when `hoverPrimary` is false (TR-5). Small, self-contained PR;
unblocks touch laptops immediately.

### Resize hooks → pointer events

Migration of the five hooks to
`pointerdown`/`pointermove`/`pointerup` + `setPointerCapture`, with
`touch-action: none` on handles (TR-6, TR-9) and TR-6's robustness
outcomes: clean abort on `pointercancel`/capture loss, unmount cleanup,
first-pointer-wins, and iframe-overlay shielding during drags (the
existing mouse paths already overlay iframes; keep that). Coarse-pointer
hit-target padding on handles (TR-7); `useResizableColumn` gains the
keyboard path the other hooks already have (TR-8). No API change for
consumers — five small, independently reviewable PRs.

Isolated handles do NOT register with the gesture dispatcher — pointer
capture plus `touch-action: none` fully arbitrates a handle nothing else
contends for. Only a handle that overlaps another recognizer's territory
(e.g. a future rail-edge handle sitting inside the `EDGE_ZONE_PX` strip)
routes through the dispatcher as a `resize`-intent claimant.

## Phase 1 — the gesture dispatcher

### Core: `useGestureDispatcher`

Generalizes the train's `useRowGesture` (commit `8dd70928c` and successors
on `feature/mobile-slide-actions-v2` / `train/polly/session-row-gesture-fix`
— recover the hardening: drift tolerance, the disjoint-region math now
normative in TR-11's threshold table, WebView haptics). One instance owns
one contended surface's pointer stream and awards it to a single intent
(TR-10). Timing and thresholds are TR-11/TR-12's; the state machine:

```
pointerdown (pointerType touch|pen; mouse passes through unchanged)
  └─ undecided ──(vertical-first movement)──────────────► release to scroll
       │        ──(horizontal-first ≥ SWIPE_ACTIVATION_PX)► swipe
       │        ──(stationary within HOLD_DRIFT_PX for HOLD_MS)► armed
       │              ├─(moves > DRAG_TOLERANCE_PX)───────► drag (menu suppressed)
       │              └─(still stationary at MENU_HOLD_MS)► context menu opens
       │                    └─ sequence consumed: no drag; menu takes focus
```

The menu opens AT `MENU_HOLD_MS` while the finger is still down (TR-12),
with a haptic cue — not on release. Award is exclusive and
first-crossed-wins per TR-11 — there is no geometric disjointness between
hold and swipe; crossing any award threshold cancels hold eligibility. A
second touch pointer joining an undecided sequence cancels it and yields
to browser pinch-zoom (TR-28). While undecided the dispatcher buffers via
refs — no React state churn, passive listeners on scroll containers
(TR-16(a)/(b) are component-test assertions).

Integration points, in order:

1. **Session rows** — replaces the direct dnd-kit TouchSensor + Radix
   long-press contention: the dispatcher decides, then *drives* dnd-kit
   (activate drag programmatically) and the context menu (controlled open)
   rather than letting their timers race (TR-12, TR-13, TR-18). #3985's
   swipe actions become the first swipe consumer (TR-17), with the #3154
   review defects encoded as regression tests (TR-29).
2. **Edge ownership table** — a small module implementing TR-14's
   normative matrix (start-edge → rail open; end-edge → released to
   browser/OS; Android back → dismissal stack; browser back → per-layer
   history entries, one `popstate` closes one token-matched layer).
   `__omnigentNativeHandleBack` (Android) and the iOS edge-pan handoff
   both resolve through it (TR-28); iOS edge-pan only ever OPENS the rail
   — dismissal on iOS is tap/scrim/keyboard per TR-24.
   Per-surface `touch-action`/`overscroll-behavior` claims land per TR-28's
   baseline table.
3. **Editor/table surfaces** — `TableBubbleMenu`'s mouse-only table
   row/column manipulation and context actions gain touch paths (TR-21):
   long-press via the dispatcher where the surface is contended (selection
   vs. menu), plain visible controls where it is not. Scheduled at the end
   of Phase 1; independent of the sidebar work.
4. **Contended resize handles only** — per the Phase 0 rule above, only
   handles overlapping another recognizer register `resize` claims (TR-9).

### What the dispatcher is *not*

Not a global event bus and not a wrapper around every click. Plain taps,
buttons, inputs, and uncontended resize handles never route through it. It
exists only where ≥2 gesture consumers contend for the same pointer stream
(rows, edges, contended handles, contended editor surfaces).

## Phase 2 — the left rail (gated)

**Go/no-go gate:** Phase 2 starts only after Phase 1's row arbitration has
landed and survived on main, and mobile-nav pain still justifies it. If
cancelled, Phases 0–1 stand alone (this is the debate's decided asymmetry:
the dispatcher is designed against the rail topology from day one via the
edge-ownership table, so the rail is a consumer, not a rewrite).

### Model

Extend, don't invent: the desktop `WorkspacePanel` Radix `Tabs` rail is
already the navigation model. Per TR-22 the mobile rail's destination set
is the `railTabs` contract's actual six tabs — Files, Changes, Agents
(subagents), Shells (terminals), Tasks (todos), **Browser** (a first-class
full-screen panel on mobile, not dropped) — plus two rail-level extensions
that are not workspace tabs: **Sessions** (today's full-screen sidebar
list) and **Settings**. The rail composes `railTabs` and adds the two
extensions above it; it does not fork the contract. Mobile gets:

- a narrow persistent rail anchor (icon column) instead of the full-screen
  sidebar overlay + top-right FAB (TR-23) — the six hand-maintained
  FAB→tab mappings in `AppShell` and the per-tool `MobilePanelDrawer`
  states collapse into one open/close machine;
- expansion by tap / start-edge swipe (dispatcher client, TR-14) /
  keyboard, with `WorkspacePanel`'s ARIA + roving tabindex inherited
  (TR-25);
- safe-area insets applied once at the rail boundary from the existing
  `--omnigent-safe-*` vars (TR-26);
- Android back and iOS edge-pan dismiss rail layers per TR-14's dismissal
  stack (TR-24); displaced header controls (the #3589/#4551 strip) get
  rail homes (TR-27).

Migration runs the rail behind a temporary fallback alongside the FAB/drawer
path, then removes the duplicates once task coverage is proven.

## Delivery mechanics

- Every PR cites the TR-ns it satisfies; reviewers judge against the spec.
- Phase 0 is parallelizable (capability hook, breakpoint consolidation,
  hover-affordance fallback, five hook migrations ≈ eight small PRs).
- Phase 1 lands the dispatcher core with the session-row integration as its
  proving consumer, then #3985 rebases onto it instead of shipping a bespoke
  swipe.
- Salvage before rewrite: audit `feature/mobile-slide-actions-v2` and
  `train/polly/session-row-gesture-fix` for reusable recognizer/test code;
  cherry-pick with history noted in PR descriptions.
- UI-visible PRs carry demo media and `tests/e2e_ui` coverage per repo
  policy (TR-31).

## Risks

| Risk | Mitigation |
|---|---|
| Dispatcher becomes a framework nobody else adopts | Scope it to contended surfaces only; four named integration points, no speculative API |
| Phase 2 stalls like #1395 | Hard gate + fallback path; Phases 0–1 are complete deliverables on their own |
| Threshold tuning churn | TR-11's table: values, ranges, and invariants in one module, changed only with device-matrix retest |
| Native shells drift from the ownership table | TR-14's table is the single source; shell PRs that add gesture heuristics are rejected per TR-28 |
| Scroll perf regression on long session lists | TR-16's testable gates (passive-listener + no-commit assertions, Moto G-class fps budget) |
