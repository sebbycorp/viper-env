# FortiGate agent — omni-viper

FortiGate MCP + SandboxAgent plan for cluster **omni-viper**. Source of
truth for manifests and image is **k8s-viper**, not this GitOps repo.

| Item | Value |
| --- | --- |
| Upstream repo | https://github.com/sebbycorp/k8s-viper |
| Manifests | `platform/kagent-ai/fortigate-*.yaml` |
| Image source | `images/fortigate-mcp/` |
| Upstream docs | `docs/fortigate-agent.md` |
| Target cluster | `omni-viper` (this repo) |
| Namespace | `kagent` |
| WorkerPool | `kagent-default` |

Do not treat the old k3s-viper Docker / Helm-0.0.9 path as the install
method here. Substrate on Omni is documented in
[kagent-substrate.md](kagent-substrate.md).

## Intended resources (ns `kagent`)

| Kind | Name |
| --- | --- |
| SandboxAgent | `fortigate` |
| RemoteMCPServer / Deployment | `fortigate-mcp` |

## FortiGate appliance

| Item | Value |
| --- | --- |
| Device | `fw-maniak-hq` |
| URL | https://172.16.10.1 |
| Vault path | `secret/platform/fortigate` |
| Vault keys | `token`, `host` |

**Do not invent tokens.** Values stay in Vault. Materialize a Kubernetes
Secret in `kagent` out-of-band or via ExternalSecret when the cluster is
Ready. The phase-1 ESO policy in this repo is scoped to
`secret/data/lab/viper/*`; reading `secret/platform/fortigate` needs a
policy extension (or a copy under `secret/lab/viper/...`) created on the
Vault server, not in git.

## Image on omni-viper

Build/push the MCP image to the **node hostNetwork registry**:

```text
127.0.0.1:5000/fortigate-mcp:dev
```

Do not rewrite that ref to the node LAN IP or a NodePort.

## UI

https://172.16.10.178:30501/ (preferred; lab self-signed HTTPS) — pick
agent **`fortigate`**. HTTP `:30500` still works but is not a secure
context. Trust notes: [kagent-substrate.md](kagent-substrate.md).

Homepage card: [lab-homepage.md](lab-homepage.md).

## Staged manifests (infra box)

Adapted manifests for this cluster **may** live on the infra workstation
at `/workspace/fortigate-omni` (not this git repo). Infra applies them
when the cluster is up. Until then, treat this page as the plan and keep
using k8s-viper as the upstream source of truth.
