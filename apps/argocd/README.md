# Argo CD (self-management placeholder)

Argo CD is **already installed** on cluster `omni-viper`. This folder is tracked
by `Application/argocd` so future Helm values or kustomize patches can live
here without a second bootstrap.

## Current contents

A ConfigMap placeholder only. It does not replace or upgrade the live install.

## Enabling self-management later

1. Export or recreate Helm values that match the **existing** Omni/Argo CD
   install. Store values in this folder (no secrets).
2. Change `apps/argocd.yaml` to a multi-source Application:

   - Helm: `https://argoproj.github.io/argo-helm` chart `argo-cd`
   - Git: this path for value files / patches
3. Diff against live objects before turning on automated sync.
4. Never commit admin passwords, Dex client secrets, repo tokens, or
   `argocd-secret` data. Those belong in Vault (`secret/lab/viper/...`) and
   ExternalSecret resources.

Do not add `manifests/install.yaml` from upstream until you intend to take
over the existing release.
