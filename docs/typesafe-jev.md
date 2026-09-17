# TypeSafe (Jev) MVP — omni-viper

Plan for the TypeSafe / Jev gated SRE agent on **this** Omni cluster.
Target namespace is **`kagent`**, not `ate-system`.

Package origin: `typesafe-kagent-mvp` (MCP tool `triage_incident` →
`api.typesafe.ai` System One, model `jev-latest`).

## Vault → cluster Secret

Path names and key *names* only. Never commit the API key.

| Item | Value |
| --- | --- |
| Vault path | `secret/lab/typesafe` |
| Vault keys | `api_key` or `TYPESAFE_API_KEY` |
| Kubernetes Secret | `typesafe-api-key` in namespace `kagent` |

Wire the Secret (ESO ExternalSecret or out-of-band `kubectl create
secret`) **when the cluster is Ready**. Do not invent a token.

If the existing `viper-eso` policy only reads `secret/data/lab/viper/*`,
extend the policy or place a copy under `secret/lab/viper/typesafe`
out-of-band before the ExternalSecret will sync.

## Intended resources (ns `kagent`)

| Kind | Name |
| --- | --- |
| Deployment / Service | `typesafe-jev-mcp` |
| RemoteMCPServer | `typesafe-jev` |
| AgentTemplate | `typesafe-gated-sre` |

Apply these manifests after the Secret exists and kagent + Agent
Substrate are healthy ([kagent-substrate.md](kagent-substrate.md)).

## Status (as of docs write)

- Key is in Vault at `secret/lab/typesafe`.
- Cluster apply may still be pending if the node was offline.
- **Action:** wire Secret `typesafe-api-key` + apply manifests when
  cluster Ready.

## UI

kagent UI: https://172.16.10.178:30501/ (preferred; lab self-signed
HTTPS). HTTP `:30500` still works but is not a secure context. Use the
TypeSafe / Jev agent once the AgentTemplate is applied. Trust notes:
[kagent-substrate.md](kagent-substrate.md). Homepage card:
[lab-homepage.md](lab-homepage.md).
