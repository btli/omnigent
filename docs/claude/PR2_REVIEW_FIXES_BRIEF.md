# PR2 review-fix brief (Codex, test-first)

Fixes from a 5-engine review of PR2 (commit 836bedf3) — Opus (session-integration, host-mapping,
coverage), Gemini 3.1 Pro, Codex. Two false-positives were verified and are EXCLUDED (see bottom).
Work test-first, in `/Users/bryanli/Projects/btli/omnigent-pr2` (branch feat/projects-inheritance).
Env: prefix `uv` with `OMNIGENT_SKIP_WEB_UI=true`; run new + related tests only.

## Fixes

1. **[MAJOR] Validate the managed `repo_url`/`default_branch` grammar at bundle-save (not only at
   launch).** `omnigent/projects/defaults.py` `validate_defaults_bundle` only type-checks the bundle,
   so a `host_type="managed"` project with a scheme-less `repo_url` (e.g. `github.com/o/r`) or a
   40-char SHA `default_branch` saves 422-free but then fails `parse_repo_workspace` at EVERY
   session-create (→ a "valid" project that can never launch). When `host_type == "managed"` and
   `repo_url` is set, validate the eventual workspace grammar in `validate_defaults_bundle` — reuse
   `omnigent/server/managed_hosts.py` (`is_repo_workspace` / `_validate_clone_branch` /
   `parse_repo_workspace` on `f"{repo_url}#{default_branch}"` when a branch is present) — and raise
   `ProjectInputError` (→ 422) at save time. (Mind import direction: `defaults.py` importing from
   `server/managed_hosts.py` — if that creates a cycle, put the managed-grammar check in the store's
   create/update path instead, still at save time.)

2. **[MINOR — silent-wrong] Workspace precedence inversion.** `omnigent/projects/resolver.py:66` mints
   `workspace = repo_url#branch` when `"workspace" not in session_overrides.model_fields_set` — but a
   **project-level** explicit `workspace` lives in `merged`, not in `session_overrides.model_fields_set`,
   so minting clobbers it silently. Change the guard to mint only when no workspace resolved from any
   layer: `if workspace is None and repo_url is not None`. Add a test: managed bundle with BOTH
   `repo_url` and an explicit `workspace` → the explicit workspace wins (not overwritten).

3. **[hardening] Replace the `model_validate({**model_dump()})` merge with `model_copy(update=…)`.**
   `omnigent/server/routes/sessions.py` (~line 12472) does
   `body = SessionCreateRequest.model_validate({**body.model_dump(), **resolved_values})`, which
   rebuilds `body.model_fields_set` as ALL fields (latent provenance poisoning; no current downstream
   reader, so not a live bug — but fix the idiom). Replace with:
   `body = body.model_copy(update={k: v for k, v in resolved_values.items() if k in body.model_fields})`.
   Keep the `try/except ValidationError → ProjectInputError(422)` semantics for a genuinely invalid
   resolved bundle (if `model_copy` can't raise the same way, validate the resolved values
   separately so an invalid resolved bundle still surfaces as 422).

## Tests to add (close the coverage gaps that would catch #1/#2/#3 regressions)
- **Managed-host end-to-end create** through HTTP: project `defaults_json={host_type:"managed",
  repo_url, default_branch}` → `POST /v1/sessions {project_id}` → 201, snapshot `workspace` has the
  `#branch` fragment, `git=None`, `host_id=None`.
- **Session override alongside `project_id`**: `POST {project_id, model_override, harness_override}`
  against a project whose bundle sets different values → the snapshot/config reflect the body
  override (proves the `model_override→model` / `harness_override→harness` remap survives #3).
- **Resolved-values 422**: a stored managed bundle with a bad `repo_url` now → 422 at project-save
  (fix #1); keep/adjust the resolve-time 422 test.
- **Resolver branches** (tests/projects/test_resolver.py): external `default_branch` present but
  `host_id is None` → raises; external with no `default_branch` → `git=None`; managed with
  `default_branch=None` → `workspace=repo_url` (no fragment); the #2 project-workspace-wins case.
- **New session picks up a project edit**: create session, edit the project bundle, create a SECOND
  session → the second snapshot reflects the new bundle (the untested half of §6).

## Excluded (verified FALSE positives — do NOT change)
- agy "every project create 422s / extra host_type crash": `SessionCreateRequest` has NO
  `extra="forbid"` and `ResolvedProjectDefaults` fields are all valid request fields — no crash.
- agy "remove host_type from direct_fields (dead code)": `host_type` IS a `SessionCreateRequest`
  field, so per-session host_type override is intentional — keep it.

## Gate + commit
`OMNIGENT_SKIP_WEB_UI=true uv run ruff check --fix && ... ruff format && ... pytest <new/related>`
green. Add a NEW commit on feat/projects-inheritance (do NOT amend 836bedf3):
`fix(projects): PR2 review fixes — managed-grammar validation, workspace precedence, safer merge`
Print a concise summary of each fix + tests + anything skipped.
