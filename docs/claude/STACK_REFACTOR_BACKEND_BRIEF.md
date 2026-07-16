# Backend maintainability pass — consolidated from 6-stream review (simplifier, DRY, 3 adversarial engines)

Apply to `/Users/bryanli/Projects/btli/omnigent-pr3b` (branch `feat/projects-web`). **BACKEND ONLY —
do not touch `web/`** (a parallel pass owns it). Behavior-preserving except the explicitly-marked
fixes. Test-first where a fix changes behavior. Env: `OMNIGENT_SKIP_WEB_UI=true` prefix for uv.

## Correctness fixes (behavior changes — these are bugs)

B1. **Stamp `defaults_schema_version` on update** (`stores/project_store/sqlalchemy_store.py`
   `update()` ~:267): it validates the bundle against the CURRENT schema version but never writes
   `values["defaults_schema_version"] = DEFAULTS_SCHEMA_VERSION` — when v2 lands, updated rows stay
   stamped 1 and every session-create on them 422s. One line + a test (update a project, assert the
   stored version equals the constant).

B2. **Single-source the snapshot literals** — the `(project_row_version=None,
   defaults_schema_version=1, defaults_json {}/"{}", origin moved|backfill)` tuple is hand-rolled at
   5 sites and has ALREADY diverged (4 sites hardcode `1`, backfill uses the constant):
   `sessions.py:~12568` (JSON create), `:~14782` (multipart), `conversation_store/sqlalchemy_store.py`
   `set_project_membership` insert (~2049) AND update (~2062) branches, `project_store` backfill
   (~538). Create ONE factory — `LiveProjectSnapshot.moved(project_id)` classmethod on the entity
   (and an equivalent for backfill or an `origin` arg) — all referencing `DEFAULTS_SCHEMA_VERSION`.
   Collapse the set_project_membership insert/update branches onto one values-builder.

B3. **Startup task must be retained + supervised** (`server/app.py` ~:154, lifespan ~:1387): the
   `asyncio.create_task(...)` result is discarded → the task is GC-eligible before it runs, its
   exceptions are unobserved, and shutdown doesn't await/cancel it. Hold it on `app.state`, log
   exceptions via a done-callback, and cancel-and-await it in lifespan shutdown. Also inline the
   one-caller `_schedule_legacy_project_label_backfill` wrapper (its return was ignored) and inline
   `_STARTUP_BACKFILL_EXCEPTIONS = (Exception,)` to an honest `except Exception:` IF ruff permits
   (verify the lint gate; if a lint rule forced the constant, keep it and add a comment saying so).

B4. **Backfill: per-group progress + kill-switch** (`project_store/sqlalchemy_store.py`
   `backfill_legacy_labels` ~:465): today `if issues: return` aborts the ENTIRE workspace before
   applying anything — one ambiguous label freezes a workspace's migration forever (re-warned every
   boot). Change to: apply the CLEAN groups, hold back only the issue groups (still reported in the
   mapping plan). Add an env kill-switch (`OMNIGENT_DISABLE_LABEL_BACKFILL=1` skips the startup
   hook, logged) so the shim has an off-ramp. Tests: a workspace with 1 clean + 1 ambiguous group →
   clean group migrates, issue reported; kill-switch skips.

B5. **`project_id` on the session wire** (`server/schemas.py` `SessionListItem` ~:2204 +
   `SessionResponse`; `sessions.py` `_build_session_list_item` ~:2230; the Conversation entity load
   if needed): the server never emits membership, yet 4 shipped web features read
   `conv.project_id`/metadata (always undefined in prod). Add the field (nullable, additive) to the
   list/detail payloads. Test: a project-filed session's list item carries its project_id.

## DRY consolidations (behavior-preserving)

B6. **`get_or_create_by_name(owner, name) -> Project`** on `ProjectStore` with the archived→409 gate
   inside. The resolve→get-or-create→archived sequence exists TWICE at different layers:
   `sessions.py:_resolve_legacy_project_label` (~12394) and `conversation_store.forward_legacy_project_label`
   (~2070) — which also constructs `SqlAlchemyProjectStore(self.storage_location)` INSIDE the
   conversation store (layering inversion). Route helper and policy path both call the new store
   method; the policy path receives an injected ProjectStore instead of building one.

B7. **`_lock_active_project(session, project_id)`** private helper in the conversation store — the
   `select(...).with_for_update()` → NOT_FOUND → archived→CONFLICT kernel is duplicated at
   `_write_project_snapshot` (~:823) and `set_project_membership` (~:2030). This is the concurrency
   kernel; one home.

B8. **`resolve_owner_principal(user_id)`** single-home (in `_auth_helpers` or similar): the
   `user_id or RESERVED_USER_LOCAL` fallback is inlined at `sessions.py:12494/14770/14848/15094/15777`,
   `projects.py:_owner`, and the conversation store (~2081; killed by B6). One function, all callers.

B9. **`warn_deprecated_project_label(write_path)`** single-home next to the constants in
   `stores/conversation_store/__init__.py` — the routes have a helper but `runtime/policies/engine.py`
   (~:522) hand-rolls the same structured log; the `event` literal must live once.

B10. **Extract project session-integration out of sessions.py** into
   `omnigent/projects/session_integration.py`: the label-compat block (~12387-12442), the
   override-remapping + resolve + snapshot construction (~12510-12574), the multipart snapshot
   block (~14770-14794), and the PATCH membership resolution helpers (~15623-15629, 15770-15802).
   The route keeps auth, transport validation, and a single call into the module. Pure move +
   signature tidy — no logic change; existing tests must pass unchanged (only import paths in tests
   may be touched).

## Simplifications (from the simplifier report — behavior-preserving)

B11. `_BackfillProposal` frozen dataclass replacing the 7-tuple in `backfill_legacy_labels`
   (spelled twice, positionally unpacked twice with throwaway names).
B12. Use the already-computed `has_ledger` in the backfill apply loop instead of re-querying the
   ledger (~:485→506) — one round-trip less, covered by the idempotency tests.
B13. Hoist the single `legacy_project_requested = PROJECT_LABEL_KEY in (body.labels or {})`
   predicate in the PATCH handler (computed twice ~15623/15789).

## Migration + API hygiene

B14. **Squash the two migrations**: nothing is deployed between them — fold `moved` into
   `cc3d4e5f6a7b`'s original CHECK constraint and DELETE `dd4e5f6a7b8c` entirely; update
   `tests/db/test_migration_moved_project_snapshots.py` to test the single migration's round-trip
   (constraint contains all three origins; downgrade drops cleanly).
B15. **Remove the legacy `project` (name) alias** from `GET /v1/sessions` and the store
   (`sessions.py:~15014-15028`, `conversation_store/sqlalchemy_store.py:~2192/2293-2318`): the web
   is fully on `project_id` and nothing external shipped. Delete the alias param, its resolution
   branch, and its tests.

## Tests hygiene (where touched)
B16. Test builders: one `sessions_test_app(db_uri, **overrides)` conftest helper replacing the
   duplicated `_app`/`_multi_user_app` builders in test_sessions_projects/test_sessions_shared_projects;
   import `PROJECT_LABEL_KEY` instead of the ~12 hardcoded `"omni_project"` literals; where you
   touch a test, prefer public-outcome assertions over exact serialized strings/log prose.

## Out of scope (parked as decisions — do NOT do)
Owner-scoped vs workspace-scoped name uniqueness; snapshot-overwrite-on-move semantics; a
chown/transfer op; converging /v1/projects + /sessions/projects; server-side bulk delete;
Sidebar.tsx splitting; MEDIUMTEXT column change; the ProjectMembership/ResolvedDefaults entity split.

## Gate + commit
`OMNIGENT_SKIP_WEB_UI=true uv run ruff check --fix && ... ruff format && ... pytest` over ALL
projects-related test files (stores/conversation/projects/routes/policies/db). Commit on
`feat/projects-web`:
`refactor(projects): backend maintainability pass — single-home invariants, extract session integration, squash migrations`
Print a concise summary listing each item applied/skipped with reason.
