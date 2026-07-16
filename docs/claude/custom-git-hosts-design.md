# Custom git-host provider abstraction — design spec (v4)

**Status:** Draft for review (v4 — P1c handoff architecture resolved: launch-scoped secretless swap
[architecture A]; sealed, ACKed, type-tagged `deliver_credential` frame; repo-path-scoped rule +
kill/relaunch revocation; PAT-first with a `kind` discriminator, OAuth deferred to P3)
**Date:** 2026-07-16
**Branch:** `feat/custom-git-hosts` (worktree off `upstream/main` @ `0f6e82fb`)
**Anchor issue:** omnigent-ai#2125. **Related:** #1937, #1421, #236.

> This spec was developed through several rounds of multi-engine adversarial review; the
> phased plan (P1a/P1b landed here, P1c/P1d to follow) and review history live in the PR
> discussion rather than in-tree.

## 0. What changed in v4 (P1c handoff architecture resolved)

The fetch/push handoff (§8.5) was grounded against the code (a 6-subsystem recon) and re-decided
through a diverse-lens + multi-engine review. The locked v3.1 "single-use, per-operation, TTL"
protocol did not match how the runtime actually works, and is **amended**:

- **Delivery is architecture A — a launch/runner-scoped secretless swap, not a per-operation handoff.**
  The real secret already has a home: the trusted runner-parent's in-process **egress-proxy**, which
  swaps the credential onto the git-over-HTTPS **upstream** leg (the sandbox child is a separate OS
  process and never holds it). There is **no per-git-operation interception point** and **no post-spawn
  rotation channel** in the code; the swap is spawn-time. Per-op single-use (B) would require building
  an egress→server callback that does not exist, for **no blast-radius gain** against the real threats
  (a compromised *runner* is itself the per-op requester, so per-op does not contain it). §8.5's
  "single-use/TTL/re-fetch/discard-on-tunnel-loss" is **reconciled to launch/runner granularity**:
  single-delivery-per-launch, discard on runner exit, re-authorize on relaunch, kept across a
  *transient* tunnel reconnect. Clone/fetch/push all ride the one swap. (A is also a net improvement
  over today's ambient `GIT_TOKEN` env.)
- **The credential rides a dedicated `deliver_credential` frame** (§8.5) — separate from the launch
  frame, **ACKed** (git is gated until the rule is installed, closing the spawn-race), **type-tagged**
  `{http-token | ssh-key | oauth}` and keyed by `{credential_slot, canonical_host}` (multi-host = N
  rules), bound to `runner_id` **plus a new monotonic `launch_generation`** as the anti-replay anchor.
  A paired **`invalidate_credential`** frame is defined in the contract now (push revocation ships
  later).
- **The frame is sealed** — the credential field is encrypted to a runner-held key established at
  launch, so confidentiality does **not** depend on deployment TLS (the tunnel can be `ws://` on
  loopback). Sealing is **pluggable** so `binding_token` can adopt it later.
- **PAT-first with a `kind: pat | oauth` discriminator** (§8.2, §12.2). The resolver returns a uniform
  **credential lease** (bearer token + optional `expires_at`); the egress-proxy never branches on
  `kind`. **OAuth is deferred to P3** as an extension of the SSO/OIDC work (access token minted at
  helper spawn + pre-expiry helper restart; no rotation channel) — a user-pasted PAT is
  zero-operator-setup and long-lived, so it sidesteps rotation entirely.
- **P1-achievable hardenings applied now:** the rewrite rule is **scoped to the repo-path prefix**
  (limits the host-scoped confused-deputy surface — hooks/submodule/LFS/terminal) and revocation is
  **kill+relaunch**. The residual is **accepted and documented**: a user PAT sits in trusted runner
  memory for the launch (a user PAT cannot be narrowed server-side; reduced later by OAuth-minted
  short-lived tokens).
- **Handoff decomposed** (§14): P1c-2 `resolve_token` owner/host-scoping *(done)* → P1c-3 resolver
  widening + relaunch binding persistence + **add `launch_generation`** → P1c-4 the sealed
  `deliver_credential` frame + host-parent swap + repo-scoped rule → P1c-5 k8s in-Pod proxy + SSH
  ssh-agent → P1c-6 commit identity + sharing notice. **k8s, the init-container clone, the tmux
  terminal, and SSH keys are explicitly separate coverage items**, not folded into P1c-4.

## 0.1 What changed in v3

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
- **Round-3 locks (v3.1):** the fetch/push handoff is a concrete confidential, fully-bound, single-use
  protocol (+ ssh-agent for SSH keys); the k8s clone Secret is isolated (drops the shared `envFrom`,
  `OwnerReference` + reconciler); the push credential is bound to the **exact repo**; and shared-session
  push is **warn-and-allow** — owner authority + a non-blocking notice, so the agent is never left
  unable to commit. Stricter teams use model B (a scoped bot).

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
| Shared-session push authority | Warn-and-allow (owner authority; non-blocking notice); exact-repo binding; model B for stricter isolation |
| Session sharing | Non-blocking notice on git-credential exposure (grant + late-attach) |

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
operator host — never a command or URL. A **`kind: pat | oauth`** discriminator (default `pat`) records
the credential type; the resolver normalizes both into a uniform **credential lease** (bearer token +
optional `expires_at`) so the egress-proxy never branches on `kind`. **P1 ships `pat` only** (zero
operator setup, long-lived → no rotation needed); **`oauth` is a P3 extension** of the SSO/OIDC work
(refresh-token grant → access token minted at helper spawn + pre-expiry helper restart; no rotation
channel).

### 8.3 Authorization & cardinality
The server-minted row **`id` is the opaque credential slot**. Routes **derive** owner, workspace,
and provider from the authenticated identity + the typed operator host record and **reject** any
client-supplied authority values.

**Cardinality (0..n user → identity):** any number of users each hold their own credentials; a user
may hold **multiple labeled identities on the same host** (e.g. a personal and a work account on
`git.acme.com`). A user-supplied **`label`** disambiguates them, so uniqueness is
`(workspace_id, owner_user_id, host_id, label)`. The row `id` remains the opaque selector the
handoff resolves by. **Selection policy** — which of a user's labeled identities a given
session/repo uses — is a **P1c-2 concern** (the handoff records the chosen slot per session);
P1c-1 only provides disambiguated storage + resolution-by-id + per-(owner,host) enumeration.

Enforcement at **write and launch**; on launch the operator host must still exist and match before
decrypt. No user-authored `CredentialSourceSpec` ever reaches the `command` branch
(`omnigent/inner/credential_proxy.py:184-208`).

### 8.4 Managed-clone secret delivery
Secret delivery is an **explicit launcher capability** (e.g. `stage_clone_credential` /
`run_with_secret_env`), separate from the non-secret `ClonePlan`, guaranteeing non-logging, host
binding, and cleanup (incl. failure paths); providers **fail closed** when the capability is
unavailable. The secret value never enters `RepoWorkspace`, `ClonePlan`, shell strings, provider
args/logs, or `SandboxPolicy`.
- **Exec:** a launcher primitive delivers the credential via a transient askpass/helper file or
  secret-env channel the provider supports — not via the `self.run` command string
  (`omnigent/onboarding/sandboxes/base.py:425-438`); removed immediately after clone.
- **Interim exec mechanism (P1b, recorded deviation):** until the secret-delivery launcher
  capability lands, the exec model delivers the credential as a shlex-quoted env prefix on the
  single clone command, with **mandatory redaction** of the values from any failure message
  before it reaches logs/SSE/error bodies. Residual: the token rides the provider exec API and
  the sandbox process table during the clone. The §8.4-conformant channel (askpass/secret-env
  primitive) is a named P1c task alongside the k8s init-container Secret.
- **Kubernetes:** a **distinct clone-only Secret** projected to the init container
  (`omnigent/onboarding/sandboxes/kubernetes.py:359-393,447-591`). The init container **must drop the
  shared `harness_secret` `envFrom`** (else it still sees every credential) and receive **only** the
  per-clone Secret; the main container never receives it. Created with an **`OwnerReference` to the
  Pod** (GC reaps it if the server crashes) plus a label-based reconciler, and **deleted as soon as
  init succeeds** (and on every failure path) — not carried to teardown.
- **Framing:** unless a provider actually mints/exchanges a token, this is **launch-scoped delivery of
  the existing credential**, not a "short-lived token" (a copied PAT stays long-lived).

### 8.5 Fetch/push handoff (built in P1) & long-lived hosts
The transient clone secret is gone after clone, so later fetch/push needs its own path. **Grounded
resolution (v4, architecture A):** the credential is delivered **once per runner launch** into the
trusted runner-parent's in-process **egress-proxy**, which swaps it onto the git-over-HTTPS **upstream**
leg. The sandbox child is a separate OS process that only ever emits **tokenless** traffic through the
proxy — it never holds the secret. Clone/fetch/push all ride this one swap. There is no
per-git-operation hook and no post-spawn rotation channel in the runtime; the earlier "single-use,
per-operation, TTL" framing is **reconciled to launch/runner granularity** (see §0 v4).

- **Managed hosts — the `deliver_credential` protocol (locked, v4):** a **dedicated versioned frame**
  over the existing host tunnel (`omnigent/server/routes/host_tunnel.py`), consumed only by the trusted
  runner parent, which installs a **repo-path-scoped** credential-rewrite rule in its egress-proxy
  (minting the proxy placeholder/rewrite) — **without** converting a user PAT into env/file/command
  policy or a real `GIT_TOKEN`. The frame is:
  - **Separate from the launch frame** — re-deliverable on relaunch/host-rescope without re-launching,
    and keeps the higher-value PAT off the launch frame's logging/replay surface;
  - **ACKed RPC, not fire-and-forget** — the runner confirms the rule is installed **before git is
    permitted**, closing the spawn-time race where a git op precedes the rule; the ACK drives
    single-delivery accounting;
  - **type-tagged** `{http-token | ssh-key | oauth}` and **keyed by `{credential_slot,
    canonical_host}`** so multiple/mixed-type credentials coexist (multi-host = N rules) and SSH/OAuth
    slot in without a frame redesign;
  - **bound to `{host_id, runner_id, launch_generation, session_id, credential_slot, canonical_host,
    repo_path}`** — `launch_generation` (added in P1c-3) is the monotonic **anti-replay anchor**;
    `runner_id` alone is insufficient (it can recur across relaunches / span OAuth helper-restart
    incarnations);
  - **sealed** — the credential field is encrypted to a **runner-held key established at launch**, so
    confidentiality does not depend on the tunnel's (deployment-provided, possibly `ws://` loopback)
    TLS; sealing is **pluggable** so `binding_token` can adopt it later. Secret-bearing frame bodies
    never enter logs/telemetry; zeroization is best-effort;
  - **launch-scoped lifecycle:** single-delivery-per-launch; **kept across a transient tunnel
    reconnect** (wiping would break in-flight git); **re-authorized + re-delivered on relaunch**;
    discarded on runner exit/stop/timeout. A paired **`invalidate_credential`** frame is defined in the
    contract now for server-driven revocation (push-revoke ships later); until then **revocation is
    kill+relaunch**.
- **Repo-path scoping (v4):** the rewrite rule matches the **specific repo-path prefix**, not the whole
  host, limiting the confused-deputy surface (malicious git hooks, submodule/LFS fetches, poisoned
  build steps, the interactive terminal) a host-wide swap would authenticate.
- **Accepted residual:** a user-pasted PAT is long-lived and cannot be narrowed server-side, so it sits
  in trusted runner-parent memory for the launch (core-dump/swap/ptrace exposure under trusted-parent
  compromise). Documented and accepted for P1; reduced in P3 when OAuth/App tokens allow a short-TTL,
  server-minted, repo-scoped credential to sit at rest instead.
- **SSH-key credentials** cannot ride the HTTP rewrite proxy — a **separate coverage item (P1c-5)**: an
  **ephemeral `ssh-agent`** in the runner parent with `SSH_AUTH_SOCK` exposed to the sandbox for the
  operation (never the real key; torn down after). The type-tagged envelope already carries `ssh-key`.
- **Runner:** placeholder path only; do **not** add real provider tokens to
  `_BASE_HARNESS_CREDENTIAL_ENV_VARS` (`omnigent/host/connect.py:435-451`).
- **Egress coupling:** the swap is a **no-op without an egress rule** for the canonical host; launching
  a managed-git session must **auto-merge that host's egress rule** at the §11 merge point, or
  push/fetch silently goes out tokenless.
- **Coverage explicitly deferred to separate slices (not folded into P1c-4):** **k8s** (no parent-side
  proxy — the same runner→helper→bwrap tree runs in-Pod, so the swap layer must run in the Pod) and the
  **init-container clone** (the *first* git op, predating the runner — a projected Secret / short-lived
  clone token, §8.4); the **tmux terminal** swap (a new auth surface — deliberate decision required);
  and **SSH** as above.
- **Long-lived `omni host`:** the server env is not the external host's env. External hosts use a
  **host-local per-git-host credential config** keyed to the server-provided canonical host identity
  (name the schema + lookup-failure behavior); only **non-secret** provider/topology + CA/known-hosts
  data crosses the launch frame. `/v1/git-credentials` does **not** configure `omni host`.

### 8.6 Identity model (shared sessions)
- **Push / write permission:** the **session owner's** credential (model A, default) or an optional
  per-workspace **service "bot"** credential (model B). Per-user push is **deferred**. An EDIT grantee
  driving the agent pushes **with that identity's full forge authority** (accepted — see the risk note).
  The push credential is **bound to the exact resolved repository** (never host-wide), so a shared
  session cannot reach other repos on the same forge.
- **Commit authorship:** `user.name`/`user.email` set to the **session starter** — consistent across
  the session (omnigent does not manage commit identity today). Set runner-scoped `GIT_AUTHOR_*` **and**
  `GIT_COMMITTER_*` (not persistent repo config on a long-lived host); snapshot the starter's validated
  name/email at session create with a deterministic no-email fallback. This is a **best-effort metadata
  default** (workspace code can override `--author`); the authoritative record of who drove each turn is
  the server's `created_by`/grant **audit trail**, not git metadata.
- **Enforcement (two-layer):** omnigent's session grant (`LEVEL_EDIT`+) gates *who can drive the
  agent*; the forge scopes / branch protection of the push identity bound *what git actions are
  possible*. omnigent never re-implements forge ACLs.
- **Accepted risk — owner-authority delegation:** in a shared session an EDIT grantee can direct the
  agent to push as the owner/bot, including bypassing protected branches if that identity can. This is
  **accepted deliberately**: a hard delegation gate would leave the agent unable to commit/push,
  breaking the workflow. Mitigations: the §8.7 warning, forge-side scopes/branch protection, exact-repo
  credential binding, and the audit trail. Teams needing stricter isolation choose model **B** (a
  least-privilege bot that cannot bypass protection) or disable push.
- **Model B provisioning:** the bot credential is an operator reference-source (or encrypted) token
  bound to a workspace; attribution reads "authored by \<starter\>, pushed by \<bot\>."

### 8.7 Session-sharing warning (non-blocking notice)
Per the accepted-risk decision (§8.6), this is an **informational warning, not a blocking gate** — it
must never leave the agent unable to commit.
- Persist the **credential host IDs in scope** for the session (per launch generation) — do not
  compute from the owner's current global credential rows.
- **Notify the credential owner** both when an edit/manage grant is created or upgraded (choke point
  `PUT /sessions/{id}/permissions`, `omnigent/server/routes/sessions.py:20782-20882`) **and** whenever
  credentials are newly attached or re-resolved for a session that already has such grants (closes the
  timing bypass). Surface an **exposure fingerprint** — push identity (owner/bot), canonical host +
  repo, grantee set, grant level — so the owner sees exactly what is exposed.
- **Non-blocking + acknowledgement:** the notice is surfaced to the owner and one-time-acknowledgeable;
  it does not hard-block execution. A manager creating a grant triggers the notice **to the credential
  owner** (a manager is not the owner).
- **Headless/API/CLI:** a `git_credential_sharing_notice` in the response + an optional `ack` field, so
  automation is informed rather than broken.
- **Stricter opt-in:** teams wanting a hard gate use model **B**, or under `RESTRICTED_READ_ONLY` block
  credential propagation into shared sessions / require the grantee's own credential.

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
**`id` is the opaque credential slot** (`Uuid16`); `owner_user_id: String(256)`; `host_id: String(256)`
(the operator host config id); `provider: String(32)` (validated denormalized snapshot, mirroring
`SqlHost.sandbox_provider`); `label: String(128)` (user-chosen, distinguishes multiple identities on
one host); `username: String(256) | None`; `token_ciphertext: Text` (Fernet);
`kind: SmallInteger` (enum `pat`|`oauth` via the codec, default `pat`, `CheckConstraint`; §8.2);
timestamps. `UniqueConstraint(workspace_id, owner_user_id, host_id, label)` — not a partial index.
Multiple labeled rows per `(owner, host)` are the 0..n-identity model (§8.3). (`kind` lands with the
P1c-3 resolver work; P1c-1 shipped without it and defaults existing rows to `pat`.)
`canonical_host`/`provider` are **validated denormalized snapshots** of operator topology, re-checked
at launch (topology can drift). **No FK**; app-cascade (`omnigent/stores/host_store.py:736-763`) **plus
a reconciliation sweeper** that also scrubs credentials orphaned by **operator-host removal from YAML**
(no DB event fires — the sweeper hooks the mtime refresh). New `GitCredentialStore` (methods keyed by
`(owner, host_id)`) + `GitCredential` dataclass + `create_git_credentials_router` at
`/v1/git-credentials`. Routes reject client-supplied owner/host/provider.

### 12.3 Resolution & precedence
Topology: operator-only → github.com default. Credential for a resolved host: in a session, the
**owner's** slot for that host (model A) or the workspace **bot** slot (model B), else the operator
host credential source, else legacy `GIT_TOKEN` (github.com). No user topology override. Resolution is
owner/host-scoped (§8.3, P1c-2) and returns a **credential lease** — bearer token + optional
`expires_at`, uniform across `kind` (§8.2) — never the raw row.

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

**P1 credential/handoff slices (P1c), grounded + sequenced (v4):**
- **P1a/P1b** *(done)* — provider ABC/registry/resolver + operator-credential clone wiring (PR #2708).
- **P1c-1** *(done)* — encrypted-at-rest user credentials (`SqlGitCredential` + store + `/v1/git-credentials`), 0..n labeled identities per `(user, host)`.
- **P1c-2** *(done)* — owner/host-scoped `resolve_token` (opaque id ≠ authorization).
- **P1c-3** — owner-aware resolver + `RepoWorkspace`/`ClonePlan` field-widening + relaunch **binding persistence** (host-config id/version + canonical URL + slot id) + **add `launch_generation`** (the anti-replay anchor P1c-4 needs) + the `kind` column.
- **P1c-4** — the sealed, ACKed, type-tagged **`deliver_credential`** frame (§8.5) + host-parent egress-proxy install with a **repo-path-scoped** rule, on **exec (bwrap/seatbelt)** sandboxes; `invalidate_credential` in the contract; kill/relaunch revocation. HTTPS-token only.
- **P1c-5** — **k8s** in-Pod swap layer + init-container clone credential; **SSH** ssh-agent path; the **tmux terminal** swap decision.
- **P1c-6** — commit identity (session starter, §8.6) + the session-sharing notice (§8.7).

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
