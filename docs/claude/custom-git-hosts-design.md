# Custom git-host provider abstraction — design spec

**Status:** Draft for review
**Date:** 2026-07-15
**Branch:** `feat/custom-git-hosts` (worktree off `upstream/main` @ `0f6e82fb`)
**Anchor issue:** omnigent-ai#2125 (multi-host git credentials). **Related:** #1937 (GHE host for
Copilot), #1421 (non-HTTP credential broker), #236 (secretless credential proxy).
**Research:** `docs/claude/custom-git-hosts-codex.md`, `docs/claude/custom-git-hosts-research.md`.

## 1. Summary

Add a first-class, provider-neutral **git-host provider abstraction** to omnigent so that
self-hosted and third-party git forges — Forgejo/Gitea, GitLab, GitHub Enterprise, Bitbucket —
are supported alongside github.com, with multiple hosts coexisting simultaneously, across both
managed/disposable sandboxes and long-lived `omni host` deployments.

This is a **program** (P1–P4), not a single feature. The clone, credential, and egress layers are
already host-neutral (§4), so the work is an additive abstraction + registry + wiring on top of
existing seams — not a rewrite. The one net-new user-facing capability (persisting a user-pasted
credential) is intentionally **out of scope** per the reference-source-only credential decision (§7).

## 2. Goals / non-goals

**Goals**
- A `GitHostProvider` abstraction + registry mirroring omnigent's sandbox/LLM provider patterns.
- Multiple git hosts usable at once (github.com + a self-hosted Forgejo + a customer GitLab).
- Works for managed/disposable sandboxes **and** long-lived `omni host`.
- Config surface: operator-curated hosts **and** user-registered hosts, with a precedence rule.
- Per-host credentials so hosts don't clobber each other (closes #2125).
- Agents can open PRs / read issues on non-GitHub forges (via MCP + policy, native-ready).
- Forge SSO: GitHub Enterprise / GitLab / self-hosted forges as omnigent login providers.
- Upstream-quality: additive, portable (no FKs/partial indexes), PR-able to omnigent-ai.

**Non-goals (this program)**
- Storing a reversible user-pasted secret at rest (reference-source-only, §7). Documented future
  extension, not a P1 gap.
- Replacing GitHub-specific agent tooling that already works for github.com.
- Git protocols beyond HTTPS and SSH.
- A full in-process REST client for every forge (native adapters are an *optional* later seam, §9).

## 3. Locked scope decisions (with rationale)

| Decision | Choice | Why |
|---|---|---|
| Depth | Generic provider abstraction | Platform capability for enterprise customers; github.com is just one provider. |
| Deployment | Both (managed sandboxes + omni host) | #2125 is managed-specific; omni host is already ~forge-neutral (pre-cloned). |
| Config surface | Operator **and** user-registered hosts | Org-curated defaults + user ad-hoc additions. |
| PR/issue mechanism | MCP-delegated + native-ready | omnigent has zero in-process git API client; it delegates to MCP/CLI. Grain-aligned, least code. |
| Login/SSO | In scope | Enterprise SSO via forge; a generic OIDC branch already exists (§11). |
| Credentials at rest | Reference-source only (env/file/command) | Keeps omnigent's "no reversible server secret" posture; reuses `credential_proxy`. |
| Delivery | One comprehensive P1–P4 spec | User directive. |

## 4. Current state (what works today, grounded)

Verified against `0f6e82fb` by Codex (gpt-5.6-sol) and three independent explorers.

- **Clone URL is user-provided and never compared to github.com.** `parse_repo_workspace`
  (`omnigent/server/managed_hosts.py:508-549`) validates only the *shape* of `https://<host>/<path>`
  or `git@<host>:<path>` and extracts the host without an allowlist check. Every `github` in the
  managed-clone path is a docstring example.
- **Managed clone = plain `git clone`.** `SandboxLauncher.materialize_workspace`
  (`omnigent/onboarding/sandboxes/base.py:320-389`, clone built at 370-379) runs
  `git clone [--branch B --single-branch] -- <url> <dest>`, injecting no credential — it relies on
  the host image's git credential helper reading the single ambient `GIT_TOKEN`/`GIT_USERNAME`.
- **Long-lived `omni host` never clones.** It runs in a pre-existing checkout
  (`omnigent/host/connect.py` launch handler), so it is already forge-neutral.
- **Credentials are host-generic but singly-baked.** `CredentialProxyEntry`
  (`omnigent/inner/datamodel.py:400-450`) is already per-host (host / scheme ∈ {basic,bearer,token}
  / source ∈ {env,file,command} / username / inject_env). Multiple entries already coexist **in the
  runner** — but the proxy is wired only into the runner (`omnigent/inner/os_env.py:439`), **not the
  pre-host managed clone**. The clone still uses one baked `GIT_TOKEN`. This is the #2125 crux.
- **No in-process git API client.** The only direct GitHub HTTP call is the login email lookup.
  PRs/issues reach the agent via a provider-neutral MCP server (`omnigent/server/mcp_pool.py`) or the
  agent's `gh` CLI; `omnigent/policies/builtins/github.py` only *classifies* operations.
- **Login token ≠ git token.** GitHub OAuth login resolves a verified email then mints a session JWT
  (`omnigent/server/routes/auth.py:250-345`); the OAuth token is discarded, never persisted, never a
  git credential.
- **Egress is a configurable default-deny allowlist.** `EgressRule`
  (`omnigent/inner/egress/rules.py:68-121`) accepts arbitrary exact/wildcard hosts;
  `egress_allow_private_destinations` (`omnigent/inner/datamodel.py:627-650`) reaches intranet forges.
- **The only real github.com couplings:** (a) the `gh_basic` credential preset's default targets +
  its `api.`-prefix "API host" heuristic (`omnigent/spec/parser.py:1206-1209,1667`); (b) the login
  OAuth endpoint constants (`omnigent/server/oidc.py:118-123`).

**Verdict:** Partial today. Long-lived omni host ≈ works (pre-cloned). Managed sandboxes are where
the gaps live: single baked clone credential, and no provider object joining
{host identity, per-host creds, API base, CLI/MCP, policy}.

## 5. Architecture

A new `GitHostProvider` layer sits on top of the already-neutral plumbing and does six jobs per
host: **identify → select credentials → normalize clone URL → emit egress rules → pick CLI/MCP →
provide policy**. Selection is keyed on the repo URL's host (or an explicit host id).

```
   repo URL / host id
          │
          ▼
   GitHostProvider registry ──────── resolve(host) → (provider, HostConfig, CredentialSourceSpec)
          │
   ┌──────┼───────────────┬────────────────┬─────────────────────┐
   ▼      ▼               ▼                ▼                     ▼
 clone   per-host creds  egress rules   PR/issue: MCP server    login/SSO
 (materialize_workspace  (CredentialProxy (already generic;    config + policy peer   (OIDC config;
  override + parse_repo_  Entry, now also  provider emits       (+ CLI gh/glab/tea)    generic branch
  workspace host hook)    wired into clone) rule strings)        native-ready seam      + GHE params)
```

The provider abstraction reuses two existing omnigent idioms (explorer-confirmed): the **sandbox
provider** shape (ABC + name→class registry + lazy factory; `materialize_workspace` template method)
for behavior, and the **LLM provider** shape (`AuthField`/`AuthMode` onboarding schema + `keychain:`/
`env:` secret refs) for onboarding/credentials.

## 6. Provider model

### 6.1 The interface

```python
class GitHostProvider(ABC):
    provider: ClassVar[str]                 # "github" | "ghe" | "forgejo" | "gitlab" | "bitbucket"
    default_clone_username: ClassVar[str]   # e.g. "x-access-token" (GitHub), "oauth2" (GitLab)

    @abstractmethod
    def matches(self, host: str) -> bool: ...                       # host → is this me?
    @abstractmethod
    def normalize_repo_url(self, url: str, cfg: HostConfig) -> str: ...  # scheme/port/ssh
    @abstractmethod
    def clone_credential(self, host: str, cfg: HostConfig) -> CredentialProxyEntry: ...
    @abstractmethod
    def egress_rules(self, cfg: HostConfig) -> list[str]: ...       # {web,git,api} rule strings
    def mcp_server_config(self, cfg: HostConfig) -> MCPServerConfig | None: return None  # P2
    def cli_env(self, cfg: HostConfig) -> dict[str, str]: return {}  # GH_HOST / GITLAB_HOST / ...
    def oauth_config(self, cfg: HostConfig) -> OIDCConfig | None: return None  # P3 (SSO)
    def api_client(self, cfg: HostConfig): return None  # native-ready seam; None = MCP-delegated
    policy_peer: ClassVar[str | None] = None            # module path of the policy classifier (P2)
```

Mirrors `SandboxLauncher(ABC)` (`omnigent/onboarding/sandboxes/base.py:152`, `provider` ClassVar at
:165, template methods at :230/:320). `clone_credential` returns the existing
`CredentialProxyEntry` (`omnigent/inner/datamodel.py:400-450`) so no new credential type is invented.

### 6.2 The registry

```python
_GIT_HOSTS: dict[str, str] = {          # provider name → "module:ClassName"
    "github":    "omnigent.git_hosts.github:GitHubProvider",
    "ghe":       "omnigent.git_hosts.github:GitHubEnterpriseProvider",
    "forgejo":   "omnigent.git_hosts.gitea:GiteaProvider",   # forgejo == gitea API
    "gitlab":    "omnigent.git_hosts.gitlab:GitLabProvider",
    "bitbucket": "omnigent.git_hosts.bitbucket:BitbucketProvider",
}
def get_git_host(provider: str) -> GitHostProvider: ...       # lazy import + instantiate
def available_providers() -> list[str]: ...                   # find_spec, no import side effects
def resolve_provider_for_host(host: str, configs: HostConfigSet) -> GitHostProvider | None: ...
```

Mirrors `omnigent/onboarding/sandboxes/__init__.py:57-132` (`_LAUNCHERS`, `get_launcher`,
`available_providers`). Closed set. `github.com` maps to the built-in `github` provider as the
default when no configured host matches. Entry-point plugin discovery (like
`omnigent.community.harness`, `omnigent/onboarding/harness_plugins.py:45,650`) is **deferred** — add
only if out-of-tree providers are required.

### 6.3 Onboarding schema

Each provider declares an `AuthField`/`AuthMode`/`ProviderConfig` schema
(`omnigent/onboarding/providers/__init__.py:46-92`) collected the way `provider_selection.py:74-141`
collects LLM credentials, and persists a `keychain:`/`env:` secret ref resolved by `resolve_secret`
(`omnigent/onboarding/provider_config.py:425-478`). New provider onboarding fields: `web_url`,
`api_url` (defaulted per provider), `credential source`, optional `ssh_host`/`ssh_port`, `ca_bundle`.

## 7. Credential model (reference-source only)

**No secret is stored at rest.** A host's credential is always a `CredentialSourceSpec`
(`omnigent/inner/datamodel.py:377-397`) — `kind ∈ {env, file, command}` — resolved **in the trusted
unsandboxed parent** at launch by `prepare_credential_proxy_runtime`
(`omnigent/inner/credential_proxy.py:103-153`). This reuses the existing mechanism verbatim; the
"encrypted-at-rest token column" considered earlier is dropped.

**Trust boundary (important).** `command` sources run `subprocess.run(shell=True)` in the trusted
parent (`omnigent/inner/credential_proxy.py:184-208`). That is safe when the spec author is trusted
(operator / agent-spec author) but a privilege-escalation vector if an untrusted end user can author
it. Therefore:

| Host origin | Allowed credential sources | Rationale |
|---|---|---|
| **Operator** (config file) | `env`, `file`, `command` | Operator is trusted. |
| **User-registered** (web/API) | `env` **only**, restricted to an allowlisted namespace (e.g. `OMNIGENT_GITHOST_*`) | A user must not reference `DATABASE_URL` or run arbitrary commands. |

**Consequence (accepted trade-off).** Under reference-source-only, a user-registered host carries
identity; its token must already be resolvable on the server (operator-provisioned env). **Pasting a
PAT in the web UI is not supported** in this program. In the common single-tenant / self-host
deployment (`OMNIGENT_LOCAL_SINGLE_USER=1`) the user *is* the operator, so this is natural. Multi-
tenant SaaS with per-user pasted PATs is the documented future extension (add the encrypted-at-rest
option from the earlier design). See Open Questions (§16).

**Closing #2125.** The provider emits one `CredentialProxyEntry` per configured host, resolved at
launch, replacing the single baked `GIT_TOKEN` helper for multi-host cases:
- **Runner git ops:** already supported — multiple `CredentialProxyEntry`s coexist in the runner via
  the existing proxy. The provider layer just generates them from host configs.
- **Managed pre-host clone (new wiring):** wire per-host credential resolution into
  `materialize_workspace` (§8) so the clone authenticates against the *right* host's token. The
  provider's `clone_credential` supplies scheme + username + source per host.
- **Fallback for baked-helper-only images:** support the issue's lighter `GIT_TOKEN_<HOST>` env
  convention as an alternative a smarter baked helper can read, for images that can't run the proxy.

## 8. Clone / transport

- **Host→provider hook:** in `parse_repo_workspace` (`omnigent/server/managed_hosts.py:508-549`),
  after the host is extracted (530-547), resolve the provider and normalize the URL (scheme, custom
  port, scp↔https) before building `RepoWorkspace`. Attach the resolved provider id to the workspace
  so the launcher can pick the right `clone_credential`. `RepoWorkspace` (`:398-420`) gains a
  `provider: str | None` field; `_arm_and_start_host` (`:1935-1946`) passes it through.
- **Per-provider clone:** `materialize_workspace` (`omnigent/onboarding/sandboxes/base.py:320-389`)
  is the designated override point. The default EXEC path stays for standard HTTPS/scp. **New P1
  work** injected here: resolve the per-host credential (via the credential proxy) and provide
  SSH host-key / custom-CA / custom-port handling for self-hosted forges (managed images need the CA
  bundle + `known_hosts`). Providers needing mirrors/SSH override this one method.
- **Runner passthrough:** provider CLI tokens (e.g. `GITLAB_TOKEN`, `FORGEJO_TOKEN`) forward via
  `_build_runner_env` (`omnigent/host/connect.py:499-550`) — add them to
  `_BASE_HARNESS_CREDENTIAL_ENV_VARS` (`:435-451`) or the operator `OMNIGENT_RUNNER_ENV_PASSTHROUGH`
  escape hatch (`:463`). Selection is exact-name; provider declares its var names.

## 9. PR / issue / metadata (MCP-delegated + native-ready)

- **Mechanism:** the provider supplies an `MCPServerConfig` pointed at the forge's MCP server
  (GitHub MCP, Gitea/Forgejo MCP, GitLab MCP), threaded through the existing provider-neutral
  `ServerMcpPool` (`omnigent/server/mcp_pool.py:146-240`) and session MCP declarations
  (`omnigent/server/routes/session_mcp_servers.py`). omnigent builds **no** in-process REST client.
- **Policy peers:** add per-provider policy modules (`omnigent/policies/builtins/gitea.py`,
  `gitlab.py`, …) that classify that provider's MCP/CLI operations and URL identities. **Do not
  stretch** `omnigent/policies/builtins/github.py` — its two-segment `owner/repo` model doesn't fit
  GitLab nested namespaces or Bitbucket workspaces.
- **CLI selection:** provider declares its CLI (`gh` / `glab` / `tea`) + host env (`GH_HOST`,
  `GITLAB_HOST`, …) via `cli_env`; images bundle or install the relevant CLI.
- **Native-ready:** `api_client()` defaults to `None` (delegated). Implement a native REST adapter
  per forge later *only if* that forge's MCP server proves inadequate. This is Approach-3's endgame
  reached incrementally.

## 10. Egress

The provider emits egress rule strings for its `{web, git, api}` hosts into the spec's `egress_rules`
(`omnigent/inner/datamodel.py:614-626`), and sets `egress_allow_private_destinations: true` for
intranet forges (`:627-650`). **Zero change to the egress layer** — pure derivation. Rule shape from
`omnigent/inner/egress/rules.py:129-175` (e.g. `"* git.acme.com/**"`, `"GET,POST git.acme.com/api/**"`).

## 11. Login / SSO

The OAuth path already dispatches on `provider_type ∈ {"github","oidc"}`
(`omnigent/server/oidc.py:129-374`) and a **generic OIDC branch already exists** (`:334-374`, uses
`<issuer>/.well-known/openid-configuration`).

- **GitLab.com / self-hosted GitLab / Forgejo SSO ≈ zero code:** set
  `OMNIGENT_OIDC_ISSUER=https://gitlab.example.com`; discovery flows through the generic branch.
  Deliverable = document + test.
- **GitHub Enterprise SSO:** parameterize the hardcoded `_GITHUB_*` endpoint constants (`:118-123`),
  the `is_github` equality check (`:310`), and `_resolve_github_email`
  (`omnigent/server/routes/auth.py:679-719`) to derive endpoints from a GHE-host env var. The
  `provider_type` dispatch (auth.py 263/285) already generalizes.
- **Separation preserved:** the login token remains identity-only (confirmed) and is never reused as
  a git credential. SSO and git-access stay independent subsystems joined only by host identity.

## 12. Data model & persistence

Portability rules **verified**: no DB foreign keys (Rule R032, migration
`p1a2b3c4d5e6_remove_all_fks.py`), no partial indexes (migration `z5a2b3c4d5e6_drop_partial_indexes`,
MySQL compat), split physical DBs (`OmnigentBase`/`ConversationBase`, `omnigent/db/db_models.py:172,
183`), application-enforced schema. (Note: `omnigent/server/DBSPEC.md` is stale — trust R032 + the
migrations.)

### 12.1 Operator hosts → file/config (no DB)

Follows `admin_list.py` + `server_config.py`. Add a `git_hosts:` key to server config, read via
`config_str_list(load_server_config().get(...))` (`omnigent/server/server_config.py:58-95`),
optionally unioned with a runtime-editable `<data_dir>/git-hosts` file
(`MtimeCachedIdentitySet`, `omnigent/server/admin_list.py:83-157`). Stores only non-secret host
identity (id, provider, web_url, api_url, ssh_host, credential *source descriptor*, egress hints).
Secrets stay in env (`server_config.py:13-18`).

### 12.2 User-registered hosts → `SqlGitHost` entity

New `SqlGitHost(OmnigentBase)` mirroring `SqlHost` (`omnigent/db/db_models.py:1063-1150`):
- Composite PK `(workspace_id, id)`; `workspace_id` `BigInteger default=current_workspace_id`; `id`
  `Uuid16`.
- `owner: String(256)` (every query filtered by it, like `HostStore.list_hosts`).
- `provider: SmallInteger` (via a new `enum_codecs` map), `web_url`, `api_url`, `host`, `label`,
  `credential_ref` (the constrained `env`-namespace name — **not** a secret), `created_at`,
  `updated_at` (Integer epoch).
- Uniqueness via `UniqueConstraint("workspace_id","owner","host")` — **not** a partial index.
- **No FK**; app-owned cleanup on user/host deletion (pattern: `host_store.delete_host:736-763`).

New `GitHostStore` (`omnigent/stores/git_host_store.py`) + `GitHost` `@dataclass` entity +
`_row_to_git_host`, mirroring `omnigent/stores/host_store.py:45-187`. Served via
`create_git_hosts_router(...)` at `/v1/git-hosts`, mirroring `omnigent/server/routes/hosts.py:288-369`,
with `owner == user_id` ownership checks. Pydantic only at the HTTP edge.

### 12.3 Precedence

Resolve a repo URL's host by exact match against: (1) the requesting user's registered hosts
(owner-scoped), which **override** (2) operator hosts for the same host string, which override
(3) the built-in `github.com` default. First match wins; ties resolved user > operator > built-in.

## 13. Provider matrix

| Provider | Clone auth username | API | CLI | MCP | SSO |
|---|---|---|---|---|---|
| github.com | `x-access-token` | REST/GraphQL api.github.com | `gh` | GitHub MCP | github branch (today) |
| GitHub Enterprise | `x-access-token` | `<host>/api/v3` | `gh` + `GH_HOST` | GitHub MCP | parameterize `_GITHUB_*` (P3) |
| Forgejo/Gitea | `<user>` or token | `<host>/api/v1` (Gitea API) | `tea` | Gitea/Forgejo MCP | generic OIDC |
| GitLab | `oauth2` | `<host>/api/v4` | `glab` | GitLab MCP | generic OIDC (≈ free) |
| Bitbucket | `x-token-auth` | `/2.0` (Cloud) / `/rest` (DC) | — | community MCP | OIDC (varies) |

The `gh_basic` preset's `api.`-prefix heuristic (`omnigent/spec/parser.py:1667`) must be generalized
per provider (GHE's `/api/v3` is not `api.<host>`): add sibling `_normalize_*` presets or a
per-preset "API host" selector.

## 14. Phasing

### P1 — Foundation (closes #2125)
Provider ABC + registry (`omnigent/git_hosts/`); host resolution (operator file-config + `SqlGitHost`
+ store + route + precedence); per-host credential generation from host configs; **wire per-host
creds into the managed clone** (`materialize_workspace`) and runner; generic clone (SSH host-keys,
custom CA, custom ports); egress-rule derivation.
**Acceptance:** clone/fetch/push works for github.com + a self-hosted Forgejo + a GitLab
simultaneously, on both managed sandboxes and omni host. Live-tested against a real Forgejo container.
**Files:** new `omnigent/git_hosts/*`, `omnigent/stores/git_host_store.py`,
`omnigent/server/routes/git_hosts.py`, `omnigent/db/db_models.py` (+migration),
`omnigent/db/enum_codecs.py`; edits to `managed_hosts.py`, `onboarding/sandboxes/base.py`,
`host/connect.py`, `server_config.py`, `admin_list.py`.

### P2 — PR/issue + tools
Per-provider `mcp_server_config` + policy peers (`policies/builtins/{gitea,gitlab}.py`) + CLI
selection for Forgejo/Gitea + GHE; generalize the `gh_basic` API-host heuristic.
**Acceptance:** an agent opens a PR and reads an issue on the Forgejo host.
**Files:** `omnigent/git_hosts/*` (mcp/cli), `omnigent/policies/builtins/*`, `spec/parser.py`.

### P3 — SSO
Parameterize the GitHub OAuth branch for GHE (`oidc.py`, `routes/auth.py`); document + test generic-
OIDC SSO for GitLab/Forgejo.
**Acceptance:** sign in to omnigent via a GHE and a GitLab instance.
**Files:** `omnigent/server/oidc.py`, `omnigent/server/routes/auth.py`, docs.

### P4 — Breadth
GitLab / Bitbucket providers (nested namespaces / workspaces); native `api_client` where a forge's
MCP is inadequate.
**Acceptance:** full provider matrix green.
**Files:** `omnigent/git_hosts/{gitlab,bitbucket}.py`, policy peers.

## 15. Cross-cutting

- **Security:** no reversible secret at rest (§7); user-registered credential sources constrained to
  namespaced `env`; provider-emitted `command` sources operator-only; per-host creds never serialized
  into `SandboxPolicy` (the credential proxy already resolves parent-only); self-hosted CA trust is
  explicit per host, not global.
- **Portability:** `SqlGitHost` obeys R032 (no FK, app-cascade), no partial index (UniqueConstraint),
  `SmallInteger` enum codec, `Uuid16` PK, workspace-scoped.
- **Testing:** unit (registry, URL normalization, credential generation, precedence, egress
  derivation); integration against a **real Forgejo + Gitea + GitLab container** (managed clone,
  multi-host coexistence, push/fetch); SSO against real GitLab OIDC; live-test real CLIs (`glab`,
  `tea`) — not fakes.
- **Backward compatibility:** github.com with a single `GIT_TOKEN` keeps working unchanged (built-in
  default provider, single-entry credential path). Additive throughout.

## 16. Open questions / risks

1. **User-registered PAT UX.** Reference-source-only means no web-pasted PAT. Confirm the
   single-tenant/operator-env model is acceptable for the target deployments, or schedule the
   encrypted-at-rest extension for multi-tenant SaaS.
2. **Managed-image dependencies.** Which managed images ship `gh`/`glab`/`tea`, a Gitea/GitLab MCP
   server, and CA/`known_hosts` for self-hosted forges? Image contract work may be needed (outside
   `omnigent/`).
3. **Entrypoint-override providers** (Kubernetes) override `start_host`/`materialize_workspace`; each
   must implement the per-host clone credential wiring — verify per sandbox provider.
4. **MCP server maturity** for Forgejo/GitLab varies; the native `api_client` seam is the mitigation.
5. **Pre-host clone egress.** The inner egress proxy governs the *runner*, not the pre-host managed
   clone; intranet-forge reachability during provisioning depends on each sandbox provider's network/
   DNS/CA — validate per provider.
