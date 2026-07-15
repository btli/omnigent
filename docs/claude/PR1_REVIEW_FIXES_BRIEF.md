# PR1 review-fix brief (Codex, test-first) — + DRY & YAGNI passes

Apply the fixes from a 5-engine adversarial review of PR1 (commit fc799c21) — Opus (schema),
Opus (access), Opus (coverage), Gemini 3.1 Pro, Codex — then run a DRY pass and a YAGNI pass over
the PR1 surface. Work test-first. This is the `feat/projects-entity` worktree
(`/Users/bryanli/Projects/btli/omnigent-projects`). **Env:** prefix every `uv` command with
`OMNIGENT_SKIP_WEB_UI=true` (the web-UI build fails here) or call `.venv/bin` directly; run your
new + directly-related tests, not the ~15k full suite.

## Fixes (do all)

1. **[MAJOR] Backfill unbounded `IN (...)` — chunk it.** Two label-backfill queries pass a
   whole-workspace list of session IDs into a single `.in_(...)`, which crashes on SQLite's 999
   bind-variable limit (this deployment migrates ~1,729 labeled sessions).
   `omnigent/stores/conversation_store/sqlalchemy_store.py`: the `.in_(non_archived_ids)` query
   (~line 1907) **and** `list_project_label_assignments`'s `.in_(session_ids)` (~line 1940). Batch
   both in chunks (≤900), accumulating results (dedupe distinct label values across chunks). Add ONE
   small shared chunk helper (DRY) — don't inline the same loop twice.

2. **[MAJOR] Middleware raises outside the exception handler → bare 500.**
   `omnigent/server/app.py` `add_workspace_scope_middleware`: a malformed/negative
   `X-Databricks-Org-Id` currently raises `OmnigentError`, but `@app.middleware("http")` runs
   OUTSIDE FastAPI's exception handlers, so it surfaces as an unformatted HTTP 500. Return the
   structured error response directly instead — `JSONResponse(status_code=400, content={"error":
   {"code": ErrorCode.INVALID_INPUT, "message": ...}})` (match the shape in the existing
   `_handle_omnigent_error`). Also add a one-line docstring note: `X-Databricks-Org-Id` is trusted
   infra metadata — the edge proxy MUST set/strip it (this binding is coarse tenancy; owner scoping
   still gates every row).

3. **[MINOR] `create()` misreports a concurrent first-project user race as a name collision.**
   `omnigent/stores/project_store/sqlalchemy_store.py` `create()`: two concurrent first-project
   creates for a new owner both see `SqlUser` absent; the losing `SqlUser` insert hits a PK
   `IntegrityError` that the broad `except IntegrityError` reports as "A project with that name
   already exists". Isolate the user provision in a savepoint (`with session.begin_nested():`) and
   ignore its `IntegrityError` (the user now exists; there are NO DB FKs so the project row doesn't
   need it), so only the project-row flush maps to the name-collision 409.

## Test gaps to close (test-first)
Add to `tests/stores/test_project_store.py`:
4. **rename → existing name = 409** (the `_mutate` IntegrityError branch, distinct from create's),
   asserting the source project's name + `row_version` are unchanged.
5. **double-archive = 409** and **restore-of-live = 409** (the `require_archived` guards), each
   asserted with the CURRENT version so a 412 can't mask the intended 409.
6. **optimistic-concurrency guarantee:** two callers hold ETag v1 — the first wins (v2); the second,
   still presenting v1, gets **412**, its write is NOT applied, and the version advances exactly
   once. (The internal `rowcount != 1` guard is the cross-dialect enforcement for a true PG/MySQL
   race, confirmed by review; on the per-test SQLite file the pre-read predicate fires — assert the
   externally observable guarantee. **Do NOT write a threaded SQLite race test** — it deadlocks on
   the shared→exclusive lock upgrade and is flaky.)

## Explicitly OUT of scope (do NOT change — pre-existing / documented)
- The two-phase cross-DB session-delete "orphaned Omnigent rows on second-txn failure" tradeoff:
  it's pre-existing and documented in `delete_conversation`; PR1 only added the snapshot row to that
  existing transaction. Do not rework the delete.
- Concurrent-Postgres-backfill mint race: backfill is operator-run and rerun-safe; leave it.

## DRY pass
Remove real duplication in the PR1 surface (the two IN-chunk loops → one helper; any repeated
error-construction, validation, or `workspace_id ==` filter patterns that collapse cleanly).
Behavior-preserving only.

## YAGNI pass
Remove speculative/unused scope introduced by PR1 that the MVP base does not need (dead params,
unused fields/branches, premature abstractions). **KEEP spec-required scaffolding** — `storage_key`
is intentionally reserved for the future storage phase (spec §3), the migration ledger and the
defaults-bundle fields are required. If unsure whether something is speculative vs. required,
KEEP it and note it rather than deleting.

## Gate + commit
`OMNIGENT_SKIP_WEB_UI=true uv run ruff check --fix && OMNIGENT_SKIP_WEB_UI=true uv run ruff format &&
OMNIGENT_SKIP_WEB_UI=true uv run pytest <new/related>` — all green. Add a NEW commit on
`feat/projects-entity` (do NOT amend fc799c21 — a separate branch is stacked on it):
`fix(projects): PR1 review fixes — backfill chunking, middleware error mapping, create race; DRY/YAGNI`
Print a concise summary: each fix, the DRY/YAGNI changes, the test count, anything left out and why.
