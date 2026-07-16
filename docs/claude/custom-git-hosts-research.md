# Custom git host (Forgejo/Gitea/GitLab) support — research notes

**Worktree:** `/Users/bryanli/Projects/btli/omnigent-githost`
**Branch:** `feat/custom-git-hosts` off `upstream/main` @ `0f6e82fb` (fetched 2026-07-15, current)
**Question:** Does omnigent support custom / self-hosted git hosts (like our Forgejo) today? What
would it take to add?

Research split: Codex (gpt-5.6-sol, ultra) does the deep code pass; this file holds the parallel
PR/issue sweep + an independent code cross-check to validate Codex's report.

---

## Existing PRs / issues (omnigent-ai/omnigent)

**No existing or in-flight support for Forgejo / Gitea / GitLab / Bitbucket as a repo host.**
The only custom-host precedent is GitHub-Enterprise-flavored and harness-scoped.

Directly relevant:
- **Issue #2125** (OPEN, filed by `btli`, 2026-07-07; labels: enhancement, help wanted,
  comp:harnesses, comp:infra, P2-medium, triaged; 0 comments, no PR) —
  *"Multi-host git credentials for managed sandboxes (GitHub + self-hosted Forgejo/GitLab can't
  coexist)."* Core gap: managed sandboxes bake a **single** HTTPS git credential helper answering
  every host from one `GIT_TOKEN`/`GIT_USERNAME`. Proposes host-scoped env vars
  (`GIT_TOKEN_GIT_EXAMPLE_COM`) or a `GIT_CREDENTIALS_JSON` map. **This is the anchor issue.**
- **PR #1937** (OPEN) `fix(copilot): add gh auth login flow and GitHub Enterprise host
  configuration` — closes #1936. Adds `copilot.github_host` in `~/.omnigent/config.yaml`,
  propagated as `HARNESS_COPILOT_GITHUB_HOST` → `GH_HOST` to the Copilot CLI. **GitHub-Enterprise
  only, Copilot-harness only** — a template for "configurable host" but not a git-host abstraction.
- **Issue #1936** (BUG) — copilot 401 + no way to configure GHE host (the #1937 driver).

Supporting infra (what multi-host creds would build on):
- **PR #1421** (OPEN) `feat(sandbox): non-HTTP credential broker` — parent-side broker hands creds
  to a single tool invocation via AF_UNIX shim; explicitly rejects `egress_rules`. Non-HTTP focus
  (psql), but the credential-brokering pattern is adjacent.
- **PR #236** (MERGED) `feat(sandbox): secretless credential_proxy for egress (bearer + basic)` —
  rewrites HTTP Authorization headers at the egress proxy.
- **PR #2306** (OPEN, = current `feat/sandbox-host-config` branch) / **Issue #2126** — inject
  omnigent host config (providers/gateway) into managed sandboxes at launch. Sibling mechanism for
  pushing config into sandboxes; about LLM provider/gateway config, not git hosts.

Not relevant (name collision): "host" in many issues = the **`omni host` daemon** (a machine
hosting runner sessions: #2038, #2039, #2100, #2524, #1953), NOT a git host.

Searches that returned nothing relevant: `SCM`, `repository provider`, `worktree clone`,
`bitbucket`. No Forgejo/Gitea/GitLab/SCM-abstraction epic exists.

---

## Independent code cross-check (worktree @ 0f6e82fb)

Ground-truth spot checks to validate Codex's report:

1. **Clone URL is user-provided, not hardcoded.** `omnigent/server/schemas.py:1252-1253` — repo URL
   is `https://github.com/org/repo#main` *or* `git@github.com:org/repo.git`, "the server clones
   it." So a Forgejo URL would clone at the transport level **if** credentials resolve. Transport
   is not the blocker (matches #2125's framing).
2. **Egress is a configurable, rule-based allowlist (default-deny).** `omnigent/inner/egress/
   rules.py` — rules like `"GET api.github.com/repos/myorg/**"`, `"GET *.github.com/**"` (wildcard
   subdomains). `omnigent/inner/datamodel.py:430,617` — host+path patterns, default deny.
   `omnigent/spec/parser.py:1206` `_GH_BASIC_DEFAULT_TARGETS = ("github.com", "api.github.com")`
   and 1649-1655 — GH-basic egress targets **default** to github.com but are "optional" /
   overridable. So a Forgejo host **can** be allowlisted; github.com is just the baked default.
3. **Single git credential.** `omnigent/host/connect.py:448-449` feeds `GIT_TOKEN`/`GIT_USERNAME`
   into the sandbox host. One pair → the #2125 multi-host blocker.
4. **omnigent's own login OAuth is hardcoded to github.com.** `omnigent/server/oidc.py:118-122` —
   `_GITHUB_ISSUER`, `_GITHUB_AUTHORIZATION_ENDPOINT`, `_GITHUB_TOKEN_ENDPOINT`,
   `_GITHUB_USERINFO_ENDPOINT` all literal github.com. (This is user login to omnigent; need Codex
   to confirm whether this OAuth token is also the git-access token or purely identity.)
5. **No git-host provider abstraction.** LLM providers get a configurable `api_base`
   (`omnigent/onboarding/providers/__init__.py:616,670`); sandbox providers are pluggable. Git host
   has **no** analogous seam — `omnigent/policies/builtins/github.py` parses only GitHub URL shapes
   and references `gh pr view` (GitHub CLI), i.e. GitHub is assumed.

### Preliminary verdict (pre-Codex)
**Partial, leaning No for a real integration.** Raw `git clone` + push to a self-hosted Forgejo can
be coaxed *today* on a long-lived `omni host` (user-provided URL + add an egress rule + a per-host
`credential.helper=store`). It does **not** work for managed/disposable sandboxes (single baked
credential = #2125) and there is **no** first-class git-host concept: onboarding, login-OAuth,
policy parsing, and the `gh`-CLI PR/issue tools all assume GitHub. GitHub-Enterprise-style hosts are
the cheapest extension (configurable base URL, same API — #1937 shows the pattern); Forgejo/Gitea
needs an API adapter (different schema); GitLab/Bitbucket different again.

---

## Reconciliation with Codex (final)

Codex (gpt-5.6-sol, ultra) full report: **`docs/claude/custom-git-hosts-codex.md`** (verified
against the checkout; wrote it to disk before the 10-min run cap cut the tail — report is complete).

**Codex verified my anchors 1-5 and CORRECTED anchor 6:** `GH_HOST` is **not** in `main` — it's the
still-open PR #1937. On main, `omnigent/inner/copilot_executor.py` accepts only GitHub token vars,
no configurable host. So there is *no* in-tree configurable-git-host precedent today; #1937 would be
the first, and only for the Copilot harness's auth.

**Three gaps Codex resolved:**
- **Login token ≠ git token.** GitHub OAuth login is identity-only: the access token resolves a
  verified email, then an omnigent session JWT (`sub/iat/exp/provider` only) is minted; the OAuth
  token is discarded (`server/routes/auth.py:250-289`, `server/oidc.py:53-83`). Git access comes
  from a *separate* host/deployment-supplied `GIT_TOKEN`/`GIT_USERNAME`. → github.com-hardcoded
  login does **not** block a Forgejo repo.
- **Managed clone = plain `git clone`.** Managed provisioning runs `git clone [--branch B] -- URL
  DEST` (`onboarding/sandboxes/base.py:285-389`) with the *user-provided* URL, injecting no
  credential — it relies on the host **image's** git credential helper reading the single ambient
  `GIT_TOKEN` (`server/managed_hosts.py:1748-1753`). External/long-lived `omni host` never clones —
  it runs in a pre-existing checkout (`host/connect.py:1029-1078`), so it's already forge-neutral.
- **No in-process GitHub API client.** The *only* direct GitHub HTTP call is the login email lookup.
  PR/issue/metadata reach the agent via (a) a configured external **MCP server** or (b) agent-issued
  **`gh`** CLI in the sandbox. `ServerMcpPool` is provider-neutral (`server/mcp_pool.py`); the GitHub
  policy only *classifies* operations, it doesn't execute them (`policies/builtins/github.py`).

**Biggest lever for a real integration:** because PR/issue support is delegated to a provider-neutral
MCP transport, adding Forgejo/Gitea PR/issue parity = **plug in a Gitea MCP server + a policy peer**,
not build an in-process API client. And a **per-host** credential primitive already exists
(`inner/credential_proxy.py`, host-bound, env/file/command sources) — it's just **not wired into the
pre-host managed clone** (the #2125 gap).

**Resolved verdict:** **Partial.** Long-lived `omni host` ≈ works today (pre-cloned, forge-neutral).
Managed/disposable sandboxes are where the gaps live: single ambient credential (#2125), no git-host
provider object joining {host identity, per-host creds, API base, CLI/MCP, policy}. GitHub-Enterprise
= cheapest (Tier A: config + host-keyed creds, same API); Forgejo/Gitea = Tier B (needs MCP adapter +
policy peer); GitLab/Bitbucket = Tier C (different identity models).
