# Touch Interaction Specification

Status: proposed. Owns the end-state for touch input across the omnigent web
app, the Android/iOS WebView shells, and touch-capable desktop viewports.
Companion doc: [touch-interaction-design.md](touch-interaction-design.md)
(architecture and phasing).

## Why this spec exists

Touch work has repeatedly been attempted as isolated PRs and abandoned. The
session-row swipe train (#3154 → #3985, with #4060/#4065 absorbed into #4057
and then closed) produced working gesture-arbitration code — a unified row
gesture recognizer with coarse-pointer gating — that now lives only on
abandoned branches, paid for two-to-three times as duplicate commits across
rebased trains. The train did not fail on engineering; it failed because no
agreed end-state existed to make each PR's scope defensible in review.

This spec is that end-state. Every touch PR should be traceable to a
requirement below, and reviewers should judge PRs against this contract
rather than re-litigating the approach.

## Root causes (verified on main)

1. **No pointer-capability detection.** There are zero uses of
   `pointer: coarse`, `any-pointer`, `hover: none`, or `maxTouchPoints` in
   `web/src`. "Mobile" is inferred from viewport width alone.
2. **Drifting breakpoint encodings.** The md boundary is written at least
   four ways: `max-width: 767.98px` (`useIsMobileViewport`),
   `max-width: 767px` (`ChatPage`), `min-width: 768px` negated
   (`AppShell`, `Sidebar`), a JS constant `MD_BREAKPOINT = 768`
   (`useResizablePanel`, `useResizableCommentsPanel`), and
   `window.innerWidth < 768` evaluated once at injection time in the Android
   bridge (`NativeBridgeScript.kt`).
3. **Mouse-only resize.** All five resize hooks (`useResizablePanel`,
   `useResizableSidebar`, `useResizableInlinePanel`,
   `useResizableCommentsPanel`, `useResizableColumn`) start on `onMouseDown`
   and listen to window `mousemove`/`mouseup`. A touch user cannot resize any
   rail or panel on any device, including touch laptops shown the desktop
   layout.
4. **Unarbitrated gesture stack.** A session row simultaneously hosts native
   scroll, dnd-kit's 250 ms touch-hold drag sensor, Radix's ~700 ms
   long-press context menu (unreachable by touch — the drag sensor always
   wins), an experimental 12 px horizontal swipe (#3985), and iOS edge-pan
   handing its drag into the same sidebar. Each consumer was written ad hoc
   with no shared owner of the pointer stream.
5. **Dual mobile navigation topology.** Desktop models Files / Changes /
   Agents / Shells / Tasks / Browser as one tabbed rail (`WorkspacePanel`);
   mobile fragments the same destinations into a top-right FAB and per-tool
   full-screen `MobilePanelDrawer`s with hand-maintained FAB→tab mappings in
   `AppShell`. Fixes get re-derived per surface (safe-area insets fixed
   serially on four surfaces; the contested header strip that killed #3589
   was relieved, not resolved, by #4551).

## Requirements

Requirements are numbered `TR-n` (touch requirement). "Coarse pointer" means
the primary pointer matches `pointer: coarse` (a finger); capability, never
viewport width, gates touch behavior.

### Foundation: capability detection

- **TR-1** The web app MUST expose a single shared input-capability primitive
  reporting at least: primary pointer coarseness, hover capability, and any
  additional pointer types (`any-pointer`), reactive to changes (e.g. a
  convertible flipping modes, attaching a mouse).
- **TR-2** All touch-conditional behavior MUST gate on capability, not
  viewport width. Viewport width remains a *layout* concern only.
- **TR-3** There MUST be exactly one canonical encoding of each layout
  breakpoint, consumed by both CSS/media-query and JS call sites. The four
  drifting md encodings are consolidated onto it.
- **TR-4** The Android bridge's width check MUST NOT be evaluated once at
  injection time; native shells consume the web layer's capability/layout
  signal rather than re-deriving it.

### Resize

- **TR-5** Every user-resizable surface (sidebar, right rail, inline panels,
  comments panel, column resizers) MUST be resizable via touch and stylus:
  pointer events (`pointerdown`/`pointermove`/`pointerup` with
  `setPointerCapture`), not mouse events.
- **TR-6** Resize handles MUST have a coarse-pointer hit target of at least
  24 CSS px (44 px preferred where layout permits) without changing their
  visual weight on fine-pointer devices.
- **TR-7** Existing keyboard resize paths (arrow keys on separators) MUST be
  preserved; hooks that lack a keyboard path (`useResizableColumn`) MUST
  gain one.
- **TR-8** During an active resize drag, no other gesture consumer
  (scroll, swipe, drag-and-drop, text selection) may activate
  (`touch-action` and pointer capture enforced).

### Gesture arbitration (the dispatcher)

- **TR-9** Each interactive surface MUST have exactly one owner of its
  pointer stream — a gesture dispatcher that observes the stream and awards
  it to at most one intent: scroll, horizontal swipe, long-press (menu),
  long-press-drag (reorder), edge-swipe, or resize.
- **TR-10** Intent thresholds MUST be defined in one place with documented
  values (activation distances, hold durations, axis-lock angles, drift
  tolerances) such that competing regions are disjoint (e.g. the recognizer
  math from the abandoned train: hold-drag tolerance vs. swipe activation
  kept non-overlapping).
- **TR-11** The long-press context menu MUST be reachable by touch on every
  surface that offers it to mouse users (right-click parity). The current
  main-branch behavior — dnd-kit's 250 ms sensor permanently shadowing
  Radix's 700 ms long-press — is a defect this requirement forbids.
- **TR-12** A slow or hesitant swipe MUST NOT convert into a drag, and
  vertical scrolling MUST never be hijacked by a horizontal consumer
  (axis-lock before award).
- **TR-13** Edge gestures MUST have a single ownership table: which surface
  owns the left edge, the right edge, and hardware/browser back, in which
  stacking order. iOS edge-pan, Android back routing
  (`__omnigentNativeHandleBack`), and browser back/forward gestures resolve
  against this table rather than per-surface heuristics.
- **TR-14** Every gesture MUST have a non-gesture equivalent (visible
  control, kebab menu, or keyboard path). Gestures are accelerators, never
  the only route to a function.
- **TR-15** The dispatcher MUST NOT regress scroll performance: passive
  listeners wherever the consumer cannot preventDefault, no per-move React
  state updates while undecided, and no synchronous layout reads in the move
  path.

### Session-row touch actions (from #3985 / #3154 / #4057 / #4060 / #4065)

- **TR-16** Session rows MUST support swipe-revealed actions on
  coarse-pointer devices, arbitrated by the dispatcher (TR-9), with the
  specific review defects from #3154 excluded by acceptance tests: no row
  background bleed, correct action icon placement on narrow screens, no
  icon/content overlap.
- **TR-17** Row long-press MUST open the row/folder context menu when the
  press ends without drag movement; drag movement past tolerance converts to
  reorder and MUST suppress the menu (the #4065 requirement).
- **TR-18** Drag-to-ungroup (the #4057 drop zone) and any future drop
  targets MUST work with touch drag; drop targets MUST meet coarse-pointer
  hit-target size.
- **TR-19** Row gestures gate on pointer capability, not viewport width
  (already prototyped in the abandoned train), so foldables and touch
  laptops at ≥768 px get working row gestures.

### Mobile navigation (left rail)

- **TR-20** Mobile navigation MUST converge on a single persistent left-rail
  model that adapts the existing desktop `WorkspacePanel` tab contract:
  one canonical destination set (Sessions, Files/Changes, Agents, Shells,
  Tasks, Settings) with one open/close state machine, replacing the
  scattered FAB + per-tool `MobilePanelDrawer` topology.
- **TR-21** The rail MUST be a narrow collapsed anchor on phones (not the
  current full-screen `fixed inset-0` session list pinned open), expanding
  into the content panel; expansion/collapse is reachable by tap, by
  edge-swipe (via the dispatcher, per TR-13), and by keyboard.
- **TR-22** Hardware/system back (Android), the iOS edge gesture, and
  browser back MUST dismiss rail layers in a defined order consistent with
  TR-13's ownership table.
- **TR-23** The rail MUST inherit the tab semantics, ARIA labels, and
  roving-tabindex keyboard behavior of the existing `WorkspacePanel` Radix
  tabs; screen-reader users get one nav landmark, not a stack of bespoke
  overlays.
- **TR-24** Safe-area insets MUST be applied once at the rail/shell
  boundary (the `--omnigent-safe-*` CSS vars from the native bridges), not
  re-derived per drawer (the #4723 class of bug).
- **TR-25** Header-strip controls displaced by the contested-strip problem
  (#3589/#4551) get a defined home in the rail model; the header strip's
  drag-surface role is preserved.

### Native shell interop

- **TR-26** Web-side gesture ownership (TR-13) is the single source of
  truth; Android's back callback and iOS's edge-pan recognizer delegate to
  it. Shell code MUST NOT grow new gesture heuristics of its own.
- **TR-27** WebView-hosted browser-native behaviors (pull-to-refresh, text
  selection, back-forward swipe where enabled) MUST be explicitly claimed or
  released per surface via `touch-action` / `overscroll-behavior`, not left
  to default.

### Accessibility and quality gates

- **TR-28** All touch behavior MUST be covered by component tests that
  simulate pointer sequences (pointerdown/move/up with timing), including
  the specific historical defects listed in TR-16/TR-17 as regression
  tests.
- **TR-29** Touch changes to visible UI require demo media on the PR per
  the repo PR template, and `tests/e2e_ui` coverage (or the maintainer skip
  label) where the judge requires it.

## Non-goals

- No new gesture *vocabulary* beyond what the PR train already established
  (swipe actions, long-press menu, long-press drag, edge-swipe, resize).
  Pinch-zoom, multi-finger gestures, and haptics beyond the existing
  WebView haptic hook are out of scope.
- No native-app rewrites: Android/iOS shells keep their bridge architecture;
  only their delegation points change.
- Desktop mouse/keyboard interaction is unchanged except where hooks gain
  pointer-event equivalence.

## Decision record

A three-vendor adversarial debate (2026-08-13) argued dispatcher+rail vs.
dispatcher-only vs. rail-only. All seats endorsed the foundation layer
(TR-1..8). The dissent worth preserving: *committing the rail in-spec risks
scope creep; product evidence for the nav redesign is thinner than for the
gesture defects.* It is absorbed by phasing (see the design doc): the rail
phase has its own go/no-go gate, and if it is cancelled the earlier phases
stand alone. The asymmetry that decided the debate: a dispatcher designed
against the rail topology degrades gracefully to today's topology, but a
dispatcher designed against the FAB/drawer topology must be rewritten when
the rail lands — and the edge-ownership table (TR-13) is the part that
changes.
