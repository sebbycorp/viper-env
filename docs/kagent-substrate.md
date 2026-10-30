# kagent Enterprise + Agent Substrate (omni-viper)

This is the **Omni** path for kagent and Agent Substrate on cluster
`omni-viper`. It is **not** the older k3s / Docker install documented on
[sebbycorp/k8s-viper](https://github.com/sebbycorp/k8s-viper). Use that
repo only as a cross-link for historical k3s-viper notes or for FortiGate
manifest originals ([fortigate-agent-omni.md](fortigate-agent-omni.md)).

## Cluster / access

| Fact | Value |
| --- | --- |
| Cluster | `omni-viper` |
| Omni UI | https://maniak.na-west-1.omni.siderolabs.io/ |
| Node | `talos-cyv-ekb` @ `172.16.10.178` |
| Versions | Talos **v1.13.10**, Kubernetes **v1.36.4** (upgrade 2026-09-16) |
| Feature gates | `PodCertificateRequest` + `ClusterTrustBundle` (required for Agent Substrate) |
| kagent | Solo Enterprise for kagent **1.0.0-alpha12** (2026-10-30, fresh DB schemas; was 1.0.0-alpha10, before that alpha3) |
| Agent Substrate | Substrate Enterprise **0.6.0-b15382d4** (Argo CD Application `substrate`; was 0.5.0-alpha3-e492046) |
| Argo CD | https://172.16.10.178:30443 and :30080 |
| kagent UI (HTTPS, preferred) | https://viper.maniak.lab:30501/ |
| kagent UI (HTTP, fallback) | http://172.16.10.178:30500 |
| Host placement | Should live on Proxmox **iceman** (`172.16.10.5`); guests are migrating off **viper** (`172.16.10.6`) |

### Namespaces

| Namespace | Role |
| --- | --- |
| `kagent` | kagent controller, agents, MCP servers, WorkerPools |
| `ate-system` | Agent Substrate (ATE) |
| `podcertificate-controller-system` | Substrate pod-certificate signer (`substrate-podcert`) |
| `postgres` | In-cluster Postgres for kagent + Substrate (lab grade, **no backups**) |

Kubeconfigs and Omni service-account tokens stay on the operator
workstation. **Never commit them.**

## kagent UI (HTTPS)

**Primary URL:** https://viper.maniak.lab:30501/

hosts / DNS (required for the hostname URL):

```text
172.16.10.178 viper.maniak.lab
```

Use **`viper.maniak.lab`**, not `kagent.viper.lab` or `kagent.maniak.lab`.
Certificate SANs should include DNS `viper.maniak.lab` and IP
`172.16.10.178`. Infra will re-issue the live `kagent-ui-tls` cert
separately so those names match — do not re-issue it from this repo.
After that, https://172.16.10.178:30501/ still works via the IP SAN.

This is an **intentional lab self-signed / internal CA**, not Let’s
Encrypt. The UI needs a browser **secure context** so
`crypto.randomUUID` works (for example `/schedules/new`). A
`crypto.randomUUID` polyfill on plain HTTP was a temporary workaround;
HTTPS is the proper fix.

HTTP NodePort **30500** remains as a fallback (plain HTTP; not a
secure context). Prefer HTTPS for UI features that need a secure
context. The chart cannot pin a nodePort, so the chart Service
`kagent-ui` stays ClusterIP and
[apps/kagent/ui-nodeport.yaml](../apps/kagent/ui-nodeport.yaml) pins 30500.

### Already live (do not redeploy)

These objects are already on the cluster. This page is operator docs
only — do not reinstall cert-manager or re-issue the lab CA from this
repo.

| Object | Where / what |
| --- | --- |
| cert-manager | Namespace `cert-manager` |
| ClusterIssuer | `selfsigned-cluster-issuer` |
| Lab CA Certificate | `kagent-lab-ca` |
| Lab CA ClusterIssuer | `kagent-lab-ca-issuer` |
| UI Certificate | `kagent-ui-tls` in namespace `kagent` (secret `kagent-ui-tls`). Intended SANs: DNS `viper.maniak.lab`, IP `172.16.10.178` (infra re-issues separately) |
| TLS front | Deployment `kagent-ui-tls` (nginx) + Service NodePort **30501** terminating TLS → `kagent-ui:8080` |
| HTTP front | Service `kagent-ui-nodeport` NodePort **30500** (GitOps, `apps/kagent/ui-nodeport.yaml`) |

### Trust the lab CA

Browsers warn on first visit because this is a lab CA, not a public CA.

1. **Browser exception** — accept the warning on
   https://viper.maniak.lab:30501/ (enough for most lab use).
2. **Import the lab CA** — export the public CA cert from the cluster
   and add it to the OS / browser trust store:

```bash
kubectl -n cert-manager get secret kagent-lab-ca-secret \
  -o jsonpath='{.data.ca\.crt}' | base64 -d > kagent-lab-ca.crt
```

That file is a **public CA certificate** for trust, not a private key.
Do not commit it (`.gitignore` already excludes `*.crt`). Do not export
or commit TLS private keys.

## Install (Argo CD)

kagent 1.0.0-alpha10 was a re-platform of alpha3: the API group moved from
`kagent.dev` to **`api.kagent.dev/v1alpha3`**, Postgres is no longer bundled
and Agent Substrate is no longer a subchart.

Argo CD installs the charts (CRDs, Substrate, kagent) from
[values/](../values/). The licenses and connection strings come from Vault
through ESO. See [agentic-stack.md](agentic-stack.md) for the Applications,
the Vault layout and the one-time move off the hand-installed Helm releases.
Postgres (StatefulSet `postgres/postgres`, `pgvector/pgvector:pg16`, DBs
`kagent` and `substrate`) is still created by hand and has no backups.

### Upgrading kagent (alpha10 → alpha12, 2026-10-30)

- alpha11 split the database login: the controller takes
  `kagent-postgres-owner` (migrations, `SET ROLE kagent_owner`) and
  `kagent-postgres-readwrite` (`SET ROLE kagent_readwrite`). The old
  `connectionStringSecretRef` is ignored silently. Both Secrets carry the
  `kagent_user` login, so Vault needed no new value.
- The pre-1.0 migrations are compacted baselines ("databases must be
  recreated when it changes"), so an alpha bump needs fresh `kagent` and
  `enterprise` schemas. Sessions, memory and Postgres evals are lost; CRDs are
  not. Recreate them with the two `setup.sql` scripts from
  `solo-io/enterprise-kagent` at the release tag, as `postgres`:
  `go/core/pkg/postgressetup/setup.sql` (`kagent_owner_user=kagent_user`,
  its existing password; `kagent_readwrite_user=kagent_readwrite_user`, empty
  password), then `GRANT kagent_readwrite TO kagent_user` and
  `GRANT USAGE ON SCHEMA extensions TO kagent_readwrite` (pgvector), then
  `enterprise/services/solo-enterprise/migrations/postgres/setup/setup.sql`
  (`agentevals_user=`).
- Substrate must match the Substrate version in the kagent release's `go/go.mod` (alpha12 → 0.6.0).
- The ApplicationSet app `kagent` (Harnesses, WorkerPools) has no auto-sync:
  sync it by hand after a merge.

## Agent Substrate

| Preference | Value |
| --- | --- |
| Charts | `oci://us-docker.pkg.dev/solo-public/enterprise-substrate-helm` 0.6.0-b15382d4 |
| Worker image | `us-docker.pkg.dev/solo-public/substrate-enterprise/ateworker-gvisor:v0.6.0-b15382d4` (renamed from `ateom-gvisor` in 0.6) |
| WorkerPools | `kagent-default` (chart), `kagent-high`, `kagent-coding` (Argo) in namespace `kagent` |
| ate-api | `dns:///api.ate-system.svc:443` |

The old from-source install (`hack/install-ate.sh`, ko images on the
node registry `127.0.0.1:5000`) is retired; do not reference
`127.0.0.1:5000` images any more.

### Agents, Harnesses and tiers

In alpha10 an **Agent** (`api.kagent.dev/v1alpha3`) pairs one Harness
(`spec.harnessRef`) with one AgentTemplate (`spec.template.ref`). The
alpha3 `kagent.dev/harness` label and `Harness.spec.allowedAgentTemplates`
are gone. Substrate has no priority queue: a Harness points at exactly
one WorkerPool, so a "tier" is a Harness + its own WorkerPool. Manifests:
[apps/kagent](../apps/kagent/) (Argo CD app `kagent`) and
[apps/remarkable-diary/k8s-assistant.yaml](../apps/remarkable-diary/k8s-assistant.yaml).

| Tier | Harness | WorkerPool | Workers | PriorityClass | Agents |
| --- | --- | --- | --- | --- | --- |
| High | `high` | `kagent-high` | 2 (reserved) | `substrate-high` (100000, `preemptionPolicy: Never`) | `k8s-assistant-priority` |
| Low | `default` | `kagent-default` (chart `substrateWorkerPool.replicas`) | 3 | none | `k8s-assistant`, `fortigate` |
| Coding | `coding` (Codex runtime) | `kagent-coding` | 3 | none | `codex-coder` (ModelConfig `codex-gpt-5-4`) |

To move an agent to another tier, change `spec.harnessRef` on its Agent.
Worker pods request no CPU, so the extra pools fit on the CPU-bound node;
the PriorityClass only decides eviction order and never preempts other pods.

Harness runtime images must match the controller's runtime contract, not
its version number. kagent Enterprise 1.0.0-alpha12 pairs with OSS
**1.0.0-alpha17** runtimes (`ghcr.io/kagent-dev/kagent/golang-adk:1.0.0-alpha17`
and `ghcr.io/kagent-dev/kagent/codex-harness:1.0.0-alpha17`, by digest; the
release's `release-manifest.json` lists them). The
OSS alpha10-12 runtimes still require `KAGENT_API_URL` and crash before the
golden snapshot (`GoldenActorCrashed`). Codex speaks the OpenAI
Responses API, so ModelConfig `codex-gpt-5-4` uses `apiFormat: responses`
and calls the in-cluster agentgateway over HTTP (`/openai/v1`). The
`/openai` backend has `policies.ai.routes` so `/v1/responses` isn't parsed
as Chat Completions.

## Models (kagent)

| ModelConfig | Provider | Notes |
| --- | --- | --- |
| `default-model-config` | OpenAI-compatible (chart `providers.openAI`) | Spark: Service `kagent/dgx-spark` → `http://172.16.10.173:8000/v1`, served model `qwen3.8-flash-next` = `Mia-AiLab/Qwen3.8-Flash-Next-NVFP4` (DGX Spark) |
| `codex-gpt-5-4` | OpenAI `gpt-5.4` via agentgateway | Gateway injects the real OpenAI key (Vault `secret/lab/oss-gateways/openai` or `secret/lab/openai`) |

Wire keys with ExternalSecret → Kubernetes Secret. Do not paste key
values into this repo. Example Vault *path* (not a value):

```text
secret/lab/oss-gateways/openai
secret/lab/openai
```

If the ESO AppRole policy is scoped to `secret/data/lab/viper/*` only,
either copy a reference object under `secret/lab/viper/...` or extend
the policy out-of-band. Do not commit AppRole ids.

## Agent plans on this cluster

| Agent | Namespace | Docs | Status (docs write) |
| --- | --- | --- | --- |
| TypeSafe / Jev | `kagent` (not `ate-system`) | [typesafe-jev.md](typesafe-jev.md) | Vault key present; apply when cluster Ready |
| FortiGate | `kagent`, Harness `default` (WorkerPool `kagent-default`) | [fortigate-agent-omni.md](fortigate-agent-omni.md) | Applied by hand as `api.kagent.dev/v1alpha3` RemoteMCPServer `fortigate-mcp` + AgentTemplate/Agent `fortigate` (not in Argo) |

## Related surfaces

- Lab Homepage cards: [lab-homepage.md](lab-homepage.md)
- GitOps / Vault model: [root README](../README.md)
- Cluster overlay notes: [clusters/omni-viper](../clusters/omni-viper/README.md)
