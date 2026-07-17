# Project defaults editor — resolved-first UX redesign

Design spec for reworking the **Session defaults** section of the Project
Settings dialog (`web/src/shell/ProjectSettingsDialog.tsx`).

Status: **approved design** (2026-07-16). Design produced by gpt-5.6-sol at
xhigh effort, grounded in the repository; three product forks resolved by the
owner (see *Decisions*). Next: implementation plan.

---

## Motivation

A project carries a versioned `ProjectDefaultsBundle` that future sessions
inherit (resolution order: server → project → session, field by field). The
current editor has three UX problems:

1. **Free-text everywhere.** `harness`, `model`, `reasoning_effort`, `host_id`
   are typed by hand — error-prone and undiscoverable.
2. **Inherited fields are blank.** When a field is left at its default the input
   is *disabled and empty*, so the user cannot see the value a session will
   actually get.
3. **The tri-state selector is heavy.** An Inherit checkbox + Clear button +
   value input per field exposes backend `absent | null | value` mechanics the
   user shouldn't have to reason about.

### Goals (product requirements)

1. Proper controls (dropdowns / pickers) instead of free text for enumerable
   fields.
2. Every field is **populated with its actual resolved value** even in the
   default state, so behavior is visible.
3. **DRY** — reuse the hooks/catalogs that already populate this data; no new
   endpoints, no redundant fetches, no per-keystroke calls.
4. **Change = override.** No separate override selector: a field is pre-filled
   with its inherited value, and editing it away from that is the override, with
   a per-field reset.
5. **Responsive** across display sizes (usable at ~360px through wide desktop).

---

## Decisions (resolved forks)

| Fork | Decision |
|---|---|
| **Tri-state handling** | **Collapse to two visible states — `Inherited` / `Overridden`.** No affordance creates a `null`; a loaded `null` is shown as Inherited and rewritten to *absent* on the next save (normalization). Whitespace-only text normalizes to absent too. |
| **Refactor scope** | **Full decomposition + shared extraction.** New `web/src/shell/project-settings/` module, extract NewChat's `HostOption`, lift model/effort catalog resolvers into a shared leaf module. |
| **Non-Claude model picker** | **Gate + degrade gracefully.** Full dropdown for claude-native; other harnesses use a warm cached catalog when present, otherwise gate ("choose a harness / the agent decides") and always keep a stored value visible as a synthetic option. No new backend. See [Follow-ups](#follow-ups). |

---

## Repository constraints that shape the design

- The resolved-defaults preview is `GET` for the **persisted** project
  (`omnigent/server/routes/projects.py`, `useResolvedProjectDefaults` in
  `web/src/hooks/useConversations.ts`). It does **not** accept an unsaved draft,
  and its result **already includes the project's own stored override** — so an
  overridden field's resolved value equals its stored value.
  **Consequence:** provenance (is this field overridden?) must come from
  `Object.hasOwn(defaults_json, field)`, *not* from comparing the draft to the
  resolved value.
- `ResolvedProjectDefaults` returns *effects*, not raw inputs: it exposes
  `workspace` and `git` but **not** `repo_url` or a raw managed `default_branch`.
  Those two fields must read from the raw `defaults_json`.
- The only server-level default is `host_type="external"`
  (`omnigent/projects/resolver.py`); every other server field is absent. So for
  every non-`host_type` field, `null` and absent resolve identically today —
  which is why the normalization decision is behavior-preserving under schema v1.
- A persisted `host_type: null` is **invalid**: it overrides the server default
  and makes the resolved endpoint fail. The editor must detect and repair it.
- `useAgents()` returns agent `{id, name}` pairs discovered from sessions —
  **not** canonical `harness_override` values. Harness options must come from the
  harness-label/catalog layer NewChat uses, not from `useAgents()`.
- No global Codex (or other native-wrapper) model catalog exists; those model
  lists come from a live session snapshot. Only claude-native has a static,
  project-usable catalog.

---

## (a) Interaction model

### Two visible states

Each field is **Inherited** (project property absent) or **Overridden** (project
supplies a concrete value). The control stays enabled and populated in both
states. There is no Inherit checkbox, Clear button, or tri-state selector.

Illustrative editor model (not a backend change — still serializes the existing
bundle):

```ts
type PersistedField =
  | { kind: "absent" }
  | { kind: "value"; value: string }
  | { kind: "legacy-null" }; // loaded null; normalized away on save

interface DefaultFieldDraft {
  persisted: PersistedField;      // from raw defaults_json
  resolvedAtOpen: string | null;  // effective value a session gets now
  displayedValue: string | null;
  resetRequested: boolean;
  touched: boolean;
}
```

### Baseline vs. provenance (two separate concepts)

1. **Effective value at open** — what a session receives now. From
   `useResolvedProjectDefaults`, supplemented by the raw bundle for the fields
   the response doesn't expose (`repo_url`, managed `default_branch`).
2. **Provenance** — whether the project stored the field, via
   `Object.hasOwn(defaults_json, field)`.

Override detection:

```ts
isOverridden =
  !resetRequested &&
  ( persisted.kind === "value" ||
    (persisted.kind === "absent" && normalizedDraft !== resolvedAtOpen) );
```

- A field initially absent is prefilled from the resolved preview; editing away
  from it creates an override; editing back to the baseline returns it to
  Inherited automatically.
- A field with a **stored value stays Overridden until Reset**, even if it
  happens to match the current effective value — so we never silently unpin it.
- Dirty state is computed separately: candidate serialized bundle vs. normalized
  original bundle.

### Reset

Per field, shown only when overridden or changed. Removes the property from the
candidate bundle and shows the server-inherited result. When the inherited
result is `null`, controls show a **behavioral label** rather than a blank box:

| Field | Reset / inherited-null label |
|---|---|
| Host type | `External` (server default) |
| Host | "No pinned host" |
| Harness | "Agent default" |
| Model | "Harness default" |
| Effort | "Harness default" |
| Workspace | "No project workspace" |
| Repository | "No repository — managed sessions start empty" |
| Branch | "Repository default" |

### Null normalization & invalid host_type

- Loaded `null` (any field) → shown as **Inherited**, omitted on next save.
  Whitespace-only text → absent.
- `host_type: null` → display `External` with a **destructive "Invalid saved
  value"** status (not a normal inherited badge), explain that saving repairs it
  by removing the null, keep Save enabled, never offer null in the select.

---

## (b) Per-field control mapping

| Field | Control | Data source | Free text? | Conditional |
|---|---|---|:---:|---|
| `host_type` | Native `<select>` External / Managed | `resolved.host_type`; raw bundle for provenance | No | Always visible |
| `repo_url` | `Input` | Raw bundle (no server value exists) | Yes | Managed only; retained inactive override summarized under External |
| `default_branch` | `Input` | Raw bundle; `resolved.git?.base_branch` confirms External behavior | Yes | Both modes; helper text changes by mode |
| `host_id` | Rich host picker | `useHosts()` + `resolved.host_id` | No | External only; force-reset when switching to Managed |
| `workspace` | `Input` | `resolved.workspace`, raw bundle | Yes | External only; stored Managed workspace shown as invalid/legacy |
| `harness` | Harness picker | shared harness-label/catalog layer (`/v1/harnesses` via `useBrainHarnessLabels`, `agentLabels.ts`, `nativeCodingAgents.ts`) | No | Always visible |
| `model` | Harness-dependent picker | Claude static catalog; matching live-cached native catalog where available | No | Gated until a harness/model catalog is known |
| `reasoning_effort` | Model-dependent picker | shared effort resolver; Claude static efforts; Codex model metadata | No | Gated until harness/model support is known |

Text fields stay editable while inherited and show a behavioral placeholder when
the resolved value is null. **Focus alone does not create an override — only a
value change does.**

---

## (c) Reuse & wiring plan

### Resolved values
`useResolvedProjectDefaults(open ? projectId : null)` supplies `host_type`,
`host_id`, `workspace`, `harness_override`, `model_override`,
`reasoning_effort`, external `git.base_branch`, and `row_version`. Do **not**
set the baseline until the project GET and resolved preview `row_version` values
match; if they differ, refetch the preview once. `git.branch_name` is minted
during resolution and must never be shown as a persistent default. `repo_url`
and managed `default_branch` come from raw `defaults_json`.

### Hosts
`useHosts({ enabled: open && effectiveHostType === "external" })`. Extract
NewChat's private `HostOption` renderer into a shared component (name,
online/offline status, unknown/stored id). Unlike session creation, an **offline
host stays selectable** as a future default with a warning.

### Harnesses
Extract a shared harness-option adapter from the existing label/catalog code
(canonical ids, display labels, alias normalization). `NEW_SESSION_HIDDEN_AGENTS`
and `AGENT_PICKER_DESCRIPTIONS` describe agent rows, not harness ids — leave them
in NewChat.

### Models & effort
Shared catalog resolver (`modelOptionsForHarness` / `effortOptionsForHarness`),
not copied NewChat conditions:
- `claude-native` **only**: `CLAUDE_NATIVE_MODELS` + extracted
  `CLAUDE_NATIVE_EFFORTS`. *(Review decision: matches the in-session picker,
  where only the Claude Code wrapper gets the static catalog — SDK sessions
  have no native model picker either, so `claude-sdk` gates.)*
- All other harnesses (codex-native, Cursor/Kiro/OpenCode/Pi, SDK, unknown):
  **gate**, preserving any stored value as a synthetic option. *(Review
  decision: live-session snapshot catalogs are per-session state with no
  project-scoped identity; wiring them in was dropped in favor of the
  project-wide catalog endpoint tracked in #2734.)*
- Harness options themselves come from `harnessOptionsForProject`: the
  canonical native wrappers (Claude Code, Codex, …) merged ahead of the brain
  catalog — `/v1/harnesses` deliberately excludes natives.
- **Never union models across harnesses** (identical ids differ in support/effort
  metadata). A stored model/effort stays visible as a synthetic option even when
  its catalog is unavailable. When harness is null, gate with: *"Choose a project
  harness to select a compatible model. Without one, the future session's agent
  decides."*

### API-call budget (normal open)
- `GET /v1/projects/{id}` — 1. Resolved preview — exactly 1.
- `useHosts` — 0 warm / 1 cold. Harness labels — 0 warm / 1 cold `/v1/harnesses`.
- Models/efforts — static modules or already-loaded state; 0.
- **No `useAgents()`, no `/v1/sessions` scan, no per-keystroke preview, no new
  endpoints.** Save = 1 PATCH (captured ETag) + optional rename POST + existing
  invalidation. A `row_version` mismatch / Retry / 412 recovery may refetch the
  preview once — correctness recovery, not normal budget.

---

## (d) Provenance & reset UX

Each field header: label (left); `Inherited` / `Overridden` badge (right); Reset
button when overridden; behavioral source text / validation below the control.

- `Inherited`: neutral outline badge, muted foreground.
- `Overridden`: subtle `border-primary/40 bg-primary/5` (not a warning color).
- Invalid/incompatible: destructive border + helper text.
- **Never signal state by color alone.**

### Test hooks (replace text-coupled `${field}-state`)
- `project-default-${field}-field` / `-control` / `-provenance`
  (`data-provenance="inherited" | "overridden" | "invalid"`) / `-reset` /
  `-hint` / `-error`; picker rows `project-default-${field}-option-${id}`.
- Tests assert semantic attributes + accessible labels, not prose.

### Impact on existing `ProjectSettingsDialog.test.tsx`
1. *"loads … value, null, and absent bundle states"* — mock the resolved hook;
   `repo_url-state`→`data-provenance="overridden"`; `model-state` null→
   `inherited` + visible "Harness default"; `harness-state`→`inherited`; keep
   metadata + host-type assertions.
2. *"saves edits with the captured If-Match…"* — keep ETag/rename assertions;
   expected `defaults_json` drops the normalized `model: null`; fetch count can
   stay 3 with module-level catalog mocks.
3. *"preserves set-to-inherit and set-to-clear transitions…"* — **replace** with
   "reset removes an override and editing creates one": click
   `project-default-repo_url-reset`, edit another field, assert provenance,
   assert reset field absent + changed field has a string value; remove all
   Clear/null assertions.
4. *rename collision* — UI assertions stand; add shared resolved/catalog mocks.
5. *stale ETag* — keep; also assert the resolved baseline refetches/invalidates
   after the latest project loads.
6. *422 message* — keep; add resolved/catalog mocks.

Add focused tests: null normalization, invalid null host type, live host-type
switching, dependent model/effort catalogs, unknown-option preservation.

---

## (e) Responsive layout

- Move scrolling to the body: shell `max-h-[85vh] overflow-hidden`; body
  `min-h-0 overflow-y-auto`; footer stays pinned (keeps iOS keyboard handling in
  `web/src/components/ui/dialog.tsx`). Keep `sm:max-w-2xl`.
- <640px: one field per row. `sm`: two-column grid for short controls;
  Repository URL / Workspace / Host type span both columns; Model + Effort may
  share a row only when both active.
- ~360px ≈ 328px content: all controls `w-full min-w-0`; picker content
  `max-w-[calc(100vw-2rem)]`,
  `max-h-[var(--radix-dropdown-menu-content-available-height)]`, `overflow-y-auto`.
- Badge/reset headers `flex-wrap`; ≥44px touch targets; Reset is a text label
  (not icon-only); long values truncate in triggers with full value via `title`.

---

## (f) Component decomposition

Under `web/src/shell/project-settings/`:
- `ProjectDefaultsEditor.tsx` — coordinates resolved data, raw bundle,
  dependencies, validation, section layout.
- `projectDefaultsDraft.ts` — **pure**: parsing, null normalization, provenance,
  reset transitions, serialization.
- `InheritedFieldShell.tsx` — label, badge, hint/error linkage, Reset.
- `DefaultTextField.tsx` — `Input` adapter (behavioral placeholder,
  empty→inherit normalization).
- `DefaultSelectField.tsx` — native select adapter (host type, simple enums).
- `DefaultHostPicker.tsx` — host selection, online/offline/unknown, rich rows.
- `DefaultHarnessPicker.tsx` — canonical harness ids + shared labels.
- `DefaultModelPicker.tsx` — harness-dependent catalog, unavailable/legacy handling.
- `DefaultEffortPicker.tsx` — model-dependent effort options.

Shared: `web/src/components/HostOption.tsx` (used by NewChat + settings); a leaf
catalog module holding Claude effort constants + pure
`modelOptionsForHarness` / `effortOptionsForHarness`.

`ProjectSettingsDialog.tsx` keeps project loading, name/description, CAS save,
rename, metadata, dialog composition — and stops owning every field's rendering.

---

## (g) Edge cases

- **Managed + host_id:** switching to Managed stages `host_id` for reset with
  "Pinned host removed because managed hosts are provisioned by the server";
  Cancel restores; Save cannot serialize both.
- **Managed + stored Workspace:** invalid/legacy (managed `workspace` must be a
  repo spec); show raw value with Reset, don't hide.
- **External + `default_branch` but no host:** inline error on Host + branch;
  disable Save until a host is pinned or branch reset.
- **External + stored `repo_url`:** preserve as inactive override (restores when
  switching back to Managed); compact "Managed-only default retained" row + Reset.
- **host_type=null:** repair state above. **Other nulls:** show inherited, omit
  on save.
- **Unknown/legacy harness or model:** synthetic selected option `legacy-id (not
  in current catalog)`; preserve untouched; Reset always works; changing its
  dependency requires a valid replacement or reset.
- **Harness change:** keep downstream model/effort only if compatible; if a
  loaded catalog proves incompatibility, show an error rather than silently
  clearing. Harness unresolved → gate model/effort, retain persisted value, no
  cross-harness union.
- **Offline host:** visible with status + warning; save allowed. **Unknown
  stored host:** show id as unavailable, preserve, offer Reset. **Empty host
  list:** "No connected hosts", keep inherited no-host state (no free-text
  fallback).
- **Empty model catalog:** disabled "No project-wide model catalog available"
  picker; preserve/reset existing values.
- **Resolved preview loading:** skeleton/disabled controls (name/description may
  render). **Error:** keep raw stored overrides visible, disable edits whose
  inherited value is unknown, Retry; invalid host null stays repairable.
- **`row_version` mismatch:** never mix a project bundle with a preview from a
  different version. **412 on save:** reload project details + resolved baseline
  before re-enabling default edits.

---

## (h) Testing outline

**Pure unit (`projectDefaultsDraft.ts`):** absent→inherited display; edit
away→value; edit back→absence; existing value stays overridden until Reset;
null→absence; host null→invalid repair; serialization never emits null; unknown
values survive; managed transition removes host id; external branch without host
errors.

**Component:** resolved values populate controls; provenance attributes; reset
for text/select/picker; live host-type conditionals; inactive overrides
discoverable/resettable; harness→model→effort dependency updates; loading/
error/retry; offline/unknown host; save disabled only for proven-invalid combos;
keyboard + `aria-describedby`.

**E2E** — extend `tests/e2e_ui/sessions/test_sidebar_projects.py` with one
settings flow: create project via API with known defaults → open settings →
assert resolved Host type + provenance → External→Managed (Host disappears,
Repo/branch appear) → fill repo+branch, save → GET and assert exact
`defaults_json` → reopen and assert overrides → reset one field, save, assert
property absent. Add a **360px viewport variant**: dialog stays in-viewport,
headers wrap, dropdowns don't overflow, Save reachable.

---

## Follow-ups

- **Model-picker inconsistency across harnesses** *(tracking issue — see owner
  request 2026-07-16).* claude-native gets a full project model dropdown, but
  Codex and other native wrappers have **no global model catalog** (lists come
  from a live session snapshot), so their pickers gate/degrade. This is an
  intentional v1 limitation under the "no new endpoint/fetch" constraint, but it
  produces an inconsistent picker experience. Proper fix: a project-wide
  model-catalog surface (e.g. `GET /v1/harnesses/{harness}/models`) so every
  harness gets a populated dropdown. Tracked upstream as
  [omnigent-ai/omnigent#2734](https://github.com/omnigent-ai/omnigent/issues/2734).
- **Resolved response lacks parent provenance** — it returns server + current
  project, so it can't reveal a server value hidden behind a project override.
  Fine for v1 (server defaults are minimal/known); revisit if server defaults
  expand (endpoint should return per-field source).
- **Null normalization is forward-looking** — behavior-preserving under schema
  v1; future non-host server defaults would make null meaningful and should
  trigger an explicit schema/UI revision.
- **Managed raw Workspace** can produce a later session-create error (the bundle
  validator is looser than `SessionCreateRequest`); the editor surfaces/repairs
  it rather than hiding it.
