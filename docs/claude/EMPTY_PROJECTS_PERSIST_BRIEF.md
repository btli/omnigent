# Brief — projects persist when emptied (user decision 2026-07-15)

UX decision: **a project row persists and stays visible in the sidebar even when it has no (live)
member sessions.** The old "project exists only while ≥1 non-archived member carries it" rule was
label-era behavior; first-class projects are explicit entities. Work test-first in
`/Users/bryanli/Projects/btli/omnigent-pr3b` (branch `feat/projects-web`, tip of the stack).
Env: `OMNIGENT_SKIP_WEB_UI=true` prefix for uv; `NODE_ENV=development` for npm.

## Changes

1. **Backend rule** (`omnigent/stores/conversation_store/sqlalchemy_store.py` `list_projects`):
   the live variant (`archived=False`) returns **ALL of the owner's non-archived project rows**,
   regardless of member count — drop the ≥1-non-archived-member join/EXISTS requirement. The
   `archived=True` variant is UNCHANGED (still: projects with ≥1 archived member — it drives the
   archived-sessions filter, where memberless projects are useless). Update docstrings and the
   store/route tests (the "hides all-archived/memberless projects" expectations flip).

2. **Delete project must now archive the PROJECT ROW too** (`web/src/hooks/useConversations.ts`
   `useDeleteProject` + the Sidebar dialog): today it archives all member sessions and relied on
   the memberless project dropping out of the list — with rule #1 that no longer happens, leaving a
   ghost row. After archiving the member sessions, also archive the project itself via the
   first-class API: `GET /v1/projects/{id}` (ETag) → `POST /v1/projects/{id}/archive` with
   `If-Match`. Invalidate `["projects"]` etc. Update the dialog copy if it references the implicit
   model. (Archived projects are already excluded from the live list — the store filters
   `archived_at IS NULL` on the project row.)

3. **Remove the "last session deletes the project" special-cases** (web/src/shell/Sidebar.tsx):
   the kebab remove flow and the drag "ungroup" flow confirm removal only-when-last-session because
   removing the last member used to delete the implicit project. Removing a session from a project
   is now ALWAYS non-destructive (the project persists) — drop the last-session confirmation and
   the `fetchProjectSessionIds` pre-check that powered it; unfiling is silent in both flows. Update
   the stale comments that describe the implicit model.

4. **Docs**: `docs/claude/PROJECTS_MVP.md` has uncommitted edits in the worktree (deferred
   contextual-awareness follow-up + this decision context) — include them in your commit. Also add
   one line to MVP-3's spirit in the doc if a natural place exists: "projects persist when emptied;
   only archive removes them from the live list" (keep it brief).

## Tests (test-first)
- Store: an owner project with ZERO members appears in `list_projects(owner)`; one whose members
  are all archived ALSO appears; an ARCHIVED project row does not. `archived=True` variant
  unchanged (assert existing behavior).
- Route: `GET /v1/sessions/projects` includes a memberless project.
- Web: delete-project flow archives member sessions AND calls the project archive API with
  If-Match; removing a session from a project (kebab + move-out) does NOT prompt the last-session
  confirmation and the project remains listed.

## Gate + commit
Backend: `OMNIGENT_SKIP_WEB_UI=true uv run ruff check --fix && ... ruff format && ... pytest <touched>`.
Web: `NODE_ENV=development npm run type-check` + touched test files (note Node-26 localStorage env
failures, don't chase). Commit on `feat/projects-web`:
`feat(projects): projects persist when emptied — sidebar lists all live projects; delete archives the project row`
Print a concise summary.
