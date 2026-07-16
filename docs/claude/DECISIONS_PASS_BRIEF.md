# Decisions pass — transfer op, move provenance, endpoint convergence, server-side delete

Four user decisions (2026-07-15) to implement in `/Users/bryanli/Projects/btli/omnigent-pr3b`
(branch `feat/projects-web`). Test-first. Env: `OMNIGENT_SKIP_WEB_UI=true` for uv,
`NODE_ENV=development` for npm. NOT in scope: name-uniqueness changes (user decided: keep hard
per-owner uniqueness — do not touch it).

## D1 — Transfer-ownership escape hatch (backend)
Problem: an auth-off install files everything under `RESERVED_USER_LOCAL`; enabling auth later
strands those projects (owner-mismatch → 404) and owner is immutable. Add:
- `ProjectStore.transfer(project_id, current_owner, new_owner, *, expected_row_version)` — re-keys
  `owner_principal_id`, provisions the new `SqlUser` if absent (same savepoint pattern as create),
  advances `row_version`, enforces the per-owner name-uniqueness on the DESTINATION (a collision
  with the new owner's existing name → 409), If-Match/412 like every mutate.
- `POST /v1/projects/{project_id}/transfer` body `{new_owner_principal_id}`, `If-Match` required —
  callable by the CURRENT owner only (404 wrong-owner as usual). Structured log for audit.
- Tests: happy re-key (old owner 404s after, new owner sees it); destination name collision → 409;
  stale If-Match → 412; wrong caller → 404.

## D2 — Preserve provenance on move (backend)
Decision: a move must NOT erase the create-time inheritance record. In
`set_project_membership` (conversation store), when an existing snapshot row is updated for a move:
set `project_id` to the new project and `snapshot_origin="moved"`, but **preserve
`defaults_json`, `project_row_version`, `defaults_schema_version`, and `created_at` as they were**
(the record of what was supplied at create). The INSERT branch (never-snapshotted session being
filed) keeps the empty moved marker as today. Update the affected tests (the re-move test asserted
empty defaults after a move of a live-snapshotted session — it now asserts the original resolved
defaults survive with origin flipped). Update the snapshot factory/builder accordingly (only the
insert path uses the empty marker).

## D3 — Converge the two project-list endpoints (backend + web)
After projects-persist-when-emptied, `GET /v1/sessions/projects` (live variant) duplicates
`GET /v1/projects`. Converge:
- Backend: give `GET /v1/projects` an `archived_members=true` query variant (or equivalent param)
  that returns the owner's projects having ≥1 archived member session (what the settings filter
  needs — this is the ONLY capability `/sessions/projects?archived=true` had over `/v1/projects`).
  Response stays the full project rows (additive superset of {id,name}).
- Web: `useProjects()` + `useArchivedProjects()` fetch from `/v1/projects` (map to the {id,name}
  shape the components use, or consume the full row — your call, minimal churn).
- Remove `GET /v1/sessions/projects` (route + `list_projects` route wiring + its tests migrate to
  the /v1/projects equivalents). Nothing external shipped; the web is the only client.
- Keep the conversation-store `list_projects` store method ONLY if the projects route needs it for
  the archived-members query; otherwise move that query into the project store where it belongs.

## D4 — Server-side delete-project op (backend + web)
Replace the client-side fan-out (archive N sessions then archive the project — partial-failure
risk) with one server operation:
- Extend `POST /v1/projects/{project_id}/archive` with optional body/query
  `include_sessions=true`: archives all member sessions (workspace+owner-scoped, chunked, based on
  metadata.project_id) then archives the project row, If-Match on the project as today. Return a
  summary `{archived_sessions: N}`. Member-session archival goes through the conversation store's
  existing archive mechanism (find it — likely metadata.archived flag) — do NOT hand-roll a second
  archival path.
- Web `useDeleteProject`: one call to the archive endpoint with include_sessions, drop the
  fan-out + the separate project-archive step (keep mutateProjectWithIfMatch for the ETag).
- Tests: archive with include_sessions archives members + project atomically per-DB; without the
  flag behaves as before; partial semantics documented (sessions archive is same-DB as metadata —
  check and state whether it's one transaction; if AP-DB conversations rows are also flagged,
  document the two-phase order).

## Gate + commit
Backend: ruff + pytest over projects-related tests. Web: type-check + touched test files (Node-26
localStorage env failures noted, not chased). Commit on `feat/projects-web`:
`feat(projects): decisions pass — owner transfer, provenance-preserving moves, one projects endpoint, server-side delete`
Print a concise per-item summary.
