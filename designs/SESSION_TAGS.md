# Session Tags — Design Scope

> Status: DRAFT / scoping. Companion to `SESSION_PROJECTS_SIDEBAR.md`. Tracks the
> "tags" concept requested alongside the archived-session project filter (#2115).
> Grounded in a read-only pass of the codebase; all "current state" claims cite
> `file:line` / endpoints.

## 1. Motivation & value

Conversations in omnigent today get at most one **project** (the reserved
`omni_project` label). That single bucket can't express the ways users actually
want to slice their history, and it offers no leverage for sharing or
rediscovery at scale. **Tags** add *multi-valued, cross-cutting* labels, in two
flavors:

- **Manual tags** (including curated "project tags"): applied by a user or team.
- **Auto-tags**: system-derived and applied without manual effort.

Three concrete value propositions motivate the feature:

1. **Bulk grant / share.** "Share everything tagged `customer:acme` with the
   Acme deal team" resolves a tag to its member conversations and grants them in
   one action. Today sharing is strictly **one conversation at a time** (§2.1) —
   there is no primitive for this, so it is net-new capability, not a UI
   convenience.
2. **Search relevant history.** "Show my conversations tagged `topic:auth` from
   last quarter," optionally full-text *within* that subset. Today only
   `omni_project` can filter the list and ranked full-text search is **not
   exposed via any endpoint** (§2.2).
3. **Automatic organization.** Auto-tags cut the curation cost of (1) and (2):
   bulk-share and topical search become useful even for users who never manually
   organize their conversations.

Projects answer "which folder is this in?"; tags answer "who should see this,
and how do I find it later?"

## 2. Current state (grounding)

### 2.1 Sharing / ACL — per-session only, no bulk
- ACL is a grant table `session_permissions`: junction `(user_id, conversation_id) -> level`,
  PK `(user_id, conversation_id)`; `level in {1 read, 2 edit, 3 manage, 4 owner}`;
  `"__public__"` sentinel `user_id` = public read
  (`omnigent/db/db_models.py:198-236`). There are **no ownership columns on
  `conversations`** — ownership is just a level-4 grant.
- `PermissionStore.grant/revoke/get/check_access/...` all take **exactly one
  `conversation_id`** (`omnigent/stores/permission_store/__init__.py`).
  `list_for_sessions` batches *reads* only.
- Endpoints are all per-session: `PUT|DELETE|GET /v1/sessions/{session_id}/permissions`,
  `GET /v1/sessions/{session_id}/owner` (`omnigent/server/routes/sessions.py:19961-20140`).
- **No org/team grantee, no group, no bulk-grant path.** <- the gap value-prop (1) fills.

### 2.2 Search — FTS exists but is unexposed; only projects filter the list
- **Session-list substring filter**: `GET /v1/sessions?search_query=...` -> `LIKE`
  on `LOWER(title)` OR `conversation_items.search_text`
  (`sqlalchemy_store.py:1581-1712`, ~1686-1694). Plain SQL LIKE for SQLite+Postgres
  portability.
- **Ranked FTS5**: `ConversationStore.search()` uses the `conversation_items_fts`
  virtual table (`MATCH ... ORDER BY rank`) on SQLite, `data::text ILIKE` on
  Postgres (`sqlalchemy_store.py:1219-1273`; table at `omnigent/db/utils.py:667-670`).
  **Not wired to any HTTP endpoint.**
- **Label filter is hard-coded to projects**: `?project=` filters
  `conversation_labels.key='omni_project'` (`sqlalchemy_store.py:1695-1714`).
  There is **no generic `?label=key:value`** filter. The FTS index covers only
  `search_text` — **labels are not indexed**.

### 2.3 Labels — single-valued per key (the core constraint)
- `conversation_labels`: `(conversation_id, key, value, updated_at)`, **composite
  PK `(conversation_id, key)`** (`db_models.py:510-548`); `value` <= 256 chars.
- Writes go through `set_labels()` -> `_upsert_labels()` with `ON CONFLICT DO UPDATE`
  on `(conversation_id, key)` (`sqlalchemy_store.py:341-342`, 860-889): a second
  write to a key **overwrites**. API types labels as `dict[str, str]`
  (`omnigent/server/schemas.py`).
- => **A conversation cannot hold multiple values for one key today.**
  Multi-valued tags cannot reuse `conversation_labels` as-is.

### 2.4 Auto-classification infra — thin, but a hook exists
- **No** conversation-level auto-tagger, LLM summarizer of finished
  conversations, or post-run / turn-complete hook that inspects a completed
  conversation. Title auto-gen is deterministic truncation of the first user
  message (no LLM).
- The closest existing mechanism: the **PolicyEngine prompt-classifier** can
  write labels as a per-prompt guardrail (not a post-conversation tagger). An
  auto-tag feature would extend that path or add a new post-run classifier.

### 2.5 Bulk operations — none
- Every mutation route (archive, delete, label, permission) is single-session
  (path param `{session_id}`). "Bulk" in the code refers only to internal
  batched *reads* (liveness, label hydration). No bulk mutation API exists.

### 2.6 Projects as precedent
- `omni_project` is set via `PATCH /v1/sessions/{id}` `labels`, listed via
  `list_projects` (`GET /v1/sessions/projects`), and queried via `?project=`.
  `list_projects` **excludes projects whose every session is archived**
  (`sqlalchemy_store.py:1548`) — the same gotcha the archived-session project
  filter (#2115) must work around.

## 3. Gaps tags must close

| Value prop | Missing primitive |
|---|---|
| Multi-valued tags | New schema — `conversation_labels` PK is single-valued per key (§2.3) |
| Bulk grant/share | A bulk-grant store method + endpoint; grantee is still per-user (§2.1) |
| Tag-scoped search | Generic label filter on the list + an exposed FTS endpoint (§2.2) |
| Auto-tags | A conversation-level classifier/hook + a `source` distinction (§2.4) |

## 4. Proposed shape (for discussion — not final)

### 4.1 Data model
- New table `conversation_tags`: PK `(conversation_id, tag)` (multi-valued per
  conversation), plus `source in {manual, auto}`, `created_at`. Optionally
  `(namespace, value)` if we adopt `key:value` tags. `ON DELETE CASCADE` with the
  conversation, mirroring labels.
- Leave `conversation_labels` / `omni_project` untouched — **projects stay the
  single-valued "primary bucket"; tags are the additive cross-cut.**

### 4.2 API
- `POST|DELETE /v1/sessions/{id}/tags`, `GET /v1/sessions/{id}/tags`.
- `GET /v1/sessions?tag=...` (repeatable; define AND vs OR semantics), plus a
  listing of tags (mirror `list_projects`, but **do not** inherit its
  archived-exclusion — see §5).
- Expose ranked FTS: `GET /v1/search?q=...&tag=...` scoping full-text to a tag
  subset.

### 4.3 Bulk grant / share
- `POST /v1/tags/{tag}/permissions` (level, grantee) -> resolve tag -> member
  conversation ids -> fan out N `PermissionStore.grant` rows via a new
  `grant_many`. Define revoke semantics and what happens when a conversation
  later gains/loses the tag (grants do not auto-follow membership unless we
  design that in).

### 4.4 Auto-tags
- Add a post-run classifier (or extend the PolicyEngine label-write path) that
  proposes tags with `source=auto`. Users can confirm / pin / remove. Keep the
  model/prompt swappable; never let an auto-tag silently widen sharing.

### 4.5 UI
- Tag chips on session rows; a tag filter in the sidebar and in the **archived
  view** (reuses the #2115 filter pattern); multi-select + "share these / share
  this tag."

## 5. Open questions & risks
- **Namespacing**: freeform tags vs enforced `key:value`. Affects filter
  semantics and auto-tag output.
- **Archived-exclusion**: the tag listing must NOT copy `list_projects`'
  fully-archived exclusion, or tags on archived-only conversations vanish from
  the picker (same class of bug as #2115).
- **Postgres search**: no `tsvector` index today (ILIKE fallback). Tag-scoped FTS
  at scale needs a real index.
- **Permission explosion / drift**: bulk grant writes N rows; membership changes
  don't propagate unless designed to. Revoke and audit story needed.
- **Privacy**: auto-tags derived from content must respect the same privacy gates
  as sharing — an auto-tag must never be the thing that leaks a sensitive
  conversation into a bulk share.

## 6. Phasing
1. **Data model + manual tags + tag filter** (mirrors projects; smallest useful
   slice).
2. **Bulk grant / share by tag** (the headline collaboration win).
3. **Expose FTS + tag-scoped search** (the rediscovery win).
4. **Auto-tags** (curation-free organization; highest complexity / risk).

## 7. Relationship to projects and #2115
The archived-session **project filter (#2115)** is the near-term, UI-only win on
the existing single-valued project model. **Tags** are the larger follow-on:
multi-valued, cross-cutting, and the substrate for bulk-share and history search.
Projects remain the primary bucket; tags layer on top.
