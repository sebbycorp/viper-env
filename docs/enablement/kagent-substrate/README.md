# Enablement: building agents on kagent Enterprise + Agent Substrate

A quick (about 5–7 minute) live demo, plus the reference snippets behind it. Everything here
was run on **omni-viper** on 2026-11-03: kagent Enterprise **1.0.0-alpha12**,
Agent Substrate Enterprise **0.6.0-b15382d4**, API `api.kagent.dev/v1alpha3`.

```
docs/enablement/kagent-substrate/
├── README.md               ← this guide (talk track + snippets)
├── demo.sh                 ← prep | (demo) | extras | cleanup
├── agents_as_code.py       ← create agents from Python (uv run)
├── substrate-map.html      ← interactive map: build wizard, live config per component (open in a browser)
└── manifests/
    ├── 01-models.yaml          3 ModelConfigs: Spark direct, OpenAI + Bedrock via agentgateway
    ├── 02-agenttemplate.yaml   behaviour: prompt + model + MCP tools
    ├── 03-tier.yaml            runtime: WorkerPool (Substrate) + Harness (kagent)
    ├── 04-agent.yaml           Agent = template + harness
    └── 05-agent-inline.yaml    Agent with an inline template (Claude on Bedrock)
```

**Interactive map:** open [`substrate-map.html`](substrate-map.html) in a browser (offline copy). It has a 9-step build
wizard with minimal and all-options YAML, every component with its live config from omni-viper, and animated request
paths. Hosted version: https://claude.ai/artifact/TFK7vSXE8RtGbADN6AxKM8 (private to the owner).

## The mental model (one slide)

```
                ┌──────────────── kagent (what) ─────────────────┐   ┌──── Substrate (where) ────┐
 ModelConfig ──►│ AgentTemplate                                   │   │                           │
 RemoteMCPServer│  prompt · modelConfig · tools · skills · subAgents│  │ WorkerPool                │
                └───────────────┬────────────────────────────────┘   │  N pre-warmed gVisor/     │
                                │                                     │  microVM sandbox workers  │
                         Agent  ├── template.ref | template.inline    │                           │
                                └── harnessRef ──► Harness ───────────┼─► workerPoolRef           │
                                                   runtime (kagent |  │   snapshotPolicy (S3)     │
                                                   codex | claude |   └───────────────────────────┘
                                                   byo) + image
```

| Object | API | Answers | Owned by |
| --- | --- | --- | --- |
| **ModelConfig** | `api.kagent.dev/v1alpha3` | Which LLM, which URL, which key? | platform team |
| **AgentTemplate** | `api.kagent.dev/v1alpha3` | What does the agent do? (prompt, model, tools) | agent author |
| **Harness** | `api.kagent.dev/v1alpha3` | Which runtime runs it, on which pool, with snapshots where? | platform team |
| **WorkerPool** | `ate.dev/v1alpha1` | How many sandboxed workers, what isolation (gVisor/microVM)? | platform team |
| **Agent** | `api.kagent.dev/v1alpha3` | Pair *one* template with *one* harness. | agent author |

Key talking points:
- **Separation of concerns.** Authors write templates. The platform team owns harnesses and pools.
  Moving an agent to another tier is a one-field change (`spec.harnessRef`).
- **Golden snapshot.** When an Agent is applied, Substrate boots the runtime once and
  snapshots it. Every chat session forks from that snapshot, so cold starts are fast and every session is
  isolated in its own sandbox. Sessions are checkpointed to `snapshotPolicy.location` and restored when
  the user comes back.
- **A tier is a Harness plus its own WorkerPool.** Substrate has no priority queue. This
  cluster runs `default` (3 workers), `high` (2 reserved, PriorityClass), `coding` (Codex runtime)
  and, for the demo, `demo`.
- **Any change creates a new revision.** Editing a template or model creates a new golden snapshot.
  Existing sessions stay on their revision.

## Run the demo

### Prerequisites

| Need | Check |
| --- | --- |
| `kubectl`, `omnictl` ≥ 1.12, [`uv`](https://docs.astral.sh/uv/) (for the Python step; it installs `kubernetes` itself) | `uv --version` |
| Omni login (a browser opens if the key has expired) | `omnictl --omniconfig ~/.config/omni/config kubeconfig -c omni-viper ~/.kube/omni-viper` |
| Cluster objects the demo reuses (already live) | Secret `kagent/kagent-spark-openai`, Service `kagent/dgx-spark`, RemoteMCPServer `kagent-tool-server`, Gateways `agentgateway-proxy` + `bedrock-proxy` (routes `/openai`, `/claude-haiku-4-5`), Harness `high` |
| Models up | `curl -s http://172.16.10.173:8000/v1/models` (Spark) · `curl -s http://172.16.10.59:30800/openai/v1/chat/completions -H 'content-type: application/json' -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'` |
| UIs reachable | kagent https://viper.maniak.lab:30501/agents (fallback `http://172.16.10.59:30500`) · Solo UI http://172.16.10.59:30400/age/ |

```bash
export KUBECONFIG=~/.kube/omni-viper
cd docs/enablement/kagent-substrate
```

### Commands

```bash
./demo.sh prep      # BEFORE the session (~6s): 3 ModelConfigs + WorkerPool kagent-demo + Harness demo
./demo.sh           # LIVE (~5-7 min with talking, ~40s of actual waiting). Enter advances
./demo.sh extras    # optional: inline Claude agent + move it to the high tier
./demo.sh cleanup   # deletes everything labelled demo=substrate-enablement (+ the Python agents)
```

`NONINTERACTIVE=1 ./demo.sh` skips the pauses for a dry run. Set `KAGENT_UI=http://172.16.10.59:30500`
if the HTTPS hostname doesn't resolve on the presenting laptop.

### Before you go on stage

1. `./demo.sh cleanup && ./demo.sh prep`: start from a clean state.
2. Open three tabs: the terminal, the kagent UI **Agents** page, and the Solo UI (`/age/`).
3. **Close old chat tabs.** Each open session holds a worker; with both `kagent-demo` workers busy, the
   model swap in step 4 waits up to about 100s instead of about 10s.
4. Do a 10-second dry run of the models (the curl commands above).

### Presenter script

| Step | On screen | Say | Expected | Time |
| --- | --- | --- | --- | --- |
| 0 | `kubectl get workerpools` + Harness `demo` spec | "The platform team pre-stages tiers: a WorkerPool of sandboxed gVisor workers and a Harness that picks the runtime image and the pool." | 4 pools Ready (`kagent-coding/default/demo/high`) | 30s |
| 1 | ModelConfigs table (name, model, baseUrl) | "Same API for all three. Spark is called directly. OpenAI and Bedrock go through agentgateway, which holds the real keys and meters every call." | `demo-spark-qwen`, `demo-openai-gpt-4o-mini`, `demo-bedrock-claude-haiku` | 1 min |
| 2 | `02-agenttemplate.yaml` + `04-agent.yaml`, apply | "The template is the behaviour: prompt, model and an allow-list of MCP tools. The Agent pairs it with a harness. Substrate boots it once and takes a golden snapshot." | `agent/demo-k8s-helper Ready - golden snapshot … after ~12-15s`, 5 conditions `True` | 1 min |
| 3 | kagent UI → `demo-k8s-helper` → New chat | "Each chat forks the snapshot into its own sandbox. The tool call runs inside it." Ask: *How many pods are running in ate-system?* | A `k8s_get_resources` call, then a table of ate-system pods | 1-2 min |
| 4 | In that chat: snapshot button next to Send ("Take a snapshot") → rename the snapshot `ate-system-baseline` → **Fork** | "A snapshot is a saved turn boundary: the sandbox runtime goes to S3 (rustfs), not just the chat text. Fork it to branch the conversation." Ask the fork *What did I ask before and what did you find?* | The fork is a new chat id and answers from the restored state; the snapshot appears on the Snapshots page | 1 min |
| 5 | Patch the template's model to OpenAI | "Change anything and you get a new revision. Running sessions keep theirs." Chat again in a **new** chat, then show the call in the Solo UI. | New snapshot hash after ~10-15s, `gpt-4o-mini` request in the Solo UI | 1 min |
| 6 | `uv run agents_as_code.py` | "They're just CRDs, so CI, portals or operators can create agents. Here's one per model from Python." | `created`/`ready` for `code-helper-{spark,openai,claude}` in ~10s | 30s |

Extras (if there's time): **A**: `05-agent-inline.yaml` puts an Agent with an inline template on Bedrock Claude.
**B**: patching `harnessRef: high` moves it to the reserved high-priority tier and creates a new snapshot in ~10s.
**C**: scheduled agents (see below): press **Run** on a schedule and open the commit it pushes to GitHub.

### Tested on omni-viper (2026-11-03)

| Run | Result |
| --- | --- |
| `prep` → `live` → `extras` → `cleanup`, clean state | All exit 0. prep 6s, live 38s (no pauses), extras 24s, cleanup 5s. Cluster back to its original 4 agents / 3 pools |
| Golden snapshot, new Agent | 12-15s |
| Model swap / tier move (new revision) | 9-12s with a free worker. **~100s** when the only free worker was held by an open chat |
| UI chat, Spark agent | `k8s_get_resources` on ate-system → pod table |
| UI chat, Bedrock Claude agent | `k8s_get_resources` on WorkerPools → table incl. `kagent-demo` |
| UI chat after swap to OpenAI | `k8s_get_events` call; agentgateway logged `gen_ai.request.model=gpt-4o-mini` |

### Known limitations

- **Chat is UI-only in this demo.** alpha12 serves chat through the UI's `/a2a` proxy (controller port 8085
  with auto-auth). The `kagent invoke` CLI (OSS 0.10.x) and plain curl to the controller don't work against it.
- **The Spark/Qwen agent may refuse to name its model** ("I can't share details about the underlying model").
  Show the model in the chat's *Conversation details* panel instead.
- **Some snippets below weren't run in the demo.** Native providers, skills, sub-agents, compaction and memory are
  checked only against `kubectl explain` on alpha12.
- **The demo objects live in the `kagent` namespace but outside Argo CD** (applied by hand, labelled
  `demo=substrate-enablement`). Always run `./demo.sh cleanup` afterwards.

---

## 1. Adding models (`ModelConfig`)

On this cluster every model speaks the OpenAI Chat Completions API to kagent
(`provider: OpenAI` + `openAI.baseUrl`). What differs is what sits behind the URL.

### A. Self-hosted, direct (DGX Spark / vLLM)

```yaml
apiVersion: api.kagent.dev/v1alpha3
kind: ModelConfig
metadata: {name: demo-spark-qwen, namespace: kagent}
spec:
  provider: OpenAI
  model: qwen3.8-flash-next
  stream: true
  apiKeySecret: kagent-spark-openai      # vLLM ignores it; kagent requires a Secret
  apiKeySecretKey: OPENAI_API_KEY
  openAI:
    apiFormat: chatCompletions
    baseUrl: http://dgx-spark.kagent.svc.cluster.local:8000/v1
```

> **Gotcha:** a bare IP in `baseUrl` is rejected ("credential injection requires an exact DNS
> hostname"). Front the box with a selectorless Service + EndpointSlice
> (`apps/kagent/dgx-spark.yaml`).

### B. SaaS model through agentgateway (OpenAI)

```yaml
spec:
  provider: OpenAI
  model: gpt-4o-mini
  apiKeySecret: kagent-spark-openai      # placeholder - the gateway injects the real key
  apiKeySecretKey: OPENAI_API_KEY
  openAI:
    apiFormat: chatCompletions           # "responses" for the Codex harness
    baseUrl: http://agentgateway-proxy.agentgateway-system.svc.cluster.local:8080/openai/v1
```

The agent never holds the provider key. The gateway's `AgentgatewayBackend` does
(`policies.auth.secretRef: openai-secret`), and every call is metered (tokens, cost, traces).

### C. Bedrock Claude through agentgateway

```yaml
spec:
  provider: OpenAI
  model: claude-haiku-4-5                # informational: the route pins the Bedrock model ID
  apiKeySecret: kagent-spark-openai
  apiKeySecretKey: OPENAI_API_KEY
  openAI:
    apiFormat: chatCompletions
    baseUrl: http://bedrock-proxy.agentgateway-system.svc.cluster.local:8080/claude-haiku-4-5
```

The gateway translates Chat Completions to Bedrock (`us.anthropic.claude-haiku-4-5-20251001-v1:0`,
us-east-2) and signs with `bedrock-secret`.

<details><summary>Native providers (no gateway) - fields from <code>kubectl explain modelconfig.spec</code></summary>

`provider` enum: `Anthropic, OpenAI, AzureOpenAI, Ollama, Gemini, GeminiVertexAI,
AnthropicVertexAI, Bedrock, Mistral, Foundry, SAPAICore` - each with its own block, e.g.

```yaml
spec:
  provider: Anthropic
  model: claude-sonnet-5-5
  apiKeySecret: anthropic-key
  apiKeySecretKey: ANTHROPIC_API_KEY
  anthropic: {maxTokens: 4096, promptCaching: true, cacheTTL: 5m}
---
spec:
  provider: Bedrock
  model: us.anthropic.claude-haiku-4-5-20251001-v1:0
  bedrock: {region: us-east-2, guardrail: {identifier: abc123, version: "1"}}
```
Not used in this demo. The lab routes SaaS models through agentgateway for key custody and cost tracking.
</details>

## 2. AgentTemplate: the behaviour

```yaml
apiVersion: api.kagent.dev/v1alpha3
kind: AgentTemplate
metadata: {name: demo-k8s-helper, namespace: kagent}
spec:
  description: Read-only Kubernetes helper
  modelConfig: {name: demo-spark-qwen}
  systemPrompt: |
    You are a read-only Kubernetes helper. Always call a tool instead of guessing...
  tools:
  - mcp:
      server: {kind: RemoteMCPServer, name: kagent-tool-server}
      tools: [k8s_get_resources, k8s_describe_resource, k8s_get_events, k8s_get_pod_logs]
      # requireApproval: true   # human-in-the-loop before each call
```

Other things a template can carry (field names from `kubectl explain agenttemplate.spec` on alpha12; not exercised in the demo):

```yaml
  systemPromptFrom: {name: my-prompts, key: k8s-helper}     # prompt from a ConfigMap
  skills:                                                    # Agent Skills from git / OCI / S3
  - name: runbooks
    source: {git: {url: https://github.com/org/skills, commit: <sha>}, path: runbooks}
  tools:
  - subAgent:                                                # delegate to another template
      name: fortigate
      description: Ask the FortiGate agent about firewall state
      templateRef: {name: fortigate}
  outputSchema: {...}                                        # structured JSON output
```

## 3. Harness + WorkerPool: the runtime tier

```yaml
apiVersion: ate.dev/v1alpha1
kind: WorkerPool
metadata:
  name: kagent-demo
  namespace: kagent
  labels:
    kagent.dev/worker-pool-name: kagent-demo
    kagent.dev/worker-pool-namespace: kagent
spec:
  replicas: 2                       # >=2 so a new revision can prepare while the old one serves
  sandboxClasses:                   # Substrate 0.6 (0.5 used spec.sandboxClass)
  - {name: gvisor, configRef: {name: gvisor-default}}       # or microvm
  workerImage: us-docker.pkg.dev/solo-public/substrate-enterprise/ateworker-gvisor:v0.6.0-b15382d4
  template:
    serviceAccountName: kagent-default
    # priorityClassName: substrate-high   # see the "high" tier
---
apiVersion: api.kagent.dev/v1alpha3
kind: Harness
metadata: {name: demo, namespace: kagent}
spec:
  kagent: {}                        # runtime: kagent | codex | claude | byo
  substrate:
    workerPoolRef: {name: kagent-demo}
    snapshotPolicy: {location: s3://ate-snapshots/kagent/demo}
  workload:
    image: ghcr.io/kagent-dev/kagent/golang-adk:1.0.0-alpha17@sha256:d20d4117...
```

The `kagent` runtime also takes context compaction and long-term memory (fields from `kubectl explain`; not exercised in the demo):

```yaml
  kagent:
    compaction: {tokenThreshold: 60000, summarizer: {modelConfigRef: {name: demo-spark-qwen}}}
    memory: {modelConfigRef: {name: demo-spark-qwen}, ttlDays: 30}
```

> **Gotchas:**
> - The runtime image must match the controller's *runtime contract*, not its version number:
>   Enterprise alpha12 ↔ OSS `golang-adk` / `codex-harness` **1.0.0-alpha17**. Pin by digest.
> - The `workerImage` must match the Substrate control-plane version.

## 4. Agent: pair them

```yaml
apiVersion: api.kagent.dev/v1alpha3
kind: Agent
metadata: {name: demo-k8s-helper, namespace: kagent}
spec:
  harnessRef: {name: demo}
  template:
    ref: {name: demo-k8s-helper}     # or: inline: {...same fields as AgentTemplate.spec}
  # egressPolicyRefs: [{name: allow-github}]   # extra HTTP(S) egress from the sandbox
```

Status conditions to show, in order: `Accepted` → `ResolvedRefs` → `Compatible` →
`EgressPoliciesApplied` → **`Ready` ("ActorTemplate golden snapshot is ready")**. About 20s on this lab.

## 5. Agents as code

Agents are just CRDs, so any Kubernetes client can create them: CI, a portal, an operator.
[`agents_as_code.py`](agents_as_code.py) creates one Agent per model and waits for `Ready`:

```python
def agent_body(suffix, model_config):
    return {
        "apiVersion": "api.kagent.dev/v1alpha3", "kind": "Agent",
        "metadata": {"name": f"code-helper-{suffix}", "namespace": "kagent"},
        "spec": {
            "harnessRef": {"name": "demo"},
            "template": {"inline": {
                "modelConfig": {"name": model_config},
                "systemPrompt": PROMPT,
                "tools": [{"mcp": {"server": {"kind": "RemoteMCPServer", "name": "kagent-tool-server"},
                                   "tools": ["k8s_get_resources", "k8s_get_events"]}}],
            }},
        },
    }

api.create_namespaced_custom_object("api.kagent.dev", "v1alpha3", "kagent", "agents", body)
```

```bash
uv run agents_as_code.py           # create / update (dependencies resolved inline by uv)
uv run agents_as_code.py --delete
```

## Scheduled agents (Schedules + EgressPolicy + GitHub MCP)

Three read-only agents run daily (America/Toronto) and commit their report to
[sebbycorp/agent-summary](https://github.com/sebbycorp/agent-summary) under `reports/<report>/latest.md` and
`reports/<report>/YYYY/YYYY-MM-DD.md`. Manifests: `apps/kagent/agent-cve-watchers.yaml`, `apps/kagent/agent-oss-update-watcher.yaml`,
`apps/kagent/agent-summary-auditor.yaml`, `apps/kagent/github-mcp.yaml`. One agent can serve several schedules:
`oss-update-watcher` gets the org and report name from each schedule's prompt.

| Schedule | Time | Agent | Reads | May reach (EgressPolicy) |
| --- | --- | --- | --- | --- |
| `agentgateway-daily-updates` (timeout 2400s) | 08:00 | `oss-update-watcher` | github.com/agentgateway: releases, merged PRs → digest | GitHub MCP (`repos,pull_requests` toolsets) |
| `kagent-daily-updates` (timeout 2400s) | 08:20 | `oss-update-watcher` | github.com/kagent-dev (kagent, substrate, tools…) → digest | GitHub MCP |
| `fortigate-daily-resource-usage` | 09:00 | `fortigate` | `fg_resource_usage`, `fg_system_status` | fortigate-mcp (in-cluster), GitHub MCP |
| `k8s-daily-cve-check` (timeout 2400s) | 09:15 | `k8s-cve-checker` | k8s version + images → OSV.dev from the sandbox shell | `https://api.osv.dev`, GitHub MCP |
| `fortigate-daily-fortiguard-cve` (timeout 2400s) | 09:30 | `fortigate-cve-watch` | FortiOS version → FortiGuard PSIRT + Outbreak Alert RSS | `https://filestore.fortinet.com`, `https://fortiguard.fortinet.com`, GitHub MCP |
| `agent-summary-daily-audit` | 10:30 | `agent-summary-auditor` (harness `high`) | the repo itself: did every report arrive today? → `reports/_status/` | GitHub MCP |

**Adding a new schedule? Make it publish.** The agent needs the GitHub tools (add this `tools` entry to its
AgentTemplate and `egressPolicyRefs: [{name: allow-github-mcp}]` to its Agent):

```yaml
  - mcp:
      server: {kind: RemoteMCPServer, name: github-mcp}
      tools: [get_file_contents, create_or_update_file, push_files]
```

Then end the schedule prompt with this (replace `<report>`), give it a timeout with room to spare, and the 10:30 auditor
picks the new `reports/<report>/` directory up automatically:

```text
When the report is done, publish it with the GitHub tools to repository sebbycorp/agent-summary on branch main:
use push_files to write the same Markdown report to BOTH reports/<report>/latest.md and
reports/<report>/YYYY/YYYY-MM-DD.md (today's date, America/Toronto) in ONE commit with message
"<report>: YYYY-MM-DD". Start the file with "# <report> - YYYY-MM-DD" and the one-line verdict.
Do not touch any other path or repository. Finish by giving the commit link.
```

How it fits together:
- **Schedules** are created in the UI (*Schedules → New*). Each firing starts a new conversation, so the run is
  visible like any chat, with a *Run* button for an on-demand execution. They are stored by the controller, not as CRDs.
- **EgressPolicy** per agent: the sandbox can reach only the listed origins (atenet-egress enforces it). The agents use
  the runtime's built-in `bash` with BusyBox `wget` (no curl; GET and POST only).
- **GitHub MCP** (`RemoteMCPServer github-mcp`, `X-MCP-Toolsets: repos`): `Authorization` comes from Secret
  `github-agent-summary`, an ExternalSecret from Vault `secret/lab/github` (`GH_TOKEN`; policy `viper-eso` extended
  to read it). The templates allow-list only `get_file_contents`, `create_or_update_file`, `push_files`.
- **The FortiGuard outbreak page is rendered by JavaScript**, so the agent reads its RSS twin instead
  (`filestore.fortinet.com/fortiguard/rss/outbreakalert.xml`), plus PSIRT advisories (`…/rss/ir.xml`).

Gotchas found while building this:
- **Use full DNS names for MCP URLs.** Sandbox DNS has no search domains: `fortigate-mcp.kagent` failed with
  `no such host`; `fortigate-mcp.kagent.svc.cluster.local` works.
- **The controller connects to a RemoteMCPServer itself** (tool discovery). An auth header injected only at egress
  leaves it `Accepted=False … Unauthorized`; use `headersFrom`.
- **The CVE runs are long on the local model.** At the default 900s both timed out (FortiGuard right at its publish step), so both get 2400s, and the k8s prompt caps OSV lookups at about 25.
- **Big reports break `push_files` on the local model.** Qwen sends an empty `{}` call once the content gets
  large; the kagent digest then pushed a `probe.md` to test access and timed out. The watcher prompt now caps
  reports at about 8 KB, falls back to `create_or_update_file`, and forbids test files.
- **The PAT in `lab/github` is a broad classic token.** Swap it for a fine-grained PAT limited to `agent-summary`
  (Contents: read/write).

## Troubleshooting cheat sheet

| Symptom | Look at |
| --- | --- |
| Agent stuck not `Ready` | `kubectl -n kagent describe agent <a>` → then `kubectl -n ate-system logs deploy/ate-api-server` ("Failed to reconcile actor template") |
| `GoldenActorCrashed` | Harness image / controller runtime contract mismatch |
| Model calls fail with x509 / 403 | atenet-egress / pod certificates: see `docs/kagent-substrate.md` |
| Bare-IP baseUrl rejected | Use a Service DNS name |
| WorkerPool not Ready | `kubectl -n kagent get workerpool <p> -o yaml` (sandboxClasses, workerImage version) |
