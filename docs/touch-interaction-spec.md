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
   `web/src`. "Mobile" is inferred from viewport width alone, and
   hover-revealed controls have no touch fallback.
2. **Drifting breakpoint encodings.** The md boundary is written at least
   five ways: `max-width: 767.98px` (`useIsMobileViewport`),
   `max-width: 767px` (`ChatPage`), `min-width: 768px` negated
   (`AppShell`, `Sidebar`), a JS constant `MD_BREAKPOINT = 768`
   (`useResizablePanel`, `useResizableCommentsPanel`), and a hardcoded
   `window.innerWidth < 768` in the Android bridge's back handler
   (`NativeBridgeScript.kt`) — a fifth independent copy of the breakpoint,
   derived without reference to the web layer's definition.
3. **Mouse-only resize.** All five resize hooks (`useResizablePanel`,
   `useResizableSidebar`, `useResizableInlinePanel`,
   `useResizableCommentsPanel`, `useResizableColumn`) start on `onMouseDown`
   and listen to window `mousemove`/`mouseup`. A touch user cannot resize any
   rail or panel on any device, including touch laptops shown the desktop
   layout.
4. **Unarbitrated gesture stack.** A session row simultaneously hosts native
   scroll, dnd-kit's 250 ms touch-hold drag sensor, Radix's ~700 ms
   long-press context menu (unreachable by touch — the drag sensor always
   wins), and iOS edge-pan handing its drag into the same sidebar; the open
   #3985 PR proposes adding a 12 px horizontal swipe to the same rows. Each
   consumer was written ad hoc with no shared owner of the pointer stream.
5. **Dual mobile navigation topology.** Desktop models Files / Changes /
   Agents / Shells / Tasks / Browser as one tabbed rail (`WorkspacePanel`,
   via the `railTabs` contract); mobile fragments the same destinations into
   a top-right FAB and per-tool full-screen `MobilePanelDrawer`s with
   hand-maintained FAB→tab mappings in `AppShell`. Fixes get re-derived per
   surface (safe-area insets fixed serially on four surfaces; the contested
   header strip that killed #3589 was relieved, not resolved, by #4551).

## Requirements

Requirements are numbered `TR-n` (touch requirement). Directional terms are
LOGICAL: "start" = left in LTR, right in RTL; "end" is the opposite. The app
is LTR-only today; requirements use logical directions so an RTL locale
changes bindings, not the spec (TR-30).

### Foundation: capability detection and input model

- **TR-1** The web app MUST expose a single shared input-capability primitive
  reporting at least: primary-pointer coarseness (`pointer: coarse`), hover
  capability (`hover: hover`), and presence of any coarse pointer
  (`any-pointer: coarse`), reactive to changes (a convertible flipping
  modes, a mouse attaching or detaching).
- **TR-2** Capability queries gate AFFORDANCES and defaults (hit-target
  sizing, persistent vs hover-revealed controls, whether swipe hints render)
  — never per-event handling. Gesture RECOGNITION binds to the active
  sequence's `PointerEvent.pointerType`: `touch` and `pen` sequences get
  gesture semantics, `mouse` sequences keep mouse semantics — on every
  device, regardless of what the capability queries report. A touch laptop
  whose primary pointer is a fine trackpad (`any-pointer: coarse`, primary
  fine) therefore still gets working touch gestures the moment a finger
  touches the screen.
- **TR-3** There MUST be exactly one canonical encoding of each layout
  breakpoint, consumed by both CSS/media-query and JS call sites. The five
  drifting md encodings are consolidated onto it. Viewport width remains a
  *layout* concern only.
- **TR-4** Native shells MUST consume the web layer's layout/capability
  signal instead of re-deriving breakpoints. The Android back handler's
  hardcoded `innerWidth < 768` check is replaced by that signal.
- **TR-5** Controls revealed only on hover (e.g. the sidebar row and folder
  action buttons, `md:opacity-0 md:group-hover:opacity-100`) MUST be
  reachable on hover-less / coarse-pointer devices: persistent visibility,
  or an equivalent touch path (row menu, swipe action). This is what makes
  TR-20's touch-laptop audience real — today those devices can reveal no
  row action at all.

### Resize

- **TR-6** Every user-resizable surface (sidebar, right rail, inline panels,
  comments panel, column resizers) MUST be resizable via touch and stylus.
  The normative mechanism is pointer events
  (`pointerdown`/`pointermove`/`pointerup` with `setPointerCapture`); an
  equivalent mechanism is acceptable if it meets the same outcomes,
  including: drag continues when the pointer leaves the handle; `pointercancel`
  and capture loss abort cleanly to the pre-drag size or last applied size
  (never a half-state); listeners are removed on unmount; a second
  concurrent pointer is ignored (first pointer wins); drags over embedded
  iframes keep receiving moves (overlay shield or capture).
- **TR-7** Resize handles MUST have an invisible hit target of at least
  24 CSS px (44 px preferred where layout permits) on ALL devices — touch
  and pen alike, regardless of what capability queries report — without
  changing their visual weight. Pen-first devices commonly expose a fine
  pointer, and a 1 px visual handle is impractical to acquire with either
  input.
- **TR-8** Existing keyboard resize paths (arrow keys on separators) MUST be
  preserved; hooks that lack a keyboard path (`useResizableColumn`) MUST
  gain one.
- **TR-9** During an active resize drag, no other gesture consumer
  (scroll, swipe, drag-and-drop, text selection) may activate
  (`touch-action: none` on the handle and pointer capture enforced).

### Gesture arbitration (the dispatcher)

- **TR-10** Each contended surface MUST have exactly one owner of its
  pointer stream — a gesture dispatcher that observes the stream and awards
  it to at most one intent: scroll, horizontal swipe, long-press (menu),
  long-press-drag (reorder), edge-swipe, or resize.
- **TR-11** Recognition thresholds are normative, defined once in an
  exported constants module. Award is EXCLUSIVE and first-crossed-wins:
  the dispatcher evaluates each move against the thresholds below, the
  first award (scroll release, swipe, drag, menu) ends arbitration for the
  sequence, and hold arming is eligible only while no award has occurred
  and total travel stays within `HOLD_DRIFT_PX`. There is NO geometric
  disjointness guarantee between hold and swipe regions — exclusivity
  comes from this precedence rule, not from region math. Invariants
  DOMINATE tuning ranges: a retune is legal only if it satisfies every
  invariant AND passes the device-matrix retest required by the design
  doc; the ranges are guidance, not a grant.

  | Constant | Value | Meaning | Tuning range | Invariant |
  |---|---|---|---|---|
  | `SWIPE_ACTIVATION_PX` | 12 | horizontal travel that awards `swipe` when horizontal-first (see TR-13) | 8–16 | `> DRAG_TOLERANCE_PX` |
  | `HOLD_MS` | 250 | stationary hold that arms drag-or-menu (dnd-kit parity on main) | 200–350 | `< MENU_HOLD_MS` |
  | `HOLD_DRIFT_PX` | 20 | total travel that cancels hold eligibility (the train's hold tolerance in `c21fec929`; its 25 px circle was the separate scroll-fallback radius) | 8–20 | crossing any award threshold also cancels hold |
  | `MENU_HOLD_MS` | 500 | stationary hold that opens the context menu (replaces Radix's ~700 ms default) | 400–700 | `> HOLD_MS` |
  | `DRAG_TOLERANCE_PX` | 8 | movement after arming that converts hold → drag (dnd-kit parity) | 5–10 | `< SWIPE_ACTIVATION_PX` |
  | `EDGE_ZONE_PX` | 24 | width of the screen-edge strip that recognizes edge-swipe | 16–32 | — |

- **TR-12** Long-press sequencing is exact: a `touch`/`pen` press that stays
  within `HOLD_DRIFT_PX` arms at `HOLD_MS`; movement past
  `DRAG_TOLERANCE_PX` after arming awards `drag` (reorder) and permanently
  suppresses the menu for that sequence (#4065's requirement); remaining
  stationary until `MENU_HOLD_MS` opens the context menu at that moment
  (not on release), with a haptic cue where the platform supports it; once
  the menu is open the sequence is consumed — no drag can start from it,
  and the menu takes focus per its normal keyboard/AT contract. The menu
  MUST be reachable by touch on every surface that offers it to mouse users
  (right-click parity) — the current main-branch behavior, where dnd-kit's
  250 ms sensor permanently shadows Radix's long-press, is a defect this
  requirement forbids.
- **TR-13** Axis lock: while a sequence is undecided, vertical-first
  movement releases it to native scroll and horizontal-first movement
  (|dx| > |dy| at award time) is eligible for `swipe`. A slow or hesitant
  swipe MUST NOT convert into a drag, and vertical scrolling MUST never be
  hijacked by a horizontal consumer.
- **TR-14** Edge and back ownership is a single normative table; native
  shells and web surfaces resolve against it rather than per-surface
  heuristics:

  | Input | Owner | Notes |
  |---|---|---|
  | Start-edge swipe (within `EDGE_ZONE_PX`) | Rail/sidebar open (dispatcher) | iOS `UIScreenEdgePanGestureRecognizer` delegates its drag here; WebKit back-forward gestures stay disabled |
  | End-edge swipe | Released to browser/OS (back-forward where the platform provides it) | No app consumer may claim it without amending this table |
  | Android hardware/system back | Dismissal stack below, then WebView history | Routed via `__omnigentNativeHandleBack` (Android shell only) |
  | Browser back (`popstate`) | Topmost history-participating layer, else normal history navigation | Each dismissible layer pushes ONE history entry on open; `popstate` closes exactly one layer, matched by a state token so re-entrant pops cannot loop; transient popovers that don't push are not browser-back-dismissible |
  | Dismissal stack (top → bottom) | modal dialogs → context menus/popovers → expanded rail panel or `MobilePanelDrawer` → rail/sidebar overlay → in-app history → browser history/app exit | One layer per back press |

- **TR-15** Every gesture MUST have a non-gesture equivalent. The parity
  matrix is normative:

  | Gesture | Non-gesture equivalent |
  |---|---|
  | Row swipe action | Same action in the row's kebab/context menu |
  | Long-press context menu | Kebab button (visible per TR-5), `Shift+F10`/menu key |
  | Long-press drag (reorder) | Move up/down commands in the row menu |
  | Drag-to-ungroup | Ungroup command in the row/folder menu |
  | Edge-swipe rail open | Tap on the persistent rail anchor; keyboard shortcut |
  | Resize drag | Arrow keys on the focusable separator |
  | Back/dismiss gestures | Visible close/back buttons on each layer |

  Additionally, every gesture-reachable function MUST remain reachable with
  a touch screen reader active (VoiceOver/TalkBack intercept swipe and
  long-press before the app sees them), which the parity matrix guarantees
  only if the equivalents are exposed to AT — see TR-29.
- **TR-16** The dispatcher MUST NOT regress scroll performance, verified by
  testable gates: (a) no non-passive move listeners on scroll containers
  while a sequence is undecided; (b) no React state commits per
  `pointermove` while undecided (both assertable in component tests); and
  (c) a manual gate on the named reference configuration — Moto G Power
  (2023), Android 13, current stable Chrome — running a scripted,
  repeatable benchmark: populate 500 session rows, 5 s warm-up, then 30 s
  of scripted fling scrolls traced with Perfetto/Chrome tracing; pass =
  mean fps ≥ 55 over the scripted window AND p95 input latency < 100 ms.
  The script and trace config are committed alongside the dispatcher;
  substituting hardware requires amending this requirement, not ad-hoc
  judgment.

### Session-row touch actions (from #3985 / #3154 / #4057 / #4060 / #4065)

- **TR-17** Session rows MUST support swipe-revealed actions for
  `touch`/`pen` sequences, arbitrated by the dispatcher (TR-10), with the
  specific review defects from #3154 excluded by acceptance tests: no row
  background bleed, correct action icon placement on narrow screens, no
  icon/content overlap. Reveal direction is logical (swipe toward start
  reveals end-side actions) per TR-30.
- **TR-18** Row long-press follows TR-12 exactly (menu at `MENU_HOLD_MS`,
  drag conversion suppresses the menu).
- **TR-19** Drag-to-ungroup (the #4057 drop zone) and any future drop
  targets MUST work with touch drag; drop targets MUST meet coarse-pointer
  hit-target size (TR-7).
- **TR-20** Row gestures bind to `pointerType` per TR-2 — never to viewport
  width — so foldables and touch laptops at ≥ 768 px get working row
  gestures (already prototyped in the abandoned train's coarse-pointer
  gating commit).

### Context-menu and editor surfaces beyond the sidebar

- **TR-21** Every surface offering a mouse-only context menu or
  mouse-only manipulation MUST gain a touch path. The known inventory at
  time of writing: sidebar row/folder Radix menus (covered by TR-12),
  and the editor's `TableBubbleMenu` (mouse-only table row/column
  manipulation and context actions). The design doc assigns each an
  implementation phase; new mouse-only surfaces added later inherit this
  requirement.

### Mobile navigation (left rail)

- **TR-22** Mobile navigation MUST converge on a single persistent
  start-side rail that EXTENDS the existing desktop `railTabs` contract —
  one open/close state machine replacing the scattered FAB +
  per-tool `MobilePanelDrawer` topology. The destination set is the
  contract's actual six tabs — Files, Changes, Agents (subagents), Shells
  (terminals), Tasks (todos), Browser — plus two rail-level extensions that
  are not workspace tabs: Sessions (the session list, today's full-screen
  sidebar) and Settings. Browser remains a first-class destination on
  mobile (full-screen panel), not silently dropped.
- **TR-23** The rail MUST be a narrow collapsed anchor on phones (not the
  current full-screen `fixed inset-0` session list pinned open), expanding
  into the content panel; expansion/collapse is reachable by tap, by
  start-edge swipe (via the dispatcher, per TR-14), and by keyboard.
- **TR-24** Hardware/system back (Android) and browser back (via the
  history participation defined in TR-14) MUST dismiss rail layers in the
  order defined by TR-14's dismissal stack. iOS has NO edge dismissal
  gesture — TR-14 assigns the start edge exclusively to opening the rail
  and releases the end edge to the OS — so on iOS dismissal is by tap on
  the scrim or rail anchor, the close button, or keyboard.
- **TR-25** The rail MUST inherit the tab semantics, ARIA labels, and
  roving-tabindex keyboard behavior of the existing `WorkspacePanel` Radix
  tabs; screen-reader users get one nav landmark, not a stack of bespoke
  overlays.
- **TR-26** Safe-area insets MUST be applied once at the rail/shell
  boundary (the `--omnigent-safe-*` CSS vars from the native bridges), not
  re-derived per drawer (the #4723 class of bug).
- **TR-27** Header-strip controls displaced by the contested-strip problem
  (#3589/#4551) get a defined home in the rail model; the header strip's
  drag-surface role is preserved.

### Native shell and browser interop

- **TR-28** Web-side gesture ownership (TR-14) is the single source of
  truth; Android's back callback and iOS's edge-pan recognizer delegate to
  it. Shell code MUST NOT grow new gesture heuristics of its own. Per
  surface, browser-native behaviors are explicitly claimed or released via
  `touch-action` / `overscroll-behavior` — the normative baseline:

  | Surface | `touch-action` | `overscroll-behavior` | Notes |
  |---|---|---|---|
  | Chat transcript scroller | `pan-y pinch-zoom` | `contain` | no pull-to-refresh reload mid-session |
  | Session list rows | `pan-y pinch-zoom` (dispatcher claims horizontal) | `contain` | swipe/hold per TR-11..13; a second pointer joining cancels the sequence and yields to pinch-zoom |
  | Rail anchor + edge zone | `pan-y pinch-zoom` | `contain` | start-edge swipe per TR-14; second pointer yields to pinch-zoom |
  | Resize handles | `none` | — | TR-9 |
  | Text/editor content | browser default | default | selection untouched |
  | Embedded browser panel | browser default inside the frame | `contain` at the boundary | |

  Browser pinch-zoom MUST remain available app-wide (no
  `user-scalable=no`, no global `touch-action` that defeats zoom);
  surfaces claiming `pinch-zoom` away must justify it in review.

### Accessibility and quality gates

- **TR-29** All touch behavior MUST be covered by component tests that
  simulate pointer sequences (pointerdown/move/up with `pointerType` and
  timing), including the historical defects in TR-17 as regression tests
  and the TR-16(a)/(b) performance assertions. Keyboard acceptance tests
  cover the TR-15 parity matrix (reorder, resize separator values via
  `aria-valuenow`, menu invocation, focus order and announcements), and a
  manual AT pass (VoiceOver + TalkBack) verifies every parity-matrix row
  with the screen reader active.
- **TR-30** Directional requirements use logical start/end semantics
  (rail side, swipe reveal direction, edge ownership). The app is LTR-only
  today; if an RTL locale ships, the bindings flip and TR-17/TR-22/TR-14
  acceptance tests run mirrored — no requirement in this spec may hardcode
  physical left/right except the OS-owned end-edge behavior in TR-14.
- **TR-31** Touch changes to visible UI require demo media on the PR per
  the repo PR template, and `tests/e2e_ui` coverage (or the maintainer skip
  label) where the judge requires it.

## Non-goals

- No new gesture *vocabulary* beyond what the PR train already established
  (swipe actions, long-press menu, long-press drag, edge-swipe, resize).
  Multi-finger gestures and new haptics beyond the existing WebView haptic
  hook are out of scope. Browser pinch-zoom is not an app gesture and is
  explicitly preserved (TR-28).
- No native-app rewrites: Android/iOS shells keep their bridge architecture;
  only their delegation points change.
- Desktop mouse/keyboard interaction is unchanged except where hooks gain
  pointer-event equivalence.

## Decision record

A three-vendor adversarial debate (2026-08-13) argued dispatcher+rail vs.
dispatcher-only vs. rail-only. All seats endorsed the foundation layer
(TR-1..9). The dissent worth preserving: *committing the rail in-spec risks
scope creep; product evidence for the nav redesign is thinner than for the
gesture defects.* It is absorbed by phasing (see the design doc): the rail
phase has its own go/no-go gate, and if it is cancelled the earlier phases
stand alone. The asymmetry that decided the debate: a dispatcher designed
against the rail topology degrades gracefully to today's topology, but a
dispatcher designed against the FAB/drawer topology must be rewritten when
the rail lands — and the edge-ownership table (TR-14) is the part that
changes.
