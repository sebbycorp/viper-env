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
| Placeholders such as `<REPLACE_ME>` | `.env`, `*.pem`, `*.key` |
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

Vault URL: `https://vault.maniak.lab:8200` (IP fallback
`http://172.16.10.111:8200`). Auth method: Kubernetes. KV prefix for this
cluster: `secret/lab/viper/`.

If you are about to `git add` a file that could unlock the cluster, stop.
`.gitignore` excludes common credential patterns. PRs also run gitleaks.

## Layout

```
bootstrap/                 # Root Application (app-of-apps) → apps/
apps/
  applicationset.yaml      # Future apps/* folders
  argocd.yaml              # Application tracking apps/argocd
  external-secrets.yaml    # Helm Application + git source
  argocd/                  # Placeholder for later Argo CD self-management
  external-secrets/        # ClusterSecretStore + demo ExternalSecret
clusters/omni-viper/       # Cluster notes and optional overlays
.github/workflows/         # gitleaks on pull requests (no workflow secrets)
```

## Prerequisites (out-of-band)

Complete these **outside git**. Do not commit the outputs.

1. **Argo CD** running on `omni-viper` (already true for this lab).
2. **Private repo credentials** registered in Argo CD (HTTPS token or SSH
   deploy key). Example — run locally only:

   ```bash
   argocd repo add https://github.com/sebbycorp/viper-oss-env.git \
     --name viper-oss-env \
     --username <REPLACE_ME> \
     --password <REPLACE_ME>
   ```

3. **Vault** reachable from the cluster, Kubernetes auth enabled, role
   `viper-eso` bound to ServiceAccount `external-secrets` in namespace
   `external-secrets`, with read on `secret/data/lab/viper/*`.
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
- `Application/external-secrets` — Helm chart 2.9.0 plus Vault store/demo
- `ApplicationSet/omni-viper-apps` — auto-Applications for new `apps/*`
  folders other than `argocd` and `external-secrets`

Watch:

```bash
kubectl -n argocd get applications,applicationsets
```

The demo Kubernetes Secret `vault-demo` in `external-secrets` appears after
ESO is Ready **and** the Vault path exists. If Vault DNS is missing, use the
overlay documented in `clusters/omni-viper/README.md`.

## Adding applications

1. Add `apps/<name>/` with a `kustomization.yaml`.
2. Merge to the tracked branch. The ApplicationSet creates the Application.
3. For Helm or multi-source apps, add an explicit Application CR under `apps/`
   (same pattern as `external-secrets.yaml`) and exclude that directory in
   `applicationset.yaml`.

Need a secret? Put it in Vault at `secret/lab/viper/<name>/...`, then add an
ExternalSecret. Do not add a `kind: Secret` with real `data` / `stringData`.

## Never commit

- `.env` files, kubeconfigs, `*.pem` / `*.key`, license keys
- Vault tokens, GitHub PATs, Omni/Sidero credentials
- Decoded TLS material or copied `argocd-secret` / `vault-token` YAML

Scan before you push:

```bash
gitleaks detect --source . --verbose --redact
```
