# MVP PR1 implementation brief — Projects (Codex, gpt-5.6-sol ULTRA, test-first)

Implement **PR1 of the Projects MVP** (spec: `docs/claude/PROJECTS_MVP.md`) — the flat Project
entity + store + single-owner access + label backfill. Per-project *contextual awareness* is the
goal; **persistent storage, the writer-lease, and multi-repo are DEFERRED — do not build them.**
Work test-first.

## Where
- Repo: `/Users/bryanli/Projects/btli/omnigent-projects`, branch `feat/projects-entity` (on current
  main; `.venv` synced). Python via **uv** only. No `pip`.
- Spec of record: `docs/claude/PROJECTS_MVP.md` (§3 schema, §5 requirements MVP-1/2/6, §6 acceptance).

## Hard constraints
- **NO SQLAlchemy `ForeignKey`/DB FK** (Rule R032) — relationships application-enforced via
  `(workspace_id, …)` lookups + plain indexes.
- **NO partial indexes** — uniqueness via a scope column + SHA-256 checksum in a plain
  `UniqueConstraint`.
- New tables on **`OmnigentBase`**, `(workspace_id, id)` PK, `workspace_id` defaulted via
  `current_workspace_id`. No `# type: ignore` / `# noqa`.

## Code map (verified file:line)
- Table template: `SqlSessionPermission` (`omnigent/db/db_models.py:320`) — copy its `workspace_id`
  BigInteger PK block. `current_workspace_id()`/`workspace_scope()` at `db_models.py:80/91`.
- `SqlPolicy` `name_cksum` uniqueness pattern (`db_models.py:~895`) — the model for
  `normalized_name_checksum`.
- `project_id` column → `SqlConversationMetadata` (`db_models.py:374`, OmnigentBase).
- Store: package `stores/project_store/{__init__.py (ABC), sqlalchemy_store.py}`, single-URI like
  `SqlAlchemyPermissionStore(db_uri)` (`permission_store/sqlalchemy_store.py:56`); filter every query
  on `current_workspace_id()`; export from `stores/__init__.py`; construct in `cli.py:~3175`.
- Route: `create_projects_router(...)` mirroring `server/routes/comments.py:122`; `_require_user` =
  `_auth_helpers.require_user`; `OmnigentError(code=ErrorCode.X)` (`errors.py:15`; NOT_FOUND→404,
  CONFLICT→409, INVALID_INPUT→422); register `app.py:~2078` `prefix="/v1"`.
- Migrations: HEAD = **`bb2c3d4e5f6a`**; chain `down_revision="bb2c3d4e5f6a"`; `op.create_table` +
  `op.batch_alter_table` (SQLite-safe).
- `omni_project` today: `list_projects()` (`conversation_store/sqlalchemy_store.py:1844`, reads the
  label across both DBs), `set_labels()` (`:1156`), key `"omni_project"`.
- **TEN-3 gate (build it):** no per-request `workspace_scope` binding exists (every REST request runs
  at workspace 0). Add an `@app.middleware("http")` (template `app.py:1484`) resolving workspace from
  auth context + wrapping in `workspace_scope(...)`; test two scopes can't mix.

## Deliverables (test-first — write the test from the §6 acceptance bullet, then implement)
1. **3 tables + column** (Alembic off `bb2c3d4e5f6a`): `projects`, `session_project_snapshots`,
   `project_migration_ledger` (exact columns/constraints per PROJECTS_MVP.md §3), + nullable
   `project_id` on `omnigent_conversation_metadata` (immutable once set).
2. **`ProjectStore`** — CRUD; ETag `row_version` (compare-and-set, 412 on stale); per-owner name
   uniqueness (SHA-256 checksum); soft archive/restore.
3. **`/v1/projects` router** — create/list/get/rename/archive/restore; single-owner access (owner
   from caller; owner-only; wrong-owner/tenant→404; archived attach→409; `If-Match`/412 on mutate,
   exempt on create; name collision→409).
4. **Label backfill** (MVP-6) — staged/idempotent/per-workspace: read `omni_project` labels (AP DB),
   create/repoint flat projects in the Omnigent DB via the ledger (reuse-if-mine else stop with a
   mapping plan; leave labels in place).
5. **Workspace middleware** (TEN-3).
6. **Tests** for every §6 acceptance bullet: owner-only + 404/409/412 codes; name uniqueness +
   collision→409; archive/restore + reserved name; backfill idempotent + two workspaces keep distinct
   "omnigent" + collision stops with a plan; a projectless flow is unchanged.

(PR2 = defaults resolver + `session_project_snapshots` writes + session-API `project_id`; PR3 =
CLI/web/composer prefill + migration review screen. Not in PR1.)

## Gate
`uv run ruff check --fix && uv run ruff format && uv run pytest` green. Scope the diff to PR1. Commit
on `feat/projects-entity`: `feat(projects): PR1 — flat Project entity, store, /v1 routes, label backfill`.
