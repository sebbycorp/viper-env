# Bootstrap (app-of-apps)

Apply **only** this directory once against the in-cluster Argo CD. The root
Application then syncs `apps/`. Later edits to `root.yaml` are not
auto-synced — re-apply if you change them.

## Apply once

Argo CD is already installed on `omni-viper`. Register this **private** repo
with the read-only deploy key titled `argocd-omni-viper` (SSH private key
stays in Argo CD, never in git):

```bash
# Example — run locally. Replace the path. Do not commit the key.
argocd repo add git@github.com:sebbycorp/viper-env.git \
  --name viper-env \
  --ssh-private-key-path <REPLACE_ME>
```

Then:

```bash
kubectl apply -k bootstrap/
```

That creates `Application/omni-viper-root` in `argocd`. From there Argo CD
pulls `apps/` and creates the child Applications.

## What lives here

| File | Purpose |
| --- | --- |
| `root.yaml` | App-of-apps; source path `apps/` |
| `kustomization.yaml` | Applies the root Application |

Child Application CRs live under `apps/` so the root Application can manage them.
