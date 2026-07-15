# PR3a rework brief — migrate OFF omni_project labels (startup backfill + one-way deprecation), drop the dual-write bridge

Decision (user): Projects ships as ONE feature (with PR3b web); we migrate **off** the legacy
`omni_project` label entirely — it's incomplete and doesn't scale — and `project_id` becomes the
**sole authority**. This reworks the PR3a commit (3ac77cb7) on `feat/projects-surface-adapt`.
Backend only. Work test-first. Env: prefix `uv` with `OMNIGENT_SKIP_WEB_UI=true`; run new + related
tests.

Context: PR3a already repointed READS to `project_id` (list_projects, `?project_id=` filter,
`GET /sessions/projects[?archived=true]`) and added the owner-only move/unfile PATCH with the
`moved`-origin snapshot — **keep all of that.** The problem a 5-engine review found is the
*bidirectional* dual-write bridge (label↔project_id) is incomplete and diverges (BLOCKER). Since we
now deprecate labels rather than sustain them, the fix is simpler than completing the bridge.

## Changes

1. **Drop the bidirectional bridge.** Remove any code that syncs `project_id → omni_project label`
   (the reverse direction). Labels are NEVER read for project membership anymore — `project_id` /
   the `projects` table is the sole read authority (already true in PR3a's reads). A label may remain
   on a row as vestigial data but is ignored.

2. **All legacy `omni_project` project-label WRITES → deprecated + forwarded ONE-WAY to project_id.**
   Cover EVERY path that today accepts `omni_project` as project membership — the review found the
   create paths bypass the bridge (the BLOCKER): JSON `POST /v1/sessions` `body.labels`
   (~sessions.py:12762), multipart create (~:12968), the PATCH `omni_project` special-case
   (~:15871), and any policy write path (runtime/policies/engine.py:489 if it routes the label). For
   each, when an `omni_project` label is present:
   - Emit a structured **deprecation warning** (once per write): "omni_project label is deprecated;
     forwarded to project_id — migrate to the project_id API."
   - Resolve the label value → the caller/owner's project (`get_by_name`, create via the store if
     absent) and set `metadata.project_id` + write the membership snapshot via the SAME
     `set_project_membership` path (so the `project_id ⇒ snapshot` invariant holds atomically).
   - If BOTH an explicit `project_id` AND an `omni_project` label are given and they disagree → 400
     (keep the existing validation). If they agree, no-op the label.
   - Do NOT treat the label as authority and do NOT re-emit it as a read source.
   This makes the forward path total (fixes BLOCKER 1b) and makes the "reverse sync" finding (1a)
   moot because labels are never read.

3. **Startup migration (new).** Add a server-startup hook (FastAPI lifespan/startup — see app.py) that:
   - Enumerates every `workspace_id` that still has `omni_project` labels (a distinct query on
     `SqlConversationLabel` where `key="omni_project"`).
   - For each, under `workspace_scope(ws)`, runs the existing idempotent, ledger-guarded
     `backfill_legacy_labels(conversation_store)` (PR1). Auto-migrates unambiguous labels.
   - For an ambiguous result (`requires_mapping`), LOG a structured deprecation notice including the
     mapping plan (owner / normalized name) for manual resolution — **NON-BLOCKING**: never fail or
     stall startup on ambiguity or on any single-workspace error (catch + log + continue).
   - Logs a one-line summary (N migrated, M need manual mapping). Idempotent — no-ops once migrated
     (ledger), safe to run every startup. Skip cleanly if `project_store`/stores aren't configured.
   - Keep it fast: only touch workspaces that actually have the labels.

4. **Fix migration downgrade** (`dd4e5f6a7b8c_allow_moved_project_snapshots.py`): in `downgrade()`,
   first `UPDATE session_project_snapshots SET snapshot_origin='live' WHERE snapshot_origin='moved'`
   BEFORE reinstating the `IN ('live','backfill')` CHECK, so it round-trips after the feature has
   written `moved` rows (currently reproduced-broken on SQLite/PG/MySQL).

5. **Fix archived TOCTOU:** re-check the target project's archived state at the point of
   `set_project_membership` (or re-validate immediately before the membership write) so a project
   archived between route validation and the write → 409, not a silent successful file.

## Tests (test-first)
- Every label-write path (JSON create, multipart create, PATCH) forwards to `project_id`, writes the
  snapshot, and logs the deprecation warning; `project_id`+label disagreement → 400; agreement → ok.
- Startup migration: seed `omni_project` labels in TWO workspaces → run the hook → both migrated;
  an ambiguous label → logged + non-blocking (startup completes); idempotent re-run is a no-op.
- Downgrade round-trip with a `moved` row present: upgrade → write a `moved` snapshot → downgrade →
  constraint reverted, no failure, the row now `live`.
- Archived TOCTOU: project archived between validation and write → 409.
- Re-move invariant (move A→B updates the one snapshot in place, no duplicate; a create-time `live`
  snapshot becomes `moved` on an actual move).
- Unresolved legacy name alias in the filter → empty result (not all rows).
- Remove/replace the obsolete bidirectional-bridge tests.

## Constraints
No DB FK / partial index / `# type: ignore` / `# noqa`. Every query workspace-scoped. Chunk any
large `IN (...)`. `project_id` is the sole read authority.

## Commit
New commit on `feat/projects-surface-adapt`:
`feat(projects): migrate off omni_project labels — startup backfill + one-way deprecation; drop dual-write bridge; fix downgrade/TOCTOU`
Summary: the write-forwarding + deprecation, the startup hook, the downgrade/TOCTOU fixes, tests, and
anything left out.
