# External Secrets Operator + Vault

The operator is installed by `apps/external-secrets.yaml` (Helm chart
`external-secrets` 2.9.0, sync wave 0). This folder is synced by
`apps/external-secrets-config.yaml` (wave 1): Vault `ClusterSecretStore`
and a non-sensitive demo `ExternalSecret`.

## Vault (out-of-band)

Nothing in git authenticates to Vault. Create the Kubernetes auth role and
KV data **on the Vault server**, not in this repo.

| Item | Value |
| --- | --- |
| Server | `https://vault.maniak.lab:8200` |
| KV mount | `secret` (KV v2; API path `secret/data/lab/viper/...`) |
| Auth | Kubernetes auth, mount `kubernetes` |
| Role | `viper-eso` — must exist in Vault; rename the ClusterSecretStore field if yours differs |
| Service account | `external-secrets` in namespace `external-secrets` (pinned in the Helm Application) |
| Demo path | `secret/lab/viper/demo` (ESO remote key `lab/viper/demo`) |

Create the policy and role on Vault (no tokens committed):

```bash
vault policy write viper-eso - <<'HCL'
path "secret/data/lab/viper/*" {
  capabilities = ["read"]
}
path "secret/metadata/lab/viper/*" {
  capabilities = ["read", "list"]
}
HCL

vault write auth/kubernetes/role/viper-eso \
  bound_service_account_names=external-secrets \
  bound_service_account_namespaces=external-secrets \
  policies=viper-eso \
  ttl=1h
```

Vault 1.21+ may require `bound_audiences=vault` on the role and
`audiences: [vault]` on the ClusterSecretStore (field is commented there).

Grant the reviewer `system:auth-delegator` if Vault uses TokenReview.
Adjust `kubernetes_host` / CA for how Vault reaches the `omni-viper` API.

Create the **non-sensitive** demo key out-of-band:

```bash
vault kv put secret/lab/viper/demo demo=hello-omni-viper
```

If `vault.maniak.lab` uses a private CA, create ConfigMap `vault-ca` in
`external-secrets` out-of-band and uncomment `caProvider` in
`cluster-secret-store.yaml`. Do not commit the CA file.

### Store not Ready

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| TLS / x509 | Private lab CA | HTTPS + out-of-band `caProvider` ConfigMap |
| Dial / DNS | Name not resolvable | Fix DNS; avoid HTTP if you can |
| 403 | Role or policy | Check `viper-eso` bindings and policy |
| Audience | Vault 1.21+ | Set matching audiences on role and store |

## Fallback server URL (lab only)

`clusters/omni-viper/patches/vault-server-ip.yaml` switches the store to
`http://172.16.10.111:8200`. That sends the Kubernetes ServiceAccount JWT
to Vault in **cleartext**. Prefer HTTPS + `caProvider`. See
`clusters/omni-viper/README.md`.

## Adding real secrets

1. Write the secret in Vault: `secret/lab/viper/<app>/...`
2. Add an ExternalSecret that references `ClusterSecretStore/vault-lab`
3. Never paste the value into YAML, Helm values, or GitHub Actions
