# Agentic stack: kagent Enterprise + agentgateway Enterprise (Argo CD)

Argo CD installs and configures kagent Enterprise, Agent Substrate and Solo
Enterprise for agentgateway on omni-viper. Earlier these were Helm releases
installed by hand, with values kept in `docs/`. The values now live in
[values/](../values/), and each chart is an Application under [apps/](../apps/).

```
apps/ (root Application omni-viper-root)
   wave -5  helm-repos.yaml               Argo CD OCI repo entries (public, no creds)
   wave -3  gateway-api-crds              kubernetes-sigs/gateway-api v1.6.0 (standard)
   wave -2  agentic-platform              apps/agentic-platform: namespaces + ExternalSecrets
   wave -1  enterprise-agentgateway-crds  kagent-crds  substrate-crds
   wave  0  substrate-podcert  substrate  enterprise-agentgateway  management  kagent-enterprise
   (ApplicationSet)  agentgateway-system  apps/agentgateway-system (Gateways, backends, routes)
                     kagent               apps/kagent (Harnesses, WorkerPools, Agents)
```

## Versions

| Component | Chart (`us-docker.pkg.dev/solo-public/...`) | Version | Release → namespace | Values |
| --- | --- | --- | --- | --- |
| agentgateway CRDs | `enterprise-agentgateway/charts/enterprise-agentgateway-crds` | v2026.10.0 | `enterprise-agentgateway-crds` → `agentgateway-system` | — |
| agentgateway control plane | `enterprise-agentgateway/charts/enterprise-agentgateway` | v2026.10.0 | `enterprise-agentgateway` → `agentgateway-system` | [enterprise-agentgateway.yaml](../values/enterprise-agentgateway.yaml) |
| Solo Enterprise UI | `solo-enterprise-helm/charts/management` | 0.5.9 | `management` → `agentgateway-system` | [management.yaml](../values/management.yaml) |
| kagent CRDs | `kagent-enterprise-helm/charts/kagent-enterprise-crds` | 1.0.0-alpha10 | `kagent-crds` → `kagent` | — |
| kagent | `kagent-enterprise-helm/charts/kagent-enterprise` | 1.0.0-alpha10 | `kagent` → `kagent` | [kagent-enterprise.yaml](../values/kagent-enterprise.yaml) |
| Substrate CRDs / podcert / core | `enterprise-substrate-helm/substrate{-crds,-podcert,}` | 0.5.0-alpha3-e492046 | `substrate*` → `ate-system` / `podcertificate-controller-system` | [substrate.yaml](../values/substrate.yaml) |

These are the versions running on omni-viper today. Each `releaseName`
matches its existing Helm release, so Argo CD renders the same object names
and label selectors and takes the live objects over without recreating them.

Newer versions were published on 2026-10-22: agentgateway v2026.10.1, kagent
1.0.0-alpha11, management 0.5.12 and substrate 0.6.0-alpha3. To upgrade, bump
`targetRevision` in `apps/<name>.yaml` in a PR. Keep kagent and Substrate in
lockstep, and check the harness runtime pairing in
[kagent-substrate.md](kagent-substrate.md).

## Endpoints

| Surface | URL | Defined in |
| --- | --- | --- |
| agentgateway proxy | `http://172.16.10.178:30800` (`/openai`, `/spark`, `/mcp/fortigate`, …) | `apps/agentgateway-system/gateway.yaml` |
| Bedrock proxy | `http://172.16.10.178:30801` | `apps/agentgateway-system/bedrock-gateway.yaml` |
| Solo Enterprise UI | `http://172.16.10.178:30400` | `apps/agentgateway-system/ui-nodeport.yaml` |
| kagent UI | `http://172.16.10.178:30500` (HTTPS `:30501` via nginx `kagent-ui-tls`, not in git) | `apps/kagent/ui-nodeport.yaml` |
| DGX Spark (vLLM) | `dgx-spark.kagent.svc:8000` → `172.16.10.173:8000` | `apps/kagent/dgx-spark.yaml` |

Neither chart can pin a nodePort, so the chart Services are `ClusterIP` and
separate NodePort Services hold the fixed ports.

## Secrets

Every chart runs with `licensing.createSecret: false`. ESO writes each
Secret from Vault through `ClusterSecretStore/vault-lab`. All paths sit under
`secret/lab/viper/`, which the `viper-eso` policy already covers.

| Vault (KV v2) | Properties | Kubernetes Secret(s) |
| --- | --- | --- |
| `secret/lab/viper/licenses` | `kagent` | `kagent/enterprise-kagent-license` (`enterprise-kagent-license-key`) |
| | `agentgateway` | `agentgateway-system/enterprise-agentgateway-license`, `ate-system/enterprise-agentgateway-license` (`enterprise-agentgateway-license-key`) |
| | `management` | `agentgateway-system/management-license` (`license-key`) |
| `secret/lab/viper/postgres` | `kagent`, `substrate_owner`, `substrate_readwrite` | `kagent/kagent-postgres`, `ate-system/substrate-postgres-{owner,readwrite}` |
| `secret/lab/viper/models` | `openai_authorization`, `spark_api_key` | `agentgateway-system/openai-secret`, `kagent/kagent-spark-openai` |
| `secret/lab/bedrock` | `BEDROCK_API_KEY` | `agentgateway-system/bedrock-secret` |

Copy each value from the live Secret so nothing changes:

```bash
export VAULT_ADDR=http://172.16.10.111:8200
get() { kubectl -n "$1" get secret "$2" -o jsonpath="{.data.$3}" | base64 -d; }

vault kv put secret/lab/viper/licenses \
  kagent="$(get kagent enterprise-kagent-license enterprise-kagent-license-key)" \
  agentgateway="$(get agentgateway-system enterprise-agentgateway-license enterprise-agentgateway-license-key)" \
  management="$(get agentgateway-system management-license license-key)"

vault kv put secret/lab/viper/postgres \
  kagent="$(get kagent kagent-postgres connectionString)" \
  substrate_owner="$(get ate-system substrate-postgres-owner ownerConnectionString)" \
  substrate_readwrite="$(get ate-system substrate-postgres-readwrite readWriteConnectionString)"

vault kv put secret/lab/viper/models \
  openai_authorization="$(get agentgateway-system openai-secret Authorization)" \
  spark_api_key="$(get kagent kagent-spark-openai OPENAI_API_KEY)"
```

## Not managed by Argo CD

- **Postgres**: StatefulSet `postgres/postgres`. Its volumeClaimTemplates are
  immutable, so Argo CD does not adopt it.
- **`ate-system/postgres-ca-pool`**: create it once with `kubectl ate admin make-ca-pool`.
- **cert-manager**, the lab CA and the nginx `kagent-ui-tls` front (NodePort 30501).
- **Talos feature gates** `PodCertificateRequest` and `ClusterTrustBundle`, set in Omni.

## Moving off the hand-installed Helm releases

Merging to `main` is the deploy, because the root Application auto-syncs. In order:

1. **Before merge**: run the `vault kv put` commands above. If you merge
   first, ESO cannot create the license Secrets and the controllers fail.
2. **Before merge**: check the installed Gateway API bundle version, to make
   sure v1.6.0 is not a downgrade:
   `kubectl get crd gateways.gateway.networking.k8s.io -o jsonpath='{.metadata.annotations.gateway\.networking\.k8s\.io/bundle-version}'`
3. **Merge.** Then watch: `kubectl -n argocd get applications`.
4. **One-time patch**: the management chart hardcodes `maxSurge: 1`, and the
   node cannot fit two UI pods. Argo CD ignores `/spec/strategy` on that
   Deployment, so patch it once:
   `kubectl -n agentgateway-system patch deploy solo-enterprise-ui -p '{"spec":{"strategy":{"rollingUpdate":{"maxSurge":0,"maxUnavailable":1}}}}'`
5. **Clean up**: after every Application is Synced/Healthy, delete the Helm
   release records (`kubectl -n <ns> delete secret -l owner=helm,name=<release>`)
   so nobody runs `helm upgrade` against Argo-owned objects. **Do not run
   `helm uninstall`**, because it would delete the live objects.

Sync waves only order when Applications are created. Argo CD does not wait for
a child Application to be healthy without an Application health check in
`argocd-cm`. Each Application therefore retries with backoff and uses
`SkipDryRunOnMissingResource`. The `*-crds` Applications run with
`prune: false`, because deleting a CRD deletes every object of that kind.

## Making changes

- **Chart values**: edit `values/<chart>.yaml`.
- **Chart version**: edit `targetRevision` in `apps/<name>.yaml`.
- **Gateway and agent config**: edit `apps/agentgateway-system/` and `apps/kagent/`.
- **Check locally**: `./scripts/validate.sh` runs `kustomize build` on every
  folder and `helm template` on every chart. CI runs it on PRs.
