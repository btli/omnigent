# Web maintainability pass — consolidated from 6-stream review

Apply to `/Users/bryanli/Projects/btli/omnigent-pr3b` (branch `feat/projects-web`). **WEB ONLY
(`web/src/**`) — do not touch Python** (a parallel pass owns it; it is ADDING a nullable
`project_id` field to the session list/detail payloads — you may rely on that field existing on the
wire when you finish W-F1/W-F2, but do not edit the server). `NODE_ENV=development` for all npm
commands; Node-26 localStorage test failures are environmental — verify with type-check + the
touched test files and note env failures rather than chasing.

## Architecture fixes (behavior changes — these close real gaps)

W-F1. **Send `project_id` in the session-create body** (`web/src/shell/NewChatDialog.tsx`
   ~:2829-2846): today the composer creates the session WITHOUT `project_id`, then PATCHes
   membership afterwards — so a web-created session gets an empty `moved` snapshot instead of the
   `live` inheritance record, and the whole defaults-inheritance machinery is bypassed for the
   primary UI path (this also blocks the planned per-project instructions follow-up). Include
   `project_id` in the POST /v1/sessions body when a project is selected; drop the post-create
   PATCH for the filing (keep error handling: if create fails, nothing to unfile). Update the flow
   tests accordingly.

W-F2. **Stop reading the phantom membership field — or read the real one** (`web/src/shell/Sidebar.tsx`
   ~:1073 grouping, ~:1303 auto-expand, ~:2364 kebab current-project; `web/src/lib/sessionListCache.ts`
   ~:124/147): these read a `project_id` off session objects that the server has not been sending
   (always undefined in prod; folders only work via per-folder refetch). The backend pass is adding
   `project_id` to the session payloads — wire these readers to the real field (type it on the
   session interface) and remove any dead fallback logic that existed only because the field was
   missing. Verify the sidebar grouping/auto-expand tests reflect the real data flow.

W-F3. **Project-create failure must not be a silent no-op** — both pickers swallow a failed
   `createProject` (no error UI, the "new project" row just sits there): `Sidebar.tsx`
   `ProjectPickerMenu` ~:3271-3387 and `NewChatDialog.tsx` `LandingProjectPicker` ~:747-877. Fixed
   once via W-D2's shared picker: show an inline error state on failure.

## DRY consolidations (behavior-preserving)

W-D1. **`web/src/hooks/projectQueries.ts`**: exported key constants (`PROJECTS_KEY`,
   `PROJECT_SESSIONS_KEY`, `PROJECT_NEWEST_KEY`, plus the existing `ARCHIVED_PROJECTS_KEY`),
   `invalidateProjectQueries(qc, { sessions: boolean })` replacing the ~9 hand-pasted invalidation
   blocks in `useConversations.ts` (:395, :466, :527, :584, :603, :780, :821, :956) and the partial
   copy in `NewChatDialog.tsx` (~:2836), and `removeSessionsFromListCaches(qc, ids)` for the 3
   restated splice loops (:455, :573, :594; + SessionUpdatesProvider usage if it fits). Preserve the
   intentional distinction: post-splice sites omit `project-sessions` — make that the `sessions:false`
   option, documented at the helper.

W-D2. **Shared project picker** between `Sidebar.tsx` `ProjectPickerMenu` and `NewChatDialog.tsx`
   `LandingProjectPicker` (the latter's docstring literally says it mirrors the former): extract a
   `useProjectPickerState()` hook owning filter text, create-new state, commit/cancel keying, and
   (new) create-error state — plus a shared body component if it extracts cleanly WITHOUT
   over-parameterizing (if the menu-item vs button rendering divergence makes the shared component
   leaky, share only the hook + the create-new row). Include W-F3's error UI once.

W-D3. **`mutateProjectWithIfMatch(projectId, action)`** helper: the GET→read-ETag→POST-with-If-Match
   sequence is duplicated between `renameProject` (~:758) and the archive step in `useDeleteProject`
   (~:940) with divergent error types. One helper, both callers, consistent 409/412 surfacing.

W-D4. **NewChatDialog reuses `moveConversationToProject`** (~:2830-2843 hand-rolls the same
   PATCH `{project_id}`) — NOTE: after W-F1 this call site may disappear entirely for the create
   flow; only keep/consolidate whatever remains.

W-D5. Extract the duplicated sidebar visibility closure (`Sidebar.tsx` ~:1320-1327 vs ~:1337-1342,
   keyboard-nav vs shift-select universes) into one `visibleProjectConversations(...)`; and a small
   `buildSessionListParams()` for the 3 restated URLSearchParams blocks in `useConversations.ts`
   (~:205, :839, :866).

## Out of scope (do NOT do)
Splitting Sidebar.tsx into modules (parked); ProjectEditRow/ConversationEditRow unification (the
simplifier judged it a leaky abstraction); converging onto /v1/projects for the sidebar list;
any Python edit.

## Gate + commit
`NODE_ENV=development npm run type-check` clean; run the touched test files (+
`npx oxlint` on changed files). Commit on `feat/projects-web`:
`refactor(projects): web maintainability pass — create-with-project_id, real membership field, shared picker/queries`
Print a concise summary listing each item applied/skipped with reason.
