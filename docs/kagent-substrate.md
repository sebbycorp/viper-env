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
| Argo CD | https://172.16.10.178:30443 and http://172.16.10.178:30080 |
| kagent UI | http://172.16.10.178:30500 |
| Host placement | Should live on Proxmox **iceman** (`172.16.10.5`); guests are migrating off **viper** (`172.16.10.6`) |

### Namespaces

| Namespace | Role |
| --- | --- |
| `kagent` | kagent controller, agents, MCP servers, WorkerPool |
| `ate-system` | Agent Substrate (ATE) |

Kubeconfigs and Omni service-account tokens stay on the operator
workstation. **Never commit them.**

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

## Models (kagent)

| ModelConfig | Provider | Notes |
| --- | --- | --- |
| `default-model-config` | OpenAI (e.g. `gpt-4.1-mini`) | API key from Vault — path names only: `secret/lab/oss-gateways/openai` or `secret/lab/openai` |
| `spark-qwen` | OpenAI-compatible | Base `http://172.16.10.173:8000/v1`, model id `unsloth/Qwen3.8-27B-NVFP4` (DGX Spark) |

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
