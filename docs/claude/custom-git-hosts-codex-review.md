# Design-correctness review: custom git hosts

Reviewed the full design against this worktree at `b993343b`, using the requested capped set of load-bearing spot checks. The spec says it was grounded at `0f6e82fb`; the anchors below are therefore reported against the current pinned worktree, not that older hash.

## Verdict

The provider registry and `SqlGitHost` persistence direction fit omnigent reasonably well, and most of the cited anchors are real. The design is not implementation-ready, however. Its P1 acceptance criterion depends on a managed-clone credential path that the existing credential proxy cannot provide, and its user-selectable env-reference rule does not authorize a user to a particular server-side secret. Those are blockers, not implementation details.

## Findings

### 1. BLOCKER — §7-§8, managed pre-host clone credentials

The design says to reuse `prepare_credential_proxy_runtime` "verbatim" and wire the proxy into `materialize_workspace`. That mechanism is not a general secret resolver or a managed-sandbox proxy. It is prepared inside the runner process for one inner helper: `omnigent/inner/os_env.py:419-454` resolves entries and starts the L7 egress proxy around the helper. `prepare_credential_proxy_runtime` produces helper env placeholders plus in-process rewrite rules (`omnigent/inner/credential_proxy.py:103-153`); it does not expose a safe clone credential transport that `SandboxLauncher.run()` can use in a remote sandbox.

The clone happens inside the provisioned sandbox (`omnigent/onboarding/sandboxes/base.py:320-389`). Unless the managed sandbox is also forced through a reachable credential-rewriting proxy, the server must deliver the real credential (for example through a provider secret primitive and an ephemeral askpass/helper), which contradicts the spec's claim that the secret "never enters the sandbox." The fallback `GIT_TOKEN_<HOST>` convention also puts the real token in the sandbox and is only an image convention, not reuse of the credential proxy.

Correction required: define a separate managed-clone credential contract and choose its trust model. It must specify (a) where the source is resolved, (b) how each sandbox provider transports the credential or reaches a broker, (c) how it is bound to the selected host, (d) cleanup/lifetime/redaction, and (e) how the resulting checkout continues to fetch/push. If raw credentials may enter a managed sandbox, narrow the "secretless" guarantee to inner runner helpers and state that managed hosts are trusted credential recipients. If they may not, P1 needs a new remotely reachable credential broker/proxy rather than `prepare_credential_proxy_runtime` verbatim.

### 2. BLOCKER — §7 and §12.2, user-registered credential authorization

Restricting a user-provided source to an env name under `OMNIGENT_GITHOST_*` prevents `command` execution and references such as `DATABASE_URL`, but it does not establish that the caller is authorized to use the named git credential. In a shared server process, every user's resolution sees the same parent environment. A caller who can choose `credential_ref` can name another tenant's or an operator's namespaced variable; overriding an operator host via §12.3 makes this especially dangerous.

Correction required: the API must not accept an arbitrary environment variable name. Persist an opaque credential-slot ID and resolve it through an operator-owned mapping scoped to `(workspace_id, owner, canonical_host)`, or derive and validate an unguessable/server-generated owner-scoped name and enforce that ownership during resolution. Construct `CredentialSourceSpec(kind="env", ...)` server-side; never deserialize a generic source object from the user route. Enforce the origin/source rule both at write time and again at launch time so a malformed or legacy row cannot reach the `command` branch at `omnigent/inner/credential_proxy.py:184-208`.

### 3. HIGH — §8 and §16.3, `materialize_workspace` is not a universal hook

The default exec-model path does call `materialize_workspace` (`omnigent/onboarding/sandboxes/base.py:230-301`), but Kubernetes overrides `start_host` and performs `git clone` in an init-container script (`omnigent/onboarding/sandboxes/kubernetes.py:359-393`, called from `build_pod_manifest` at `:447-579` and from `start_host` at `:1013-1100`). It never calls `materialize_workspace`. Islo's override delegates to `super().start_host` (`omnigent/onboarding/sandboxes/islo.py:516-541`) and is safe.

The spec acknowledges this only as an open question while P1 simultaneously promises managed-provider acceptance. Correction required: make clone inputs (credential handle, CA bundle, known-hosts data, normalized URL) part of the `start_host`/workspace-materialization contract and explicitly implement both execution models. Kubernetes needs Secret/volume/env and init-container wiring plus cleanup; adding a field only to `RepoWorkspace` and changing base `materialize_workspace` cannot satisfy P1.

### 4. HIGH — §8 and §12.3, provider resolution is placed in a context-free parser

`parse_repo_workspace` is a pure shape parser (`omnigent/server/managed_hosts.py:508-549`). It is also called during Pydantic model validation, before request identity is available (`omnigent/server/schemas.py:1341-1386`), and during relaunch from a stored raw label (`omnigent/server/routes/sessions.py:7026-7045`). User-over-operator precedence requires the authenticated owner and a `GitHostStore`; that lookup cannot realistically live in this function without fighting these call sites.

The create route does have both the authenticated user and app state when it parses again (`omnigent/server/routes/sessions.py:14586-14605`), and relaunch has `host.owner`. Correction required: keep `parse_repo_workspace` pure, add a separate owner-aware resolver after authentication, and pass a resolved immutable clone plan into launch. Define relaunch semantics explicitly: either persist the selected host-config ID/version with the session, or deliberately re-resolve the raw URL for `host.owner` and document that registration/config changes affect later relaunches.

The parser also does not canonicalize a hostname: at `managed_hosts.py:530-542`, `host` can retain case, a port, or HTTPS userinfo. Exact-match precedence needs a single validated representation (lowercase IDNA hostname plus explicit port, with userinfo rejected) derived with a URL parser, not string partitioning.

### 5. HIGH — §13/P2, GHE git and API credentials cannot be represented by sibling `gh_basic` entries

The cited heuristic is real: `_normalize_gh_basic` treats `api.*` as the API binding (`omnigent/spec/parser.py:1638-1685`). But the proposed fix overlooks the harder GHE case: clone and API commonly share one hostname, while clone wants Basic and `gh` wants a synthetic token-style authorization flow. The parser rejects two bindings for the same host (`omnigent/spec/parser.py:1462-1478`), and the proxy has exactly one swap-on-access rule per host (`omnigent/inner/egress/proxy.py:225-240`).

Correction required: design a single same-host binding that can distinguish placeholder-auth requests from no-auth git requests, or extend credential bindings to path/client-aware behavior. Merely adding provider-specific `_normalize_*` presets or an API-host selector does not make GHE work. This is P2 rather than the P1 clone blocker, but it invalidates the GHE row of the provider matrix as currently designed.

### 6. HIGH — §10 and §15, private-destination egress is global, not per host

Provider-derived exact/wildcard rules do fit `EgressRule` (`omnigent/inner/egress/rules.py:68-175`). However, a provider must not silently "set `egress_allow_private_destinations: true`" merely because one configured forge is intranet-hosted. The actual flag lifts the private/reserved/cloud-trap check for every allowed destination in the sandbox, explicitly including metadata-adjacent ranges (`omnigent/inner/datamodel.py:627-650`). It is not scoped to the provider's host.

Correction required: keep this an explicit operator/spec-author opt-in, or extend the egress model to allow private resolution only for named hosts/CIDRs. Provider output may recommend the setting but should not broaden the whole sandbox policy automatically. Also identify the concrete merge point between server-side host configuration and the agent's `OSEnvSandboxSpec`; §10 says the provider emits rules "into the spec" but no phase describes who is authorized to mutate that agent-authored policy.

### 7. HIGH — §6.3 versus §7, the at-rest model contradicts itself

§6.3 says onboarding persists `keychain:` or `env:` references and cites `resolve_secret`. A `keychain:` entry resolves from the omnigent secret store, whose documented fallback is a reversible `0600` JSON file (`omnigent/onboarding/provider_config.py:425-478`). §7 then says every credential is a `CredentialSourceSpec` with only `env`, `file`, or `command` and that no secret is stored at rest. `keychain:` is neither that source model nor necessarily secretless at rest.

Correction required: remove keychain onboarding from this program and accept only validated source descriptors, or explicitly add a keychain source and relax the non-goal/security claim to permit the existing local secret store. Operator config and user-registration flows should be described separately; the LLM onboarding schema is a useful UI idiom but not a drop-in trust model for server-owned git credentials.

### 8. MEDIUM — §12.1, the cited config/list helpers cannot store the proposed records

The anchors exist, but the proposed use is inaccurate. `config_str_list` coerces list elements to strings (`omnigent/server/server_config.py:82-95`), which would destroy structured host mappings. `MtimeCachedIdentitySet` parses one lowercased token per line into a set (`omnigent/server/admin_list.py:83-157`); it cannot store `id`, provider, URLs, credential descriptors, CA hints, or egress hints.

Correction required: reuse only the high-level pattern (operator YAML plus an mtime-cached runtime file). Add a typed, fail-closed host-config parser and a cache of structured immutable records. Do not reuse `config_str_list` or `MtimeCachedIdentitySet` for this data. Because malformed host/credential configuration affects security and clone selection, silently stringifying or failing open to an empty set is the wrong behavior.

### 9. HIGH — §8/P1, runner and long-lived-host credential wiring is missing

`_build_runner_env` forwards a fixed credential-name set plus operator-selected exact names (`omnigent/host/connect.py:435-463`, `:499-550`). A provider declaration does not dynamically extend the module-level frozen set. More importantly, forwarding `GITLAB_TOKEN`/`FORGEJO_TOKEN` places a real token in the runner environment, whereas the credential-proxy model says the runner receives only a synthetic placeholder.

Correction required: choose per client whether it uses a real forwarded secret or a credential-proxy placeholder. For the latter, generate `inject_env` names in the runner's `CredentialProxyEntry` and ensure the referenced source is actually available to the runner parent; do not add the real variable to `_BASE_HARNESS_CREDENTIAL_ENV_VARS`. Describe how server-side resolved host credentials reach a long-lived external host, because the server's process env is not the external host's process env.

This is acceptance-critical for the claim that one server-side host registration supplies fetch/push credentials to both managed sandboxes and `omni host`. A workable design may instead treat external-host credential sources as host-local configuration and store only the host identity server-side; if so, say so and define how an identically named host config is matched without implying that the server transports its secret.

### 10. MEDIUM — §11, generic OIDC is feasible but not "zero code" for every forge

The OAuth anchors are accurate: GitHub endpoints are hardcoded at `omnigent/server/oidc.py:118-123`, GitHub is selected by issuer equality at `:309-332`, and generic discovery is at `:334-374`. The callback dispatch is at `omnigent/server/routes/auth.py:250-288`, and the GitHub email helper is at `:679-719`.

The generic callback accepts only a signed `id_token` carrying `email` and (unless explicitly waived) `email_verified`; it does not fall back to the discovered userinfo endpoint (`omnigent/server/routes/auth.py:742-814`). Therefore a forge's discovery document alone is insufficient. Correction required: make P3 acceptance conditional on the forge actually issuing the required claims, add provider-specific integration tests, and either document the secure skip-verification requirement or implement a validated userinfo path. For GHE, parameterizing constants is feasible, but `_resolve_github_email` must receive a configured emails endpoint rather than continue importing the github.com constant.

### 11. LOW — §12.2, store/route anchors overstate the exact pattern

The proposed `SqlGitHost(OmnigentBase)` shape is sound: composite workspace-scoped PK, `Uuid16`, stable integer enum, plain `UniqueConstraint`, and no FK match `SqlHost` at `omnigent/db/db_models.py:1063-1150`. `delete_host` is a valid application-cascade example at `omnigent/stores/host_store.py:736-763`, and `create_hosts_router` begins at `omnigent/server/routes/hosts.py:288`.

The cited `host_store.py:45-187` covers the entity, mapper, helpers, and store constructor, not the full store pattern. The actual owner-filtered list is `omnigent/stores/host_store.py:577-598`; `get_host` at `:600-616` is workspace-scoped but not owner-scoped, with ownership enforced later in the route (`omnigent/server/routes/hosts.py:371-390`). Correction required: for the new user-editable entity, prefer store methods keyed by both owner and ID/host so authorization is difficult to omit, even if the HTTP edge also checks ownership.

## Areas that are sound

- **Current-state clone analysis and anchors:** `RepoWorkspace` is at `omnigent/server/managed_hosts.py:397-420`; `parse_repo_workspace` at `:508-549` has no github.com allowlist; the default clone really is plain `git clone` at `omnigent/onboarding/sandboxes/base.py:370-380`.
- **Provider registry fit:** the lazy name-to-class registry analogy is accurate (`omnigent/onboarding/sandboxes/__init__.py:54-132`). A closed built-in registry is a reasonable P1 choice. The ABC should remain focused on forge behavior and produce immutable data/clone plans; lifecycle execution should remain with sandbox launchers.
- **Credential datamodel anchors:** `CredentialSourceSpec` and `CredentialProxyEntry` are accurately cited at `omnigent/inner/datamodel.py:377-450`, and multiple distinct-host entries are supported. The important qualification is one binding per host and runner-helper scope.
- **Persistence portability:** the proposed table itself respects the stated no-FK, no-partial-index, and workspace-scoped rules. Use a migration, a check constraint for the integer enum, explicit workspace predicates in every store operation, and application-owned cleanup. No redesign of this part is needed.
- **Entity → store → route fit:** a dataclass domain entity, SQL row mapper, store, and FastAPI router are consistent with the host implementation. Pydantic-at-the-edge is also consistent.
- **Egress rule derivation:** exact host/path rule strings are supported by `omnigent/inner/egress/rules.py:129-175`; keep the private-address opt-in separate as described above.
- **OAuth separation:** the current login access token is used to resolve identity and is not persisted as a git credential. Keeping SSO and repository credentials independent is correct.
- **`gh_basic` anchor:** defaults at `omnigent/spec/parser.py:1200-1209` and the `api.` heuristic at `:1638-1685` are accurate; the limitation is the same-host GHE case, not the citation.

## Required design changes before implementation

1. Specify a real managed-clone credential transport/broker and its trust boundary for every sandbox execution model.
2. Replace arbitrary user env names with owner/workspace/host-authorized credential slots.
3. Split pure URL parsing from authenticated host-provider resolution; define canonical-host and relaunch behavior.
4. Make clone configuration a launcher-wide contract and explicitly cover Kubernetes init-container cloning.
5. Resolve same-host GHE git/API credential behavior before claiming P2/GHE support.
6. Keep private-network permission operator-controlled and identify the authorized merge point into agent egress policy.
7. Reconcile keychain persistence, runner env forwarding, and the stated no-secret-at-rest/no-secret-in-sandbox guarantees.

With those changes, the registry, persistence, egress-rule generation, and OAuth parameterization are feasible and largely aligned with omnigent's existing patterns.
