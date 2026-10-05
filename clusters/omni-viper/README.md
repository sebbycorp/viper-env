# Cluster: omni-viper

Phase-1 notes and optional overlays for the Omni-managed Kubernetes cluster.

| Fact | Value |
| --- | --- |
| Cluster name | `omni-viper` |
| Omni UI | https://maniak.na-west-1.omni.siderolabs.io/ |
| Kubernetes | 1.36.4 (upgrade 2026-09-16) |
| OS / node image | Talos 1.13.10 |
| Node | `talos-cyv-ekb` @ `172.16.10.178` |
| Host placement | Should live on Proxmox **iceman** (`172.16.10.5`); guests migrating off **viper** (`172.16.10.6`) |
| Feature gates | `PodCertificateRequest` + `ClusterTrustBundle` (Agent Substrate) |
| GitOps | Argo CD (already installed; NodePorts `:30443` / `:30080`) |
| kagent UI | https://viper.maniak.lab:30501/ (preferred). HTTP `:30500` is the fallback. |
| Secret backend | HashiCorp Vault + External Secrets Operator |
| Vault URL | `http://172.16.10.111:8200` (HTTP only) |
| Vault auth | AppRole (`vault-approle` Secret, created out-of-band) |
| Vault KV prefix | `secret/data/lab/viper/` (KV v2 mount `secret`) |
| This repo | Private. Argo CD uses read-only deploy key `argocd-omni-viper` (SSH private key stays out of git). |

Lab operator docs (kagent, Homepage, agent plans): [docs/](../../docs/README.md).

Kubernetes auth was abandoned for Omni (TokenReview 403 via the API proxy).
Do not point the store at HTTPS or kubernetes auth.

## Layout

| Path | Purpose |
| --- | --- |
| `cluster-info.yaml` | Non-secret cluster metadata ConfigMap |
| `patches/vault-server-ip.yaml` | Optional ClusterSecretStore server override (HTTP lab Vault) |

This directory is **not** synced by the phase-1 ApplicationSet. Use it as
documentation and as a kustomize overlay when you need cluster-specific
patches.

## Using the Vault IP overlay

The base `ClusterSecretStore` already uses `http://172.16.10.111:8200` and
AppRole. This overlay restates that HTTP URL if you sync this directory.

```bash
# Review only — do not apply until ESO CRDs exist
kubectl kustomize clusters/omni-viper
```

To make Argo CD use this overlay instead of `apps/external-secrets` as the
git source, change `spec.source.path` on `Application/external-secrets-config`
to `clusters/omni-viper`. That overlay includes the ESO config bases plus
the IP patch.

## Do not store here

Kubeconfigs, Omni service-account tokens, Vault tokens, AppRole
role-id/secret-id, Solo/agentgateway licenses, or any `.env` / `*.pem` /
`*.key` files.
