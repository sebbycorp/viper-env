# External Secrets Operator + Vault

Installs via the Argo CD Helm Application `apps/external-secrets.yaml`
(chart `external-secrets` 2.9.0). This folder is the second source: Vault
`ClusterSecretStore` template and a non-sensitive demo `ExternalSecret`.

## Vault (out-of-band)

Nothing in git authenticates to Vault. Create the Kubernetes auth role and
KV data **on the Vault server**, not in this repo.

| Item | Value |
| --- | --- |
| Server | `https://vault.maniak.lab:8200` (fallback overlay: `http://172.16.10.111:8200`) |
| KV mount | `secret` (KV v2; API path `secret/data/lab/viper/...`) |
| Auth | Kubernetes auth, mount `kubernetes` |
| Role | `viper-eso` — must exist in Vault; rename the ClusterSecretStore field if yours differs |
| Service account | `external-secrets` in namespace `external-secrets` (Helm default) |
| Demo path | `secret/lab/viper/demo` (ESO remote key `lab/viper/demo`) |

Suggested Vault policy (run on Vault, not committed as a token):

```hcl
path "secret/data/lab/viper/*" {
  capabilities = ["read"]
}
path "secret/metadata/lab/viper/*" {
  capabilities = ["read", "list"]
}
```

Create the **non-sensitive** demo key out-of-band:

```bash
vault kv put secret/lab/viper/demo demo=hello-omni-viper
```

Bind Vault Kubernetes auth to the ESO service account (adjust host/CA for
how Vault reaches the `omni-viper` API). Grant that reviewer
`system:auth-delegator` if Vault uses TokenReview.

If `vault.maniak.lab` uses a private CA, create ConfigMap `vault-ca` in
`external-secrets` out-of-band and uncomment `caProvider` in
`cluster-secret-store.yaml`. Do not commit the CA file.

## Fallback server URL

`clusters/omni-viper/patches/vault-server-ip.yaml` switches the store to
`http://172.16.10.111:8200`. See `clusters/omni-viper/README.md`.

## Adding real secrets

1. Write the secret in Vault: `secret/lab/viper/<app>/...`
2. Add an ExternalSecret that references `ClusterSecretStore/vault-lab`
3. Never paste the value into YAML, Helm values, or GitHub Actions
