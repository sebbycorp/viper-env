# viper-env

Private GitOps repository for Omni Kubernetes cluster **omni-viper**
(Talos 1.13.10 / Kubernetes 1.36.4, as of the 2026-09-16 upgrade).

Argo CD is already installed on the cluster. This repo is the desired-state
source for phase 1: app-of-apps bootstrap, Argo CD self-management path, and
External Secrets Operator talking to HashiCorp Vault.

Proxmox guests are migrating from node **viper** (`172.16.10.6`) to
**iceman** (`172.16.10.5`). **omni-viper should live on iceman** going
forward.

## Argo CD deploy key (required)

This repo is **private**. Argo CD must clone it over SSH:

```text
git@github.com:sebbycorp/viper-env.git
```

Register a **read-only** GitHub deploy key titled `argocd-omni-viper` on
[the repo deploy keys page](https://github.com/sebbycorp/viper-env/settings/keys).
Put the matching SSH **private** key only in Argo CD (repo credential Secret).
**Never commit the private key**, a kubeconfig, or a copied `argocd-secret`.

```bash
argocd repo add git@github.com:sebbycorp/viper-env.git \
  --name viper-env \
  --ssh-private-key-path <REPLACE_ME>
```

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
        │  AppRole (role-id + secret-id live only in Secret vault-approle)
        ▼
ClusterSecretStore/vault-lab
        │
        ▼
ExternalSecret  →  Kubernetes Secret (cluster only, not this repo)
```

Vault URL: `http://172.16.10.111:8200` (lab Vault speaks **HTTP only** —
HTTPS clients get `http: server gave HTTP response to HTTPS client`).
Auth method: **AppRole**. Kubernetes auth was abandoned for Omni: the
API proxy breaks TokenReview (403). KV prefix: `secret/lab/viper/`.

The AppRole credentials live in a bootstrap-only Secret `vault-approle`
in namespace `external-secrets` (keys `role-id`, `secret-id`). Create it
out-of-band. **Never commit that Secret.**

If you are about to `git add` a file that could unlock the cluster, stop.
`.gitignore` excludes common credential patterns.

## Lab status / endpoints

Operator landing page (not GitOps): http://172.16.10.126:3000 — see
[docs/lab-homepage.md](docs/lab-homepage.md).

| Surface | Endpoint | Notes |
| --- | --- | --- |
| Omni UI | https://maniak.na-west-1.omni.siderolabs.io/ | Cluster name `omni-viper` |
| Node LAN | `172.16.10.178` (`talos-cyv-ekb`) | Talos v1.13.10 / Kubernetes v1.36.4 |
| Argo CD | https://172.16.10.178:30443 and :30080 | NodePorts on the Talos node |
| kagent UI | https://viper.maniak.lab:30501/ | Preferred. Lab self-signed TLS (NodePort 30501); hosts/DNS `viper.maniak.lab` → `172.16.10.178`. HTTP `:30500` is the fallback. See [docs/kagent-substrate.md](docs/kagent-substrate.md) |
| Vault | `http://172.16.10.111:8200` | HTTP only; AppRole (see above) |
| Lab Homepage | http://172.16.10.126:3000 | Alpine LXC 143 on iceman |

| Topic | Doc |
| --- | --- |
| OSS kagent + Agent Substrate (this Omni cluster, not k3s-viper) | [docs/kagent-substrate.md](docs/kagent-substrate.md) |
| TypeSafe / Jev MVP (ns `kagent`) | [docs/typesafe-jev.md](docs/typesafe-jev.md) |
| FortiGate agent plan (upstream k8s-viper) | [docs/fortigate-agent-omni.md](docs/fortigate-agent-omni.md) |
| Homepage cards + OSS gateway LXCs | [docs/lab-homepage.md](docs/lab-homepage.md) |
| TD Bank demo traffic for the agentgateway UI (laptop tool) | [demos/td-traffic/README.md](demos/td-traffic/README.md) |
| Doc index | [docs/README.md](docs/README.md) |

Feature gates `PodCertificateRequest` and `ClusterTrustBundle` are
enabled (required for Agent Substrate). kagent talks to real ate-api at
`dns:///api.ate-system.svc:443` — the `SUBSTRATE_OPTIONAL` workaround is
gone after 1.36.4.

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
docs/                           # Lab operator docs (not synced by Argo CD)
.github/workflows/              # td-traffic release build (no workflow secrets)
```

`bootstrap/` is apply-once. After the root Application exists, Argo CD syncs
`apps/` only. Edits to `bootstrap/root.yaml` need another `kubectl apply`.

## Prerequisites (out-of-band)

Complete these **outside git**. Do not commit the outputs.

1. **Argo CD** running on `omni-viper` (already true for this lab).
2. **Deploy key** for `git@github.com:sebbycorp/viper-env.git` — see
   [Argo CD deploy key](#argo-cd-deploy-key-required). If you lock down
   AppProject `sourceRepos` (default is `*`), allow that SSH URL and
   `https://charts.external-secrets.io`.
3. **Vault** reachable from the cluster over HTTP, AppRole auth enabled, and
   a bootstrap-only Kubernetes Secret `vault-approle` in namespace
   `external-secrets` (keys `role-id` and `secret-id`). Never commit that
   Secret.

   ```bash
   vault policy write viper-eso - <<'HCL'
   path "secret/data/lab/viper/*" {
     capabilities = ["read"]
   }
   path "secret/metadata/lab/viper/*" {
     capabilities = ["read", "list"]
   }
   HCL

   vault write auth/approle/role/viper-eso \
     token_policies=viper-eso \
     token_ttl=1h

   kubectl -n external-secrets create secret generic vault-approle \
     --from-literal=role-id='<REPLACE_ME>' \
     --from-literal=secret-id='<REPLACE_ME>'
   ```

   If the store is not Ready: HTTP vs HTTPS → use
   `http://172.16.10.111:8200` (lab Vault is HTTP-only); 403 → AppRole
   role/policy or missing `vault-approle` keys.
4. **Demo KV object** (non-sensitive) created before the sample ExternalSecret
   will sync:

   ```bash
   vault kv put secret/lab/viper/demo message=hello-from-vault
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
ESO is Ready, Secret `vault-approle` exists, **and** Vault path
`secret/lab/viper/demo` has property `message`.

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
- Vault tokens, AppRole role-id/secret-id, GitHub PATs, Omni/Sidero credentials
- Decoded TLS material or copied `argocd-secret` / `vault-token` / `vault-approle` YAML
