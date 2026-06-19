# Upstream extraction plan — multi-subscription "net-new" slice

> **Scaffold note, not for the eventual PR.** This file tracks how the
> `feat/multi-subscription` fork feature is being carved into upstream-able
> pieces. Remove or relocate it before opening the actual PR.

## Why this branch exists

`feat/multi-subscription` (the deployed fork branch — homelab CI builds it, do
**not** history-rewrite it) is one cohesive DDD package. Upstream
(`omnigent-ai/omnigent`) is already building the overlapping *pool-selection*
slice — issues #503 / #692, PRs #583 (`claude_profiles` via `CLAUDE_CONFIG_DIR`)
/ #700 — and `main` moves very fast (v0.2.0, 2026-06-19). So we **split**:

- **Net-new upstream** (no competing work; `anthropic-ratelimit-unified` = 0 hits
  on `main`): usage-limit detection, on-429 failover, subscription-token
  onboarding, the CEL credential-label bridge.
- **Deferred to the fork** until #583 lands: pool config/selection/launch wiring,
  DB migrations touching `conversations`, `POST /v1/sessions`, the `ap-web`
  credential UI — the surfaces #583 will reshape.

This branch is based on **`upstream/main`** (not the fork branch) so the diff is
minimal and rebases cleanly; every carved file is a **new** file (the package is
absent upstream), so there is no collision risk for what is staged here.

## Stage 0 — pure domain foundation  ✅ DONE (this commit)

Carved, dependency-free (stdlib only, no infra/DB/server/UI). Verified on
`upstream/main` 997ed7fe: **32 tests pass, `ruff` clean, `mypy --strict` clean.**

| File | What |
| --- | --- |
| `domain/value_objects/usage_window.py` | 5h-rolling / weekly window value object |
| `domain/value_objects/rate_limit_headers.py` | parse `anthropic-ratelimit-unified-*` → windows + reset times |
| `domain/value_objects/limit_state.py` | account limit-state model (headroom, reset, staleness source) |
| `domain/value_objects/rotation_policy.py` | ranking logic (`max_headroom` / `soonest_reset`, tier fallback, best-effort recovery) |
| `domain/value_objects/enums.py` | shared literals (family, kind, failover, rotation, limit status, source) |
| `domain/entities/provider_account.py` | one credential slot |
| `domain/entities/credential_pool.py` | a family-scoped pool of accounts |
| `tests/subscription_tokens/test_domain.py` | the proven 32-test suite for the above |

## Staging the rest (draw final PR boundary with maintainer input)

1. **Reactive on-429 detection** — ✅ *drafted on this branch.*
   `infrastructure/detection/reactive_output_detector.py` (pure; scans agent
   output → `LimitDetectionResult`) + `application/ports/ports.py` (Protocols) +
   `application/use_cases/track_usage_limit.py`, with extracted tests
   (`test_reactive_detection.py` + `test_track_usage_limit.py`). Green: 11 added
   tests, `ruff` + `mypy --strict` clean. **Remaining for the PR:** the
   `claude_native_forwarder` / `codex_native_forwarder` seam that calls the
   detector — deferred until cutting the PR, since it modifies upstream files
   that have moved and is best adapted against current `main`. Net-new, no #583
   collision.
2. **Proactive poller + header gateway** — `usage_endpoint_poller`,
   `composite_usage_limit_gateway`, `probes`, `detection/credentials.py` +
   persistence (`repositories/sqlalchemy_repositories.py`, `sql_upsert.py`) + the
   **new** limit-state/account/cost DB tables. Additive tables only; keep clear
   of #583's `conversations` migration. ⚠️ Needs the Postgres + concurrency test
   for the `pg_advisory_xact_lock` path (the one coverage gap from review).
3. **Subscription-token onboarding** — `onboarding/provider_config.py`
   (`oauth_token_ref` / `resolve_secret`) + headless-OAuth credential source.
   Extends the #606 ambient-detection pattern. Low collision.
4. **CEL credential-label bridge** — `labels.py` + `runtime/policies/` projection.
   Sits on selection, so it follows whatever the pool decision settles.

### Deferred to the fork pending #583

`config/pool_config*.py`, `infrastructure/selection/priority_selection_policy.py`,
`application/use_cases/select_credential.py`, `container.py`, the launch path in
`integration.py`, the `POST /v1/sessions` changes, session-credential binding,
and all of `ap-web/`. These are what #583's `claude_profiles` / `CLAUDE_CONFIG_DIR`
primitive will reshape.

## Open question (blocks the final boundary)

Per #692 / #503 (both `help wanted`, `triaged`): do the maintainers want pooling
**layered on #583's `claude_profiles` primitive**, or shipped as an **independent
pool abstraction**? Coordination comment drafted; post before finalizing stages
1–4. Keep any design compatible with the implemented secretless credential proxy
(real secret stays parent-side).
