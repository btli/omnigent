# Custom git-host provider abstraction — design spec (v5)

**Status:** Draft for review (v5 — P1c-5a Kubernetes parity grounded: init-clone credential via a
distinct, init-scoped, delete-after-init per-Pod Secret [§8.4a]; the §8.5 fetch/push chain verified
provider-independent on k8s [no in-Pod swap layer to build]; P1c-5 split into 5a/5b/5c)
**Date:** 2026-07-16
**Branch:** `feat/custom-git-hosts` (worktree off `upstream/main` @ `0f6e82fb`)
**Anchor issue:** omnigent-ai#2125. **Related:** #1937, #1421, #236.

> This spec was developed through several rounds of multi-engine adversarial review; the
> phased plan (P1a/P1b landed here, P1c/P1d to follow) and review history live in the PR
> discussion rather than in-tree.

## 0. What changed in v5 (P1c-5a Kubernetes parity grounded)

P1c-5's Kubernetes leg was grounded against the code and the target cluster before design lock
(recon of `kubernetes.py` / `managed_hosts.py` / the host image, a Codex design review, and a live
bwrap smoke test on a restricted-PSA cluster):

- **The §8.5 fetch/push chain needs no k8s work.** v4 deferred "k8s in-Pod swap layer" as an open
  item; grounding resolved it: k8s is entrypoint-as-host, so the trusted host parent
  (`omnigent host`, the Pod's main container) and the runner's in-process egress proxy already run
  **in-Pod**. The sealed `deliver_credential` frame, the path-scoped swap, wake/relaunch re-delivery
  (P1c-4b), and every fail-closed gate are provider-independent and transfer verbatim. There is no
  swap layer to build — P1c-5a's **k8s-specific** runtime scope is the init-container clone, plus
  the two small **provider-neutral** fail-closed gates below (one server-side launch check, one
  runner-side consumption check).
- **Init-clone delivery is specified concretely (§8.4a)**, honoring the v3.1/v4 locked properties
  with **one explicit, gate-routed deviation** (no in-server reconciler — labels remain the sweep
  hook; §8.4a item 4, decision owner: project review):
  a **distinct clone-only per-Pod Secret** (never the token Secret, never literal Pod-spec env),
  projected **only** to the init container which **drops the shared `harness_secret` `envFrom`**
  (the clone step stops seeing the deployment's LLM credentials — least privilege), best-effort
  **`OwnerReference` to the Pod** (crash insurance), and **deleted as soon as init succeeds** plus
  on every failure path and defensively at terminate. The server seam is already provider-uniform
  (`_build_clone_env` → `start_host(clone_env=...)` on launch *and* relaunch), so the k8s launcher
  replaces its fail-closed `SandboxCapabilityError` with delivery — no server-route changes.
- **The image contract already fits:** the host image ships a git credential helper that reads
  `GIT_TOKEN`/`GIT_USERNAME` from env, exactly the pair `_build_clone_env` emits — no image change.
- **Two fail-closed gates added by review (both provider-neutral, both in this slice):**
  (1) **sandbox-inactive consumption gate** — the runner's managed-credential consumption lives
  inside `if sandbox.active:` (`omnigent/inner/os_env.py`), so with `sandbox.type: none` a
  delivered credential was silently ignored while the ambient operator `GIT_TOKEN` (forwarded via
  `HARNESS_CREDENTIAL_ENV_VARS`) kept flowing to git — the "silently the wrong identity" class.
  P1c-5a moves the delivery check ahead of the gate: delivery vars present + sandbox inactive ⇒
  `ManagedGitCredentialError` (same deterministic-misconfig surface as the no-egress-allowlist
  case). This also fixes the identical latent gap on exec. (2) **HTTPS-only launch gate** —
  `_build_clone_env` emits the HTTPS helper pair for **every** credentialed launch (a bound user
  slot *or* an operator `credential_source`), and an `ssh://`/`git@` URL cannot consume it — git
  would clone via ambient SSH identity, silently bypassing the selected credential. Until P1c-5b,
  **any launch that would deliver `clone_env` requires an HTTPS clone URL**; launch refuses
  otherwise (server chokepoint, all providers — on exec this is a bug fix, not a regression).
- **Cluster caveat recorded (out of scope):** on clusters that deny unprivileged user namespaces
  to restricted Pods (e.g. Debian 13's AppArmor default; smoke-tested 2026-07-16), the default
  `linux_bwrap` sandbox cannot start: bound sessions on such clusters fail **loud** (the helper
  cannot start; with `sandbox.type: none` they now fail closed via gate (1) above — never silently
  mis-credentialed). Restoring a working sandbox there (privileged pod, AppArmor sysctl, gVisor)
  — and any future "soft proxy" for inactive sandboxes — is a separate infra/hardening track.
- **P1c-5 split (§14):** P1c-5a k8s parity (this slice) → P1c-5b SSH ssh-agent → P1c-5c the tmux
  terminal swap decision.

## 0.1 What changed in v4 (P1c handoff architecture resolved)

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
  *transient* tunnel reconnect. Clone/fetch/push all ride the one swap *(v4 wording — refined in
  v5: the pre-runner initial clone rides §8.4/§8.4a delivery; see §8.5)*. (A is also a net
  improvement over today's ambient `GIT_TOKEN` env.)
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
  ssh-agent *(v4 roadmap — superseded in v5: no in-Pod proxy layer is needed; now split
  P1c-5a/5b/5c, see §0 and §14)* → P1c-6 commit identity + sharing notice. **k8s, the
  init-container clone, the tmux terminal, and SSH keys are explicitly separate coverage items**,
  not folded into P1c-4.

## 0.2 What changed in v3

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
- **Kubernetes:** a **distinct clone-only Secret** projected to the init container only; the init
  container **drops the shared `harness_secret` `envFrom`**; `OwnerReference` to the Pod; **deleted
  as soon as init succeeds**. Fully specified in **§8.4a** (P1c-5a, grounded v5).
- **Framing:** unless a provider actually mints/exchanges a token, this is **launch-scoped delivery of
  the existing credential**, not a "short-lived token" (a copied PAT stays long-lived).

### 8.4a Kubernetes init-clone delivery (P1c-5a, grounded v5)

**Grounding (verified in code, 2026-07-16).** The server seam is already provider-uniform:
`omnigent/server/managed_hosts.py` resolves the owner's slot (or the operator `credential_source`)
via `_build_clone_env(...)` → `{"GIT_TOKEN": ..., "GIT_USERNAME": ...}` and passes it to
`launcher.start_host(clone_env=...)` on **launch and relaunch alike** (managed wake respawns
through the same path). The k8s launcher (`omnigent/onboarding/sandboxes/kubernetes.py`) currently
**fails closed** with `SandboxCapabilityError` — so today a custom-host or per-user-credential
session on the k8s provider cannot launch at all. The host image ships a git credential helper
that answers `git credential get` from `GIT_TOKEN` / `GIT_USERNAME` env
(`deploy/docker/Dockerfile`, `.ubi`), exactly the pair `_build_clone_env` emits; the launch token
already rides a per-Pod Secret (`build_token_secret_manifest`) whose create/delete lifecycle covers
every failure path. P1c-5a replaces the rejection with delivery that reuses those two contracts.

**Mechanics.**

1. **A second, distinct per-Pod Secret** — `"{pod}-clone-cred"` — built by a new pure
   `build_clone_secret_manifest(*, secret_name, namespace, clone_env)`: `type: Opaque`,
   `stringData = clone_env`, the same managed-by/role GC labels as the token Secret. It is **not**
   merged into the token Secret: the clone credential's lifetime (delete after init) is deliberately
   shorter than the token's (delete at terminate), and a user PAT must not inherit a longer at-rest
   window for implementation convenience. Two further reasons the objects stay distinct: Secret
   cleanup and RBAC are **object-granular** (shortening a merged key's lifetime would mean patching
   individual keys), and an `envFrom` projection of a merged Secret would hand the **host launch
   token** to the clone step — the two keys have different consumer trust domains.
2. **Manifest projection is value-free.** `build_pod_manifest` gains `clone_secret_name: str | None`
   plus `clone_env_keys: Sequence[str] | None` (the Secret's *key names*, needed for the collision
   rule below — names only, never values, so the builder stays pure and its output
   audit-loggable). When set, the
   **init container's** `envFrom` becomes `[{secretRef: clone_secret_name}]`, **replacing** the
   shared `harness_secret` ref — the clone step sees only the git pair, not the deployment's LLM
   credentials, and the replacement removes any two-sources-of-`GIT_TOKEN` precedence question.
   Because operator vars a clone may need (`HTTPS_PROXY`, `GIT_SSL_CAINFO`, …) could previously
   only reach the init container via the harness Secret, the credential-bound manifest **also
   projects `env_literals`** (the non-secret, sensitivity-filtered `sandbox.kubernetes.env`
   passthrough the main container already receives) **into the init container** — the supported
   channel for proxy/CA config once the harness ref is dropped. **Collision rule:** keys named in
   `clone_env_keys` are **excluded** from the init-container projection — explicit `env` entries
   beat `envFrom` in Kubernetes, and the sensitivity filter blocks `GIT_TOKEN` from `env_literals`
   but not `GIT_USERNAME`, so an operator literal must never half-override the delivered
   credential pair (the clone Secret's keys are authoritative for the clone). The exclusion is
   **init-only**: the main container keeps *all* `env_literals` exactly as today, colliding names
   included. The **main container is untouched**
   (keeps the harness `envFrom`; never references the clone Secret in any form). When `None`, the
   manifest is **byte-identical to today** (ambient behavior preserved, no `env_literals` added).
3. **`start_host` order & lifecycle:** validate `clone_env` keys (env-name charset; reject a
   collision with the token key) → create token Secret → create clone Secret → create Pod
   (Secrets first, so `secretKeyRef`/`envFrom` resolve — a Pod referencing a missing Secret sits in
   `CreateContainerConfigError`, which the start wait treats as terminal) → **best-effort PATCH**
   the clone Secret with an `ownerReferences` entry for the created Pod → wait for `Running` (all
   init containers succeeded) → **delete the clone Secret immediately** (at-rest window ≈
   scheduling + init duration, not Pod lifetime). The ownerRef is precise about what it buys:
   Kubernetes GC is **deletion-triggered, not time-based** — it ties the Secret's lifetime to the
   Pod *object's*, so any eventual Pod deletion (terminate, operator cleanup, sweep) reaps the
   Secret even if the server never comes back; it does **not** expire a Secret while its Pod
   lingers. On RBAC/patch failure: warn and continue (the surrounding lifecycle still bounds the
   window).
4. **Failure paths, fail-closed inventory:** clone-Secret create failure → launch fails, Pod +
   token Secret torn down; Pod create / readiness failure → the existing best-effort teardown also
   deletes the clone Secret; init-clone failure (bad credential, unreachable host) → init exits
   non-zero → the start wait fast-fails with the git log tail and everything is reaped;
   delete-after-init transient failure → warn; `terminate()` gains a defensive 404-ok delete of
   the derivable clone Secret name, and that delete **must be attempted independently of earlier
   failures in the teardown sequence** (today's loop aborts on the first non-404 API error — a Pod
   delete failure must not skip the credential-bearing Secret's delete). **Crash orphans, stated
   precisely:** a crash before Pod create, or between Pod create and the PATCH landing, leaves a
   labeled, unowned Secret covered only by the operator GC sweep posture the token Secret already
   has (no in-server reconciler is added in this slice — the labels are the sweep hook); a crash
   after the PATCH leaves the Secret until *whatever* eventually deletes the Pod. ownerRef +
   delete-after-init + failure-path deletes + independent terminate delete are the four in-code
   mechanisms.
5. **Values never leave the Secret body — with redaction as backstop.** The credential appears
   only in the clone-Secret create call: never in the Pod manifest, `click.echo` progress lines,
   or launcher-composed messages. Because some error surfaces embed **attacker- or
   cluster-influenceable text** (apiserver/admission-webhook response bodies in API-error
   formatting; the init container's git log tail, which a hostile remote can seed via pkt-line
   `remote:` messages), the k8s path adopts the same **mandatory redaction** obligation as the
   §8.4 exec interim channel: every error/warning/log string the launcher emits on the clone path
   is scrubbed of the credential values before it propagates (helper-based auth already keeps the
   token off argv/URL; this closes the reflection channels).
6. **HTTPS-only launch gate (until P1c-5b).** `_build_clone_env` emits the HTTPS helper pair for
   **every credentialed launch** — a bound user slot *or* an operator `credential_source` — but an
   `ssh://`/`git@` clone URL cannot consume it (git ignores the helper for SSH transport), and the
   §8.5 rewrite rule is HTTP(S)-only, so an SSH-URL session would clone/push under the **ambient
   SSH identity**, silently bypassing the selected credential in either case. Today k8s masks this
   by rejecting all `clone_env`; removing the rejection must not inherit the bypass: **any launch
   that would deliver `clone_env` requires an HTTPS clone URL** — refused at the server chokepoint
   (candidate: `_build_clone_env`, which sees the slot, the operator source, and the URL), for
   **all providers** (on exec this is a bug fix — the same silent bypass exists there today), with
   a distinct fail-closed error.
7. **Sandbox-inactive consumption gate (runner-side, provider-neutral).** The runner consumes a
   delivered credential only inside `if sandbox.active:` (`omnigent/inner/os_env.py`); with
   `sandbox.type: none` the delivery was silently ignored (stripped, unvalidated, no proxy, no
   error) while the ambient `GIT_TOKEN` kept flowing to git. P1c-5a hoists the delivery *check*
   ahead of that gate: delivery env present + sandbox inactive ⇒ `ManagedGitCredentialError`
   (the existing deterministic-misconfig surface). A bound session on a no-sandbox runner fails
   closed instead of silently running git as the operator. (A "soft proxy" for inactive sandboxes
   — consuming the credential without bwrap — is a possible later hardening, out of scope here.)

**Ambient interaction (documented).** Sessions with **no** bound slot and **no** operator
`credential_source` are byte-identical to today (init container keeps the harness `envFrom`,
ambient `GIT_TOKEN` clones). The deployment's shared harness Secret continues to flow to runners
via `HARNESS_CREDENTIAL_ENV_VARS` in the **main** container — pre-existing, operator-controlled;
the per-user token never joins it (it exists only in the init container's env and the §8.5
runner-proxy swap). Residual, pre-existing, **non-bound sessions only**: where ambient
`GIT_TOKEN` is visible to git (the init clone without a bound slot; inactive-sandbox runners),
git's 401 fallback consults the image helper with the *ambient* token — cross-host bleed under
operator control. On the **launch and runner-proxy git paths**, **bound-slot** sessions cannot
fall back to ambient in the default configuration, by four verified mechanisms: the P1c-4 launch
gate, the HTTPS-only gate (item 6), the sandbox-inactive gate (item 7), and the sandbox env
passthrough itself — `GIT_TOKEN` is not in the default allowlist, so in-sandbox git has nothing
ambient to offer and its first tokenless request receives the per-user injection. Two deliberate
exceptions, stated rather than implied: (a) the proxy **never clobbers a client-set,
non-synthetic `Authorization` header** (a P1c-4 lock — it must not destroy an unrelated
credential a tool deliberately sent), so naming `GIT_TOKEN` in `sandbox.env_passthrough`
re-opens an ambient path — and that field is **workspace-spec-author-declared, not
operator-only** (admin policy can cap it; the runner-auth strip already blocks re-admitting the
*managed* token this way, but the *ambient* token is not in that strip set) — a pre-existing
ambient-token property this slice does not change; (b) **operator-
`credential_source` sessions** receive §8.4/§8.4a *clone* delivery but no §8.5 *runner* delivery
(that frame is gated on a bound slot), so their post-clone fetch/push behavior is unchanged,
pre-existing scope — extending §8.5 delivery to operator sources is a named later item, not
silently claimed here. The **tmux interactive terminal** remains the third exception — it
inherits ambient `GIT_TOKEN` (while the managed token is stripped), so terminal-typed git can
still authenticate ambiently; that surface is exactly the deliberate P1c-5c decision.

**Deploy/RBAC.** The server SA already needs create/delete on `secrets` (token Secret). New:
`patch` on `secrets` for the ownerRef, with graceful degradation when absent — a chart
(`omnigent-server-kubernetes`) values note, called out in the PR.

**Testing.** Unit: clone-Secret manifest (labels/type/stringData); Pod manifest `envFrom` swap +
init `env_literals` projection + "main container never references the clone Secret name"
(serialize-and-scan) + `None` ⇒ regression-identical; invalid `clone_env` keys and
token-key-collision rejection; `start_host` create order; ownerRef PATCH payload carries the
created Pod's UID + PATCH-denied warns-and-continues; delete-after-Running, and its failure
warns without failing the launch; both failure-path deletes (incl. clone-Secret-create failure
after the token Secret exists); zero clone-Secret API calls when `clone_env` is absent;
credential value absent from every non-Secret API body, echoed line, raised exception, and
redacted from API-error/log-tail compositions; `terminate` deletes both 404-ok **and still
deletes the clone Secret when the Pod delete raises**; the server-side HTTPS-only gate refuses a
non-HTTPS URL for **both** a bound slot and an operator `credential_source` (all providers); the
runner-side sandbox-inactive gate raises `ManagedGitCredentialError` when delivery env is present
without an active sandbox; the init `env_literals` projection **excludes `clone_env_keys`**
while the **main container retains the colliding literal unchanged** (an operator `GIT_USERNAME`
literal must not override the delivered pair in init, and must not vanish from main).
Live (per §15): a private Forgejo repo + per-user slot on a real cluster — observe the clone
Secret create→delete, then fetch/push through the delivered §8.5 rule; an ambient-only session as
regression.

**Out of scope for P1c-5a:** SSH remotes (P1c-5b — the launch gate above refuses them for any
credentialed launch, bound slot or operator source, until then), the tmux terminal swap decision
(P1c-5c), extending §8.5 runner delivery to operator-`credential_source` sessions (pre-existing
scope; a named later item), the §8.4-conformant exec askpass channel (separate named obligation),
cluster userns/bwrap hardening and any inactive-sandbox "soft proxy" (infra/hardening track),
PVC-backed workspace persistence, and the k8s provider's pre-existing runner-exit diagnostics
pointing at host-local log paths (a provider-wide UX wart unrelated to credentials).

### 8.5 Fetch/push handoff (built in P1) & long-lived hosts
The transient clone secret is gone after clone, so later fetch/push needs its own path. **Grounded
resolution (v4, architecture A):** for sessions with a **bound user credential slot**, the
credential is delivered **once per runner launch** into the trusted runner-parent's in-process
**egress-proxy**, which swaps it onto the git-over-HTTPS **upstream** leg. (Operator-
`credential_source` sessions receive §8.4/§8.4a *clone* delivery only — this runner-delivery
frame is slot-gated; extending it to operator sources is a named later item, see §8.4a.) The sandbox child is a separate OS process that only ever emits **tokenless** traffic through the
proxy — it never holds the secret. Fetch/push — and any git op the runner performs after spawn —
ride this one swap; the **pre-runner initial clone** rides §8.4/§8.4a launch-scoped delivery
instead (it predates the runner on every provider). There is no per-git-operation hook and no
post-spawn rotation channel in the runtime; the earlier "single-use, per-operation, TTL" framing
is **reconciled to launch/runner granularity** (see §0.1 v4).

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
- **SSH-key credentials** cannot ride the HTTP rewrite proxy — a **separate coverage item (P1c-5b)**: an
  **ephemeral `ssh-agent`** in the runner parent with `SSH_AUTH_SOCK` exposed to the sandbox for the
  operation (never the real key; torn down after). The type-tagged envelope already carries `ssh-key`.
- **Runner:** placeholder path only; do **not** add real provider tokens to
  `_BASE_HARNESS_CREDENTIAL_ENV_VARS` (`omnigent/host/connect.py:435-451`).
- **Egress coupling:** the swap is a **no-op without an egress rule** for the canonical host; launching
  a managed-git session must **auto-merge that host's egress rule** at the §11 merge point, or
  push/fetch silently goes out tokenless.
- **Coverage status (updated v5):** **k8s fetch/push is NOT a separate layer** — grounding confirmed
  the entrypoint-as-host Pod runs the same trusted host parent + in-runner egress proxy in-Pod, so
  this §8.5 chain (sealing, ACK, path-scoping, wake/relaunch re-delivery, fail-closed gates)
  transfers verbatim with no k8s-specific code. The **k8s init-container clone** (the *first* git
  op, predating the runner) is covered by **§8.4a (P1c-5a)**. Still deferred to their own slices:
  the **tmux terminal** swap (a new auth surface — deliberate decision required, P1c-5c) and
  **SSH** as above (P1c-5b).
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

**P1 credential/handoff slices (P1c), grounded + sequenced (v4; split refined in v5):**
- **P1a/P1b** *(done)* — provider ABC/registry/resolver + operator-credential clone wiring (PR #2708).
- **P1c-1** *(done)* — encrypted-at-rest user credentials (`SqlGitCredential` + store + `/v1/git-credentials`), 0..n labeled identities per `(user, host)`.
- **P1c-2** *(done)* — owner/host-scoped `resolve_token` (opaque id ≠ authorization).
- **P1c-3** — owner-aware resolver + `RepoWorkspace`/`ClonePlan` field-widening + relaunch **binding persistence** (host-config id/version + canonical URL + slot id) + **add `launch_generation`** (the anti-replay anchor P1c-4 needs) + the `kind` column.
- **P1c-4** — the sealed, ACKed, type-tagged **`deliver_credential`** frame (§8.5) + host-parent egress-proxy install with a **repo-path-scoped** rule, on **exec (bwrap/seatbelt)** sandboxes; `invalidate_credential` in the contract; kill/relaunch revocation. HTTPS-token only.
- **P1c-4b** *(done)* — wake/relaunch credential **re-delivery** (re-authorize the binding, fail closed, distinct `credential_delivery_failed` classification).
- **P1c-5a** — **k8s parity** (§8.4a): accept `clone_env` in the k8s launcher via the distinct init-scoped, delete-after-init per-Pod Secret; the two provider-neutral fail-closed gates (HTTPS-only credentialed URLs; sandbox-inactive consumption refusal); verify the §8.5 chain in-Pod (no new swap layer); live-validate on a real cluster.
- **P1c-5b** — **SSH** ssh-agent path (ephemeral agent in the runner parent, `SSH_AUTH_SOCK` to the sandbox; `ssh-key` frame kind).
- **P1c-5c** — the **tmux terminal** swap decision (deliberate new-auth-surface call, not an accident of 5a/5b).
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
