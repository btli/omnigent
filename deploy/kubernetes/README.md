# Omnigent on Kubernetes

A reference deployment for running Omnigent on a Kubernetes cluster, in two parts:

- **`server/`** — the Omnigent **server** (the meta-harness hub) as a `Deployment`.
  This is straightforward: per the OSS entrypoint the server runs in
  *external-runners-only* mode (it accepts runner connections at
  `/v1/runner/tunnel` and never spawns harness subprocesses itself), so the pod
  is **unprivileged**.
- **`host/`** — a **stopgap** that runs the `omnigent host` daemon as a
  `Deployment` *on the cluster*, so cluster nodes become agent compute. See the
  gap below.

> Extracted and generalized from a working homelab K3s deployment. Everything
> here is environment-agnostic with placeholders — adapt the namespace, image
> tag, domain, Postgres, auth, storage class, and Ingress to your cluster.

## The gap this addresses (`omnigent-ai/omnigent#39`)

Omnigent already has **managed sandboxes** with a pluggable `sandbox.provider`
(see [`deploy/README.md`](../README.md#run-hosts-in-cloud-sandboxes)) — the
server can provision a sandbox per session and run the agent there. The shipped
providers are **lakebox / Modal / Daytona**. What's missing is a **Kubernetes
sandbox provider**: there's no `sandbox.provider: kubernetes` that spawns runner
Pods on your own cluster. So "run the agents as Pods on my K8s cluster" isn't a
config flag today — that one provider is the gap.

The proper fix is a **server-side Kubernetes sandbox provider** that spawns
runner Pods on demand (via the existing launch-token seam, exactly like the
Modal/Daytona providers do). Until that lands, `host/` is a pragmatic
workaround: it runs the **`omnigent host` daemon** in a pod, which dials the
server's `/v1/runner/tunnel` (**outbound only**) and spawns Claude Code / Codex
runners locally on that node. One Deployment per node you want as compute.

This deployment is offered as a starting point for that work — it proves the
server runs cleanly on K8s and demonstrates the host-on-cluster pattern that a
native Kubernetes provider would replace.

## Architecture

```
  Browser ──HTTP──▶ omnigent server (Deployment, unprivileged)
                       │   external-runners-only
                       │   accepts host/runner WS at /v1/runner/tunnel ◀──┐
                       ├─ Postgres (DATABASE_URL)                         │  outbound only
                       └─ artifact PVC (/data)                           │
                                                                          │
                                            omnigent host (Deployment, host/)
                                            runs the prebaked host image +
                                            spawns Claude/Codex runners locally
```

## Quickstart

Default auth is the built-in **`accounts`** provider (multi-user,
username/password, no external IdP; first boot auto-creates an admin — password
in the pod logs). Prefer your own IdP? See the opt-in OIDC block in
`server/deployment.yaml`.

```bash
# 1. Server
kubectl apply -f server/namespace.yaml
# edit server/secret.example.yaml (database-url + accounts-cookie-secret) -> apply as a real Secret
kubectl apply -f server/secret.example.yaml      # or your sealed-secrets/SOPS/ESO equivalent
kubectl apply -f server/configmap.yaml
kubectl apply -f server/pvc.yaml
kubectl apply -f server/deployment.yaml
kubectl apply -f server/service.yaml
kubectl apply -f server/ingress.example.yaml     # adapt to your ingress controller

# first-boot admin password (accounts mode):
kubectl -n omnigent logs deploy/omnigent | grep -A4 "Created initial admin"

# 2. (optional, stopgap) agent compute on the cluster — see host/README.md
kubectl apply -f host/secret.example.yaml        # the host's session + harness tokens
kubectl apply -f host/host.yaml
```

## Notes that bit us (worth knowing)

- **Image is `linux/amd64`-only** → `nodeSelector: kubernetes.io/arch: amd64`.
  The host image can't run on arm64 either (`omnigent==0.1.0` →
  `cel-expr-python` has no aarch64-linux wheel).
- **Alembic migrations run in the entrypoint** — no migration Job/initContainer.
- **`DATABASE_URL`** can be any Postgres; the entrypoint normalizes
  `postgresql://` → `postgresql+psycopg://`.
- **Auth (default = accounts):** set `OMNIGENT_AUTH_ENABLED=1` +
  `OMNIGENT_ACCOUNTS_COOKIE_SECRET` + `OMNIGENT_ACCOUNTS_BASE_URL` (the public
  URL). First boot creates an admin with a random password (in the logs + on the
  PVC at `/data/admin-credentials`); pin it with
  `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` for headless deploys.
- **OIDC (opt-in):** Omnigent rejects logins where `email_verified != true`.
  Some IdPs (e.g. Authentik's default OpenID `email` scope mapping) emit
  `email_verified: false` — make your IdP assert `true`.
- **Session TTL**: a connecting *host* authenticates with the JWT from
  `omnigent login`; the default 8h expiry breaks a long-lived host on reconnect.
  Raise `OMNIGENT_ACCOUNTS_SESSION_TTL_HOURS` (or `OMNIGENT_OIDC_SESSION_TTL_HOURS`
  under OIDC) — e.g. 720 = 30d — for unattended hosts.

See `server/deployment.yaml` and `host/host.yaml` for inline detail.
