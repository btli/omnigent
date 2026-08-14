# ArgoCD overlay

Deploy Omnigent with the kubernetes sandbox provider via ArgoCD. This overlay
layers sync-wave annotations onto the
[`sandbox-runners`](../sandbox-runners/README.md) overlay so ArgoCD applies
resources in dependency order:

| Wave | Resources |
|------|-----------|
| 0 | Namespaces (`omnigent`, `omnigent-sandboxes`) |
| 1 | ServiceAccounts, Role, RoleBinding |
| 2 | PVC, ConfigMaps, Secrets, Service, Ingress |
| 3 | Deployment |

ArgoCD renders Kustomize natively — no plugin or Helm chart needed.

## Quick start

1. **Edit secrets** — set real values in `base/secret.yaml` (or use
   sealed-secrets / external-secrets and skip the checked-in placeholder):

   ```bash
   DATABASE_URL: "postgresql+psycopg://user:pass@your-db-host:5432/omnigent"
   OMNIGENT_ACCOUNTS_COOKIE_SECRET: "$(openssl rand -hex 32)"
   ```

2. **Set your domain** *(optional)* — replace `omnigent.example.com` in
   `base/ingress.yaml`, or delete the Ingress if you don't need public access.

3. **Apply the ArgoCD Application:**

   ```bash
   # Edit application.yaml — set repoURL to your fork and targetRevision to
   # your branch, then apply it to the argocd namespace:
   kubectl apply -f deploy/kubernetes/overlays/argocd/application.yaml
   ```

4. **Create the harness-credentials Secret** — this holds the LLM API keys
   runner Pods need. It is deliberately not in the repo (credentials don't
   belong in Git):

   ```bash
   kubectl create secret generic omnigent-creds -n omnigent-sandboxes \
     --from-literal=ANTHROPIC_API_KEY=sk-ant-... \
     --from-literal=OPENAI_API_KEY=sk-...
   ```

   For production, manage this Secret with
   [sealed-secrets](https://github.com/bitnami-labs/sealed-secrets) or
   [external-secrets](https://external-secrets.io/) so ArgoCD can track it
   without storing plaintext credentials in Git.

## What ArgoCD does not manage

- **`omnigent-creds` Secret** (step 4 above) — created out of band. Without
  it, runner Pods stall in `CreateContainerConfigError`. See the
  [sandbox-runners README](../sandbox-runners/README.md#apply) for which keys
  to set.
- **OIDC / external-auth Secrets** — if you front the server with OIDC, create
  the provider Secret separately (see the
  [base README](../../README.md#use-your-own-idp-instead-oidc--optional)).

## Customizing

Fork the repo and edit the source files directly — ArgoCD picks up changes on
the next sync. Common adjustments:

- **Sandbox config** — `../sandbox-runners/sandbox-config.yaml` (namespace,
  image, node selector, resource limits, PVC mounts).
- **Server resources** — `../../base/deployment.yaml`.
- **Ingress** — `../../base/ingress.yaml` (hostname, TLS, annotations).
- **In-cluster Postgres** — compose with the `../postgres/` overlay by adding
  it as an extra resource in `kustomization.yaml`.

## ApplicationSet (multi-environment)

For staging/production splits, use an ArgoCD
[ApplicationSet](https://argo-cd.readthedocs.io/en/stable/operator-manual/applicationset/)
with a list generator. Point each entry at a different `targetRevision` (branch)
or fork the overlay directory per environment with its own config values.
