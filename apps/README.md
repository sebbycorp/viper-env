# Applications

Kustomize root synced by `bootstrap/root.yaml`. This directory holds:

- **Application CRs** for phase-1 apps (`argocd`, `external-secrets`,
  `external-secrets-config`)
- An **ApplicationSet** that generates Applications for future `apps/*` folders
- One subdirectory per workload

The root Application uses `apps/kustomization.yaml`. A new Application CR
file is ignored until you add it to that list.

Do not put Secret objects, license files, kubeconfigs, or `.env` files here.
Workloads that need credentials must use ExternalSecret resources that read
from Vault (`secret/lab/viper/...`).

## Phase 1 apps

| Folder / CR | How it is installed |
| --- | --- |
| `argocd/` + `argocd.yaml` | Application tracks this path (placeholder; Argo CD is already on the cluster) |
| `external-secrets.yaml` | Helm chart for the operator (sync wave 0) |
| `external-secrets/` + `external-secrets-config.yaml` | ClusterSecretStore + demo ExternalSecret (sync wave 1) |

## Adding a new app

Directory-only:

1. Create `apps/<name>/` with a `kustomization.yaml` (or plain manifests).
2. Commit. The ApplicationSet creates `Application/<name>` automatically.

Helm or multi-source:

1. Create `apps/<name>/` and `apps/<name>.yaml`.
2. Add `- <name>.yaml` to `kustomization.yaml` in this folder.
3. Exclude `apps/<name>` in `applicationset.yaml`.
