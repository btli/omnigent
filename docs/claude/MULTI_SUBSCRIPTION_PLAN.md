# Subscription-Aware Token Management for Omnigent

> Status: in progress. Owner: Claude Code session 2026-06-14.

## Goal

Give omnigent native support for **multiple provider subscriptions/credentials per
family**, modeled on [claude-swap](https://github.com/realiti4/claude-swap)
and mirroring the feature already shipped in `remote-dev`.

Confirmed requirements:

1. **Multiple credentials per family.** Surfaces: Claude subscription (OAuth, isolated
   `CLAUDE_CONFIG_DIR` per account), Claude API key, Codex subscription, OpenAI API key.
2. **Tier fallback.** When all subscription accounts are exhausted, fall back to API-key
   credentials.
3. **Intelligent routing.** Select among available accounts by **remaining headroom**,
   using **soonest quota reset** as a tiebreaker / when all are limited.
4. **Detection (both).** Reactive (429 / "usage limit reached" in the agent stream) and
   proactive (probe `POST /v1/messages` with `max_tokens=1`, read rate-limit headers).
5. **Auto-failover** with modes: `notify` / `auto` / `disabled`.
6. **Per-account cost attribution**, extending the existing cost-tracking pipeline.

Architecture: **Clean DDD** in a new `omnigent/subscription_tokens/` package. YAML config (`pools:`
block) is the source of truth; it is synced into DB tables at startup. Volatile state
(limit windows, per-account cost, session→account bindings) lives in DB.

## Naming

`entities/account.py` + `server/routes/accounts_auth.py` are **web-UI users** — a
different concept. This feature uses `ProviderAccount` / `CredentialPool` /
`credential_id` throughout. Never bare `Account`.

## Package layout

```
omnigent/subscription_tokens/
  domain/
    value_objects/  account_kind, usage_window, limit_state, rate_limit_headers, rotation_policy
    entities/       provider_account, credential_pool, failover_mode
  application/
    ports/          repositories + gateway + selection + cost sink + notifier + session registry (Protocols)
    use_cases/      track_usage_limit, select_credential, failover_on_limit
  infrastructure/
    repositories/   sqlalchemy_* + cost sink + session binding registry
    detection/      reactive_output_detector, anthropic_usage_probe, openai_usage_probe,
                    usage_endpoint_poller, composite_usage_limit_gateway
    selection/      priority_selection_policy (headroom + reset aware)
    notification/   sse_failover_notifier
  config/           pool_config (load_pools), pool_config_syncer
  container.py      DI wiring
```

## Config schema (`~/.omnigent/config.yaml`)

```yaml
providers:        # unchanged, still works for single-account use
  ...
pools:
  claude-pool:
    family: anthropic
    failover: auto            # notify | auto | disabled
    members:
      - name: claude-pro-1
        kind: subscription
        claude_config_dir: ~/.claude-acct1
        priority: 0
      - name: claude-pro-2
        kind: subscription
        claude_config_dir: ~/.claude-acct2
        priority: 1
      - name: claude-api
        kind: api_key
        api_key_ref: env:ANTHROPIC_API_KEY
        priority: 10          # tier fallback (api_key) — used only when subs exhausted
  codex-pool:
    family: openai
    failover: notify
    members:
      - name: codex-sub-1
        kind: subscription
        codex_config_dir: ~/.codex-acct1
        priority: 0
      - name: openai-api
        kind: api_key
        api_key_ref: env:OPENAI_API_KEY
        priority: 5
```

## DB tables (Alembic, down_revision = m1a2b3c4d5e6)

- `credential_pools` (id, name, family, failover_mode, timestamps)
- `provider_accounts` (id, pool_id?, name, family, kind, priority, claude_config_dir?,
  codex_config_dir?, api_key_ref?, is_active, timestamps)
- `provider_account_limit_states` (credential_id PK, limit_status, window_5h_pct,
  window_7d_pct, reset_at_5h, reset_at_7d, detection_source, last_checked_at, updated_at)
- `provider_account_costs` (credential_id, day_utc PK pair, cost_usd, input_tokens,
  output_tokens, turn_count, updated_at)
- `session_credential_bindings` (session_id PK, credential_id, family, bound_at)

## Integration seams (existing omnigent files)

- `onboarding/provider_config.py` — unchanged structurally; `pools:` parsed separately.
- `claude_native.py:resolve_native_claude_config` / `_native_claude_config_from_entry` —
  pool selection + `CLAUDE_CONFIG_DIR` injection.
- `runtime/workflow.py:_resolve_provider_for_build` — pool selection for SDK harnesses.
- `inner/claude_sdk_executor.py` (~2202 `api_retry`) + native forwarders — reactive detect.
- `server/routes/sessions.py` (`_accumulate_session_usage`, `_persist_native_cumulative_usage`)
  — per-account cost attribution.
- `host/connect.py:HARNESS_CREDENTIAL_ENV_VARS` — forward `CLAUDE_CONFIG_DIR` / `CODEX_HOME`.
- `server/app.py:_lifespan` — start proactive poll loop.

## Routing algorithm (headroom + reset aware)

Among candidates for a family, ordered by configured priority then:
1. **Available now** = not limited, or limited but `earliest_reset_at <= now`.
2. Prefer subscription tier over api_key tier; only fall to api_key when **no**
   subscription is available (tier fallback).
3. Within the chosen tier's available accounts, pick **max remaining headroom**
   (`100 - max(window_5h_pct, window_7d_pct)`), break ties by lower priority.
4. If none available: pick the candidate with the **soonest** `earliest_reset_at`
   (best effort — never block a launch).

## Build phases

1. Scaffold + plan (this doc).
2. Domain value objects + entities (pure, TDD).
3. `pools:` config parsing (TDD).
4. DB models + migration + config syncer.
5. Ports + SQLAlchemy repositories.
6. Detection adapters (reactive pure + httpx probes + poller + composite gateway).
7. Selection policy + use-cases + DI container.
8. Wire auth-resolution seams (+ env forwarding).
9. Wire reactive detection + cost attribution + poller + status/CRUD routes.
10. Validate: ruff, mypy --strict, pytest; code review.

## Status (2026-06-14)

**Complete & validated** — `omnigent/subscription_tokens/` package (domain, config, DB, repos,
detection, selection, use-cases, container, integration facade). 67 unit/integration
tests, ruff clean, `mypy --strict` clean. Existing DB/onboarding/host suites still green;
single Alembic head `n1a2b3c4d5e6`.

Wired into omnigent:
- `db_models.py` + migration `n1a2b3c4d5e6` — 5 tables.
- `claude_native.py` (CLI) + `runner/app.py` (server-spawned) — launch-time Claude account
  selection → `CLAUDE_CONFIG_DIR` / tier-fallback API key injection.
- `codex_native.py` (CLI) + `runner/app.py` (server-spawned) — launch-time **OpenAI/Codex**
  account selection via `integration.select_codex_launch`. Because Codex isolates each
  session in a private `CODEX_HOME` and bridges auth/config in from a *source* home, the
  selected subscription account's `CODEX_HOME` is threaded as `config_source` through
  `resolve_native_codex_launch` (login check) + `build_codex_native_server` → `start()`
  (auth bridge), not merged as a flat env override; a tier-fallback api_key is exported as
  `OPENAI_API_KEY` with codex's `openai` provider forced. The session binds at launch so
  reactive failover + cost attribution resolve.
- `host/connect.py` — `CLAUDE_CONFIG_DIR` / `CODEX_HOME` added to the forward allowlist.
- `server/app.py` lifespan — config→DB sync + proactive poll loop (flag-gated).
- `cli.py` — points the facade at the server's DB via `OMNIGENT_DATABASE_URI`.
- `server/routes/sessions.py` — per-account cost attribution in `_accumulate_session_usage`.
- `server/routes/subscription-tokens.py` — `GET /v1/subscription-tokens/status`, `POST /v1/subscription-tokens/accounts/{id}/mark-available`.
- `claude_native_forwarder.py` + `codex_native_forwarder.py` — **reactive in-stream
  detection**: every forwarded assistant/system message is scanned
  (`integration.extract_message_text` → `integration.record_reactive_text`, parse-first so
  it's regex-only when no pool is configured) for a family usage-limit signal
  (`family="anthropic"` / `family="openai"`), recording the limit + firing failover. The
  OpenAI detector requires a provider mention so a quoted error never triggers a bogus
  failover; OpenAI limits are additionally covered by the proactive poller + 429.

**Follow-ons:**
- SDK-harness (`claude-sdk`/`codex`/`openai-agents`) launch selection via
  `runtime/workflow.py:_resolve_provider_for_build` + a 429 hook in
  `inner/claude_sdk_executor.py` (only the native CLI/runner path is wired today).

## Adversarial review (Opus + Codex + Gemini) — round 2

Ran three independent adversarial reviews. Fixed the confirmed issues:
- **Native cost path**: per-account attribution was only on the relay path; added it to
  `_persist_native_cumulative_usage` (the path subscription-token's native sessions actually use).
- **CLI binding**: `_claude_terminal_request` now threads `session_id` so `omnigent claude`
  sessions bind (failover/cost work, not just selection).
- **Reactive false positives**: the forwarder scans only assistant/system **message** items
  (not user/tool content), so a user prompt quoting the limit phrase can't trigger failover.
- **Sub-agent attribution**: sub-agent items scan against the parent (account-bearing)
  session via a new `subtoken_session_id` param.
- **Event loop**: the reactive hook is offloaded via `asyncio.to_thread`; lazy init is now
  guarded by a `threading.Lock` (double-checked).
- **Poller lockout**: poll-sweep detections run through `_with_recovery` so a 429/retry-after
  with no window still auto-recovers; the sweep's track call is inside the try/except.
- **Facade safety**: `status_snapshot` wrapped in try/except → `[]`; `select_launch_env_for_family`
  binds the session only after a usable account-specific env is built.

Deferred (low value / single-worker-safe): TOCTOU double-fire of failover and non-atomic cost
increments matter only under multi-process uvicorn workers; RFC-1123 HTTP-date `Retry-After`
parsing; cosmetic window-slot labelling in `status`; tie-break ordering. **Auto failover**
rebinds the session for the *next* launch + notifies; it does not kill the in-flight process
(matches remote-dev's "never kill a running session").

## Credential visibility (which account is a session using?)

The binding registry already knew which account each session runs on
(`session_credential_bindings`, `active_credential(session_id)`), but it was internal-only —
no log, API, or UI surfaced it. This feature exposes it:

- **Per-session indicator.** `SessionResponse.active_credential`
  (`ActiveCredentialInfo`: id, name, kind, family, limit_status) is populated in
  `_get_session_snapshot` via `integration.active_credential_for_session(root_id)` (resolved
  on `root_conversation_id`, matching cost attribution; run concurrently with subtree-usage via
  `asyncio.gather`). `None` without a `pools:` block, so single-account setups are unaffected.
  Stable for the session's lifetime (failover rebinds the *next* launch, not the running
  process), so no SSE event is needed — the normal snapshot refetch carries it.
- **Launch log.** `select_launch_env_for_family` / `select_codex_launch` emit one INFO line
  naming the chosen account, the cheap operator win.
- **Operator reverse-view.** `GET /v1/subscription-tokens/status` attaches per-account
  `active_sessions` — the registry's never-deleted bindings, batch-resolved
  (`sessions_for_credentials(ids, only_session_ids=running)`) and **filtered in SQL to the live
  `_session_status_cache` set** so a long-running session is never hidden behind newer dead
  bindings. **Admin-gated**: session ids cross user boundaries, so non-admins get the base
  snapshot without `active_sessions`.
- **Web UI.** A self-hiding composer-footer `CredentialChip` (account name + kind icon +
  limit-status dot, HoverCard detail) beside the agent/harness pill, plus an "Account" row in
  the AgentInfo popover. Reads `chatStore.sessionActiveCredential`, seeded on bind exactly like
  `sessionHarness`. No i18n (matches ap-web); oxlint/prettier/tsc clean.

### Adversarial review (Opus + Codex + Gemini) — round 3

All three converged on one HIGH: `active_sessions` leaked cross-user session ids on the
auth-but-not-admin status endpoint → **admin-gated**. Codex caught the `LIMIT 200`-before-
intersection bug (a live session hidden behind 200 dead bindings) → **fixed by filtering to the
live set in SQL before the per-credential cap** (`sessions_for_credentials`, replacing the
singular method + N-query loop). Opus's hot-path note → `asyncio.gather`. Docstrings clarified
(launch-binding semantics; best-effort liveness cache). Tests cover the admin gate, the
live-filter-beats-cap fix, the batched distribution, the mapper, and the chip.

## Headless OAuth subscription tokens (`oauth_token_ref`)

A subscription pool member can authenticate by a **headless OAuth token** (`claude
setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`; Codex → `CODEX_ACCESS_TOKEN`) instead of an
isolated config dir — the cleanest way to rotate between multiple Claude/Codex
**subscriptions** without per-account login dirs:

- New `ProviderAccount.oauth_token_ref` (reference only, e.g. `env:VAR` — never a raw
  token), a new nullable `provider_accounts.oauth_token_ref` column (additive migration
  `o1a2b3c4d5e6`, `batch_alter_table` for SQLite), config parse + `resolve_account_oauth_token`.
- Launch injection: `select_launch_env_for_family` emits `CLAUDE_CODE_OAUTH_TOKEN`;
  `select_codex_launch` carries `access_token` → `CODEX_ACCESS_TOKEN` (also allowlisted into
  `codex_terminal_env` so the TUI inherits it). Config dir wins if both present — and the
  parser **rejects** a member that sets both (launch + poller would otherwise auth as
  different accounts). The poller's `load_subscription_token` uses the token ref too.
- **Binding correctness**: a subscription only binds when a credential was applied OR it is a
  genuine default-login account (no dir, no token ref); an oauth ref that doesn't resolve does
  not bind (no cost/failover mis-attribution).

### Adversarial review (Opus + Codex + Gemini) — round 4

Gemini CLEAN first pass. Opus (2) + Codex (4) found real issues, all fixed + tested:
`load_subscription_token` no longer falls back to the ambient `~/.claude`/`~/.codex` login
when an oauth-only sub's token is unset (mis-attribution); `codex_terminal_env` allowlists
`CODEX_ACCESS_TOKEN`; the config-dir+`oauth_token_ref` combo is a hard parse error; the poll
sweep logs `account.id` not the secret-bearing dataclass; the resolvers catch the full
best-effort tuple. **All three CLEAN on the final round.**
