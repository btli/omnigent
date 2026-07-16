# Projects — MVP: per-project contextual awareness

**Date:** 2026-07-15 · **Status:** Releasable base (post-simplification) · **Supersedes** the scope
of `PROJECTS_AND_PERSISTENT_STORAGE_REQUIREMENTS.md` (v12), which is retained as the **deferred
full-design reference** for the later phases listed in §7.

> **Why this doc exists.** The full requirements grew large chasing edge cases — a persistent-storage
> subsystem and a single-writer lease protocol whose distributed-systems depth dominated ~15 review
> passes. Antigravity 2 and remote-dev already ship "projects" as a bounded feature. The **critical,
> releasable thing is per-project *contextual awareness*** — start a session "in project X" and have
> it inherit that project's repo, branch, sandbox, harness, model, and credentials. That is this
> MVP. Persistent workspaces, concurrency leasing, and multi-repo are **deferred** (§7), not part of
> the base.

## 1. Goal

An operator defines a **Project** once (its repo, default branch, sandbox/host, harness, model).
Every new session started **in that project inherits all of it** — one choice in the
composer/CLI/API, everything prefilled. This is the daily-workflow unlock; sessions still run in
today's ephemeral workspaces (persistent workspaces are the next phase, not the base). **Scope:**
inheritance applies to the standard JSON session-create path (`SessionCreateRequest`); the multipart
bundle/custom-agent create path (`SessionCreateMetadata`, which lacks the host/model fields today) is
**excluded from project inheritance in the MVP** — extending it is a deferred follow-on.

## 2. Scope

**In (the releasable base):** a flat, tenant-scoped Project entity; a per-project **defaults
bundle**; **defaults inheritance** (snapshotted onto the session at create); **one-time label
migration** (`omni_project` → real projects); CRUD via REST/CLI/web; a flat sidebar filter;
single-owner access.

**Out (deferred — see §7 and the full-design doc):** persistent K8s workspace storage; the
single-writer lease / concurrency protocol; multi-repo per project; storage bindings /
materializations; managed dynamic PVCs; backup/restore; hard purge; grouping/folders; peer messaging.

## 3. Data model (3 tables + 1 column — obeys the codebase constraints)

Constraints (verified, non-negotiable): **no DB foreign keys** (Rule R032 — relationships
application-enforced), **no partial indexes** (portable SQLite/Postgres/MySQL — uniqueness via a
scope column + SHA-256 checksum in a plain `UniqueConstraint`), **split physical DBs** (all new
tables on `OmnigentBase`). Tables use a `(workspace_id, id)` PK **except where a natural key is
clearer** (`session_project_snapshots` is `(workspace_id, session_id)` — one snapshot per session);
`workspace_id` is defaulted via `current_workspace_id()`. **No-FK cleanup:** since there are no DB
FKs, **session deletion must explicitly delete the session's `session_project_snapshots` row** — add
it to the existing dependent-row enumeration in the deletion path (`sqlalchemy_store.py:3391`), with
a test asserting `metadata.project_id` and the snapshot are removed together.

**`projects`** — PK `(workspace_id, id)`; `owner_principal_id` (= the existing `SqlUser (workspace_id,
id)`, an email/`local` today); `name`, optional `description`; `normalized_name` +
`normalized_name_checksum` (NFKC → trim → case-fold); `storage_key` (immutable DNS-safe slug **derived from the globally-unique `id`**, e.g.
`proj-<id-hash>` — **never from `name`**, so `UNIQUE(workspace_id, storage_key)` can't collide across
two owners who both name a project "frontend"; reserved now for the future storage phase);
`defaults_json` (§4) + `defaults_schema_version`; `row_version` (ETag);
`created_at`, `updated_at`, `archived_at`.
Constraints: `UNIQUE(workspace_id, storage_key)`; `UNIQUE(workspace_id, owner_principal_id,
normalized_name_checksum)` (per-owner, flat); index `(workspace_id, owner_principal_id, id)`.

**`session_project_snapshots`** — PK `(workspace_id, session_id)`; `project_id`; `snapshot_origin`
(`live | backfill`); `project_row_version` (the version resolved from — **nullable; `NULL` for
`backfill`** snapshots, which carry no resolved defaults); `defaults_schema_version`;
fully-resolved `defaults_json`; `created_at`. Captures the project's defaults **at create time** so a
later project edit never changes an existing session.

**`project_migration_ledger`** — PK `(workspace_id, id)`; `owner_principal_id`; `normalized_name` +
`normalized_name_checksum`; resolved `project_id`; `source_fingerprint`; `created_at`.
`UNIQUE(workspace_id, owner_principal_id, normalized_name_checksum)`. Makes the label backfill idempotent + provable
("this migration created that project").

**`omnigent_conversation_metadata.project_id`** — new nullable `String(64)` column (same
`OmnigentBase` DB as the snapshot, so create writes both in one transaction) + index `(workspace_id,
project_id, id)`. **Immutable once set.**

## 4. Defaults bundle (`projects.defaults_json`, versioned) + resolver contract

A **typed, versioned** JSON object (`defaults_schema_version` gates its shape). All fields optional;
each is `absent` (inherit), `null` (explicit clear), or a value (override). The **store validates**
the bundle against the schema on `create`/`update` (invalid → **422**); the **resolver** validates
again at session-create.

**Fields and their authoritative mapping to omnigent's existing session-create fields.** The mapping
is **host-type-specific** — `managed` and `external` interpret repo/branch/workspace differently
(verified against `server/schemas.py:1145` `GitConfig`, `:1324` create schema, and
`server/managed_hosts.py:507`). Two rules the mapping must obey (both verified in code):

- **`git` is external-only.** A `git` block *requires* `host_id`, which **managed hosts prohibit**.
  So for managed hosts `git = None` and the branch travels **in the workspace URL fragment**
  (`repo_url#branch`). Never map `default_branch` onto `git.branch_name` — that field is the *new,
  unique per-session worktree branch* minted per launch, not a source ref.
- **The snapshot is create-time provenance, not the runtime authority.** `model`/`harness`/`reasoning`
  live in the split-DB `agent_configuration`, which stays **mutable** after create (session PATCH,
  terminal-originated updates — `sessions.py:15658/3659/3797`) and is what launch/forks read
  (`sqlalchemy_store.py:2984`). So the resolver **initializes that persisted `agent_configuration`
  from the resolved bundle in the same create transaction** (one write), and it remains the runtime
  authority edited normally thereafter. The immutable snapshot is the **inheritance record** (proof
  of what the project supplied at create); it is never re-read at launch, so it cannot drift from a
  later legitimate config edit. **The snapshot lives on `OmnigentBase` with `metadata.project_id`;
  `agent_configuration` lives on `ConversationBase` (split DBs — no cross-DB transaction). So the
  config is seeded from the resolved bundle via the *existing* post-create override-persist write
  (`sessions.py:12672`, which already runs before the runner reads config on the first turn); the
  durable snapshot is the reconciliation source if that second write fails.**

| Bundle field | Maps to (managed host) | Maps to (external host) |
|---|---|---|
| `repo_url` | `workspace = "<repo_url>#<default_branch>"` (branch as URL fragment); `git = None` | n/a (external mounts a filesystem path, not a clone) |
| `default_branch` | folded into the `repo_url` workspace fragment above | `git.base_branch` (the source ref); the required unique `git.branch_name` is **minted per session**, never from the bundle |
| `host_type` | `host_type="managed"` | `host_type="external"` |
| `host_id` | **prohibited** (managed rejects it) | `host_id` (the pinned host) |
| `workspace` | derived from `repo_url` (above) | absolute filesystem path |
| `harness` | `harness_override` | `harness_override` |
| `model` | `model_override` | `model_override` |
| `reasoning_effort` | per-session reasoning hint (optional) | same |

**Resolution (MVP-4):** precedence `server/workspace defaults → project bundle → explicit session
override`, **field-by-field** (a project value overrides the server default; a session override
overrides the project; `null` clears). No deep-merge — the bundle is flat, so per-field replacement
suffices. The **fully-resolved** result (already mapped to the host-specific session-create fields)
is what gets snapshotted (§3) and applied at launch.

**Deferred out of the MVP bundle (`credentials_ref`, `policy_ref`):** neither is wired to a launch
frame today — external-host launch (`host/frames.py:93`, `host/connect.py:499`) carries no
credential/env payload, and policy runtime *composes* session+agent+default policies with no
selection-by-reference (`runtime/policies/builder.py:263`). Adding two optional request fields would
not make them real, so they are **excluded from the MVP** and handled in the later per-project
credentials/policy phase (§7). MVP credentials come from the host environment exactly as today.

## 5. Requirements

| ID | Requirement |
|---|---|
| MVP-1 | `Project` CRUD via `/v1/projects`, `omni project …`, web settings: `create`, `list`, `get`, `rename` (name only), **`update`** (owner-only PATCH editing `description` + `defaults_json` — the write-path MVP-4/acceptance depend on so future sessions inherit the change), `archive`/`restore`. All mutates are `If-Match`-guarded and advance `row_version` (MVP-2). `id`/`owner`/`storage_key` immutable throughout. Archive is soft (name stays reserved until a future hard-purge). |
| MVP-2 | **Access (single-owner):** the server derives `owner_principal_id` from the caller (request body can't set it); only the owner may list/use/update/archive; wrong-owner/wrong-tenant → **404**; archived attach → **409**; name collision → **409**. **Every mutating op (`rename`, `update`, `archive`, `restore`) requires `If-Match` and atomically increments `row_version` in the same UPDATE**; a mutate with a missing/stale `If-Match` → **412** (create is exempt). One rule for all mutates — not just `update`. |
| MVP-3 | Sessions carry `project_id`; sidebar/search/palette **filter** by it (flat). Projects persist when emptied; only archive removes them from the live list. "No project" behaves exactly as today. |
| MVP-4 | **Defaults resolution + snapshot** (standard JSON `SessionCreateRequest` path only — §1 scope): at session-create in a project, resolve `server/workspace defaults → project → explicit session override`, then **write `session_project_snapshots` and set `metadata.project_id` atomically in one `OmnigentBase` transaction** (both tables are on that DB), and **seed `agent_configuration` (ConversationBase) from the resolved bundle via the existing post-create override write** (`sessions.py:12672`) — split DBs mean these are *separate* commits, not one cross-DB transaction. **Invariant (single-DB, always holds): a session with `project_id` set ALWAYS has a snapshot row** (never `project_id` without a snapshot), because those two are the atomic pair. If the ConversationBase config seed fails after the OmnigentBase commit, the snapshot is the durable source to reconcile/retry from before the first turn runs. **Child/sub-agent/fork sessions** (a normal omnigent flow) — **rule: `project_id` is top-level-only in the MVP.** Sub-agents and forks do **not** inherit `project_id` or a snapshot: sub-agents already share the parent's checkout/runner (no separate context to inherit), and forks are new top-level sessions that today copy ordinary labels/config but drop host/workspace (`schemas.py:2023`, `conversation_store/sqlalchemy_store.py:2858`) — so a copied snapshot would describe a workspace the fork doesn't have. Inheritance for forks is a deferred follow-on. This matches today's child-spawn, which does *not* copy the `omni_project` label. Legacy/backfilled sessions get a `backfill` snapshot with empty defaults (they don't absorb current defaults). A later project edit never changes an existing session. |
| MVP-5 | **Composer/CLI prefill** (the "one action" — the headline): from the project row / `--project`, the composer opens **pre-scoped**, all defaults prefilled and editable, manual edits respected (#509). Common case = one click to a running session. |
| MVP-6 | **Label migration + staged cutover (single authority).** Today `omni_project` labels are a *mutable* authority editable by anyone with edit access (`sessions.py:14645/15425/15735`, `useConversations.ts:685`); the new `metadata.project_id` is immutable + owner-only. Because the AP label DB and Omnigent DB are **split** (no atomic dual-write), the cutover is staged: **(a)** the per-workspace backfill reads `omni_project` labels, creates/repoints flat projects in the Omnigent DB via the ledger (reuse-if-mine, else stop with a mapping plan; leave the pre-cutover labels in place so the imported state is recoverable — this preserves **pre-cutover mappings only**, not a lossless ongoing downgrade, since post-cutover creates/renames live solely in `project_id`); **(b)** on/after cutover, `project_id` is the sole authority — legacy project-label **writes are frozen/rejected** and project reads/filters are served from `project_id`, not the label. Labels carry **no owner** (`db_models.py:656`), so a label with **zero or multiple** candidate owners, or a normalized-name alias/collision, routes to the **mapping plan** for operator resolution — never an arbitrary pick. A one-time "your projects were imported" review screen shows the per-project mapping preview before cutover. |
| MVP-7 | **No regression:** a projectless session behaves byte-for-byte as pre-Projects. |

## 6. Acceptance (end-to-end)

Create a project → start a session in it → the session's snapshot holds the resolved defaults and the
composer prefilled them; edit the project → an existing session is unchanged, a new one picks up the
edit; owner-only access enforced (404/409/412); label backfill is idempotent and per-workspace, two
workspaces keep distinct "omnigent" projects, a pre-existing collision stops with a mapping plan; a
projectless session is unchanged. **Success = the operator starts an agent in an existing project in
one action with full context inherited, and the ~33 remote-dev projects migrate cleanly.**

## 7. Deferred — later phases (design preserved in the full doc)

These are **planned follow-ons**, ordered; each is a mostly-additive migration on the MVP schema.
The detailed (adversarially-reviewed) design for them lives in
`PROJECTS_AND_PERSISTENT_STORAGE_REQUIREMENTS.md`.

0. **Per-project contextual awareness (instructions + knowledge) — the flagship follow-up, NOT
   required for this MVP** (decided 2026-07-15). Full design + Antigravity-2/remote-dev parity
   analysis in `ANTIGRAVITY2_PROJECTS_PARITY.md` §3: (a) a bounded `instructions` field via a
   defaults-bundle **v2** (`defaults_schema_version` gates it; flows through the existing
   store-validate → resolver → snapshot path, materialized into the workspace at launch); (b) a
   living **`project_knowledge`** table (agent-writable `convention|pattern|gotcha|skill` rows,
   `project_knowledge_add/list` builtins, injected at launch; the snapshot records the
   `knowledge_revision` while the table stays the runtime authority). Repo-committed context
   (CLAUDE.md, `.agents/rules`, skills, MCP config) already flows into every session today via the
   per-session checkout — this phase adds only the server-side, non-repo-committed layer.

1. **Persistent K8s workspaces** — project-keyed PVCs, clone-if-empty init, pod-replacement recovery
   (the bulk of the full doc's §4.2). The single largest follow-on; the `storage_key` is already in
   the MVP schema for it.
2. **Single-canonical-writer lease / concurrency** — the LEASE-1..3 reservation state machine, only
   needed once workspaces persist and are shared.
3. **Multi-repo** — promote `repo_url`/`branch` out of `defaults_json` into a `project_repositories`
   (0:N) table.
4. **Managed dynamic PVCs, backup/restore, hard purge, grouping/folders, per-project credentials
   store, budgets, messaging.**

## 8. Delivery

| PR | Contents |
|---|---|
| **PR1** | `projects` + `session_project_snapshots` + `project_migration_ledger` + `metadata.project_id` (Alembic off head `bb2c3d4e5f6a`); `ProjectStore`; `/v1/projects` + access rules; label backfill. |
| **PR2** | Defaults resolver + snapshot-at-create + session-API `project_id`/overrides. |
| **PR3** | CLI `omni project`, web settings + flat sidebar filter + composer prefill + migration review screen. |

Implementation notes (verified code map) in `PR1_IMPLEMENTATION_BRIEF.md`: OmnigentBase template =
`SqlSessionPermission` (db_models.py:320); `current_workspace_id` (db_models.py:80); store pattern =
`permission_store`; route pattern = `comments.py:122`; register in `app.py:2078` `prefix="/v1"`;
**TEN-3 gate:** the per-request `workspace_scope` middleware must be built (app.py:1484 template).
