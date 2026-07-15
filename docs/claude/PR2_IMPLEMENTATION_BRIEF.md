# MVP PR2 implementation brief — Projects defaults resolver + snapshot-at-create (Codex, test-first)

Implement **PR2 of the Projects MVP** (spec: `docs/claude/PROJECTS_MVP.md` §4 resolver + MVP-4).
PR1 (the flat `Project` entity, `ProjectStore`, `/v1/projects`, `session_project_snapshots` +
`project_migration_ledger` tables, `metadata.project_id`) is already committed on this branch
(fc799c21). **PR2 wires a project's defaults into session creation** so a session started *in* a
project inherits them. **PR3 (CLI/web/composer prefill/migration UI) is OUT of scope — do not build
it.** Work test-first.

## Where
- Repo: `/Users/bryanli/Projects/btli/omnigent-pr2`, branch `feat/projects-inheritance` (stacked on
  `feat/projects-entity`). Python via **uv** only.
- **Env gotcha:** the editable build runs an npm web-UI build that FAILS here — prefix every `uv`
  command with `OMNIGENT_SKIP_WEB_UI=true`, or call `.venv/bin/python` / `.venv/bin/ruff` directly.
  The venv is already synced (`--extra dev --extra all`). Run your NEW tests + directly-related
  ones, NOT the ~15k full suite. Model tests on `tests/stores/test_project_store.py` (PR1, passing).

## Scope (PR2 only)
1. **Typed, versioned defaults bundle** (`projects.defaults_json`): a Pydantic model gated by
   `defaults_schema_version`; each field tri-state **absent** (inherit) / **null** (clear) / value.
   (If PR1 already added bundle validation in the store, reuse it; otherwise add the model here and
   have the store + resolver both validate → invalid = 422.)
2. **Defaults resolver** (new module, e.g. `omnigent/projects/resolver.py`): precedence
   `server/workspace defaults → project bundle → explicit session override`, **field-by-field** (no
   deep-merge; `null` clears). Emits the **host-specific** session-create fields per spec §4:
   - **managed host:** `workspace = "<repo_url>#<default_branch>"`, `git = None`, `host_id`
     prohibited.
   - **external host:** `workspace` = absolute path, `host_id` = pinned host, `default_branch` →
     `git.base_branch`; the unique `git.branch_name` is **minted per session**, never from the
     bundle. (Verify against `server/schemas.py:1145` `GitConfig`, `:1324`, `managed_hosts.py:507`.)
3. **Session-create wiring — JSON path ONLY** (`SessionCreateRequest`, `server/schemas.py:1195`;
   the multipart `SessionCreateMetadata` path stays EXCLUDED/deferred):
   - Add nullable `project_id` to `SessionCreateRequest`.
   - When `project_id` is set: load the project via `ProjectStore` (owner+workspace checked;
     wrong-owner/tenant → 404, **archived project attach → 409**), resolve defaults, then in the
     create flow (`sessions.py` create path around `create_conversation`, ~4825/5023):
     - **atomically on OmnigentBase:** write `session_project_snapshots` (resolved bundle,
       `snapshot_origin="live"`, `project_row_version` = project's current `row_version`,
       `defaults_schema_version`) **and** set `metadata.project_id` — one transaction.
     - **seed `agent_configuration`** (ConversationBase — separate commit) from the resolved
       model/harness/reasoning via the **existing post-create override write**
       (`update_conversation(model_override=…, harness_override=…, reasoning_effort=…)`, the same
       call the JSON create already uses for overrides). Snapshot is the reconciliation source if
       this second write fails before the first turn.
   - **Invariant (enforce + test): a session with `project_id` set ALWAYS has a snapshot row.**
4. **Child/sub-agent/fork = top-level-only:** child-spawn and fork create paths do **NOT** inherit
   `project_id` or a snapshot (matches today's non-copy of the `omni_project` label). Verify + test
   that a fork of a project session has no `project_id`.
5. **Immutability + no-regression:** `metadata.project_id` immutable once set; a later project
   `update` (bundle edit) never mutates an existing session's snapshot; a projectless create is
   byte-for-byte unchanged.

## Hard constraints
- No cross-DB transaction (snapshot+metadata = OmnigentBase atomic pair; agent_configuration seed =
  separate ConversationBase commit). No DB FKs, no partial indexes, no `# type: ignore` / `# noqa`.
- `credentials_ref` / `policy_ref` are DEFERRED — do NOT add them.

## Tests (test-first — one per acceptance point)
Resolution precedence (server→project→session, field-by-field, `null` clears); managed vs external
host mapping correctness (managed → `repo_url#branch`/git=None; external → base_branch + minted
branch_name); snapshot written atomically with metadata.project_id; invariant (no project_id without
snapshot) even on a simulated config-seed failure; project edit doesn't change an existing session's
snapshot; wrong-owner project → 404, archived project → 409; fork/child has no project_id; multipart
path unaffected; projectless create unchanged.

## Gate
`OMNIGENT_SKIP_WEB_UI=true uv run ruff check --fix && OMNIGENT_SKIP_WEB_UI=true uv run ruff format &&
OMNIGENT_SKIP_WEB_UI=true uv run pytest <your new/related tests>` — all green. Scope the diff to PR2.
Commit on `feat/projects-inheritance`:
`feat(projects): PR2 — defaults resolver + snapshot-at-create + session-API project_id`
