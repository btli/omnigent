# PR3a hardening brief — atomic create forwarding, non-blocking startup, orphan/label cleanup (Codex, test-first)

Second review round on the reworked PR3a (commit 509efe4c). Codex (BLOCKER) + Gemini/agy (MAJOR)
caught a real durability gap two Opus lenses missed — fix it. Work test-first, in
`/Users/bryanli/Projects/btli/omnigent-adapt` (branch feat/projects-surface-adapt). Env: prefix `uv`
with `OMNIGENT_SKIP_WEB_UI=true`; run new + related tests.

Already verified correct by review (do NOT touch): the move/PATCH archived TOCTOU
(`set_project_membership` re-reads under `with_for_update` in its txn), the migration downgrade
`moved`→`live` sweep, tenancy/workspace scoping, the four-path label forwarding *presence*, the
re-move single-snapshot invariant.

## Fixes

1. **[BLOCKER] Make create-path project membership ATOMIC — no separate post-create write.**
   JSON create (`sessions.py` ~12765 create, ~12791 membership) and multipart create (~14775 /
   ~14783) currently `create_conversation(...)` then call `set_project_membership(...)` in a SEPARATE
   transaction. A crash / DB failure / archival between the two leaves a committed session with
   `project_id=NULL`, no snapshot, and the `omni_project` label already stripped → stranded,
   unrecoverable by the startup backfill. `create_conversation` ALREADY accepts a `project_snapshot`
   param and writes `metadata.project_id` + the snapshot atomically (PR2). So for the create paths:
   - Resolve the legacy `omni_project` label → the owner's project id (see fix 4 for ordering),
     build a **`snapshot_origin="moved"`, empty-defaults, `project_row_version=None`** snapshot, and
     pass it as `create_conversation`'s `project_snapshot` (same atomic path the explicit
     `project_id` create uses for its `live` snapshot). **Remove the post-create
     `set_project_membership` call from BOTH create paths.** Keep the deprecation warning.
   - Result: metadata.project_id + snapshot are written in `create_conversation`'s single
     transaction; there is no window that can strand a session. (Preserve the semantic distinction:
     explicit `project_id` create → `live` snapshot with resolved defaults; forwarded legacy label →
     `moved` empty snapshot, no re-inheritance.)

2. **[MAJOR] Close the create-path archived TOCTOU.** An explicit-`project_id` create validates
   archived via `get_for_use` in the route, then `create_conversation` commits later — a project
   archived in between is silently filed. Add the same lock+recheck `set_project_membership` uses:
   inside `create_conversation`, immediately before inserting the `project_snapshot`, re-read the
   `SqlProject` row (`with_for_update()` on OmnigentBase) and if `archived_at is not None` raise
   `OmnigentError(CONFLICT)` (→409), rolling back the create. Covers both explicit and forwarded
   membership.

3. **[MAJOR] Startup backfill must not gate boot.** `_backfill_legacy_project_labels_on_startup` is
   awaited in the lifespan (`app.py` ~1373) before the server yields; a slow/hung workspace stalls
   boot and fails readiness probes. Dispatch it as a fire-and-forget background task
   (`asyncio.create_task(...)`) from the lifespan so the app binds immediately; keep the existing
   per-workspace non-blocking try/except and log a completion summary from the task. Keep the
   underlying function directly callable (so tests exercise it synchronously).

4. **[MAJOR] No orphan project on a rejected (400) request.** `_resolve_legacy_project_label`
   (`sessions.py` ~12408) calls `project_store.create` (which commits) BEFORE the
   `project_id`-vs-label disagreement check, so `project_id=A` + a not-yet-existing label `B` → 400
   but leaves project `B` behind. Reorder: resolve with `get_by_name` first; run the disagreement
   check; only `create` the project AFTER the check passes (for all of JSON create, multipart, PATCH,
   policy). A 400 must create nothing.

5. **[MINOR] Delete vestigial `omni_project` labels once migrated/forwarded.** The backfill
   (`project_store/sqlalchemy_store.py` ~525) and the forwarding paths set membership but never
   delete the legacy label row. So the startup scan never shrinks AND a later re-file via the
   `project_id` API makes the stale label resolve to a different project → the next backfill emits
   `existing_project_binding` → a recurring, operator-unclearable "mapping required" warning on every
   boot. After a session's membership is successfully migrated (backfill) or forwarded (write paths),
   **delete its `omni_project` label row** in the same/adjacent transaction. (We are migrating OFF
   labels — dropping them post-migration is intended.)

## Tests (test-first)
- **Atomic create forwarding:** a JSON (and multipart) create with an `omni_project` label writes the
  session + `metadata.project_id` + a `moved` snapshot together; assert there is NO code path that
  commits the session before the snapshot (e.g. inject a failure into the snapshot write and assert
  the session row is NOT committed / the request errors without a `project_id=NULL` orphan).
- **Create-path archived TOCTOU:** project archived between route validation and `create_conversation`
  commit → 409, no session/snapshot written. (Mirror the existing `ArchiveBeforeMembershipStore` PATCH
  test for the create path.)
- **Non-blocking startup:** (a) a single workspace whose backfill RAISES → the hook logs the failure
  and the other workspaces still migrate, no re-raise; (b) stores unconfigured → `(0,0)`, no raise;
  (c) workspace-scan failure → `(0,0)`, no raise.
- **Orphan on 400:** `project_id=A` + label `B` (new name) → 400 AND no project `B` row exists after.
- **Vestigial label deletion:** after backfill and after a forward, the `omni_project` label row is
  gone; a second startup scan no longer returns that workspace (once all its labels are migrated).
- **Downgrade mixed rows:** a table with `moved` + `live` + `backfill` rows → downgrade rewrites only
  `moved`→`live`, leaves `live`/`backfill` untouched, constraint reverts, no failure.
- Route-level A→B move preserves the single snapshot (end-to-end).

## Constraints
No DB FK / partial index / `# type: ignore` / `# noqa`. Workspace-scoped queries. Chunk large `IN`.
`project_id` is the sole read authority.

## Commit
Amend into the PR3a commit is NOT required — add a NEW commit on `feat/projects-surface-adapt`:
`fix(projects): PR3a hardening — atomic create forwarding, non-blocking startup, no orphan-on-400, drop migrated labels`
Summary of each fix + tests + anything left out.
