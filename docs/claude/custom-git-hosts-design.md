# Custom git-host provider abstraction — design spec (v3)

**Status:** Draft for review (v3 — incorporates codex + agy review, re-review, and the credential/
identity discussion)
**Date:** 2026-07-15
**Branch:** `feat/custom-git-hosts` (worktree off `upstream/main` @ `0f6e82fb`)
**Anchor issue:** omnigent-ai#2125. **Related:** #1937, #1421, #236.
**Research:** `custom-git-hosts-codex.md`, `custom-git-hosts-research.md`.
**Reviews:** `custom-git-hosts-codex-review.md`, `custom-git-hosts-agy-review.md`,
`custom-git-hosts-codex-rereview.md`.

## 0. What changed in v3

- **Shared-session identity model settled.** Push auth = session **owner** (default, model A) or an
  optional per-workspace **service "bot"** (model B); per-user push is deferred. **Commit authorship
  = the session starter** (consistent across the session). Enforcement is two-layer: omnigent grant
  gates *who drives*, the forge scopes gate *what's possible*.
- **Server→runner credential handoff is built in P1** — the authenticated channel that delivers a
  server-held (encrypted) user credential to the managed runner parent for fetch/push. Resolves the
  re-review's second blocker.
- **Exec clone secret delivery is an explicit launcher capability** (not smuggled through a shell
  string); the "short-lived" claim is corrected to "launch-scoped delivery."
- **Kubernetes clone credential is a distinct Secret deleted right after init** (not at teardown).
- **Sharing warning covers late credential attachment / re-resolution**, defines owner-consent
  semantics, and adds a headless/API confirm field.
- **Credential row id *is* the opaque slot**; all authority fields are server-derived; launch
  re-validates the operator host still exists.
- **Resolver runs before durable session creation**; relaunch semantics are locked (persist non-secret
  host-config id/version + canonical URL + slot id; re-authorize for `host.owner`).
- **Egress effective-spec merge point named**; **Fernet key *list*** for rotation.

## 1. Summary

A provider-neutral **git-host provider abstraction** so self-hosted/third-party forges (Forgejo/Gitea,
GitLab, GitHub Enterprise, Bitbucket) are supported alongside github.com, multiple hosts coexisting,
across managed sandboxes **and** long-lived `omni host`. The clone/credential/egress layers are
already host-neutral (§4); the genuinely new work is the provider abstraction, the credential
*authorization + transport* (§8), and the shared-session identity model (§8.6).

## 2. Goals / non-goals

**Goals:** provider abstraction + registry; multiple coexisting hosts on managed **and** external
hosts; operator-defined topology + user/bot credentials; per-host credentials (closes #2125); agent
PR/issue on forges (MCP + policy, native-ready); forge SSO; safe session sharing with honest
attribution; upstream-quality + portable.

**Non-goals:** users defining/overriding host *topology*; per-user *push* identity (deferred, §8.6);
protocols beyond HTTPS/SSH; a full in-process REST client per forge (native adapters are a later seam).

## 3. Locked decisions

| Decision | Choice |
|---|---|
| Depth / deployment | Generic abstraction; managed sandboxes **and** omni host |
| Host topology | Operator-only (users cannot define/override) |
| User contribution | Credentials only, bound to an operator host |
| Operator credentials | Reference-source (env/file/command) |
| User credentials | Encrypted-at-rest (Fernet **key list**, keys in env) |
| Managed-clone transport | Explicit launcher secret-delivery capability; ephemeral into sandbox |
| Fetch/push credential path | **Server→runner authenticated handoff (built in P1)** |
| PR/issue | MCP-delegated + native-ready |
| Login/SSO | In scope |
| Shared-session push identity | Owner (A, default) or service bot (B, optional); per-user deferred |
| Shared-session commit author | **The session starter** (consistent) |
| Session sharing | Warn on git-credential exposure (grant + late-attach) |

## 4. Current state (grounded, verified)

- Clone URL is user-provided, never compared to github.com; `parse_repo_workspace`
  (`omnigent/server/managed_hosts.py:508-549`) validates shape only and does not canonicalize.
- It is a **pure parser without identity** — runs in Pydantic validation
  (`omnigent/server/schemas.py:1341-1386`) and relaunch (`omnigent/server/routes/sessions.py:7026-
  7059`); the create route re-parses with identity (`:14565-14625`).
- Clone differs by launcher: exec uses `materialize_workspace`
  (`omnigent/onboarding/sandboxes/base.py:320-389`) whose only channel is `self.run(sandbox_id,
  command: str)` (`:425-438`); **Kubernetes** clones in an **init-container** via
  `_render_workspace_prep_command` (`omnigent/onboarding/sandboxes/kubernetes.py:359-393`), placed in
  `initContainers` by `build_pod_manifest` (`:447-591`), Secret+Pod created in `start_host`
  (`:1013-1103`).
- The credential proxy is runner-only: `prepare_credential_proxy_runtime`
  (`omnigent/inner/credential_proxy.py:103-153`) resolves **locally** (env/file/command) and is
  consumed by `omnigent/inner/os_env.py:419-454`; it is *not* the pre-host clone.
- **Credentials today = one shared blob.** `GIT_TOKEN`/`GIT_USERNAME` are injected into *every*
  sandbox (`omnigent/server/managed_hosts.py:811-921`) and forwarded by `_build_runner_env`
  (`omnigent/host/connect.py:435-463,499-550`), exact-name, host-blind.
- **Commit identity is unmanaged** — no `user.name`/`user.email`/`GIT_AUTHOR` anywhere; commits get
  the image default. `GIT_USERNAME` is only the HTTPS auth username.
- **Per-turn sender is tracked**: every user turn carries `created_by`
  (`omnigent/runtime/pending_inputs.py:349`, applied at `omnigent/server/routes/sessions.py:5166`).
- **Sharing** = `SharingMode` (`omnigent/server/auth.py:77-102`, levels READ=1/EDIT=2/MANAGE=3/
  OWNER=4); effective level via `omnigent/server/permissions.py:91-126`; grant choke point
  `PUT /sessions/{id}/permissions` (`omnigent/server/routes/sessions.py:20782-20882`);
  `workspace_sharing_blocked` (`omnigent/server/auth.py:131-152`) is the existing shareability signal.
- Only real github.com couplings: `gh_basic` defaults + `api.` heuristic
  (`omnigent/spec/parser.py:1206-1209,1638-1685`); login OAuth constants (`omnigent/server/oidc.py:
  118-123`).

## 5. Architecture

```
   repo URL / host id                       (authenticated request)
          │
   pure parse (shape) ─► owner-aware RESOLVER (before durable create) ─► ClonePlan (non-secret)
   parse_repo_workspace   operator hosts ∪ user/bot creds                {provider, canonical host,
   (no identity)          (topology operator-only)                        normalized URL, cred slot id,
          │                          │                                    CA, known_hosts, identity}
   ┌──────┴──────────┬─────────────┬─┴──────────────┬────────────────────┬──────────────────┐
   ▼                 ▼             ▼                ▼                    ▼                  ▼
 clone (launcher    secret         fetch/push:     egress (provider     PR/issue: MCP     login/SSO
 secret-delivery    delivery:      server→runner   recommends rules;    + policy peer     (OIDC; GHE
 capability; k8s    ephemeral      handoff         merge = operator     + CLI             param; secret
 init Secret)       clone secret   (authenticated) authoritative)                          via env)
```

## 6. Provider model

`GitHostProvider(ABC)` — `provider`/`default_clone_username` ClassVars; `matches`,
`normalize_repo_url`, `clone_binding` (auth *shape*, not a secret), `egress_rules`,
`mcp_server_config` (P2), `cli_env`, `oauth_config` (P3), `api_client` (native-ready, default `None`),
`policy_peer`. Mirrors `SandboxLauncher(ABC)` (`omnigent/onboarding/sandboxes/base.py:152`). Registry
`_GIT_HOSTS` + `get_git_host()` + `available_providers()` (mirrors `sandboxes/__init__.py:54-132`);
closed set; github.com → built-in default. Onboarding collects **non-secret topology only** (LLM-style
`AuthField`/`AuthMode` schema, `omnigent/onboarding/providers/__init__.py:46-92`); **no `keychain:`
ref** (reversible-file fallback contradicts the model).

## 7. Host topology (operator-only)

Operators are the sole authority for host definitions; users cannot register/override routing. Stored
as operator YAML (`git_hosts:` key, `omnigent/server/server_config.py:58-79`) parsed by a **new typed,
fail-closed parser** into an immutable structured record set, mtime-refreshed (the *pattern* of
`omnigent/server/admin_list.py:83-157`, not `MtimeCachedIdentitySet`/`config_str_list`, which flatten
to lowercased tokens). Record: `{id, provider, web_host, api_base, ssh_host?, ssh_port?, ca_bundle?,
credential_source, egress_hints, oidc?}` — all non-secret. Hosts are validated lowercase IDNA + explicit
port, userinfo rejected; resolution is exact canonical-host match.

## 8. Credential, transport & identity model

### 8.1 Operator credentials — reference-source
`CredentialSourceSpec` (`omnigent/inner/datamodel.py:377-397`): env/file/command, resolved in the
trusted parent. `command` is operator-only.

### 8.2 User / bot credentials — encrypted at rest
`SqlGitCredential` (§12.2) holds `token_ciphertext` (Fernet; **key list** in env; decrypt-only in the
trusted parent; never in `SandboxPolicy`). A user/operator supplies only an opaque token bound to an
operator host — never a command or URL.

### 8.3 Authorization
The server-minted row **`id` is the opaque credential slot**. Routes **derive** owner, workspace,
canonical host, and provider from the authenticated identity + the typed operator host record and
**reject** any client-supplied values. Uniqueness `(workspace_id, owner, host_id)`. Enforcement at
**write and launch**; on launch the operator host must still exist and match before decrypt. No
user-authored `CredentialSourceSpec` ever reaches the `command` branch
(`omnigent/inner/credential_proxy.py:184-208`).

### 8.4 Managed-clone secret delivery
Secret delivery is an **explicit launcher capability** (e.g. `stage_clone_credential` /
`run_with_secret_env`), separate from the non-secret `ClonePlan`, guaranteeing non-logging, host
binding, and cleanup (incl. failure paths). The secret value never enters `RepoWorkspace`, `ClonePlan`,
shell strings, provider args/logs, or `SandboxPolicy`.
- **Exec:** a launcher primitive delivers the credential via a transient askpass/helper file or
  secret-env channel the provider supports — not via the `self.run` command string
  (`omnigent/onboarding/sandboxes/base.py:425-438`); removed immediately after clone.
- **Kubernetes:** a **distinct clone-only Secret** projected to the init container
  (`omnigent/onboarding/sandboxes/kubernetes.py:359-393,447-591`), **deleted as soon as init
  succeeds** (and on every failure path) — not carried to teardown.
- **Framing:** unless a provider actually mints/exchanges a token, this is **launch-scoped delivery of
  the existing credential**, not a "short-lived token" (a copied PAT stays long-lived).

### 8.5 Fetch/push handoff (built in P1) & long-lived hosts
The transient clone secret is gone after clone, so later fetch/push needs its own path.
- **Managed hosts:** a **second authenticated server→runner-parent credential handoff** delivers the
  resolved secret as an **in-memory handle over the existing authenticated host tunnel**
  (`omnigent/server/routes/host_tunnel.py`), consumed only by the trusted runner parent, which mints
  the credential-proxy placeholders/rewrite rules (`inject_env`) — **without** converting a user PAT
  into env/file/command policy or a real `GIT_TOKEN`. Specify TTL, reconnect/relaunch re-fetch,
  best-effort zeroization, and the trusted-boundary process.
- **Runner:** placeholder path only; do **not** add real provider tokens to
  `_BASE_HARNESS_CREDENTIAL_ENV_VARS` (`omnigent/host/connect.py:435-451`).
- **Long-lived `omni host`:** the server env is not the external host's env. External hosts use a
  **host-local per-git-host credential config** keyed to the server-provided canonical host identity;
  only **non-secret** provider/topology + CA/known-hosts data crosses the launch frame. `/v1/git-
  credentials` does **not** configure `omni host` (documented), unless an explicit opt-in secret-
  transfer is later added.

### 8.6 Identity model (shared sessions)
- **Push / write permission:** the **session owner's** credential (model A, default) or an optional
  per-workspace **service "bot"** credential (model B). Per-user push is **deferred** (fights the
  agent execution model and needs per-turn credential switching).
- **Commit authorship:** `user.name`/`user.email` set to the **session starter** (the owner) —
  consistent across the session — since omnigent does not manage commit identity today. omnigent sets
  it in the runner git environment (e.g. `GIT_AUTHOR_*`/`GIT_COMMITTER_*` or `git config`).
- **Enforcement (two-layer):** omnigent's session grant (`LEVEL_EDIT`+) gates *who can drive the
  agent*; the forge scopes / branch protection of the push identity (owner or bot) bound *what git
  actions are possible*. omnigent never re-implements forge ACLs.
- **Model B provisioning:** the bot credential is an operator reference-source (or encrypted) token
  bound to a workspace; attribution reads "authored by \<owner\>, pushed by \<bot\>."

### 8.7 Session-sharing warning
- Persist the **credential host IDs in scope** for the session (per launch generation) — do not
  compute from the owner's current global credential rows.
- **Warn/confirm** both when an edit/manage grant is created or upgraded (choke point `PUT /sessions/
  {id}/permissions`, `omnigent/server/routes/sessions.py:20782-20882`) **and** whenever credentials
  are newly attached or re-resolved for a session that already has such grants (closes the timing
  bypass).
- **Consent semantics:** define that exposing an owner's credential requires the credential owner's
  confirmation (a manager creating a grant is not the owner's consent) — e.g. only `LEVEL_OWNER`
  approves, or the owner's original attach explicitly authorizes manager re-sharing.
- **Headless/API/CLI:** an explicit `confirm_git_credential_sharing` field (not a synchronous UI
  assumption) so automation doesn't break or silently expose credentials.
- **Optional hardening:** under `RESTRICTED_READ_ONLY`, block credential propagation into shared
  sessions or require the grantee's own credential.

## 9. Clone / transport & resolver

- **Resolver before durable create:** keep `parse_repo_workspace` pure; add an owner-aware resolver
  that runs **before** the session/owner grant is persisted (the current create insertion point is
  after persistence, `omnigent/server/routes/sessions.py:14565-14625` — reorder, or make create+
  cleanup atomic on resolver failure). It produces an immutable `ClonePlan` {provider, canonical host,
  normalized URL, credential slot id, CA, known_hosts, identity}. Use `host.owner` (not the waker) for
  shared-session relaunch.
- **Relaunch (locked):** persist the **non-secret** host-config id/version + canonical URL + credential
  slot id; **re-authorize that slot for `host.owner` at every launch**. Topology/credential changes
  deliberately take effect on relaunch; tested.
- **Launcher-wide clone contract:** the `ClonePlan` + secret-delivery capability (§8.4) are honored by
  **both** exec and Kubernetes; SSH `known_hosts`, `GIT_SSL_CAINFO`/mounted CA, custom ports.

## 10. PR / issue / metadata (MCP-delegated + native-ready)

Provider supplies an `MCPServerConfig` via the neutral `ServerMcpPool`
(`omnigent/server/mcp_pool.py:146-240`) + a **policy peer** (`omnigent/policies/builtins/{gitea,
gitlab}.py`) — not a stretched `github.py`. CLI via `cli_env` (`gh`/`glab`/`tea`). **GHE same-host
(P2):** clone + API share one hostname, but the parser rejects two bindings per host
(`omnigent/spec/parser.py:1462-1478`) and the proxy allows one swap per host
(`omnigent/inner/egress/proxy.py:225-240`) — needs an explicit **multi-mode same-host rule**
(path/client-aware), not sibling `gh_basic` presets. `api_client()` native seam default `None`;
**Bitbucket** experimental until a native adapter (may precede P4).

## 11. Egress

Provider **recommends** `{web,git,api}` rule strings; it **must not** set the global
`egress_allow_private_destinations` (`omnigent/inner/datamodel.py:627-650`). **Merge point (named):**
a single component composes the effective `OSEnvSandboxSpec` from operator/server host policy +
agent-authored rules, with **immutable precedence** (operator authoritative) and **revalidation after
merge**; provider host/path rules are added **only for the resolved host**; the global private flag
comes **only** from operator/server policy. Tests prove an agent-authored spec cannot enable or widen
private access.

## 12. Data model & persistence

Portability verified: no FK (R032, `p1a2b3c4d5e6_remove_all_fks.py`), no partial index
(`z5a2b3c4d5e6_drop_partial_indexes`), split DBs (`OmnigentBase`, `omnigent/db/db_models.py:172`),
app-enforced schema.

### 12.1 Operator hosts → typed file config (§7). No DB.

### 12.2 `SqlGitCredential(OmnigentBase)`
Mirrors `SqlHost` (`omnigent/db/db_models.py:1063-1150`): composite PK `(workspace_id, id)` where
**`id` is the opaque credential slot** (`Uuid16`); `owner: String(256)`; `host_id`;
`provider: SmallInteger` via a stable `enum_codecs` map + `CheckConstraint`; `token_ciphertext: Text`
(Fernet); timestamps. `UniqueConstraint(workspace_id, owner, host_id)` — not a partial index.
`canonical_host`/`provider` are **validated denormalized snapshots** of operator topology, re-checked
at launch (topology can drift). **No FK**; app-cascade (`omnigent/stores/host_store.py:736-763`) **plus
a reconciliation sweeper** that also scrubs credentials orphaned by **operator-host removal from YAML**
(no DB event fires — the sweeper hooks the mtime refresh). New `GitCredentialStore` (methods keyed by
`(owner, host_id)`) + `GitCredential` dataclass + `create_git_credentials_router` at
`/v1/git-credentials`. Routes reject client-supplied owner/host/provider.

### 12.3 Resolution & precedence
Topology: operator-only → github.com default. Credential for a resolved host: in a session, the
**owner's** slot for that host (model A) or the workspace **bot** slot (model B), else the operator
host credential source, else legacy `GIT_TOKEN` (github.com). No user topology override.

## 13. Provider matrix

| Provider | Clone username | API base | CLI | MCP | SSO |
|---|---|---|---|---|---|
| github.com | `x-access-token` | api.github.com | `gh` | GitHub MCP | github branch (today) |
| GHE | `x-access-token` | `<host>/api/v3` (same host) | `gh`+`GH_HOST` | GitHub MCP | parameterize (P3) |
| Forgejo/Gitea | `<user>`/token | `<host>/api/v1` | `tea` | Gitea MCP | generic OIDC (claim-dep) |
| GitLab | `oauth2` | `<host>/api/v4` | `glab` | GitLab MCP | generic OIDC (claim-dep) |
| Bitbucket | `x-token-auth` | `/2.0` `/rest` | — | community MCP (exp.) | OIDC (varies) |

## 14. Phasing

### P1 — Foundation (closes #2125)
Provider ABC + registry; operator topology (typed parser); `SqlGitCredential` (slot-id, encrypted,
server-derived fields, sweeper incl. YAML-removal); owner-aware resolver → `ClonePlan` **before
durable create** (locked relaunch); **launcher secret-delivery capability** (exec + k8s distinct clone
Secret deleted post-init); **server→runner fetch/push handoff**; runner placeholders; commit-author =
session starter; **identity model A (owner)** + optional **B (service bot)**; egress derivation +
named merge point; **session-sharing warning** (grant + late-attach + owner consent + headless field);
Fernet key-list.
**Acceptance:** github.com + self-hosted Forgejo + GitLab clone/**fetch/push** simultaneously on exec
**and** Kubernetes managed sandboxes and omni host; shared session warns and attributes commits to the
starter. Live Forgejo/Gitea containers; sharing/handoff integration tests.

### P2 — PR/issue + tools
Per-provider MCP + policy peers + CLI; **GHE same-host multi-mode binding**; generalize `gh_basic`
API-host heuristic. **Acceptance:** agent opens a PR / reads an issue on Forgejo.

### P3 — SSO
Parameterize GHE OAuth (`oidc.py:118-123,309-332`, `auth.py:679-719`); generic-OIDC for GitLab/Forgejo
**conditional on `email`/`email_verified` claims** (no userinfo fallback, `auth.py:742-814`) — add
userinfo lookup or document; **OIDC client secret** via operator env.

### P4 — Breadth
GitLab / Bitbucket providers; native `api_client` where MCP is inadequate.

## 15. Cross-cutting

- **Security:** operator-only topology; encrypted user/bot creds (Fernet key-list), opaque slot,
  server-derived authority, decrypt parent-only, never in `SandboxPolicy`; clone secret launch-scoped +
  redacted + immediately deleted; fetch/push via authenticated in-memory handoff; two-layer enforcement;
  sharing warns on exposure incl. late attach; private egress operator-only with a validated merge point.
- **Portability:** `SqlGitCredential` obeys R032/no-partial-index/workspace-scoped; `SmallInteger`
  enum + `CheckConstraint`; `Uuid16` slot id.
- **Testing:** unit (registry, canonicalization, authorization at write+launch, resolver precedence,
  sharing signal, egress merge non-broadening); integration on **real Forgejo/Gitea/GitLab** for exec
  **and** k8s incl. fetch/push + the handoff + multi-host; SSO vs real GitLab OIDC; live `glab`/`tea`.
- **Backward compat:** github.com + single `GIT_TOKEN` unchanged.

## 16. Open questions

1. **Multi-tenant vs self-host default** for encrypted-PAT vs bot identity; Fernet key-list rotation
   runbook (decrypt-old/encrypt-new).
2. **Service-bot provisioning UX** (model B) — how operators register a workspace bot + its scopes.
3. **Managed-image dependencies** — `gh`/`glab`/`tea`, forge MCP servers, CA/`known_hosts` (image
   contract, outside `omnigent/`).
4. **GHE same-host multi-mode mechanism** — concrete path/client-aware rule design (P2).
5. **Broker milestone** — when to graduate the pre-host clone from ephemeral delivery to a
   never-in-sandbox broker.
