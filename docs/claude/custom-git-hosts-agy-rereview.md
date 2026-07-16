Here is a focused correctness re-review of the v2 design document, addressing the prior review items, new inconsistencies, and evaluating the core structural changes.

### 1. Resolution of Prior Review Items

The v2 document does a very good job addressing the foundational issues raised in the prior review. Here is the verdict for each item:

*   **Precedence rule hijacking (Correctness/Auth):** **Resolved.** Splitting operator-owned topology from user-owned credentials completely closes this loophole. Users can no longer redefine routing for an existing host to steal credentials.
*   **Keychain onboarding contradiction:** **Resolved.** The design explicitly removes the LLM-style keychain reference for operator hosts, cementing the operator reference-source model (§6.3).
*   **OIDC client secret storage:** **Resolved.** Acknowledged as a gap from the git credential model and explicitly defined as an operator environment variable, similar to `OIDCConfig.from_env` (§14, P3).
*   **Bitbucket missing CLI:** **Resolved (via scoping).** The document acknowledges the lack of a native CLI, designates the community MCP as experimental, and plans for a native `api_client` adapter if the MCP is inadequate (§10).
*   **Over-permissive egress rules:** **Resolved.** The global `egress_allow_private_destinations` flag is strictly protected. Providers can only *recommend* rules, and private access remains an explicit operator opt-in (§11).
*   **Under-specified details (CA/SSH, tokens, DB, testing):** **Resolved.** 
    *   **CA/SSH:** Now clearly specified via `known_hosts`, `GIT_SSL_CAINFO`, and mounted bundles (§9).
    *   **Clone Token:** Explicit contracts are defined for both Exec (`GIT_ASKPASS` / helper removed after clone) and Kubernetes (init-container Secret deleted on teardown) (§8.4).
    *   **DB Cleanup:** Explicitly handles the lack of FKs via app-cascades and a reconciliation sweeper (§12.2).
    *   **Testability:** P1 acceptance criteria now require integration against live containerized instances of Forgejo/Gitea/GitLab (§14, P1).

### 2. New Inconsistencies & Gaps (Ranked by Severity)

While the major blockers are fixed, the v2 rework introduces a few new edge cases and under-specified seams:

*   **[Medium] Missing API/Headless handling for Session Sharing Warning (§8.6):** The design states that the grant-creation flow "warns the grantor... grantor must explicitly confirm." This assumes a synchronous UI flow. If session sharing is initiated via a headless API or CLI, how is this handled? It likely needs an explicit `--confirm-git-credential-sharing` flag or a specific API boolean to prevent breaking automation.
*   **[Medium] Orphaned credentials on YAML topology changes (§12.2 & §7):** The design notes an "app-cascade on user/host deletion" for `SqlGitCredential`. However, operator hosts are now defined in an *in-memory YAML configuration*, not the database. If an operator removes a host from the YAML, there is no database event to trigger a cascade. The reconciliation sweeper must be explicitly designed to poll the YAML state or hook into the `mtime` refresh to scrub orphaned DB credentials.
*   **[Low] Relaunch semantics are listed as an "Or" (§9):** The spec says "Define relaunch: persist the resolved host-config id/version, or deliberately re-resolve for host.owner." This is an unresolved design decision. Re-resolving is generally safer to pick up operator topology changes (e.g., rotated CA certs or updated URLs), but this should be locked in before implementation.
*   **[Low] Fernet key rotation is glossed over (§8.2 & §16):** Stating "rotation = re-encrypt" is insufficient for a single `FERNET_KEY` env var. To re-encrypt, the application needs *both* the old key (to decrypt) and the new key (to encrypt). The system needs to support a list of keys (e.g., `FERNET_KEYS=new_key,old_key`) to allow graceful rotation without downtime or data loss.
*   **[Low] GHE same-host binding is stated as a need, but not designed (§10):** The document identifies that GHE needs a way to distinguish placeholder-auth API requests from git requests on the same host, but doesn't actually specify *how* the router or proxy will achieve this.

### 3. Evaluation of Core Structural Changes

**Topology / Credential Split:**
This is the strongest improvement in v2. Moving topology to operator YAML and keeping credentials in the database creates a clean, secure boundary. It accurately reflects how enterprises manage infrastructure versus how users manage access. 

**Encrypted-at-Rest User Credentials:**
Internally consistent and well-scoped. By ensuring the decryption key only lives in the trusted parent environment and the raw secret is never serialized into the `SandboxPolicy`, it successfully narrows the blast radius.

**Session-Sharing Warning:**
Conceptualizing session sharing as an authorization escalation (granting the grantee access to the grantor's git identity) is a very mature security posture. The integration with `SharingMode` is sound, provided the API-level confirmation (mentioned in section 2 above) is addressed.

### 4. Overall Verdict

**Status: SOUND AND BUILDABLE (with minor clarifications).**

Version 2 is a massive improvement. The architectural boundaries are now correct, the security model is defensively designed (especially the Kubernetes init-container isolation), and the database constraints are portable. The remaining gaps are largely implementation-level details rather than structural flaws. The team can safely proceed to implementation (P1) after answering the open questions on key rotation and API session sharing.
