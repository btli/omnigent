# Kimi harnesses: emit model, token usage, and cost

**Date:** 2026-08-09
**Status:** Approved design, pre-implementation
**Scope:** `kimi` (headless) and `kimi-native` (interactive) harnesses

## Problem

Kimi sessions surface no model name, token usage, or cost anywhere in
omnigent — no cost badge, no per-model attribution, no context ring — and
cost-budget policies hard-DENY tool calls on kimi sessions because
`total_cost_usd` is never populated. Every other native harness
(claude-native, codex-native) reports all three.

### Root cause (investigated, Kimi Code 0.34.0)

- The headless `kimi` executor launches
  `kimi --output-format stream-json -p …` and parses stdout JSONL
  (`omnigent/inner/kimi_executor.py:460-508`). Kimi's stream-json protocol
  emits **only** assistant/tool/meta records — no usage, model, or cost
  fields exist in that stream (upstream
  `apps/kimi-code/src/cli/prompt-render.ts:77-99,262-265`). The executor's
  `TurnComplete` is emitted without `usage`
  (`omnigent/inner/kimi_executor.py:554-556`) even though the runtime
  accepts `usage` keys (`omnigent/inner/executor.py:151-177`).
- The `kimi-native` forwarder already tails Kimi's persisted wire log
  (`~/.kimi-code/sessions/<wd>/session_<id>/agents/main/wire.jsonl`) but
  maps only `turn.prompt`, `context.append_loop_event` content parts, and
  terminal `step.end` status (`omnigent/kimi_native_forwarder.py:195-263`).
  It drops the records that carry the missing data.
- The wire log **does contain everything needed**:
  - `usage.record` — `{model, usage: {inputOther, output, inputCacheRead,
    inputCacheCreation}, usageScope: "turn"}` (upstream
    `packages/agent-core-v2/src/agent/usage/usageOps.ts:52-65`,
    `packages/agent-core-v2/src/kosong/contract/usage.ts:9-14`).
  - `llm.request` — `{model, modelAlias, maxTokens, provider, …}` with the
    provider-resolved model id (e.g. `system.ai.kimi-k3`) and configured
    alias (e.g. `kimi-k3-databricks`).
- Kimi emits **no monetary data** in any mode. This matches codex-native,
  which sends tokens-only and lets the omnigent server price them.

## Ground-truth pattern to mirror (claude-native / codex-native)

Both native forwarders POST two generic event types to
`POST /v1/sessions/{id}/events`
(`omnigent/server/routes/sessions/routes_events.py:1002-1019`):

1. **`external_session_usage`** — cumulative (SET-semantics, monotonic
   server-side) token fields, `context_tokens`, `context_window`, and a
   `model` field riding along on every post. If no `cumulative_cost_usd` is
   sent, the server prices tokens via the model catalog
   (`_persist_native_cumulative_usage`,
   `omnigent/server/routes/_sessions/orchestration.py:1274-1462`;
   pricing in `omnigent/llms/context_window.py:159-289`). This is the
   codex path and the one kimi follows.
2. **`external_model_change`** — the active model id, persisted to
   `conv.model_override`. Codex deliberately does **not** seed the posted
   model at spawn (`omnigent/codex_native_forwarder.py:449-461`) so the
   real spawn model lands in `model_override` for pricing and model-gated
   policies. Kimi mirrors this.

Downstream consumers (no changes needed): `conv.session_usage` persistence,
per-model buckets, `session.usage` SSE + UI cost badge/context ring, usage
report route, and the `cost_budget` / `subagent_cost_budget` policies
(`omnigent/policies/builtins/cost.py:31,118-132,758-781`).

## Design

### Shared concept

Kimi's persisted `wire.jsonl` is the usage/model source of truth. Two
independent consumers, matching the existing headless/native split. No
server changes; no client-side cost computation.

### 1. kimi-native forwarder (`omnigent/kimi_native_forwarder.py`)

- **Map `usage.record`:** accumulate per-session cumulative totals from
  the per-turn records:
  - `cumulative_input_tokens` = Σ(`inputOther` + `inputCacheCreation`)
  - `cumulative_cache_read_input_tokens` = Σ(`inputCacheRead`)
  - `cumulative_output_tokens` = Σ(`output`)
  - Implementer note: verify the exact accepted keys against
    `_persist_native_cumulative_usage` and map cache-creation losslessly
    if the server accepts a dedicated cumulative cache-creation field;
    otherwise folding creation into input (above) is the accepted
    approximation.
- **Post coalesced `external_session_usage`** with `model` attached to
  every post, mirroring codex's `_SessionUsageCoalescer`
  (`omnigent/codex_native_forwarder.py:1392-1441`).
- **`context_tokens`** = latest record's
  (`inputOther` + `inputCacheRead` + `inputCacheCreation`) — current
  context occupancy.
- **`context_window`** resolved from omnigent's model-catalog helpers
  (`omnigent/llms/context_window.py`) by model id; omitted when
  unresolvable. Never derive it from `llm.request.maxTokens` (that is max
  *output* tokens).
- **Map `llm.request`:** track the effective model, preferring the
  provider-resolved `model` and falling back to `modelAlias`. Post deduped
  `external_model_change` mirroring codex `_sync_model_change`
  (`omnigent/codex_native_forwarder.py:2836-2875`): not seeded at spawn.

### 2. Headless executor (`omnigent/inner/kimi_executor.py`)

- Locate the wire log via glob
  `~/.kimi-code/sessions/*/session_<SID>/agents/main/wire.jsonl`, where
  `<SID>` comes from the `session.resume_hint` meta record the executor
  already captures.
- Keep a per-session byte-offset checkpoint so resumed sessions
  (`-S` / `-C`) only count records new to this turn.
- After the subprocess exits, sum the turn's `usage.record`s into
  `TurnComplete.usage`: `input_tokens` (= `inputOther`), `output_tokens`,
  `cache_read_input_tokens`, `cache_creation_input_tokens` — all already
  accepted by `omnigent/inner/executor.py:151-177`.
- Surface the effective model from `llm.request` through the headless
  runtime's existing model-reporting seam (implementer confirms the exact
  mechanism; if none exists for headless harnesses, report tokens only and
  note the gap in the PR).

### 3. Error handling

Usage extraction is strictly best-effort:

- Missing / locked / unparseable wire.jsonl, schema drift, unknown record
  shapes → log once (debug/warning), emit nothing, never fail the turn or
  the forwarder loop.
- Tolerant parsing: ignore unknown keys, type-check every field before
  use.
- The 0.34.0 wire schema is pinned in test fixtures so upstream drift
  fails loudly in CI rather than silently in production.

### 4. Testing

Mirror the existing suites:

- Forwarder: record mapping, cumulative accumulation, coalescer model
  ride-along, model-change dedup / spawn-model behavior — alongside the
  patterns in `tests/test_codex_native_forwarder.py:75-268,575,607`.
- Executor: fixture wire.jsonl tests in the style of
  `tests/inner/test_kimi_harness.py`, covering single-turn sums,
  resume/checkpoint offsets, missing file, and malformed records.
- Server attribution already covered by
  `tests/server/routes/test_native_usage_attribution.py` (no changes).
- Every new test is shown red with the mapping reverted (mutation-check).

## Out of scope / follow-up

- Upstream a structured usage record to MoonshotAI/kimi-cli stream-json;
  when it ships, the headless wire.jsonl read can be deleted.
- Kimi pricing entries in the model catalog: if `system.ai.kimi-k3` /
  Moonshot models are absent from the pricing catalog, tokens will persist
  but `total_cost_usd` stays unpriced — catalog additions are a separate
  concern (cf. PR #3144's precedent for pricing-id fixes).

## Prior art check (2026-08-09)

GitHub searches (`kimi usage`, `kimi cost`, all kimi issues/PRs on
omnigent-ai/omnigent) found no existing issue or PR addressing usage /
model / cost emission for kimi. Nearest neighbors, non-overlapping:
#1877 / #2323 (tool-call cards), #4441 (forwarder hardening), #3144
(pricing ids).
