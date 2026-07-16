# Focused v2 re-review: custom git hosts

Reviewed v2 against the prior Codex findings and the requested changed-area anchors. This is a design-correctness review only; no source changes were made.

## Verdict

V2 materially improves the design. It resolves the user-selected-secret authorization blocker, correctly separates parsing from authenticated resolution, recognizes both managed clone execution models, accurately scopes the credential proxy to the inner runner, and no longer overclaims GHE same-host support.

It is still not implementation-ready for P1. The new managed-clone contract states the right trust boundary but does not define a secret-safe exec-launcher transport that the current `SandboxLauncher.run(command: str)` API can implement. More importantly, the transient clone credential is removed after clone while the later managed runner credential proxy has no defined way to receive the server-held user credential for fetch/push. Those are acceptance-critical gaps.

## Findings

### 1. BLOCKER — §8.4/§9, the exec managed-clone transport is still not a complete launcher contract

The Kubernetes claim is grounded: `_render_workspace_prep_command` really performs `git clone` in the init container (`kubernetes.py:359-393`), `build_pod_manifest` places that command in `initContainers` (`:447-591`), and `start_host` creates the Secret and Pod (`:1013-1103`). Adding a clone-only Secret/env or volume projection to that manifest is feasible.

The exec path is not equally specified. `materialize_workspace` invokes only `self.run(sandbox_id, command)` (`base.py:320-389`), and the launcher abstraction accepts a shell-command string with no stdin, secret-env, or secret-file channel (`base.py:425-438`). Putting the decrypted token in an askpass/helper file does not explain how it crosses the provider boundary without first appearing in a command string, provider audit log, process arguments, or ordinary environment. Different exec providers have different secret primitives, and managed-only launchers are not required to implement the CLI `put` path.

The promised “short-lived” token is also not generic. `GitHostProvider.clone_binding()` describes an auth shape but has no token-mint/exchange capability. A stored PAT is commonly long-lived; copying it into a short-lived file does not make the credential itself short-lived.

**Correction:** make secret delivery an explicit launcher capability separate from `ClonePlan`, for example a `stage_clone_credential`/`run_with_secret_env` contract whose implementations guarantee non-logging, host binding, cleanup, and failure cleanup. Keep the secret value out of `RepoWorkspace`, `ClonePlan`, shell strings, and `SandboxPolicy`. Either add a provider capability that really mints/exchanges a launch-scoped token, with PAT fallback stated explicitly, or call this a launch-scoped delivery of the existing credential rather than a short-lived token.

For Kubernetes, use a distinct clone-credential Secret projected only to the init container and delete it as soon as init succeeds (or on every failure path). “Deleted on teardown” leaves a clone-only secret in the API for the entire session and does not satisfy the stated shortest-lifetime contract. The existing launch-token Secret lifecycle is a useful implementation pattern, but it should not force the clone credential to share that longer lifetime.

### 2. BLOCKER — §8.5/§14 P1, post-clone managed fetch/push still has no credential path

V2 correctly says the inner helper should receive only a placeholder. However, `prepare_credential_proxy_runtime` resolves the real source in the runner parent and builds in-memory rewrite rules (`credential_proxy.py:103-153`); it accepts only locally resolvable env/file/command sources. The encrypted `SqlGitCredential` is decrypted on the server. After the transient clone helper/Secret is removed, the design defines no channel that supplies that real credential to the managed sandbox's trusted runner parent.

Consequently, clone can be made to work while the P1 acceptance requirement for later fetch/push cannot. Serializing a decrypted PAT as a `CredentialSourceSpec` or `SandboxPolicy` would contradict §8.2, and forwarding it as `GIT_TOKEN` would restore the exact runner-env problem v2 rejects.

**Correction:** define a second, authenticated server-to-managed-host/runner-parent credential handoff, distinct from the clone transport and policy. It may deliver an in-memory resolved-secret handle over the existing authenticated tunnel, retain a narrowly mounted provider secret for the trusted parent, or use a broker. Then extend runtime composition to accept already-authorized resolved secrets and mint placeholders/rewrite rules without converting a user PAT into env/file/command policy. Specify lifetime, reconnect/relaunch behavior, zeroization/best-effort cleanup, and which process is inside the trusted boundary. Until that exists, remove fetch/push from P1 acceptance.

### 3. HIGH — §8.6, grant-time-only sharing warnings have authorization and timing gaps

The grounding is accurate. `SharingMode` defines OFF/read-only/restricted/on and levels 1-4 (`auth.py:77-102`); `workspace_sharing_blocked` is the existing restricted-sharing signal (`:131-152`). The actual grant choke point is `PUT /sessions/{session_id}/permissions` (`sessions.py:20782-20882`), where edit/manage grants can be warned or rejected before `permission_store.grant`. Adding an explicit-confirmation field or challenge there is feasible.

Checking only when a grant is created is insufficient. A session can be shared before the owner attaches a host credential, or an existing shared session can gain a different credential on relaunch if resolution is dynamic. No new grant is created, so no warning occurs. Also, the endpoint requires manage access, not ownership; a manager can create further grants, but warning that manager is not necessarily consent from the owner of the git credential.

**Correction:** persist the credential host IDs actually in scope for the session/launch generation. Enforce the warning/confirmation both when an edit/manage grant is created or upgraded and whenever credentials are newly attached or re-resolved for a session that already has such grants. Define whether only `LEVEL_OWNER` may approve exposing an owner's credential, or make the owner's original confirmation explicitly authorize re-sharing by managers. Do not compute the signal from the owner's current global credential rows alone.

### 4. HIGH — §8.5/§12.3/§14 P1, long-lived `omni host` is no longer covered by the server credential model

V2 accurately acknowledges that the server environment is not an external host's environment and chooses host-local credentials for long-lived hosts. That is a valid trust decision, but it conflicts with the broader goals and P1 acceptance language: operator-defined topology plus server-stored user credentials are presented as working across managed sandboxes and `omni host`, while §8.5 says the server retains only host identity and transports no secret.

The current host process forwards a fixed credential set plus operator-named passthrough variables (`host/connect.py:435-463,499-550`). V2 neither defines a host-local per-git-host credential configuration nor explains how non-secret provider topology, CA/known-host data, and placeholder bindings reach the external runner. A server `SqlGitCredential` therefore cannot satisfy the external-host half of the advertised flow.

**Correction:** split the acceptance contract explicitly. For managed hosts, use the server credential row and the new secure runner-parent handoff. For external hosts, define a host-local credential/proxy configuration keyed to the server-provided canonical host identity, and send only non-secret resolved provider data over the launch frame. State that adding a credential through `/v1/git-credentials` does not configure `omni host`, or add an explicit opt-in secret-transfer protocol if one server-side registration is truly required.

### 5. MEDIUM — §9/§16.3, resolver placement is conceptually right but relaunch and create failure semantics remain open

Keeping `parse_repo_workspace` pure is correct: it is a shape parser with no identity (`managed_hosts.py:508-549`). The create path has authenticated `user_id` plus app state, and relaunch passes `host.owner` (`sessions.py:7026-7059`), so those are the correct identities for owner-aware resolution. Using `host.owner`, rather than the user who happens to trigger wake, is especially important for shared sessions.

However, the cited create insertion point is after the session response object has already been created (`sessions.py:14565-14625`). A new resolver failure there can return an error after leaving a persisted session/owner grant unless the flow is reordered or compensates. V2 also still leaves persistence-versus-re-resolution as an open question; the current relaunch stores only the raw URL label and re-parses it.

**Correction:** resolve and authorize the host/credential before durable session creation, or make creation plus cleanup atomic on resolver failure. Choose the relaunch rule in the design: preferably persist the non-secret host-config ID/version and canonical URL plus credential slot ID, then re-authorize that slot for `host.owner` at every launch; alternatively state that topology and credential changes deliberately affect relaunch and test that behavior. Do not leave this as a P1 open question.

### 6. MEDIUM — §8.2-§8.3/§12.2, the authorization fix is sound but the “slot” and SQL record need one authoritative identity

The prior blocker is resolved in substance. A caller supplies only a token for an operator-defined host; the server derives owner/workspace from authentication, mints the row ID, and store/launch lookups include owner and host. There is no user-chosen env name or user-supplied `CredentialSourceSpec`, so another tenant's/operator's env or command source is no longer selectable. Write-time binding plus launch-time re-authorization is realistic.

`SqlGitCredential` also follows the important `SqlHost` portability conventions: `OmnigentBase`, workspace-scoped composite key, `Uuid16`, no FK, plain unique constraint, application cleanup, and a sweeper. Two details are internally vague: §8.2 says the row holds ciphertext directly while §8.3 describes a separate opaque slot/mapping; and `host_id`, `canonical_host`, and integer `provider` duplicate operator topology that can change or drift.

**Correction:** define the server-minted row `id` itself as the opaque credential slot. Routes must ignore/reject client-supplied owner, canonical host, and provider, deriving them from authenticated identity plus the typed operator host record. Make `(workspace_id, owner, host_id)` the authoritative uniqueness key; treat canonical host/provider as derived data or validated denormalized snapshots. Add the same stable enum codec and `CheckConstraint` pattern used by `SqlHost` for any persisted provider code. On launch, require the operator host still to exist and match before decrypting.

### 7. HIGH — §11, private egress policy no longer auto-broadens globally, but the merge point is still asserted rather than designed

V2 fixes the dangerous rule: a provider only recommends host rules and cannot auto-set the global `egress_allow_private_destinations` flag. That part is correct. But “operator rules are authoritative; agent rules cannot broaden private access” does not identify the concrete object owner, call site, precedence algorithm, or validation boundary that merges server host configuration into the agent-authored `OSEnvSandboxSpec`.

**Correction:** name the component that composes the effective sandbox spec, define immutable merge rules, and revalidate after merging. Provider host/path allow rules may be added only for the resolved host; the global private flag must come only from operator/server policy, never from provider or agent input. Add tests proving an agent-authored spec cannot enable or widen private access.

## Requested changed-area checks

- **Clone transport (§8.4/§9): PARTIAL.** Kubernetes init-container injection is feasible; the “secretless” guarantee narrowed to inner helpers is now accurate. Exec delivery, actual short-lived minting, immediate Kubernetes cleanup, and the later managed-runner secret handoff are not complete.
- **Credential authorization (§8.2/§8.3/§12.2): RESOLVED, with schema clarifications.** The owner/workspace/operator-host binding removes arbitrary secret naming. Enforce derived identity at write and launch as stated; make row ID the slot and add a provider check constraint if provider remains persisted.
- **Resolver placement (§9): PARTIAL.** Pure parser plus post-auth owner-aware resolver is the right separation, and `host.owner` is correct on relaunch. Move resolution before durable create and settle relaunch persistence semantics.
- **Session sharing (§8.6): PARTIAL.** The enum, levels, broad-workspace signal, and grant endpoint are real; grant-time warning is implementable. It also needs credential-entry/relaunch checks and credential-owner consent semantics.
- **GHE same-host (§10): RESOLVED as a design finding.** V2 correctly records both the parser's one-binding-per-host rejection (`parser.py:1462-1478`) and proxy's one-rule-per-host swap table (`proxy.py:225-240`), and no longer claims sibling `gh_basic` entries solve it. The required multi-mode same-host rule is appropriately explicit P2 work.

## New issues introduced or exposed by the v2 rework

- **HIGH — §8.4:** “Short-lived token” assumes a provider mint/exchange operation that the proposed provider interface does not expose; most stored PATs do not become short-lived merely because their transport is ephemeral.
- **HIGH — §8.4:** retaining a clone-only Kubernetes Secret until pod teardown contradicts the stated shortest feasible lifetime. Delete a separate clone Secret after init completion.
- **BLOCKER — §8.5/P1:** separating transient clone delivery from runner placeholders exposes a missing server-to-managed-runner-parent secret handoff. Without it, later fetch/push cannot use the encrypted user credential.
- **HIGH — §8.6:** warning only during grant creation is subject to a timing bypass when credentials enter scope later, and a manager confirming is not necessarily the credential owner's consent.
- **MEDIUM — §9:** placing resolution at the cited late create-route block can fail after the session and owner grant have already been persisted.
- **MEDIUM — §8.2-§8.3/§12.2:** the spec describes both direct ciphertext storage and an opaque slot/mapping without declaring that the row ID is the slot, while duplicating mutable topology fields into the credential row.

## Prior findings scorecard

| Prior finding | v2 status | Section | Assessment / concrete correction |
|---|---|---|---|
| Managed-clone transport | **PARTIAL** | §8.4, §9 | Trust boundary and Kubernetes model are corrected. Add a secret-safe exec launcher primitive and a real mint/exchange capability or stop calling copied PATs short-lived. |
| User credential authorization | **RESOLVED** | §7, §8.2-§8.3, §12.2-§12.3 | Operator-only topology plus authenticated owner/host rows prevents arbitrary env/command selection. Define row ID as the slot and derive all authority fields server-side. |
| `materialize_workspace` universality / Kubernetes | **RESOLVED** | §4, §8.4, §9 | V2 correctly treats clone as launcher-wide and explicitly covers the init container. New lifecycle correction: delete a distinct clone Secret immediately after init, not only at teardown. |
| Parser resolution | **PARTIAL** | §9, §16.3 | Resolver is correctly outside the pure parser and has the right owner contexts. Reorder it before durable create and choose/persist relaunch semantics. |
| GHE same-host | **RESOLVED** | §10, P2 | V2 acknowledges the one-binding/one-swap limit and requires a multi-mode same-host design rather than sibling presets. |
| Global private egress | **PARTIAL** | §11 | Automatic global broadening is removed. The concrete effective-policy merge/validation point is still missing. |
| Keychain contradiction | **RESOLVED** | §6.3 | Keychain onboarding is explicitly excluded and the reversible-file contradiction is gone. |
| Config helpers | **RESOLVED** | §7 | V2 calls for a new typed fail-closed structured parser and reuses only the mtime-cache pattern. |
| Runner forwarding / long-lived host | **PARTIAL** | §8.5, P1 | Real-token forwarding is rejected and external credentials are correctly recognized as host-local, but managed runner secret handoff is absent and external-host configuration contradicts the unified acceptance claim. Define both flows. |
| OIDC | **RESOLVED** | P3 | V2 makes generic support claim-dependent, identifies userinfo/documentation alternatives, parameterizes GHE endpoints, and places the OIDC client secret in operator env. |

## Bottom line

The two original blockers were not equally resolved: user credential authorization is resolved; managed clone/runner credential transport is only partially resolved and remains blocking for P1 clone/fetch/push acceptance. The v2 rework otherwise correctly fixes or narrows the earlier GHE, keychain, config-helper, OIDC, and Kubernetes-universality claims. The principal newly introduced issues are the false generic “short-lived token” assumption, retaining a clone-only Kubernetes Secret until teardown, the missing post-clone managed-runner handoff, and warning-only sharing checks that do not cover credentials entering scope after a grant.
