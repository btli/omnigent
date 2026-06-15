# On-demand runner Pods — kubernetes sandbox provider

This overlay turns the cluster itself into Omnigent's agent compute: instead of
registering a long-lived external host, the server spawns a **runner Pod on
demand** for each `host_type="managed"` session and deletes it when the session
ends. It uses the same managed launch-token seam as the Modal and Daytona
sandbox providers — no per-host browser login, credentials never enter the
sandbox.

It layers on `../../base` (the server Deployment/Service/Ingress/PVC are
unchanged) and adds only what the provider needs:

- **`rbac.yaml`** — the `omnigent-server` ServiceAccount bound to a namespaced
  Role granting exactly what the launcher calls: `pods` (create/get/delete),
  `pods/exec` (get+create), and `events` (list, for surfacing scheduler/pull
  failures) — so the in-cluster launcher can create runner Pods and exec
  `omnigent host` into them. Plus a deliberately powerless `omnigent-runner`
  ServiceAccount for the runner Pods.
- **`sandbox-config.yaml`** — a `config.yaml` with the `sandbox: provider:
  kubernetes` section, mounted at `/etc/omnigent` (the deployment patch sets
  `OMNIGENT_CONFIG` to it). The server reads the managed-sandbox backend from
  this file, not from env.
- **`runner-credentials.yaml`** — the `omnigent-creds` Secret whose keys are
  projected into every runner Pod via `envFrom` (the harness LLM/git creds).
- **`deployment-patch.yaml`** — runs the server as `omnigent-server` and mounts
  the config.

## Requirements

- **A server image built with the `kubernetes` extra.** The base image ships no
  managed-sandbox extras, so build with `--build-arg OMNIGENT_EXTRAS=kubernetes`
  (`deploy/docker/Dockerfile`) — or otherwise ensure `pip install
  'omnigent[kubernetes]'` is present in the server image — so `_ensure_sdk()`
  resolves the client at runtime.
- **amd64 nodes.** The prebaked host image is amd64-only (`cel-expr-python` has
  no aarch64 wheel), so the launcher always sets `nodeSelector:
  kubernetes.io/arch: amd64` on runner Pods. Make sure the cluster has schedulable
  amd64 nodes.
- A Postgres database for the server (as for the base deploy).

## Deploy

1. Set `DATABASE_URL` + cookie secret in `base/secret.yaml` (see the
   [base README](../../README.md#deploy-with-an-external-database)), or use the
   `postgres` overlay's DB and reference it here.
2. Edit **`runner-credentials.yaml`** — real harness credentials, drop the keys
   you don't use. (Prefer a sealed-secret / external-secrets operator in prod.)
3. Edit **`sandbox-config.yaml`** — set `server_url` (in-cluster service DNS is
   the default and usually correct), and optionally `image` / `node_selector`.
4. Apply:

   ```bash
   kubectl kustomize deploy/kubernetes/overlays/sandbox-runners/ | kubectl apply -f -
   ```

## How it works

A new chat that requests a managed sandbox triggers, server-side:

1. `provision()` creates a runner Pod (`sleep infinity` under a tiny PID-1
   reaper, `runAsUser: 1000`, writable `HOME` on an emptyDir,
   `automountServiceAccountToken: false`, harness creds via `envFrom`,
   `nodeSelector: kubernetes.io/arch: amd64`) and waits for it to be ready,
   fast-failing on `Unschedulable` / `ImagePullBackOff`.
2. The server execs `omnigent host` into the Pod (`pods/exec`); the host dials
   back over the launch-token tunnel and registers.
3. The agent runs in the Pod. On session end (or relaunch), `terminate()`
   deletes the Pod.

**Supported agent classes:** `claude-sdk` and `codex` — parity with the Modal
and Daytona providers. Terminal / native-ui agents are out of scope (they need a
`bwrap` sandbox an unprivileged Pod can't provide).

**In-cluster vs out-of-cluster.** Running in-cluster (the default here), the
launcher authenticates to the API with the `omnigent-server` ServiceAccount
token — no kubeconfig needed. To drive a cluster from a server running outside
it, set `OMNIGENT_KUBERNETES_KUBECONFIG` to a kubeconfig path instead.

## Troubleshooting

- **Session hangs / host never comes online.** Find the runner Pod
  (`kubectl get pods -n omnigent -l omnigent.ai/role=sandbox-host`,
  or watch `kubectl get pods -n omnigent -w` after starting a chat) and read the
  host log: `kubectl exec -n omnigent <pod> -- cat /tmp/omnigent-host.log`.
- **`pods "..." is forbidden`** — the server isn't running as `omnigent-server`
  or the Role/RoleBinding wasn't applied. Confirm
  `kubectl get rolebinding omnigent-sandbox-manager -n omnigent`.
- **Pod stuck `Pending` / `Unschedulable`** — no schedulable amd64 node (check
  taints / `kubectl get nodes -L kubernetes.io/arch`). The provider surfaces the
  scheduler event in the session error.
- **`ImagePullBackOff`** — the runner image isn't pullable on the amd64 nodes
  (private registry needs an imagePullSecret; set `image` to a reachable ref).
- **Agent auth failures inside the Pod** — a key is missing from
  `omnigent-creds`; the provider rejects reserved names (`HOME`, `IS_SANDBOX`).
