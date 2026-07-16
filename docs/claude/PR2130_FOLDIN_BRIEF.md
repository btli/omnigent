# Fold-in brief — upstream PR #2130 (project folder right-click menu + rename) on first-class projects

Port the FEATURES of closed upstream PR #2130 ("fix(web): project folder right-click menu + rename")
onto the new first-class Projects stack. #2130 was built on the OLD implicit label model (rename =
re-label every member session, with partial-failure toasts). On first-class projects this collapses
dramatically: **rename = one atomic owner-scoped API call; membership never moves.** Do NOT port the
label-relabel machinery — only the UX.

- Work in `/Users/bryanli/Projects/btli/omnigent-pr3b` (branch `feat/projects-web`, tip of the
  Projects stack). **Web only** (`web/src/**`) — the backend rename API already exists.
- REFERENCE implementation (read for UI structure/patterns, do NOT copy label logic):
  `/Users/bryanli/Projects/btli/omnigent-worktrees/pr2130` — see `web/src/shell/Sidebar.tsx`
  (`ProjectMenuItems`, `ProjectEditRow`, the right-click wiring via the `MenuComponents` bundle
  pattern) and `web/src/shell/Sidebar.projectActions.test.tsx` (the 8-test suite to adapt).

## Backend contract (already live — verify in omnigent/server/routes/projects.py)
- `GET /v1/projects/{id}` → project JSON + `ETag: "<row_version>"` header.
- `POST /v1/projects/{id}/rename` body `{name}`, header `If-Match: "<row_version>"` →
  404 wrong-owner/nonexistent · **409 name collision** · **412 stale/missing If-Match** · 200 new
  project JSON (+ fresh ETag).
- Client rename flow: GET the project for its ETag, then POST rename with If-Match. On 409 → inline
  collision error; on 412 → toast ("project changed elsewhere") + invalidate/refetch; empty or
  unchanged name → exit edit without any request.

## Features to implement
1. **Right-click context menu on the project folder header** — opens the same actions as the kebab.
   One shared menu body (`ProjectMenuItems`) rendered by both surfaces via the existing
   `MenuComponents` bundle pattern (exactly how conversation rows do it — mirror the reference).
2. **Menu actions** (both surfaces):
   - **New session** — navigates to `/?project_id=<id>` (the same target as the header pencil link).
   - **Rename project** — enters inline edit (`ProjectEditRow`, matching the conversation-row
     rename UX): autofocus/select, Enter or blur commits, Escape cancels; collision (409) shows an
     inline `aria-describedby`-linked error and stays in edit; unchanged/empty exits silently;
     412/other failure → toast, exit edit, refetch projects.
   - **Delete project** — reuse the EXISTING delete flow/dialog (already in the kebab today); just
     make sure it's offered from both surfaces via the shared body.
3. **`useRenameProject` hook** (web/src/hooks/useConversations.ts): mutation that does the GET-ETag →
   rename POST; on success invalidate `["projects"]`, `["project-sessions"]`, archived-projects and
   any project-name-displaying caches. NO member re-labeling (that was the old model).
4. **Expand/collapse state survives rename.** Check how `expandedProjects` is keyed
   (web/src/shell/Sidebar.tsx ~:1097/:1275 — if it still keys by NAME, switch it to the project
   **id** so rename can't collapse the folder and the persisted state is rename-proof; migrate the
   persisted value gracefully — stale name entries may simply be dropped).
5. **Tests** — adapt the reference `Sidebar.projectActions.test.tsx` to the id-based world: both
   surfaces show New session + Rename + Delete; New session links to `/?project_id=<id>`;
   right-click opens the same actions as the kebab; rename enters inline edit and issues the
   API call with If-Match; 409 collision → inline error (Enter and blur paths); unchanged name →
   no request; 412 → toast; delete confirm flow unchanged; folder stays expanded across a rename.

## Constraints
- No `@ts-ignore` / `eslint-disable`. Match the file's existing idioms (MenuComponents bundle,
  Radix patterns, aria labeling). Web only; no Python; no openapi.json edit needed.
- Env: `NODE_ENV=development` for any npm command (a production NODE_ENV skips devDeps). Node 26
  jsdom lacks localStorage — tests touching it fail locally for that env reason ONLY; run
  `npm run type-check` and the new/changed test files, and note any localStorage-env failures
  rather than chasing them.

## Gate + commit
`NODE_ENV=development npm run type-check` clean; new/adapted tests pass (modulo the documented
localStorage-env failures); `npx oxlint` on changed files clean. Commit on `feat/projects-web`:
`feat(projects): fold in #2130 — project folder right-click menu + inline rename on first-class projects`
Print: features implemented, how rename maps to the API, expandedProjects keying decision, test
results, anything deferred.
