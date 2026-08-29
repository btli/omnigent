<!-- Consolidated: absorbs #4441 (forwarder auth/lifecycle/discovery) and #4481 (model/usage/cost) into this PR. -->

## Related issue

Closes omnigent-ai/omnigent#4437. Also fixes #4479 (kimi model/usage/cost reporting), absorbed from #4481; the forwarder-lifecycle findings B, C, D, E, H from #4437 are absorbed from #4441.

## Summary

Kimi-native sessions were the flakiest lane: turns injected into an unready TUI, sessions stranded `running` forever after a kimi crash, parallel sessions adopting each other's transcripts, and no model / token / cost data at all (which made cost-budget policies hard-DENY every kimi tool call). This PR is the consolidated kimi hardening + accounting omnibus, in three layers:

**TUI interaction hardening (originally this PR)**

- Separate 120 s readiness budget waiting for kimi's input row + context footer (cold first boot takes ~36 s; the old 30 s gate pasted into an unready pane and reported success), with a typed error when the TUI never mounts.
- Verify pasted drafts appear and then leave the input box, retrying Enter before failing the turn with an actionable error.
- Bound approval polling below kimi's 600 s hook ceiling, re-park with the same elicitation id, and make approval-marker misses loud and typed; empty pane captures are retried before the menu is declared gone.

**Forwarder lifecycle, auth, and discovery (absorbed from #4441)**

- **Turn edges:** the top-level `turn.ended` wire record is the authoritative terminal signal (`completed`/`cancelled` → idle, `failed` → failed with the provider error surfaced; `step.end` finish reasons are never edges — failed/cancelled turns write no `step.end` at all, pinned by real-session fixtures under `tests/fixtures/kimi_wire/`). A dead kimi tmux pane mid-turn posts a failed edge; a 5-minute quiet-wire quiescence fallback closes edge-less turns, suppressed while a tool call is in flight (with a 30-minute hung-tool ceiling). Turn lifecycle is persisted with the cursor so fallbacks stay armed across forwarder restarts, and edges are deduped by id across restarts.
- **Auth + delivery:** refresh-capable `httpx.Auth` (mirroring qwen/claude) instead of a one-shot bearer snapshot; transient 4xx (401/403/408/409/425/429) retry with backoff, permanent 4xx poison-drop after 3 attempts so one bad line can't stall the tail forever; a dropped edge's status is remembered so the fallback close never softens a failure to idle; sustained edge-delivery failure alerts via rate-limited error logs.
- **Discovery:** each session home gets a private `sessions/` store + `session_index.jsonl` (no more symlinks to the global kimi store), so parallel kimi sessions can't adopt each other's wire logs; strict launch-epoch discovery.
- **Reads + reaper:** byte-offset incremental tailing (truncation rewind, partial trailing line deferred) instead of whole-file re-reads at 4 Hz; kimi exempted from the 1-hour pane idle reaper (no resumable chat id → a reaped pane is context-free).

**Model, token usage, and server-priced cost (absorbed from #4481)**

- The forwarder maps `usage.record` rows into cumulative, coalesced `external_session_usage` posts (input inclusive of cache reads per the server contract; cache-creation folds into input; `context_tokens` from the latest record; `context_window` resolved per model and omitted when unresolvable) and mirrors `llm.request` as a deduped `external_model_change` — so the cost badge, context ring, and cost-budget gate work on kimi sessions. Cumulative totals persist separately from the disposable wire cursor and survive terminal recreation; wire-cursor advancement is never gated on usage durability (recorded design ruling), and a per-poll sync retries failed usage posts even when no new wire rows arrive.
- The headless `kimi -p` executor sums each turn's `usage.record` rows into `TurnComplete.usage` with a byte-offset checkpoint and time-gated first read so resumes never re-bill history; the effective model is stamped into `usage["model"]`.
- `find_model_context_window()`: the existing window lookup minus the 128 K default, for callers that must omit rather than guess.
- **Kimi posted rates (consolidation addition):** the shared MLflow catalog typically carries no kimi entries, so `fetch_model_pricing()` now falls back to kimi's posted API rates when the catalog misses (catalog stays authoritative when present). Rates fetched from https://platform.kimi.ai/ on 2026-08-28 — K3: $3.00 input / $15.00 output / $0.30 cache-hit per MTok; K2.7 Code: $0.95 / $4.00 / $0.19; K2.6: $0.95 / $4.00 / $0.16. Kimi publishes no cache-write rate, so cache writes use the standard derived ratio. Family ids live in an owned `StaticModelFallback` record (`KIMI_PRICED_MODEL_FAMILIES`) per the no-hardcoded-models guard; the rate table in `context_window.py` keys off it.

ELI5: don't hand kimi a letter until its mailbox exists, and confirm the letter left the box. Kimi keeps a diary (`wire.jsonl`) of what it said, when each turn really ended, and every model call's token counts; we now tail that diary with a bookmark — recognizing crashes and interruptions as endings, giving each session its own diary shelf, renewing our library card — and tell the server, which prices the tokens (at kimi's posted rates when the catalog doesn't know the model) into the cost badge and budget gate.

```
web turn ► readiness: input row + footer ► paste ► draft visible ► Enter/retry ► draft gone
permission hook ► web poll ► empty window ► same elicitation id ► re-park ► approve/deny key

kimi TUI (tmux pane) ──appends──► wire.jsonl (session-private)
                                     │ byte-offset tail (250 ms)
                                     ▼
                              kimi forwarder ── refresh-capable auth ──► POST /v1/sessions/{id}/events
                               ├─ turn.ended completed/cancelled ─► idle       ├─ external_conversation_item
                               ├─ turn.ended failed ──────────────► failed     ├─ external_session_usage (cumulative)
                               ├─ pane died mid-turn ─────────────► failed     ├─ external_model_change (deduped)
                               ├─ wire quiet 5 min (no tool) ─────► idle       └─ external_session_status
                               └─ tool in flight quiet 30 min ────► failed
                                  cost: client-priced sum over per-model segments (cumulative_cost_usd);
                                  server token-prices at the current model as fallback (catalog, else posted kimi rates)
kimi -p (headless) ─► wire.jsonl ─► executor ─► TurnComplete.usage {tokens, model}
```

## Design rulings

Recorded human rulings from the #4481 review tribunal — engines must not re-raise these:

- **`AP_CONTEXT_WINDOW_OVERRIDE` is honored by the forwarder's context-window lookup.** An explicit operator action documented as override-everything; uncatalogued kimi models are exactly its use case.
- **Wire-cursor advancement is never gated on usage-state durability (transcript liveness > usage durability).** A failed usage-state persist logs once, keeps totals in memory, and the per-poll sync retries delivery and the write; clamp-safe under the server's monotonic clamp.

## Test Plan

- `uv run pytest tests/test_kimi_native_forwarder.py tests/inner/test_kimi_harness.py tests/inner/test_kimi_native_executor.py tests/llms/test_context_window.py tests/runner/test_app_sessions_native_workflow_init.py tests/terminals/test_pane_reaper.py tests/test_kimi_native_credentials.py tests/test_kimi_native_hook.py` — **445 passed** (the union of all three source PRs' suites plus the new pricing, per-model-segment cost, wire-recreation, restart, and first-read tests).
- `pre-commit run --files <all changed files>` — all hooks pass except pyrefly, whose only errors are pre-existing environmental missing-imports (optional opentelemetry extras) in untouched `omnigent/runtime/telemetry.py`.
- **Kimi rate verification:** fetched https://platform.kimi.ai/ on 2026-08-28 and pinned the posted per-MTok rates (K3 3.00/15.00/0.30, K2.7 Code 0.95/4.00/0.19, K2.6 0.95/4.00/0.16) into `_KIMI_POSTED_PRICING`; unit tests assert the exact values, the substring match on forwarder-reported ids (`system.ai.kimi-k3`, `kimi-k3-databricks`), and that a catalog entry still wins.
- Merge reconciliation covered by tests: the real-wire fixtures now also exercise the usage/model parser (`failed.jsonl` yields `message → model → turn_failed`); the usage-sync live-loop tests run against the byte-offset loop from #4441.
- Adversarial review round 1 (3 reviewers, 6 findings) applied: per-model segment-priced `cumulative_cost_usd` (mid-session model switches no longer mis-charge under the server's monotonic clamp), generation-dedupe reset on same-path wire recreation, first-read `llm.request` attribution without a fabricated `time`, persisted in-flight assistant text, wire-independent usage retry + fallback-close sync, and the pending-first-read billing floor — each with mutation-checked tests.
- Deflaked `test_empty_capture_is_retried_before_menu_disappears` (patches the settle timeout/poll interval like the rest of its class; the real 0.5 s window left ~0.2 s of scheduling margin under load).

## Demo

N/A — non-visual bridge/forwarder/pricing change.

## Type of change

- [x] Bug fix
- [x] Feature
- [ ] UI / frontend change
- [ ] Refactor / chore
- [ ] Docs
- [ ] Test / CI
- [ ] Breaking change

## Test coverage

- [x] Unit tests added / updated
- [x] Integration tests added / updated
- [ ] E2E tests added / updated
- [ ] Manual verification completed
- [x] Existing tests cover this change
- [ ] Not applicable

## Coverage notes

Wire fixtures are pinned verbatim from real Kimi Code 0.34.0 `wire.jsonl` logs (turn edges, `usage.record` with `usageScope:"turn"`, `llm.request` with provider-resolved model + alias), so upstream schema drift fails loudly in CI. Native/Python-only (no `web/` changes), so the e2e-ui judge's maintainer skip label applies.

## Changelog

Kimi sessions now report their model, token usage, and server-priced cost (at kimi's posted API rates when uncatalogued), deliver reliably into the web transcript, and can no longer strand a session `running` after a crash or interrupt.
