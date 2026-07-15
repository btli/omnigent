# PR3a implementation brief — backend: label→project_id surface adaptation (Codex, test-first)

Repoint the backend "projects" surfaces from the legacy `omni_project` label to the first-class
`project_id` primitive (PR1 `projects` table + `metadata.project_id`; PR2 create-time inheritance).
Map of record: `docs/claude/PROJECTS_LABEL_ADAPTATION_MAP.md`. **This is the BACKEND half (PR3a);
the web half is PR3b — do NOT touch `web/`.** Stacked on PR2 (`feat/projects-inheritance`). Work
test-first. **Env:** prefix `uv` with `OMNIGENT_SKIP_WEB_UI=true`; run new + related tests only.

## Design decisions (implement exactly)
1. **`project_id` is the READ authority.** Project existence, membership, and filtering come from the
   `projects` table + `metadata.project_id`, NOT from scanning labels. (PR1's backfill populates
   `project_id` for legacy labeled sessions.)
2. **Membership can change post-create, and MUST preserve PR2's invariant** "a session with
   `project_id` set ALWAYS has a snapshot row." So **moving a session into a project** sets
   `metadata.project_id` AND writes a `session_project_snapshots` row with
   `snapshot_origin="moved"`, `project_row_version=NULL`, empty resolved `defaults_json` — in ONE
   OmnigentBase transaction. (A post-hoc move does NOT re-inherit defaults; the snapshot is
   membership provenance. Distinct from PR2's create-time `live` snapshot.) **Unfiling** (clearing
   `project_id`) deletes the membership snapshot in the same transaction.
3. **Compat bridge — NO hard freeze in PR3a.** Keep the legacy `omni_project` label paths FUNCTIONAL
   so today's web keeps working until PR3b migrates it. Where a legacy label write still occurs,
   **dual-write**: also update `metadata.project_id` (resolve the label value → the owner's project,
   creating it via the store if absent) so the two never diverge. The actual write-freeze is a
   later cutover step (PR3b), not this PR.

## Backend changes (file:line anchors from the map — verify against real code)
**Reads → `project_id` (`stores/conversation_store/sqlalchemy_store.py`, `server/routes/sessions.py`):**
- `list_projects()` (~`sqlalchemy_store.py:1600`): return the owner's projects from the `projects`
  table that have ≥1 **non-archived** member (via `metadata.project_id`). Return project identity
  (id + name), not bare names. Keep a thin name-list accessor if an internal caller needs it.
- `list_conversations()` project filter (~`:1825`): accept a **`project_id`** (`""`/None →
  `metadata.project_id IS NULL` (unfiled); `"<id>"` → `== id`). Also accept the legacy `project`
  (name) param as a **compat alias** that resolves name→id for the caller's owner.
- `GET /sessions/projects` (~`sessions.py:14350`): return `[{id, name}]`, owner-scoped, from the
  `projects` table. **Add an `?archived=true` variant** → projects that have ≥1 **archived** member
  (this replaces the web's client-side full-scan of archived sessions; the archived-search surface).
- The `project` query param on `GET /v1/sessions` (~`:14546`) → thread a `project_id` (keep the
  name alias).

**Write API → `metadata.project_id` (`server/routes/sessions.py` PATCH `/sessions/{id}`):**
- Add a first-class `project_id` field on the session PATCH body: set = move (decision #2, with the
  `moved` snapshot), `""`/null = unfile (delete membership snapshot). Owner-checked; attaching to a
  wrong-owner/nonexistent project → 404; an archived project → 409.
- The existing `omni_project` label special-case (~`:15434`) stays as the bridge (decision #3):
  when it fires, ALSO apply the `project_id` change (dual-write) rather than only the label.

## Constraints
- No DB FKs, no partial indexes, no `# type: ignore`/`# noqa`. All new queries filter by
  `current_workspace_id()`. Chunk any `IN (...)` over a potentially-large id list (reuse the
  `_id_chunks` helper PR1 fixes added).
- Do NOT hard-freeze label writes. Do NOT touch `web/`. Do NOT build persistent storage / leases.

## Tests (test-first)
- `list_projects` reflects the `projects` table (owner-scoped), hides all-archived projects, and
  includes a project with a mix of archived + non-archived members.
- `?archived=true` returns exactly projects with ≥1 archived member (and excludes purely-live ones).
- filter by `project_id` (== id, and IS NULL for unfiled); the legacy name alias resolves to the
  same result.
- move: PATCH `project_id` sets membership + writes a `moved` snapshot (invariant holds);
  wrong-owner project → 404; archived project → 409; unfile deletes the membership snapshot.
- dual-write bridge: a legacy `omni_project` label write also lands `metadata.project_id`.
- no-regression: a session with no project behaves as before; a legacy labeled session (post
  PR1-backfill) is discoverable by `project_id`.

## Gate + commit
`OMNIGENT_SKIP_WEB_UI=true uv run ruff check --fix && OMNIGENT_SKIP_WEB_UI=true uv run ruff format &&
OMNIGENT_SKIP_WEB_UI=true uv run pytest <new/related>` green. Commit on `feat/projects-surface-adapt`:
`feat(projects): PR3a — backend project_id read/write parity + archived-projects endpoint (label bridge, no freeze)`
Print a concise summary: reads repointed, the move/unfile write API + snapshot handling, the
dual-write bridge, tests, and anything deferred to PR3b.
