Here is a rigorous correctness and completeness review of the `Custom git-host provider abstraction` design document. 

### 1. Overall Assessment
The design is largely sound, well-factored, and aligns cleanly with the existing architecture by reusing established patterns (LLM provider shapes, Sandbox launcher paradigms, and existing proxy boundaries). The intentional decision to avoid writing custom REST clients up front and instead delegate to MCP servers and native CLIs is a smart, pragmatic engineering trade-off that significantly accelerates delivery. 

However, the design contains a **critical security vulnerability** in its precedence rules, internal contradictions regarding credential storage, and several under-specified areas (particularly around OIDC and container injection) that must be resolved before implementation.

---

### 2. Top Risks and Gaps (Ranked)

**1. [BLOCKER] Security / Traffic Hijacking via Precedence (Section 12.3)**
* **Issue:** The precedence rule states `User > Operator > Built-in`. This allows an untrusted user to register a custom host with the ID `github.com` (or an operator’s internal `git.corp.com`), but point the `api_url` or `web_url` to a malicious external server. When the agent attempts to interact with the repository, it will send MCP requests—and potentially route git credentials via the proxy—to the attacker-controlled server.
* **Impact:** Complete compromise of git credentials and source code for the hijacked domains.

**2. [HIGH] Contradictory Secret Storage Specs (Section 6.3 vs. Section 7)**
* **Issue:** Section 6.3 states the schema "persists a `keychain:`/`env:` secret ref", borrowing directly from the LLM provider pattern. However, Section 7 explicitly declares a strict reference-source-only model: "No secret is stored at rest... The encrypted-at-rest token column considered earlier is dropped," and restricts kinds to `{env, file, command}`. 
* **Impact:** Architectural confusion. If `keychain:` implies an encrypted-at-rest KMS or vault (as it typically does for LLMs), it directly violates Section 7.

**3. [HIGH] Missing OIDC Client Secret Management (Section 6.1 & 11)**
* **Issue:** Section 11 integrates Forge SSO via the provider's `oauth_config(cfg)`. OIDC requires both an OAuth Client ID and a Client Secret. However, Section 7's credential model (`CredentialProxyEntry`) is explicitly scoped to **git credentials** (username/token for HTTP or SSH keys). There is no defined storage, injection, or resolution mechanism for the OIDC client secret.
* **Impact:** SSO integration cannot be built as described without leaking or hardcoding client secrets in the host configuration.

**4. [MEDIUM] Blindspot on Bitbucket (Section 13 & 14)**
* **Issue:** Section 13 lists Bitbucket with no CLI tool (`—`). Because Section 9 relies heavily on a mix of MCP and CLI fallbacks, and the native `api_client` is explicitly deferred to Phase 4, the agent will be entirely reliant on a "community MCP" for Bitbucket in P1-P3. If that MCP is incomplete or fails, the agent has no alternative way to manipulate PRs or issues.
* **Impact:** Bitbucket support will likely be brittle or unusable until P4.

**5. [LOW] Over-permissive Egress Derivations (Section 10)**
* **Issue:** Generating blanket `* git.acme.com/**` egress rules is fine for isolated single-tenant instances. However, GitLab and Bitbucket use nested namespaces heavily. For a shared enterprise Forge, an operator may only want to grant the agent network access to a specific subgroup (e.g., `git.acme.com/my-org/**`).
* **Impact:** The auto-generated egress rules violate the principle of least privilege for multi-tenant enterprise deployments.

---

### 3. Inconsistencies and Under-Specified Areas

* **Container Trust Injection (Section 8 vs. 16.2):** Section 8 mandates that `materialize_workspace` provides custom CA and SSH host-key handling. However, the exact mechanism of injecting these into the managed clone container is entirely unstated. If the container image relies on standard `git`, standardizing on injecting `GIT_SSL_CAINFO` or mounting to `/etc/ssl/certs` must be explicitly defined in Section 8 to ensure Sandbox launcher contracts are met.
* **Pre-host Clone Credential Handling (Section 8):** The document states `materialize_workspace` will "resolve the per-host credential (via the credential proxy)". The credential proxy is documented to run in the trusted parent, but the clone runs in the sandbox. The document fails to specify how the parent securely passes this ephemeral token to the sandbox clone process without it leaking into the sandbox's shell history, env dumps, or logs.
* **App-Owned Database Cleanup (Section 12.2):** R032 forbids Foreign Keys, and the document specifies "app-owned cleanup on user/host deletion". Given that there are no FKs, if the application crashes mid-deletion, `SqlGitHost` records will be permanently orphaned. A reconciliation mechanism (e.g., background sweeper or transactional outbox) must be specified to prevent identity drift.
* **Phase 1 Testability (Section 14):** P1 asserts that "clone/fetch/push works" and can be verified. But P1 does not include the MCP or CLI tools (which are deferred to P2). Without these tools, the agent cannot organically trigger a push by creating a PR. The testing strategy for P1 needs to clarify that it relies strictly on harness/operator mocks, not autonomous agent behavior.

---

### 4. The Single Weakest Part & Concrete Alternative

**The Weakest Part:** The combination of the Precedence Model (Section 12.3) and the User-Registered PAT UX constraints (Section 7).
By conflating "Infrastructure Topology" (what the API URL is) with "Authentication" (the credentials to access it), the design creates a massive security hole. Furthermore, by strictly enforcing reference-source-only (`env`) for user-registered hosts, the design completely breaks the standard multi-tenant SaaS UX: users cannot simply paste a Personal Access Token (PAT) into a UI. 

**Concrete Better Alternative:**
Decouple the **Host Definition** from the **User Credential**.

1. **Operator defines Infrastructure (Global):** The Operator is the sole authority for defining Git Hosts (`github.com`, `git.corp.com`, `api_url`, `ca_bundle`, OIDC config). Users *cannot* override host routing definitions or register new network topologies. (Resolves the Blocker security risk).
2. **Users define Credentials (Per-Workspace):** Introduce a separate `WorkspaceGitCredential` entity. Instead of registering a "Host", users register a credential that maps to an existing Operator-defined host. (e.g. "Use this PAT for `git.corp.com` in Workspace A").
3. **Reintroduce Encrypted-at-Rest for User PATs:** The "reference-source only" rule is excellent for protecting server environments from RCE via command injection, but it should apply *only* to Operator-defined server credentials. For User Workspace credentials, allow an encrypted-at-rest database column. This allows SaaS users to securely paste a PAT in the web UI without needing server-side environment variables.
4. **New Precedence Rule:** 
   * **Routing:** Operator > Built-in (Users have no say).
   * **Credentials:** Workspace Credential > Operator Default Credential. 

This alternative closes the security vulnerability, restores the necessary user experience for SaaS, and cleanly separates network configuration from authentication.
