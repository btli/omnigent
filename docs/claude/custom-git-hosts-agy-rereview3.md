Here is a focused correctness re-review of the net-new sections (8.4 – 8.7) of the v3 design document.

### 1. Soundness Verdicts by Area

**8.4 Managed-clone secret delivery**
**Verdict: Highly Robust.** 
Moving the clone credential out of `ClonePlan` and shell command strings into an explicit launcher capability completely neutralizes the risk of secrets leaking via `ps` output, shell history, or application logs. For Kubernetes, using a distinct, tightly scoped init Secret that is deleted immediately after initialization (rather than at teardown) perfectly minimizes the credential's exposure window. The updated "launch-scoped" framing is accurate and precise.

**8.5 Server-to-runner credential handoff & long-lived hosts**
**Verdict: Architecturally Excellent.**
Reusing the existing authenticated host tunnel to deliver an in-memory handle to the trusted runner parent is a highly secure solution. It entirely avoids writing long-lived PATs to the sandbox disk or process environment. Furthermore, explicitly splitting the behavior for long-lived `omni host` environments (where credentials remain host-local and only non-secret topology crosses the wire) prevents dangerous syncing of server-held secrets to unmanaged infrastructure. 

**8.6 Shared-session identity model**
**Verdict: Pragmatic and Coherent.**
Deferring per-user push authorization avoids massive state-machine complexity for the agent (swapping credentials mid-turn) while maintaining strict security via the two-layer enforcement model (platform gates the driver; forge gates the push). Pinning the `user.name`/`user.email` commit authorship to the session starter is the correct approach for honest, consistent attribution across the lifecycle of the session.

**8.7 Session-sharing warning**
**Verdict: Thorough and Secure.**
Catching the "late-attach" or "re-resolution" timing bypass is a sophisticated and necessary security control; without it, the sharing warning would be trivial to circumvent. The addition of the headless/API `confirm_git_credential_sharing` field is a smart operational inclusion that ensures automated workflows (CLI/CI) don't hang or fail silently when a warning is triggered.

---

### 2. Remaining Gaps (Ranked)

While the architecture is sound, a few implementation-level specifics need to be locked down before coding begins:

**1. [8.5] SSH Proxy Support for Handoff (Medium)**
The design states the runner parent will mint "credential-proxy placeholders" for the handoff. This is well understood for HTTPS operations (where an HTTP proxy intercepts the request and injects an `Authorization` header). However, Section 7 indicates support for `ssh_host` and `ssh_port`. If a user supplies an SSH key as their credential, an HTTP proxy cannot inject it into a git SSH stream. 
*Fix:* Explicitly define how the runner parent handles SSH credentials in memory (e.g., exposing an ephemeral `ssh-agent` socket to the sandbox rather than relying on HTTP proxy rewrites). 

**2. [8.7] Ambiguous Consent Rule (Low/Medium)**
The text states: *"Consent semantics: define that exposing an owner's credential requires the credential owner's confirmation... e.g. only `LEVEL_OWNER` approves, or the owner's original attach explicitly authorizes manager re-sharing."* 
*Fix:* Remove the "e.g." and lock in a deterministic rule. The safest standard is: Only the credential owner can attach their credential to an already-shared session, and only the credential owner can share a session that currently uses their credential. If a `LEVEL_MANAGE` user attempts to share the session, they must either strip the owner's credential or replace it with their own.

**3. [8.4] K8s Clone Secret Orphan Mitigation (Low)**
The design states the Kubernetes clone Secret is "deleted as soon as init succeeds (and on every failure path)." If this active deletion is driven by the Omnigent server, a server crash during the clone phase will result in an orphaned Secret.
*Fix:* Specify that the Secret must be created with a Kubernetes `OwnerReference` pointing to the Sandbox Pod. This ensures that even if the Omnigent server crashes and fails to actively delete it, the Kubernetes garbage collector will automatically reap the Secret when the Pod is eventually cleaned up.

---

### 3. Overall Verdict

**Ready to Implement.** 
The v3 updates are highly coherent, internally consistent, and represent a mature security architecture. The most difficult edge cases (credential exposure during clone, long-lived PAT storage, and shared-session privilege escalation) have been resolved with elegant, standard-compliant mechanisms. The remaining gaps identified above are implementation details that can be easily patched into the specification without altering the fundamental architecture.
