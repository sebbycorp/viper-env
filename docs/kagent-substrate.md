# OSS kagent + Agent Substrate (omni-viper)

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
| Argo CD | https://172.16.10.178:30443 and :30080 |
| kagent UI (HTTPS, preferred) | https://viper.maniak.lab:30501/ |
| kagent UI (HTTP, fallback) | http://172.16.10.178:30500 |
| Host placement | Should live on Proxmox **iceman** (`172.16.10.5`); guests are migrating off **viper** (`172.16.10.6`) |

### Namespaces

| Namespace | Role |
| --- | --- |
| `kagent` | kagent controller, agents, MCP servers, WorkerPool |
| `ate-system` | Agent Substrate (ATE) |

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
context.

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
| HTTP front | Existing kagent UI Service NodePort **30500** (unchanged) |

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

## Agent Substrate

Installed **from source**, not the old `0.0.9` Helm chart used in
k8s-viper docs:

1. Clone `agent-substrate/substrate`.
2. Run `hack/install-ate.sh` and build/push images with **ko**.

| Preference | Value |
| --- | --- |
| Dataplane | `--atenet-dataplane=agentgateway` |
| Image registry | hostNetwork registry `127.0.0.1:5000` **on the node** |
| WorkerPool | `kagent-default` in namespace `kagent` |
| ate-api | `dns:///api.ate-system.svc:443` (real API) |

**Image refs:** always write `127.0.0.1:5000/<name>:<tag>`. Do **not**
put the node LAN IP or a NodePort in image references — the registry is
reached via hostNetwork on the node.

After the 1.36.4 upgrade, the kagent controller uses the real ate-api
address above. The `SUBSTRATE_OPTIONAL` workaround is **removed**.

### Priority tiers

Substrate has no priority queue: a Harness points at exactly one
WorkerPool, and AgentTemplates pick a Harness by the
`kagent.dev/harness` label. A "tier" is therefore a Harness + its own
WorkerPool. Manifests: [apps/kagent](../apps/kagent/) (Argo CD app
`kagent`).

| Tier | Harness | WorkerPool | Workers | PriorityClass | Agents |
| --- | --- | --- | --- | --- | --- |
| High | `high` | `kagent-high` | 2 (reserved) | `substrate-high` (100000, `preemptionPolicy: Never`) | `k8s-assistant-priority` |
| Low | `default` | `kagent-default` (Helm `substrateWorkerPool.replicas`) | 3 | none | everything else |
| Coding | `coding` (Codex runtime) | `kagent-coding` | 3 | none | `codex-coder` (ModelConfig `codex-gpt-5-4`) |

To move an agent to the high tier, set `kagent.dev/harness: high` on its
AgentTemplate. Existing actors of that agent were built from the old
harness's template, so treat it as a new agent. Worker pods request no
CPU, so the extra pool fits on the CPU-bound node; the PriorityClass only
decides eviction order and never preempts other pods.

The coding tier uses the `codex` Harness runtime
(`ghcr.io/kagent-dev/kagent/codex-harness:1.0.0-alpha3`, matching the
Enterprise controller; older builds fail on `unknown field "runtime_telemetry"`). Codex speaks the OpenAI Responses API, so ModelConfig
`codex-gpt-5-4` uses `apiFormat: responses` and calls the in-cluster
agentgateway over HTTP (`/openai/v1`). The beta4 egress can't do HTTPS to
api.openai.com. The `/openai` backend has `policies.ai.routes` so
`/v1/responses` isn't parsed as Chat Completions.

## Models (kagent)

| ModelConfig | Provider | Notes |
| --- | --- | --- |
| `default-model-config` | OpenAI (e.g. `gpt-4.1-mini`) | API key from Vault — path names only: `secret/lab/oss-gateways/openai` or `secret/lab/openai` |
| `spark-qwen` | OpenAI-compatible | Base `http://172.16.10.173:8000/v1`, served model `qwen3.8-flash-next` = `Mia-AiLab/Qwen3.8-Flash-Next-NVFP4` (DGX Spark; old name `unsloth/Qwen3.8-27B-NVFP4` is aliased to it) |

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
| FortiGate | `kagent`, WorkerPool `kagent-default` | [fortigate-agent-omni.md](fortigate-agent-omni.md) | Manifests sourced from k8s-viper; apply when cluster up |

## Related surfaces

- Lab Homepage cards: [lab-homepage.md](lab-homepage.md)
- GitOps / Vault model: [root README](../README.md)
- Cluster overlay notes: [clusters/omni-viper](../clusters/omni-viper/README.md)
