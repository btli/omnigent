# PR3b implementation brief — web: label→project_id migration (Codex, careful edits)

Migrate the **web** "projects" surfaces from the legacy `omni_project` label (keyed on project NAME)
to the first-class `project_id` primitive, consuming the backend APIs PR3a added (this branch is
stacked on PR3a). Map of record: `/Users/bryanli/Projects/btli/omnigent/docs/claude/PROJECTS_LABEL_ADAPTATION_MAP.md`.
**Web only — do NOT touch `omnigent/` Python.** Do NOT flip the label-write freeze (that's a
separate deliberate cutover step after this lands). Worktree: `/Users/bryanli/Projects/btli/omnigent-pr3b`,
branch `feat/projects-web`.

## Toolchain (this repo uses npm for web, NOT bun)
Work under `web/`. Use `npm` (there is a `web/package-lock.json`). Best-effort verify with
`npm run typecheck` and `npm run lint` from `web/` if the toolchain is healthy. The system Node may
be too new (Node 26) and can break the local web build/tests — if install/typecheck fails on an
environment/toolchain issue (NOT your edits), NOTE it in your summary and proceed; do NOT rabbit-hole
on Node/lockfile problems. Prefer type-correct edits verified by reading the types over fighting the
env.

## Backend contract you're consuming (from PR3a — verify signatures in the stacked Python if unsure)
- `GET /v1/sessions/projects` → returns project objects `{id, name}` (was `string[]` names).
- `GET /v1/sessions/projects?archived=true` → projects with ≥1 archived member (replaces the web's
  client-side archived-session full scan).
- `GET /v1/sessions?project_id=<id>` (and `project_id=""`/absent semantics) for filtering (the legacy
  `?project=<name>` still works as a compat alias, but migrate to `project_id`).
- Session PATCH accepts a `project_id` field: set = move, `""`/null = unfile (replaces the
  `{labels:{omni_project:…}}` write).

## Web changes (file:line anchors from the map — verify against real code)
1. **`web/src/hooks/useConversations.ts`** (the project hub, "Project hooks" ~:654):
   - `PROJECT_LABEL_KEY` usage for project membership → remove/replace with `project_id`.
   - `useProjects()` (~:665) → consume `{id,name}` objects (update the type + all call sites).
   - `moveConversationToProject`/`useMoveToProject` (~:677/:696) → PATCH `{ project_id }` (empty/null
     = unfile) instead of `{ labels:{ omni_project } }`.
   - project-session paging `fetchAllProjectSessionIds`/`fetchProjectSessionIds`/`fetchProjectSessionsPage`/`useProjectSessions` (~:716-790) → `?project_id=`.
   - `useDeleteProject` (~:810) → key on `project_id`.
   - `useNewestProjectSession(project)` (~:942) → `?project_id=` (query key too).
   - Query keys `["projects"]`/`["project-sessions"]`/`["project-newest-session"]` — keep stable or
     rename consistently; ensure invalidations still line up.
2. **`web/src/shell/NewChatDialog.tsx`**: `LandingProjectPicker` (~:731) picks by `project_id`
   (options are `{id,name}`, match by id); `?project=` URL param (~:1884) → carry `project_id`;
   create-time filing (~:2709 main / prefill worktree ~:2865) → send `project_id`, not the label.
   "New project…" must call the real create-project endpoint (POST /v1/projects) and use the returned
   id.
3. **`web/src/shell/projectPrefill.ts`**: anchor the prefill state on `project_id` (was the name
   string); the newest-session lookup uses `?project_id=`.
4. **`web/src/pages/SettingsPage.tsx` `ArchivedSection`** (~:737): replace the client-side
   `fetchAllArchivedProjectNames` scan with `GET /v1/sessions/projects?archived=true`; filter by
   `project_id`; drop the `PROJECT_VALUE_PREFIX`/`ALL_PROJECTS_VALUE` name-encoding (stable ids make
   it unnecessary). Remove/repoint `useArchivedProjectNames` and `ARCHIVED_PROJECT_NAMES_KEY`.
5. **`web/src/lib/sessionListCache.ts`** (~:139): the push-delta membership check reads
   `conv.labels[omni_project]` → read `conv.metadata?.project_id` instead. Update the duplicate
   `PROJECT_LABEL_KEY` (:24).
6. **`web/src/shell/Sidebar.tsx`** (~:3170): per-project links `/?project=<name>` → `?project_id=<id>`.

## Constraints
- Web only. No Python. No `# type: ignore`/`@ts-ignore`/`eslint-disable`. Do NOT flip the label
  freeze. Keep behavior identical from the user's POV (same UI, now keyed on ids).

## Commit
Commit on `feat/projects-web`:
`feat(projects): PR3b — web surfaces migrate omni_project label → project_id`
Print a concise summary: files changed, each surface migrated, the archived-scan replacement,
typecheck/lint result (or the env issue that blocked it), and anything deferred (the freeze flip).
