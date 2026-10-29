# Lab documentation

Operator notes for **omni-viper** (this GitOps repo). These files are
documentation only — they are not synced by Argo CD.

**Never commit secrets.** Vault path names, Kubernetes object names, and
placeholders such as `<REPLACE_ME>` are allowed. Tokens, API keys,
kubeconfigs, and license material are not.

| Doc | What it covers |
| --- | --- |
| [lab-homepage.md](lab-homepage.md) | Lab Homepage LXC (gethomepage) on iceman |
| [agentic-stack.md](agentic-stack.md) | kagent + agentgateway Enterprise via Argo CD: charts, Vault paths, cutover |
| [kagent-substrate.md](kagent-substrate.md) | OSS kagent + Agent Substrate on this Omni cluster (HTTPS UI `:30501`) |
| [typesafe-jev.md](typesafe-jev.md) | TypeSafe / Jev MCP + gated SRE agent (plan) |
| [fortigate-agent-omni.md](fortigate-agent-omni.md) | FortiGate agent plan adapted from k8s-viper |

Cluster facts and the GitOps / Vault security model live in the
[root README](../README.md) and [clusters/omni-viper](../clusters/omni-viper/README.md).
