# Applications

Kustomize root synced by `bootstrap/root.yaml`. This directory holds:

- **Application CRs** for phase-1 apps (`argocd`, `external-secrets`)
- An **ApplicationSet** that generates Applications for future `apps/*` folders
- One subdirectory per workload

Do not put Secret objects, license files, kubeconfigs, or `.env` files here.
Workloads that need credentials must use ExternalSecret resources that read
from Vault (`secret/lab/viper/...`).

## Phase 1 apps

| Folder | How it is installed |
| --- | --- |
| `argocd/` | Application `argocd` tracks this path (placeholder; Argo CD is already on the cluster) |
| `external-secrets/` | Application `external-secrets` installs the Helm chart, then this path (ClusterSecretStore + demo ExternalSecret) |

## Adding a new app

1. Create `apps/<name>/` with a `kustomization.yaml` (or plain manifests).
2. Commit. The ApplicationSet creates `Application/<name>` automatically.
3. If the app needs a Helm chart or extra sources, add an explicit Application
   CR next to `argocd.yaml` / `external-secrets.yaml` and exclude that folder
   from the ApplicationSet (see `applicationset.yaml`).
