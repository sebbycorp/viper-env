# Lab Homepage

The lab Homepage is a **gethomepage** dashboard on Proxmox LXC **143**
(hostname `homepage`), not a Kubernetes workload and **not** GitOps-synced
from this repo.

| Fact | Value |
| --- | --- |
| URL | http://172.16.10.126:3000 |
| Guest | Alpine LXC 143 on node **iceman** (`172.16.10.5`) |
| Resources | 512 MB RAM, Alpine + gethomepage |
| GitOps | None — config lives on the LXC |

Use it as the operator landing page for Omni, Argo CD, kagent, Vault, and
the AI gateways (Enterprise agentgateway on omni-viper and the OSS LXCs).

## Cards / services

Cards on this dashboard (as of 2026-09-16). IPs below are **LAN
identifiers only** — not credentials.

| Card | Where / what |
| --- | --- |
| agentgateway Enterprise | http://172.16.10.178:30400/age/ — Solo UI (tracing + cost) on omni-viper; proxy :30800 (`/openai`, `/spark`, `/mcp/fortigate`) |
| OSS gateway — agw-oss | LXC 140 @ `172.16.10.114` (iceman) |
| OSS gateway — bifrost | LXC 141 @ `172.16.10.120` (iceman) |
| OSS gateway — litellm | LXC 142 @ `172.16.10.125` (iceman) |
| PDM | Proxmox Datacenter Manager (homepage card; LAN URL not specified in this brief) |
| iceman | Proxmox node `172.16.10.5` — target host for guest migration |
| viper | Proxmox node `172.16.10.6` — source host; guests are moving off this node |
| Omni | https://maniak.na-west-1.omni.siderolabs.io/ (cluster `omni-viper`) |
| Argo CD | https://172.16.10.178:30443 and :30080 |
| kagent | http://172.16.10.178:30500 |
| FortiGate agent | Same kagent UI — pick agent `fortigate` ([plan](fortigate-agent-omni.md)) |
| TypeSafe / Jev | Same kagent UI — gated SRE / Jev path ([plan](typesafe-jev.md)) |
| Vault | `http://172.16.10.111:8200` (HTTP only; see root README) |
| FortiGate | `fw-maniak-hq` @ https://172.16.10.1 |
| self | This Homepage — http://172.16.10.126:3000 |

Do not put Vault tokens, Omni service-account tokens, or FortiGate API
tokens into Homepage widgets. If a card needs a credential, keep it on
the LXC only.

## Related OSS AI gateway LXCs

These Alpine guests sit on **iceman** next to the Homepage LXC. They are
not part of the omni-viper Argo CD app-of-apps.

| LXC | VMID | LAN |
| --- | --- | --- |
| agw-oss | 140 | `172.16.10.114` |
| bifrost | 141 | `172.16.10.120` |
| litellm | 142 | `172.16.10.125` |

Intended config home: [sebbycorp/oss-ai-gateways](https://github.com/sebbycorp/oss-ai-gateways)
(separate repo). kagent model `default-model-config` may read an OpenAI
key from Vault paths `secret/lab/oss-gateways/openai` or
`secret/lab/openai` — path names only; see [kagent-substrate.md](kagent-substrate.md).

## Host migration

Proxmox guests are moving from node **viper** (`172.16.10.6`) to
**iceman** (`172.16.10.5`). Homepage LXC 143 and the OSS gateway LXCs
already live on iceman. Cluster **omni-viper** should live on iceman
going forward.
