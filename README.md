# viper-oss-env

Private GitOps repository for Omni Kubernetes cluster **omni-viper**
(Kubernetes 1.34 / Talos 1.11).

Argo CD is already installed on the cluster. This repo is the desired-state
source for phase 1: app-of-apps bootstrap, Argo CD self-management path, and
External Secrets Operator talking to HashiCorp Vault.

## Security model

**This repository must never contain secrets.**

| Allowed in git | Never in git |
| --- | --- |
| Kubernetes / Argo CD manifests | API keys, tokens, passwords |
| Helm values without credentials | Vault tokens, AppRole secret-ids |
| ExternalSecret *references* (path + property names) | kubeconfigs, `admin.conf` |
| Placeholders such as `<REPLACE_ME>` | `.env`, `*.pem`, `*.key`, SSH **private** keys |
| Vault *paths* (`secret/lab/viper/...`) | Solo / agentgateway license keys |
| | Anything that grants access to the lab |

Secrets live in **HashiCorp Vault** and are materialized into the cluster by
**External Secrets Operator** (ESO):

```
Vault KV v2  secret/data/lab/viper/...
        │
        │  Kubernetes auth (ServiceAccount, no static token in git)
        ▼
ClusterSecretStore/vault-lab
        │
        ▼
ExternalSecret  →  Kubernetes Secret (cluster only, not this repo)
```

Vault URL: `https://vault.maniak.lab:8200`. Prefer HTTPS. The IP fallback
`http://172.16.10.111:8200` is lab-only and sends the Kubernetes auth JWT
in cleartext — use HTTPS plus an out-of-band `caProvider` ConfigMap instead
when you can. Auth method: Kubernetes. KV prefix: `secret/lab/viper/`.

If you are about to `git add` a file that could unlock the cluster, stop.
`.gitignore` excludes common credential patterns. PRs also run gitleaks.

## Layout

```
bootstrap/                      # Apply-once root Application → apps/
apps/
  kustomization.yaml            # Must list every Application CR in this folder
  applicationset.yaml           # Future apps/* folders
  argocd.yaml                   # Application tracking apps/argocd
  external-secrets.yaml         # Helm Application (operator)
  external-secrets-config.yaml  # Vault ClusterSecretStore + demo ExternalSecret
  argocd/                       # Placeholder for later Argo CD self-management
  external-secrets/             # ClusterSecretStore + demo ExternalSecret
clusters/omni-viper/            # Cluster notes and optional overlays
.github/workflows/              # gitleaks on pull requests (no workflow secrets)
```

`bootstrap/` is apply-once. After the root Application exists, Argo CD syncs
`apps/` only. Edits to `bootstrap/root.yaml` need another `kubectl apply`.

## Prerequisites (out-of-band)

Complete these **outside git**. Do not commit the outputs.

1. **Argo CD** running on `omni-viper` (already true for this lab).
2. **Read-only GitHub deploy key** titled `argocd-omni-viper` on this private
   repo. Keep the SSH **private** key only in the Argo CD repo Secret on the
   cluster — never in git.

   ```bash
   # Run locally against the cluster. The private key file stays on disk / in
   # Argo CD, not in this repository.
   argocd repo add git@github.com:sebbycorp/viper-oss-env.git \
     --name viper-oss-env \
     --ssh-private-key-path <REPLACE_ME>
   ```

   If you lock down AppProject `sourceRepos` (default is `*`), allow both
   `git@github.com:sebbycorp/viper-oss-env.git` and
   `https://charts.external-secrets.io`.
3. **Vault** reachable from the cluster, Kubernetes auth enabled, and role
   `viper-eso` bound to ServiceAccount `external-secrets` in namespace
   `external-secrets`:

   ```bash
   vault policy write viper-eso - <<'HCL'
   path "secret/data/lab/viper/*" {
     capabilities = ["read"]
   }
   path "secret/metadata/lab/viper/*" {
     capabilities = ["read", "list"]
   }
   HCL

   vault write auth/kubernetes/role/viper-eso \
     bound_service_account_names=external-secrets \
     bound_service_account_namespaces=external-secrets \
     policies=viper-eso \
     ttl=1h
   ```

   Vault 1.21+ may also need `bound_audiences=vault` on that role and
   `audiences: [vault]` on the ClusterSecretStore (commented in
   `apps/external-secrets/cluster-secret-store.yaml`). Grant the reviewer
   ServiceAccount `system:auth-delegator` if Vault uses TokenReview.

   If the store is not Ready: DNS/TLS → HTTPS + `caProvider` (not HTTP);
   403 → role/policy; audience error → uncomment `audiences`.
4. **Demo KV object** (non-sensitive) created before the sample ExternalSecret
   will sync:

   ```bash
   vault kv put secret/lab/viper/demo demo=hello-omni-viper
   ```

## Bootstrap

From a workstation that already has cluster admin (kubeconfig stays local):

```bash
kubectl apply -k bootstrap/
```

This creates `Application/omni-viper-root` in namespace `argocd`. Argo CD
then syncs `apps/`, which creates:

- `Application/argocd` — tracks `apps/argocd` (placeholder ConfigMap)
- `Application/external-secrets` — Helm chart 2.9.0 (wave 0)
- `Application/external-secrets-config` — Vault store + demo ExternalSecret (wave 1)
- `ApplicationSet/omni-viper-apps` — auto-Applications for new `apps/*`
  folders other than `argocd` and `external-secrets`

Watch:

```bash
kubectl -n argocd get applications,applicationsets
```

The demo Kubernetes Secret `vault-demo` in `external-secrets` appears after
ESO is Ready **and** the Vault path exists. If Vault DNS is missing, prefer
HTTPS + `caProvider`. The HTTP IP overlay is documented in
`clusters/omni-viper/README.md` as a last resort.

## Adding applications

Directory-only apps (plain manifests / kustomize):

1. Add `apps/<name>/` with a `kustomization.yaml`.
2. Merge to the tracked branch. The ApplicationSet creates the Application.

Helm or multi-source apps (same pattern as External Secrets):

1. Add `apps/<name>/` for the workload manifests.
2. Add `apps/<name>.yaml` (the Application CR).
3. **List that file in `apps/kustomization.yaml`** — the root Application
   only syncs resources named there.
4. Exclude `apps/<name>` in `applicationset.yaml` so the ApplicationSet does
   not create a second Application.

Need a secret? Put it in Vault at `secret/lab/viper/<name>/...`, then add an
ExternalSecret. Do not add a `kind: Secret` with real `data` / `stringData`.

## Never commit

- `.env` files, kubeconfigs, `*.pem` / `*.key`, SSH private keys, license keys
- Vault tokens, GitHub PATs, Omni/Sidero credentials
- Decoded TLS material or copied `argocd-secret` / `vault-token` YAML

Scan before you push:

```bash
gitleaks detect --source . --verbose --redact
```
