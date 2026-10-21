# TD demo traffic for the Solo Enterprise UI — design

Date: 2026-10-21
Status: draft for review

## Intent

Sebastian is demoing Solo Enterprise for agentgateway to TD, clicking live through the
Solo UI (management chart 0.5.9) on omni-viper. Today the UI is nearly empty. The goal is
for it to look like TD has rolled agentgateway out across the bank: dashboards, cost
management, budgets, traces and request graphs full of believable `@td.com` usage, so TD
can picture who is using AI, through which tools, on which models, and what it costs per
team.

`ui.frontend.enableMockUI` was ruled out. It replaces live data with generic
`globex`/`hooli` fixtures on most pages, mixes real and fake data on others, and turns off
UI auth.

## Goals

- **Organization:** TD (`td.com`) with **15 teams**, **12 projects** and **35 users**
  (`firstname.lastname@td.com`, plausible names).
- **Client tool per request:** Claude Code, GitHub Copilot or Claude Desktop, shown as a
  breakdown in cost management.
- **Models, by traffic share:** mostly **Azure AI Foundry** (GPT-family), then **Bedrock
  Claude Opus**, then **Mistral** (Mistral Large).
- **Real per-token prices:** spend uses the providers' published rates, so the dollar
  figures look like what TD would actually pay.
- **Budgets:** one per team. Most well under the limit, a couple near it, one or two over.
  The over-limit ones use `Audit` mode so nothing is blocked during the demo.
- **History:** **7 days** by default, adjustable with a flag (for example 30). Graphs look
  lived-in from the moment the demo starts.
- **Live traffic:** while the script runs, new requests keep arriving through the real
  gateway, so the UI moves during the demo and traces can be opened.
- **Portable:** checked into viper-env and runnable **from a laptop** against any kube
  context that has enterprise agentgateway and the Solo UI installed. omni-viper is the
  first target.
- **Clean removal:** one command removes everything it created.

## Non-goals

- Real provider calls. No requests reach Azure, AWS or Mistral, and nothing is billed.
- Exercising auth flows, rate limiting or guardrails beyond what makes the data look right.
- Becoming part of the Argo CD-managed app set.

## Anti-goals (failure even if it "works")

- Breaking or hiding Sebastian's real omni-viper traffic or config: the Spark, OpenAI and
  Bedrock routes, `desk`, tracing, and the existing cost catalog entries.
- Numbers that look fake to a bank audience: uniform traffic, round dollar amounts, every
  user identical, no weekends or nights, zero errors.
- Anything that blocks requests mid-demo, such as a budget in `Block` mode or a broken route.
- Leaving drift that Argo CD or a later `helm upgrade` silently reverts mid-demo.

## What we know about the platform (verified on omni-viper)

- **Data path:** everything the UI shows for agentgateway traffic comes from ClickHouse
  `platformdb.otel_traces_json`.
  - Materialized views fan it out into `agw_spans_typed`, `agw_chat_spans`,
    `agw_cost_dimensions_5m`, `agw_cost_model_5m`, `agw_trace_request_counts_5m` and the
    rest.
  - Rows have no row-level TTL; only the Events and Links columns expire after 7 days.
  - So back-dated spans sent through the telemetry collector, or inserted into that table,
    populate every graph.
- **Span shape:** a real gateway LLM span (ServiceName `agentgateway-proxy`, ScopeName
  `agentgateway`) carries:
  - `agw.ai.usage.cost.{input,output,total,...}` as decimal strings
  - `agw.ai.budgets.{applied,count}`
  - `agw.llm.custom_dimensions`, a JSON list of `[dimension, value]` pairs such as
    `[["model","..."],["provider","openai"]]`
  - resource attributes `cluster_name`, `k8s.*` and `service.*`

  Copy a live span as the template for backfill.
- **Cost dimensions:** the `agentgateway-enterprise-budget-dimensions` ConfigMap holds CEL
  expressions per dimension.
  - It is owned by the `enterprise-agentgateway` Helm release, value `budgetDimensions.config`.
  - Today it has a Group → User hierarchy plus a Virtual Key attribute.
  - `model` and `provider` are built in.
  - The UI's own reference config uses `apiKey.organization`, `apiKey.costCenter`,
    `apiKey.group` and `apiKey.user`, which are metadata on virtual API keys.
  - The chart notes that explicit Gateway attributes override these defaults.
- **Budgets:** CRD `EnterpriseAgentgatewayBudget`.
  - Fields: `spec.budgets[]` with `name`, `subject` (a map of dimension → value, up to 16
    keys), `limit` {amount, unit `USD`|`Tokens`}, `window` {`Day`|`Week`|`Month`|`Year`},
    `onBudgetExceeded` `Audit`|`Block`.
  - None exist yet.
- **Pricing:** comes from the `model-costs` and `spark-costs` catalog ConfigMaps
  referenced by `EnterpriseAgentgatewayParameters/agentgateway-proxy`. They are generated
  by `scripts/gen-model-costs.py` from models.dev; later sources win per model.
- **Installs:**
  - `enterprise-agentgateway` (v2026.10.0) and `management` (0.5.9) were installed by hand
    with Helm, not through Argo CD.
  - `apps/<dir>` is synced by an Argo ApplicationSet, so any new directory under `apps/`
    would be picked up automatically.
- **Capacity:** the omni-viper node is CPU-request bound (4 cores). The UI already runs
  with `maxSurge=0` because two UI pods don't fit.

## Decisions

### Decided by Sebastian
- Run on omni-viper, mixed in with the real data. Script lives in the repo and is runnable
  elsewhere.
- Execute from the laptop (locally), not as an always-on in-cluster workload.
- Providers: mostly Azure AI Foundry, then Bedrock Claude Opus, then Mistral.
- Fake calls priced at real rates.
- 7 days of history by default, adjustable.
- 15 teams, 12 projects, 35 users at `@td.com`; tools: Claude Code, Copilot, Claude Desktop.
- **Tools match models.** Each tool talks to the models it really would, rather than any
  tool to any model.
- **`down` can wipe the fake history** as well as the Kubernetes objects. It is an option,
  for example `down --purge-history`, so plain `down` keeps the history.
- **Prices and volume:** whatever is normal for enterprise AI-assistant usage at real
  rates. There is no fixed dollar target.

### My calls (open to override)
- **Hierarchy:** Organization (TD) → Group (line of business, about 5, such as Personal
  Banking, Wealth, TD Securities, Enterprise Technology, Risk) → Team (15) → User (35).
  **Project** (12) and **Client tool** are attributes, alongside Virtual Key. Rationale:
  projects cut across teams, so they don't nest cleanly in the tree.
- **Identity:** each user gets virtual API keys whose metadata carries organization, group,
  team, project and user. Users on several projects or tools get more than one key.
- **Client tool dimension:**
  - Preferred: read from the request's `User-Agent`, with the generator sending realistic
    strings for Claude Code, Copilot and Claude Desktop.
  - If the CEL environment can't read request headers, put the tool in key metadata
    instead (one key per user per tool).
- **Fake LLM:** one small in-cluster mock server in a new namespace (for example `td-demo`)
  that answers each provider's wire format with believable latency and token counts. The
  gateway gets new backends and routes that use the **native provider types** (Azure,
  Bedrock, Mistral, or openai-compatible with a Mistral provider label) pointed at the mock
  via host override, so the UI's `provider` dimension reads correctly.
- **No second proxy:** TD routes attach to the existing `agentgateway-proxy`. They are
  isolated by hostname (for example `td.demo.local`), so they never collide with real
  paths. This shares the existing price catalog and tracing policy, and adds no proxy pod
  to a CPU-tight node; in this product every Gateway gets its own proxy Deployment, which
  is why a separate TD Gateway is avoided. The only new pod is the mock server, with small
  requests (about 25m CPU and 64Mi).
- **Mistral provider label:** if no backend type can produce `provider=mistral`, serve
  Mistral Large through Azure AI Foundry, which hosts Mistral models. Price it at Azure's
  Mistral rate.
- **History by direct backfill:** synthetic spans are written straight to the telemetry
  collector's OTLP endpoint, through a port-forward, with past timestamps. They are not
  replayed through the gateway, because the gateway stamps "now".
- **Live mode:** the laptop script port-forwards to the TD gateway and sends real requests
  at a demo-friendly rate. It follows Toronto business hours and quieter evenings, but is
  never dead, since demos happen at all hours.
- **Tool → model mapping:**
  - **GitHub Copilot → Azure AI Foundry GPT models.** Most users (about 20 of 35) are
    Copilot users, so Azure leads on request count.
  - **Copilot BYOK → Mistral Large.** A few data-residency-sensitive teams use Copilot
    with their own key pointed at Mistral.
  - **Claude Code → Bedrock Claude Opus**, for about 10 power users.
  - **Claude Desktop → Bedrock Claude Opus**, for about 5 users, mostly in risk and
    business roles.

  **Azure leads on both requests and dollars** (user requirement, 2026-10-21): Azure spend
  is at least 3× Bedrock and at least 3× Mistral, Bedrock Opus second, Mistral smallest. Copilot users run agent mode with
  large contexts, and some are power users.
- **Realism targets (rough, typical rather than a fixed target):**
  - Claude Code power users: about $40–80 per working day each, with large
    agentic-context prompts.
  - Copilot users: a few dollars a day each.
  - Claude Desktop users: in between.
  - Org total: **over $10k per 7 days** (user requirement, 2026-10-21), so roughly $45–75k a month. Scale per-user usage up to hit it.
- **Shared team keys** (user addition): 4 teams also have one shared team virtual key, used by headless Claude Code in CI on Bedrock Opus, attributed to `svc-<team>@td.com`.
  - Keep total row volume modest: well under about 300k spans for 7 days.
  - Errors: about 1–2% 4xx/5xx, including some 429s.
  - Weekends: low.
  - Latency: lognormal per model.
- **Packaging:** manifests and script under a new top-level `demos/td-traffic/` directory,
  deliberately **not** under `apps/`, so Argo never adopts it.
  - Python script with `uv run` inline dependencies or the stdlib, matching
    `scripts/gen-model-costs.py`.
  - Subcommands: `up` (apply mock, backends, routes, keys, budgets, dimensions), `backfill
    --days N`, `live`, `down`.
  - Everything labeled `demo=td` for teardown.
- **Dimension config:**
  - First try putting the TD dimensions on the gateway's own config, which the chart says
    overrides the ConfigMap. Keep that only if the UI's cost-management dimension list
    picks them up.
  - Otherwise use `helm upgrade --reuse-values` plus a values file in the demo directory.
  - Omni-viper's current dimension config was checked against the chart v2026.10.0
    default and is identical. Even so, `up` saves the live ConfigMap contents and `down`
    puts back exactly what was saved.
  - Keep the existing Group/User semantics working for current traffic. Real traffic will
    show `<missing>` org and team, which is fine.

## Left to the builder

- Exact names, the team ↔ group ↔ project mapping, and user personas, within the counts
  above.
- The mock server's implementation and image (stdlib Python in a ConfigMap on a slim
  python image is fine).
- Whether backfill goes to the collector over OTLP/HTTP, OTLP/gRPC, or as a ClickHouse
  insert. The collector is preferred so the pipeline matches live data.
- Verifying, on omni-viper before building on it:
  - the virtual-key Secret format and how its metadata maps to `apiKey.*` in CEL
  - whether CEL can read request headers
  - which provider label each backend type produces in `custom_dimensions`
- Adding Bedrock Opus, Azure and Mistral rates to the catalog if `model-costs.yaml` lacks
  them (via `gen-model-costs.py` providers, not hand edits).
- Budget amounts that produce the near and over-limit mix given the generated spend.
- **How history purge works.**
  - Every synthetic span, whether backfilled or live, must carry a marker such as span
    attribute `demo=td`, plus the TD organization dimension.
  - The raw table and per-span tables can be deleted by that marker.
  - Rollup tables that aggregate without the dimensions (for example per-model totals,
    where real and fake Bedrock Opus mix) need a different strategy, such as recomputing
    the affected time range from the cleaned raw table or another approach. Verify the
    result against real-traffic totals from before the demo.
  - Running `backfill` twice must not double the history. Purge the TD history first, or
    refuse to run.
