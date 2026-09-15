# Bootstrap (app-of-apps)

Apply **only** `root.yaml` (or this directory) against the in-cluster Argo CD.
The root Application then syncs `apps/`, which owns child Applications and the
ApplicationSet.

## Apply once

Argo CD is already installed on `omni-viper`. Register this **private** repo
out-of-band first (token or SSH deploy key — never commit it):

```bash
# Example — run locally. Replace placeholders. Do not commit the result.
argocd repo add https://github.com/sebbycorp/viper-oss-env.git \
  --name viper-oss-env \
  --username <REPLACE_ME> \
  --password <REPLACE_ME>
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
