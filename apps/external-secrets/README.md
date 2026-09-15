# External Secrets Operator + Vault

The operator is installed by `apps/external-secrets.yaml` (Helm chart
`external-secrets` 2.9.0, sync wave 0). This folder is synced by
`apps/external-secrets-config.yaml` (wave 1): Vault `ClusterSecretStore`
and a non-sensitive demo `ExternalSecret`.

## Vault (out-of-band)

Kubernetes auth was abandoned for Omni: the API proxy breaks Vault
TokenReview (403). Use **AppRole**. Create the AppRole, the bootstrap-only
Secret `vault-approle`, and KV data **on the Vault server / cluster**, not
in this repo. Never commit `role-id` or `secret-id`.

| Item | Value |
| --- | --- |
| Server | `http://172.16.10.111:8200` (HTTP only) |
| KV mount | `secret` (KV v2; API path `secret/data/lab/viper/...`) |
| Auth | AppRole, mount `approle` |
| Credentials | Secret `vault-approle` in namespace `external-secrets` (keys `role-id`, `secret-id`) |
| Demo path | `secret/lab/viper/demo` (ESO remote key `lab/viper/demo`, property `message`) |

Create the policy and AppRole on Vault (no credentials committed):

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

Create the **non-sensitive** demo key out-of-band:

```bash
vault kv put secret/lab/viper/demo message=hello-from-vault
```

### Store not Ready

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| `HTTP response to HTTPS client` | Lab Vault is HTTP-only | Use `http://172.16.10.111:8200` |
| TokenReview 403 | Kubernetes auth via Omni proxy | Keep AppRole; do not switch back |
| 403 / invalid role | AppRole or policy | Check Vault AppRole and `viper-eso` policy |
| Missing credentials | `vault-approle` not created | Create the Secret out-of-band |

## Lab server URL

`ClusterSecretStore/vault-lab` already defaults to
`http://172.16.10.111:8200`. The optional overlay
`clusters/omni-viper/patches/vault-server-ip.yaml` keeps that same HTTP
URL if you sync `clusters/omni-viper`.

## Adding real secrets

1. Write the secret in Vault: `secret/lab/viper/<app>/...`
2. Add an ExternalSecret that references `ClusterSecretStore/vault-lab`
3. Never paste the value into YAML, Helm values, or GitHub Actions
