# Custom git-host provider abstraction — design spec (v2)

**Status:** Draft for review (v2 — incorporates codex + agy adversarial review)
**Date:** 2026-07-15
**Branch:** `feat/custom-git-hosts` (worktree off `upstream/main` @ `0f6e82fb`)
**Anchor issue:** omnigent-ai#2125 (multi-host git credentials). **Related:** #1937 (GHE host for
Copilot), #1421 (non-HTTP credential broker), #236 (secretless credential proxy).
**Research:** `custom-git-hosts-codex.md`, `custom-git-hosts-research.md`.
**Reviews:** `custom-git-hosts-codex-review.md`, `custom-git-hosts-agy-review.md`.

## 0. What changed in v2 (review-driven)

Two blockers and several highs from the two-engine review reshaped the design:
- **Topology vs credentials are now decoupled.** The operator is the sole authority for git-host
  *definitions* (routing/URLs/CA/OIDC); users contribute only *credentials* bound to an operator host.
  (Fixes the precedence-hijack blocker.)
- **User credentials are encrypted at rest** (per-user opaque PATs); operator credentials stay
  reference-source. The "no reversible secret at rest" claim is now scoped precisely.
- **A real managed-clone credential contract** replaces "reuse the proxy verbatim": a short-lived,
  host-scoped token delivered into the sandbox (exec + Kubernetes init-container), with the
  "secretless" guarantee narrowed to the inner runner.
- **Session sharing is a first-class concern:** a shared session that carries a user's git
  credentials must warn (grounded in `SharingMode`/grants).
- Clone made launcher-wide (Kubernetes clones in an init-container, not `materialize_workspace`);
  provider resolution split out of the pure parser; GHE same-host git/API binding; operator-scoped
  private egress; keychain onboarding removed; typed operator-config parser; OIDC claim/secret
  handling specified.

## 1. Summary

Add a first-class, provider-neutral **git-host provider abstraction** to omnigent so self-hosted and
third-party git forges — Forgejo/Gitea, GitLab, GitHub Enterprise, Bitbucket — are supported
alongside github.com, multiple hosts coexisting, across both managed/disposable sandboxes and
long-lived `omni host`. The clone/credential/egress layers are already host-neutral (§4), so this is
an additive abstraction + registry + wiring — but the credential *transport* and *authorization* are
genuinely new work (§8), not verbatim reuse.

## 2. Goals / non-goals

**Goals**
- A `GitHostProvider` abstraction + registry mirroring omnigent's sandbox/LLM provider patterns.
- Multiple git hosts usable at once, on managed sandboxes **and** long-lived `omni host`.
- **Operator-defined hosts** (topology) + **user-supplied credentials** (bound to those hosts).
- Per-host credentials so hosts don't clobber each other (closes #2125).
- Agents open PRs / read issues on non-GitHub forges (MCP + policy, native-ready).
- Forge SSO (GHE / GitLab / self-hosted) as omnigent login providers.
- Safe session sharing: warn when a shared session carries user git credentials.
- Upstream-quality: additive, portable (no FKs/partial indexes), PR-able to omnigent-ai.

**Non-goals (this program)**
- Users defining or overriding host *topology* (operator-only, §7).
- Git protocols beyond HTTPS and SSH.
- A full in-process REST client per forge (native adapters are an optional later seam, §10).
- A remote credential broker in P1 (ephemeral-token transport first; broker is later hardening, §8.4).

## 3. Locked scope decisions

| Decision | Choice | Source |
|---|---|---|
| Depth | Generic provider abstraction | user |
| Deployment | Managed sandboxes **and** omni host | user |
| Host topology | **Operator-only** (users cannot define/override) | review B2 |
| User contribution | **Credentials only**, bound to an operator host | review B2 |
| Operator credential storage | Reference-source (env/file/command) | user |
| User credential storage | **Encrypted-at-rest** (Fernet, key in env) | user (v2) |
| Managed-clone transport | **Ephemeral token into sandbox** (P1); broker later | user (v2) |
| PR/issue mechanism | MCP-delegated + native-ready | user |
| Login/SSO | In scope | user |
| Session sharing | Warn when git credentials are in scope | user (v2) |
| Delivery | One comprehensive P1–P4 spec | user |

## 4. Current state (grounded, verified by review)

- **Clone URL is user-provided, never compared to github.com.** `parse_repo_workspace`
  (`omnigent/server/managed_hosts.py:508-549`) validates only the *shape* of `https://<host>/<path>`
  or `git@<host>:<path>` and does not canonicalize (case/port/userinfo remain).
- **`parse_repo_workspace` is a pure parser with no request identity** — it runs during Pydantic
  validation (`omnigent/server/schemas.py:1341-1386`) and on relaunch from a stored raw label
  (`omnigent/server/routes/sessions.py:7026-7045`). The create route re-parses *with* identity
  (`omnigent/server/routes/sessions.py:14586-14605`).
- **Managed clone paths differ by launcher.** The exec default runs `git clone` in
  `materialize_workspace` (`omnigent/onboarding/sandboxes/base.py:320-389`). **Kubernetes overrides
  `start_host` and clones in an init-container script** (`omnigent/onboarding/sandboxes/
  kubernetes.py:359-393`), never calling `materialize_workspace`. Islo delegates to `super()`
  (`omnigent/onboarding/sandboxes/islo.py:516-541`).
- **The credential proxy is runner-only.** `CredentialProxyEntry`/`CredentialSourceSpec`
  (`omnigent/inner/datamodel.py:377-450`) is host-generic, but `prepare_credential_proxy_runtime`
  (`omnigent/inner/credential_proxy.py:103-153`) is consumed only by the inner runner
  (`omnigent/inner/os_env.py:419-454`) — **not** the pre-host clone, which relies on the host image's
  helper reading a single baked `GIT_TOKEN`.
- **Runner env forwarding is a fixed set + operator passthrough.** `_build_runner_env`
  (`omnigent/host/connect.py:435-463,499-550`) forwards `_BASE_HARNESS_CREDENTIAL_ENV_VARS` +
  `OMNIGENT_RUNNER_ENV_PASSTHROUGH` names, exact-match, host-blind.
- **No in-process git API client.** PRs/issues reach the agent via the provider-neutral
  `ServerMcpPool` (`omnigent/server/mcp_pool.py`) or the agent's `gh` CLI;
  `omnigent/policies/builtins/github.py` only *classifies*.
- **Login token ≠ git token** (`omnigent/server/routes/auth.py:250-345`) — OAuth token resolves an
  email then is discarded; never a git credential.
- **Egress** is default-deny with arbitrary host/wildcard rules (`omnigent/inner/egress/
  rules.py:68-175`); `egress_allow_private_destinations` (`omnigent/inner/datamodel.py:627-650`) is a
  **global** switch (lifts private/reserved/IMDS protections for *every* destination).
- **Session sharing** = `SharingMode` (`omnigent/server/auth.py:83-102`: OFF/READ_ONLY/
  RESTRICTED_READ_ONLY/ON) governing read/edit/manage grants (`LEVEL_OWNER=4`);
  `workspace_sharing_blocked` (`:131-152`) already blocks over-broad cwds.
- **Only real github.com couplings:** the `gh_basic` preset defaults + `api.`-prefix heuristic
  (`omnigent/spec/parser.py:1206-1209,1638-1685`); the login OAuth constants
  (`omnigent/server/oidc.py:118-123`).

## 5. Architecture

```
   repo URL / host id                     (authenticated request)
          │
          ▼
   pure parse (shape only) ──►  owner-aware RESOLVER  ──►  immutable ClonePlan
   parse_repo_workspace          (operator hosts ∪ user creds;   {provider, canonical host,
   (no identity)                  operator-only topology)         normalized URL, cred handle,
          │                                                       CA, known_hosts}
          │                                    │
   ┌──────┴───────────┬──────────────┬─────────┴────────┬───────────────────┐
   ▼                  ▼              ▼                   ▼                   ▼
 clone (launcher-    per-host creds  egress rules    PR/issue: MCP + policy  login/SSO
 wide: exec          (operator ref-  (provider-       peer + CLI             (OIDC; GHE
 materialize +       source OR user  recommended;     (gh/glab/tea)          param; client
 k8s init-container; encrypted PAT;  operator opt-in                          secret via env)
 ephemeral token)    ephemeral into  for private)
                     sandbox)
```

Operator authority = topology (which hosts exist and where). User authority = a credential bound to
an operator host. The resolver is the single authenticated choke point; the parser stays pure.

## 6. Provider model

### 6.1 Interface

```python
class GitHostProvider(ABC):
    provider: ClassVar[str]                 # github | ghe | forgejo | gitlab | bitbucket
    default_clone_username: ClassVar[str]   # x-access-token | oauth2 | git | x-token-auth
    policy_peer: ClassVar[str | None] = None

    @abstractmethod
    def matches(self, host: str) -> bool: ...
    @abstractmethod
    def normalize_repo_url(self, url: str, cfg: HostConfig) -> str: ...
    @abstractmethod
    def clone_binding(self, cfg: HostConfig) -> CloneAuthBinding: ...   # scheme + username + how
    def egress_rules(self, cfg: HostConfig) -> list[str]: return []
    def mcp_server_config(self, cfg: HostConfig) -> MCPServerConfig | None: return None  # P2
    def cli_env(self, cfg: HostConfig) -> dict[str, str]: return {}
    def oauth_config(self, cfg: HostConfig) -> OIDCConfig | None: return None            # P3
    def api_client(self, cfg: HostConfig): return None                  # native-ready; None=MCP
```

Mirrors `SandboxLauncher(ABC)` (`omnigent/onboarding/sandboxes/base.py:152`). `clone_binding`
returns how to authenticate (Basic vs token-placeholder, username), not a secret.

### 6.2 Registry

`_GIT_HOSTS: dict[str, "module:Class"]` + `get_git_host()` + `available_providers()`, mirroring
`omnigent/onboarding/sandboxes/__init__.py:54-132`. Closed set. `github.com` → built-in `github`
default when no operator host matches. Entry-point plugin discovery deferred.

### 6.3 Onboarding (topology only — no secret)

Operator host definition uses an `AuthField`/`AuthMode`-style field schema
(`omnigent/onboarding/providers/__init__.py:46-92`) to collect **non-secret topology**: `web_url`,
`api_url` (defaulted per provider), `ssh_host`/`ssh_port`, `ca_bundle`, and a *reference* to the
operator credential source. **The `keychain:` LLM-style secret ref is not used here** (its fallback
is a reversible file — it would contradict the operator reference-source model). Secrets are supplied
per §8, not by onboarding.

## 7. Host topology & configuration (operator-only)

Operators are the sole authority for git-host definitions. Users cannot register hosts or override
routing — this closes the precedence-hijack blocker.

- **Storage:** operator YAML under a `git_hosts:` key in server config
  (`omnigent/server/server_config.py:58-79`) loaded by a **new typed, fail-closed parser** into an
  immutable structured record set, optionally refreshed on file mtime (the *pattern* of
  `omnigent/server/admin_list.py:83-157`, **not** `MtimeCachedIdentitySet`/`config_str_list`
  directly — those flatten to lowercased single tokens and cannot hold structured records).
- **Record:** `{id, provider, web_host, api_base, ssh_host?, ssh_port?, ca_bundle?, credential_source,
  egress_hints, oidc?}`. All non-secret.
- **Canonicalization:** hosts stored/compared as validated lowercase IDNA hostnames with explicit
  port; userinfo rejected. Resolution is exact-match on canonical host.

## 8. Credential & trust model

### 8.1 Operator credentials — reference-source
Operator host credentials are a `CredentialSourceSpec` (`omnigent/inner/datamodel.py:377-397`) —
`env`/`file`/`command`, resolved in the trusted parent. No secret at rest. `command` is
operator-only (trusted author).

### 8.2 User credentials — encrypted at rest
A user attaches a credential to an operator-defined host: a new `SqlGitCredential` row (§12.2) holding
`token_ciphertext` (Fernet, application-encrypted; **key in env**, never in DB), scoped
`(workspace_id, owner, host_id)`. Decrypted only in the trusted parent when composing the launch
credential; never serialized into `SandboxPolicy`. A user controls only an opaque secret — never a
command or URL — so this is safe from the RCE/topology risks that motivated reference-source-only.

### 8.3 Authorization (no user-chosen env names)
User credential resolution is **not** an arbitrary env-name lookup (which would let a caller name
another tenant's/operator's variable). The stored reference is an opaque, server-minted slot resolved
through an operator-owned mapping scoped to `(workspace_id, owner, canonical_host)`. Source
restrictions are enforced at **both write and launch time**, so no malformed/legacy row can reach the
`command` branch (`omnigent/inner/credential_proxy.py:184-208`).

### 8.4 Managed-clone credential transport (the #2125 crux)
`prepare_credential_proxy_runtime` is runner-internal and cannot carry a secret to a remote sandbox's
clone. P1 defines a **managed-clone credential contract**, delivered by the launcher:
- **Exec model:** the resolved, short-lived, host-scoped token is written into the sandbox via the
  provider's clone step in `materialize_workspace` (`omnigent/onboarding/sandboxes/base.py:320-389`)
  — as a transient git credential-helper file or per-host `GIT_ASKPASS`, removed after clone.
- **Kubernetes model:** injected as a Kubernetes **Secret** mounted/`env`-fed to the **init-container**
  clone script (`omnigent/onboarding/sandboxes/kubernetes.py:359-393`), Secret scoped to the pod and
  deleted on teardown.
- **Lifetime/redaction:** shortest feasible TTL; scrubbed from logs, shell history, and env dumps;
  bound to the exact canonical host.
- **Scope of the "secretless" guarantee:** narrowed to the **inner runner** helpers (the egress
  credential proxy). The pre-host clone is *not* secretless — the token enters the sandbox. This is
  explicit.
- **Broker (later hardening):** a reachable credential-rewriting proxy the clone routes through, so
  the raw secret never enters the sandbox. Out of P1.

### 8.5 Runner & long-lived host
- **Runner git ops:** prefer the credential-proxy **placeholder** path (`inject_env`) over forwarding
  real tokens. **Do not** add real provider tokens (`GITLAB_TOKEN`) to
  `_BASE_HARNESS_CREDENTIAL_ENV_VARS` — that puts real secrets in the runner env, defeating the
  placeholder model.
- **Long-lived `omni host`:** the server's environment is not the external host's environment.
  External-host credential sources are **host-local configuration**; the server retains only host
  *identity*. No server-side secret transport is implied for external hosts.

### 8.6 Session sharing
A shared session's runner may hold the sharer's per-host git credentials, so an **edit/manage** grant
(`SharingMode`/grants, `omnigent/server/auth.py:80-102`) lets the grantee act with the grantor's git
access. Requirements:
- A per-session signal `session_git_credentials_in_scope()` (analogous to `workspace_sharing_blocked`,
  `:131-152`) reports whether user git credentials are attached.
- The grant-creation flow **warns** the grantor when that signal is true, naming the hosts the grantee
  would gain access to; the grantor must explicitly confirm.
- Optional hardening (config): under `RESTRICTED_READ_ONLY`, block credential propagation into shared
  sessions, or require the grantee to supply their own credential for the host.

## 9. Clone / transport (launcher-wide)

- **Resolver, not parser:** keep `parse_repo_workspace` pure (shape only); add an owner-aware resolver
  after authentication (create route `sessions.py:14586-14605`; relaunch carries `host.owner`) that
  produces an immutable `ClonePlan` {provider, canonical host, normalized URL, credential handle, CA,
  known_hosts}. Define relaunch: persist the resolved host-config id/version, or deliberately
  re-resolve for `host.owner`.
- **Launcher-wide clone contract:** the `ClonePlan` is honored by **both** the exec
  `materialize_workspace` path **and** the Kubernetes init-container path (Secret/volume/env + CA
  bundle + `known_hosts`), with cleanup. `RepoWorkspace` (`managed_hosts.py:398-420`) carries the
  provider + plan reference.
- **Transport specifics:** SSH host-keys via injected `known_hosts`; custom CA via `GIT_SSL_CAINFO`
  or a mounted bundle; custom ports honored in URL normalization.

## 10. PR / issue / metadata (MCP-delegated + native-ready)

- Provider supplies an `MCPServerConfig` (GitHub/Gitea/GitLab MCP) through the neutral
  `ServerMcpPool` (`omnigent/server/mcp_pool.py:146-240`) + a **policy peer**
  (`omnigent/policies/builtins/{gitea,gitlab}.py`) — not a stretched `github.py` (its two-segment
  `owner/repo` model breaks on GitLab namespaces / Bitbucket workspaces).
- **CLI:** provider declares `gh`/`glab`/`tea` + host env via `cli_env`.
- **GHE same-host binding:** GHE serves clone + API on one hostname, but the parser rejects two
  bindings per host (`omnigent/spec/parser.py:1462-1478`) and the proxy allows one swap-per-host
  (`omnigent/inner/egress/proxy.py:225-240`). Needs a same-host binding that distinguishes
  placeholder-auth API requests from git requests (path/client-aware) — sibling `gh_basic` presets
  alone don't solve it.
- **Native-ready:** `api_client()` defaults `None`; implement per-forge later only where the MCP
  server is inadequate. **Bitbucket** has no first-class CLI and relies on a community MCP; treat it
  as **experimental until a native adapter lands** (may pull its `api_client` earlier than P4).

## 11. Egress

Provider **recommends** `{web,git,api}` rule strings into `egress_rules`
(`omnigent/inner/datamodel.py:614-626`). It **must not** auto-set `egress_allow_private_destinations`
(`:627-650`) — that flag is global and lifts protections for every destination. Private-forge access
stays an explicit **operator/spec-author opt-in**, or (better) is extended to host/CIDR-scoped
private access. The design defines the authorized merge point between server host configuration and
the agent-authored `OSEnvSandboxSpec` (operator rules are authoritative; agent rules cannot broaden
private access).

## 12. Data model & persistence

Portability verified: no FKs (Rule R032, migration `p1a2b3c4d5e6_remove_all_fks.py`), no partial
indexes (`z5a2b3c4d5e6_drop_partial_indexes`), split DBs (`OmnigentBase`, `omnigent/db/
db_models.py:172`), app-enforced schema.

### 12.1 Operator hosts → typed file config (§7). No DB.

### 12.2 User credentials → `SqlGitCredential(OmnigentBase)`
Mirrors `SqlHost` (`omnigent/db/db_models.py:1063-1150`): composite PK `(workspace_id, id)`,
`Uuid16`, `owner: String(256)`, `host_id`/`canonical_host`, `provider: SmallInteger`,
`token_ciphertext: Text` (Fernet; key in env), timestamps. `UniqueConstraint(workspace_id, owner,
canonical_host)` — **not** a partial index. **No FK**; app-cascade on user/host deletion
(`omnigent/stores/host_store.py:736-763`) **plus a reconciliation sweeper** for crash-orphaned rows
(no FK means no DB cascade). New `GitCredentialStore` + `GitCredential` dataclass +
`create_git_credentials_router` at `/v1/git-credentials`. **Store methods keyed by `(owner, host)`**
so authorization is hard to omit even though routes also check ownership
(`omnigent/server/routes/hosts.py:371-390`).

### 12.3 Resolution & precedence
Topology: operator-defined only (users have no say) → built-in github.com default. Credential for a
resolved host: the requesting user's `SqlGitCredential` for that host, else the operator's host
credential source, else (github.com) the legacy single `GIT_TOKEN`. No user override of topology.

## 13. Provider matrix

| Provider | Clone username | API base | CLI | MCP | SSO |
|---|---|---|---|---|---|
| github.com | `x-access-token` | api.github.com | `gh` | GitHub MCP | github branch (today) |
| GitHub Enterprise | `x-access-token` | `<host>/api/v3` (same host) | `gh`+`GH_HOST` | GitHub MCP | parameterize `_GITHUB_*` (P3) |
| Forgejo/Gitea | `<user>`/token | `<host>/api/v1` | `tea` | Gitea/Forgejo MCP | generic OIDC (claim-dependent) |
| GitLab | `oauth2` | `<host>/api/v4` | `glab` | GitLab MCP | generic OIDC (claim-dependent) |
| Bitbucket | `x-token-auth` | `/2.0` / `/rest` | — | community MCP (experimental) | OIDC (varies) |

## 14. Phasing

### P1 — Foundation (closes #2125)
Provider ABC + registry; **operator host topology** (typed config parser); **user `SqlGitCredential`**
(encrypted-at-rest) + store + route + reconciliation sweeper; **owner-aware resolver → ClonePlan**;
**launcher-wide clone credential contract for exec AND Kubernetes** (ephemeral token, CA/known_hosts,
cleanup); runner placeholder credentials; egress-rule derivation (operator-controlled private);
**session-sharing credential warning**.
**Acceptance:** github.com + a self-hosted Forgejo + a GitLab clone/fetch/push simultaneously, on
exec **and** Kubernetes managed sandboxes and omni host. **Verified via harness/operator flows and
mocks** (no PR/issue tooling yet — that's P2), plus a **live Forgejo + Gitea container**.
**Files:** new `omnigent/git_hosts/*`, `omnigent/stores/git_credential_store.py`,
`omnigent/server/routes/git_credentials.py`, `omnigent/db/db_models.py` (+migration), `enum_codecs`;
edits to `managed_hosts.py`, `sandboxes/base.py` + `sandboxes/kubernetes.py`, `host/connect.py`,
`server_config.py`, `server/auth.py` (sharing warning).

### P2 — PR/issue + tools
Per-provider `mcp_server_config` + policy peers + CLI selection (Forgejo/Gitea + GHE); GHE same-host
git/API binding; generalize the `gh_basic` API-host heuristic.
**Acceptance:** an agent opens a PR and reads an issue on the Forgejo host.

### P3 — SSO
Parameterize the GitHub OAuth branch for GHE (`oidc.py:118-123,309-332`, `auth.py:679-719`); make
generic-OIDC SSO work for GitLab/Forgejo — **acceptance conditional on the forge issuing `email`/
`email_verified` claims** (the generic path has no userinfo fallback, `auth.py:742-814`); add a
userinfo lookup or document the requirement. Define **OIDC client-secret** handling (operator env,
like `OIDCConfig.from_env`) — the git credential model does not cover it.
**Acceptance:** sign in via a GHE and a GitLab instance.

### P4 — Breadth
GitLab / Bitbucket providers (nested namespaces / workspaces); native `api_client` where a forge's
MCP is inadequate (Bitbucket likely).

## 15. Cross-cutting

- **Security:** operator topology only (no user routing); user credentials encrypted-at-rest, opaque,
  owner-scoped, decrypted parent-only, never in `SandboxPolicy`; managed-clone token is short-lived +
  redacted with the secretless guarantee scoped to the inner runner; session-sharing warns on git
  credentials; private egress stays operator-controlled.
- **Portability:** `SqlGitCredential` obeys R032 (no FK, app-cascade + sweeper), no partial index,
  `SmallInteger` enum, `Uuid16`, workspace-scoped.
- **Testing:** unit (registry, URL canonicalization, resolver precedence, credential authorization at
  write+launch, egress derivation, sharing signal); integration against **real Forgejo/Gitea/GitLab
  containers** for exec **and** Kubernetes clone paths + multi-host coexistence; SSO against real
  GitLab OIDC; live `glab`/`tea`. P1 uses harness/operator/mocks (no agent PR tooling yet).
- **Backward compatibility:** github.com + single `GIT_TOKEN` unchanged (built-in default provider).

## 16. Open questions

1. **Multi-tenant vs self-host default.** Encrypted-at-rest user PATs enable SaaS; confirm the
   Fernet-key management/rotation story (env-provided key; rotation = re-encrypt).
2. **Session-sharing hardening default.** Warn-only (P1) vs block credential propagation under
   `RESTRICTED_READ_ONLY` — pick the default.
3. **Relaunch semantics** — persist resolved host-config id/version vs re-resolve for `host.owner`.
4. **Managed-image dependencies** — which images ship `gh`/`glab`/`tea`, forge MCP servers, and
   CA/`known_hosts` (image-contract work outside `omnigent/`).
5. **Broker milestone** — when to graduate the pre-host clone from ephemeral-token to a
   never-in-sandbox broker.
