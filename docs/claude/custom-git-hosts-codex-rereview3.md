# Final focused v3 re-review: security-critical additions

Reviewed only the net-new v3 material in §8.4-§8.7. This is a design-correctness review; no
source changes were made. I read the prior re-review and the complete v3 spec, then spot-checked the
eight requested source areas: the launcher contract, the three Kubernetes clone/lifecycle areas, the
host tunnel and its frame model, runner environment construction, and the permission-grant endpoint.

## Verdict

V3 resolves both prior architectural blockers:

1. **Managed clone transport (§8.4): RESOLVED.** A launcher-owned secret-delivery capability is a
   realistic extension of `SandboxLauncher`; it no longer pretends the existing
   `run(sandbox_id, command: str)` can safely carry the secret.
2. **Post-clone fetch/push path (§8.5): RESOLVED as the prior no-path blocker.** V3 now requires a
   second authenticated server→managed-host handoff in P1, separate from clone delivery and policy.

The changed sections are not all implementation-ready, however. The handoff's actual wire/lifetime
contract is still written as work to "specify," the sharing-consent rule still offers alternative
semantics instead of choosing one, and the owner-push model makes an edit grant a delegation of the
owner's forge authority. Those are HIGH remaining issues, but they are narrower than the two prior
"there is no credential path" blockers.

| v3 addition | Status | Remaining severity | Assessment |
|---|---|---:|---|
| §8.4 exec/clone delivery | **RESOLVED** | MEDIUM hardening | The capability and launch-scoped framing are sound; make Kubernetes secret isolation and crash cleanup explicit. |
| §8.5 fetch/push handoff | **PARTIAL** | HIGH | The route is feasible and fixes the missing-path blocker, but the protocol and trusted process boundary are not yet locked. |
| §8.6 identity model | **PARTIAL** | HIGH | Commit defaults are feasible; shared EDIT currently delegates all forge power of the owner/bot token. |
| §8.7 sharing consent | **PARTIAL** | HIGH | Both mutation timings are recognized, but consent ownership, persistence, and fail-closed re-resolution remain undecided. |

## Findings

### 1. RESOLVED / MEDIUM hardening — §8.4 exec secret delivery is realistic; Kubernetes needs explicit isolation cleanup

The prior blocker is resolved. `SandboxLauncher.run()` accepts only a command string, but adding a
separate capability (with a fail-closed default or capability check) is a normal extension of the
launcher abstraction. Exec implementations can use their provider's secret-env/file primitive while
the default `materialize_workspace()` remains unable to receive a secret through its command. The
secret-delivery method, not `ClonePlan`, must own staging, execution, redaction, and cleanup.

The Kubernetes design also fits the existing lifecycle. `build_pod_manifest()` already constructs a
dedicated init container, and `start_host()` creates Secrets before the Pod and waits for Pod
`Running`; that transition means init succeeded, so the launcher can delete the clone Secret before
returning. Its existing exception cleanup is the right pattern for clone failure.

One isolation detail should be explicit: today the init container receives the shared
`harness_secret` through `envFrom`. Adding a clone-only Secret without removing that projection would
still expose every harness credential to the clone container and would not produce a genuinely
clone-only secret boundary.

**Concrete correction:** state that P1 removes `harness_secret`/shared credential `envFrom` from the
init container and projects only the distinct per-clone credential there; the main container must not
receive that clone Secret. Delete it immediately after the init-success observation, delete both
Secrets on ordinary failure/cancellation, and add a label-based reconciler or equivalent orphan
cleanup for a server crash between Secret creation and deletion. Provider implementations must fail
closed when the new capability is unavailable.

The new wording is correct: a transient delivery of a stored PAT is **launch-scoped delivery**, not a
short-lived credential. No correction is needed to that framing.

### 2. PARTIAL / HIGH — §8.5 chooses a feasible handoff, but does not yet define its security contract

The existing host tunnel is a viable carrier. It authenticates before WebSocket acceptance; a managed
launch token is bound to one `host_id`; and the server already queues server→host control frames. The
host daemon is also the process that spawns the runner, so a new credential-control exchange can reach
a trusted parent without adding the real token to `_build_runner_env()`. This resolves the prior
BLOCKER that managed fetch/push had no credential path at all.

What remains is material. An "in-memory handle over the tunnel" is ambiguous:

- If the frame contains the resolved token, it is secret delivery, not merely a handle; the host
  daemon, decoder, queues, and any IPC used to reach the credential proxy are inside the secret-bearing
  boundary.
- If the frame contains only an opaque handle, the spec still needs the broker/request protocol by
  which the trusted process redeems it.
- "Specify TTL, reconnect/relaunch re-fetch, best-effort zeroization, and the trusted-boundary
  process" is an implementation TODO, not the promised specification of those properties.
- Tunnel authentication alone does not provide confidentiality. Secret-bearing frames must require
  authenticated server TLS (`wss`) with certificate validation, or equivalent application-layer
  encryption; plaintext `ws` cannot be accepted for this feature.

**Concrete correction:** lock a versioned P1 protocol. Name the receiver (normally the `omni host`
daemon) and its IPC/FD/local-broker path to the credential proxy; bind each delivery to
`{host_id, runner_id, session_id, launch_generation, credential_slot, canonical_git_host}` (and the
resolved repository path where possible); make it single-use with an explicit short delivery TTL and
acknowledgement; discard it on runner exit, stop, timeout, or tunnel loss; and require a fresh
authorization plus re-fetch after reconnect/relaunch. State that Python/JSON copies make zeroization
best-effort and prohibit secret-bearing frame bodies from logs/errors. Require confidential transport.

The long-lived `omni host` split is coherent: host-local credentials stay local, while the launch
frame carries canonical host/topology/CA/known-host data only. The P1 design should still name the
host-local config schema and lookup failure behavior, but it correctly states that
`/v1/git-credentials` does not configure an external host.

### 3. PARTIAL / HIGH — §8.6 commit defaults are feasible, but EDIT becomes owner/bot authority delegation

Setting commit identity at runner spawn is feasible and does not conflict with current
`GIT_USERNAME`/`GIT_TOKEN` handling: those variables are HTTPS authentication inputs, not commit
identity. Prefer runner-scoped `GIT_AUTHOR_*` **and** `GIT_COMMITTER_*` values over persistent local
repository config on a long-lived host. Snapshot the starter's validated name/email when the session
is created and define a deterministic no-email fallback.

The two-layer model is coherent only if described as delegation, not per-user authorization. The
forge sees the owner or bot, not the EDIT-grant user. Therefore an EDIT grantee can direct the agent to
push to a protected branch whenever that push identity can push or bypass protection. The same issue
is broader than branches: a host-wide PAT/helper may allow the shared session to access or push a
different repository on the same forge. Forge scopes cap the credential, but do not enforce the
session's intended repository or identify the human driver.

Commit environment/config is also not tamper-proof attribution. Code running in the workspace can
override `--author`, environment, or config. "Authored by owner" is a default metadata policy; the
server's `created_by`/grant audit trail is the trustworthy record of who drove a turn.

**Concrete correction:** state explicitly that EDIT delegates the selected push identity's authority.
Bind the credential helper/handoff to the exact normalized repository path, require a repo-scoped,
least-privilege credential where the forge supports it, and do not let a helper answer for arbitrary
same-host repositories. For editable shared sessions, either default to a least-privilege bot that
cannot bypass protected branches, disable push, or require an explicit owner delegation that names
the repository and warns that owner-level branch privileges may be exercised. Reframe commit identity
as a best-effort default; if verified attribution is a goal, add signed commits or an immutable
server-side action record rather than relying on `GIT_AUTHOR_*`.

This owner-authority/protected-branch consequence is a **new issue introduced by the settled v3
identity choice**.

### 4. PARTIAL / HIGH — §8.7 covers both timing paths, but consent semantics are still open

Both enforcement points are feasible. The permission endpoint checks policy before
`permission_store.grant()`, so it can reject an edit/manage create or upgrade. A new credential route
or the owner-aware launch resolver can likewise gate a credential that enters an already-shared
session before any clone delivery or fetch/push handoff occurs. Persisting the credentials actually
selected for the launch is better than inspecting current global credential rows.

The design is not complete because its central security rule remains an alternative: "only owner
approves, **or** original attach authorizes manager re-sharing." A manager-supplied boolean cannot be
owner consent. A bare `confirm_git_credential_sharing: true` is also vulnerable to stale or overly
broad confirmation unless it is tied to the exposure being approved. Host IDs alone do not distinguish
owner vs bot selection, credential replacement/scope change, repository, grantee set, or grant-level
upgrade.

**Concrete correction:** choose one rule for P1. The safest is a persisted, owner-authenticated consent
record keyed to session, owner, credential slot/version (or bot identity), canonical host + repository,
and the edit/manage exposure being approved. Have the server return an exposure fingerprint/challenge;
the owner confirms that exact fingerprint. A manager may propose a grant but cannot satisfy owner
consent. In both the grant and late-resolution paths, reject before mutation/launch/handoff when the
record is absent or stale. Invalidate/reconfirm on a newly selected host/slot, owner↔bot switch,
credential privilege change, new grantee exposure outside the approved scope, or grant upgrade.
Headless clients use the same challenge/confirmation fields.

This unresolved owner-consent alternative and the overly broad bare-confirm field are **new remaining
issues in v3's sharing addition**.

## Final blocker scorecard

| Prior BLOCKER | v3 disposition | Why |
|---|---|---|
| §8.4 exec managed-clone secret transport | **RESOLVED** | Secret delivery is now a launcher capability outside `ClonePlan` and shell strings; exec and Kubernetes models are both feasible. |
| §8.5 managed fetch/push credential path | **RESOLVED as the no-path blocker; section remains PARTIAL/HIGH** | V3 requires a second authenticated P1 handoff, but must replace the TTL/binding/trusted-process TODO with a concrete confidential protocol. |

## Net-new issues in the reviewed sections

- **HIGH — §8.5:** an authenticated tunnel is not by itself a confidential secret transport, and
  "in-memory handle" does not choose token-bearing frame vs redeemable broker handle.
- **HIGH — §8.6:** every EDIT driver can exercise the owner/bot credential's forge authority,
  including protected-branch bypass if that identity has it; a host-wide helper may also escape the
  intended repository.
- **MEDIUM — §8.6:** commit author variables are mutable defaults, not verified attribution.
- **HIGH — §8.7:** owner consent is not settled, and a bare confirm boolean is not bound to the
  credential/repository/grantee exposure being approved.
- **MEDIUM — §8.4:** the current Kubernetes init container receives the shared harness Secret;
  P1 must explicitly remove that projection and reconcile clone Secrets orphaned by process death.

## Bottom line

V3 fairly fixes the two earlier architectural blockers. §8.4 is sound with explicit Kubernetes
isolation/cleanup acceptance criteria. §8.5-§8.7 still need three security decisions before P1 is
implementation-ready: a confidential and fully bound handoff protocol, a least-privilege rule for
shared EDIT push authority, and one concrete owner-consent record/challenge model enforced before both
grant mutation and late credential delivery.
