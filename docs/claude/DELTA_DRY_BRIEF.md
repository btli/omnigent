# Delta simplification + DRY pass (round 2) — consolidated findings

Apply to `/Users/bryanli/Projects/btli/omnigent-pr3b` (branch `feat/projects-web`). Behavior-
preserving except S4/D2's noted semantic reconciliation. Test-first where semantics are touched.
Env: `OMNIGENT_SKIP_WEB_UI=true` for uv, `NODE_ENV=development` for npm.

## Backend

D1. **Collapse the 3 copies of the project CAS/If-Match kernel.**
   - `transfer` (`stores/project_store/sqlalchemy_store.py:211-268`) is a near-verbatim copy of
     `_mutate` (:270-327). Extend `_mutate` with an optional `prepare(session, row)` hook;
     `transfer` becomes a `_mutate` call with `values={"owner_principal_id": new_owner}` and a
     `prepare` that provisions the destination `SqlUser` (same savepoint idiom — extract
     `_ensure_user_row(session, user_id)` used by BOTH `create` and the hook; absorbs the 3rd
     SqlUser-provisioning copy).
   - `archive_project_with_sessions` (`conversation_store/sqlalchemy_store.py:2061-2170`) hand-rolls
     the same kernel AND imports the project store's private `_to_entity`. Extract the shared
     precondition+guarded-update+reload kernel to a module-level function in the project store's
     module (public name), used by `_mutate` and callable by the conversation store for the project
     leg. Also collapse its internally-duplicated precondition block (read phase :2107-2117 vs
     immediate phase :2133-2143) into one nested helper — PRESERVE the two-phase order (stale ETag
     must fail before AP timestamps advance).
   - Error strings/codes must stay byte-identical (tests + HTTP contract).

D2. **Single-home the "owner's member sessions of a project" predicate** — 3 diverging forms:
   `member_ids` (:2083-2105, no PUBLIC exclusion), `list_conversations` owned_by+project_id branch
   (:2382-2391, no PUBLIC exclusion), `list_project_label_assignments` (:2233-2236, excludes
   `RESERVED_USER_PUBLIC`). One helper/filter-expression shared by all three. **Semantic
   reconciliation: the shared form EXCLUDES `RESERVED_USER_PUBLIC`** (a public grant can never be an
   owner; belt-and-suspenders consistent with the backfill's form). Add a test pinning that a
   PUBLIC-granted session is neither listed as a member nor archived by include_sessions.

D3. **Share the snapshot→row mapping with the backfill.** The backfill write
   (`project_store/sqlalchemy_store.py:623-640`) hand-maps every field + its own `json.dumps`
   canonicalization beside the canonical `_project_snapshot_values`
   (`conversation_store/sqlalchemy_store.py:818-836`) — drift breaks the backfill's field-by-field
   idempotency re-check (:525-560). Move `_project_snapshot_values` to a shared home (beside
   `LiveProjectSnapshot` in entities, or a small db-mapping helper both stores import) and use it in
   both. Do NOT touch `set_project_membership`'s keep-old-defaults else-branch (intentional D2
   provenance semantics).

D4. **entities/project.py tidies:** delete dead `ProjectIdentity` (:28-33 + the two export lines in
   entities/__init__.py — orphaned by the endpoint convergence; verify zero in-tree consumers);
   `LiveProjectSnapshot.backfill = dataclasses.replace(cls.moved(project_id), snapshot_origin="backfill")`;
   unify `ProjectBackfillResult` empty defaults to `()` style consistently.

D5. **Align `defaults_schema_version` defaults on the constant:** abstract `ProjectStore.create`
   (`project_store/__init__.py:46`) hardcodes `1` while the concrete uses `DEFAULTS_SCHEMA_VERSION`
   — align the abstract to the constant. `routes/projects.py:31` (`CreateProjectRequest`) also
   hardcodes `1`: reference the constant there too (the request default should track the server's
   current schema).

D6. **session_integration.py internal dedupe:** one place for the ×3 "Project storage is not
   configured" guard and the ×2 `get_for_use→None→NOT_FOUND` pair; single-source the moved-snapshot
   condition duplicated between `prepare_json_session_project` and `prepare_multipart_session_project`;
   have `resolve_legacy_project_label` take the already-computed label-presence/`legacy_project_requested`
   from the caller instead of re-deriving (:33 vs sessions.py:15465). Skip the both-set double-fetch
   (rare path, low payoff) unless it falls out naturally.

## Tests

D7. Point `tests/server/routes/test_projects.py` at the shared `sessions_test_app` conftest builder
   (kill the second FastAPI skeleton + duplicate error-handler); relocate the migrated
   `test_list_projects_*` tests from `tests/stores/test_conversation_store.py` (~:4386-4460) into
   `tests/stores/test_project_store.py` (they exercise `SqlAlchemyProjectStore.list` now); dedupe
   the `/v1/projects` list coverage to one route-level `archived_members` test.

## Web

D8. Extract a `ProjectCreateRow` component (input + inline error, `errorId` prop) shared by
   `LandingProjectPicker` (NewChatDialog ~:796-855) and `ProjectPickerMenu` (Sidebar ~:3316-3380) —
   container-specific wrappers stay at call sites. Drop one of the two duplicated
   "shows an inline error when project creation fails" component tests (keep the hook test + one
   component test).

D9. Delete the dead `project_id?: string` field on `createBundledSession`'s metadata type
   (`web/src/lib/sessionsApi.ts:460`) — no caller passes it, the server schema forbids it, and the
   follow-up-PATCH mechanism is the documented one.

D10. Fold `useProjects`/`useArchivedProjects` identical queryFns into one `fetchProjects(query)`
   (minor).

## Gate + commit
Backend: ruff + pytest over projects-related tests. Web: type-check + touched test files (+oxlint
changed files; Node-26 localStorage env failures noted, not chased). Do NOT git commit (orchestrator
commits). Print a concise per-item applied/skipped summary.
