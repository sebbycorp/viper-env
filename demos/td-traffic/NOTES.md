# TD demo traffic: verified platform notes (omni-viper)

Verified on 2026-10-21 against kube context `maniak-omni-viper`, which runs
enterprise-agentgateway v2026.10.0 and management/UI 0.5.9. Everything here was tried
live: the mock ran in `td-demo`, routes went on `agentgateway-proxy` with host
`td.demo.local`, and results were read back from ClickHouse. All spike objects were then
deleted and the Helm value restored. The span template is
`tdlib/span_template.json`.

## Summary (read this first)

| Question | Answer |
|---|---|
| Key format | One Secret per key set. Each data entry is `<keyId>: <JSON>`, in the same format the UI writes (§1). `organization`, `group`, `user` and `id` sit at the top of `metadata`. `team`, `project` and anything else go in `metadata.custom`. |
| CEL for key fields | Top-level metadata fields resolve as `apiKey.<field>`. `custom` fields resolve **only** as `apiKey.custom.<field>`; they are not flattened. |
| Headers in CEL | **Yes.** `request.headers["user-agent"]` works, including `startsWith`, `contains` and `?:`. One key per user is enough; no key per tool is needed. |
| Provider labels | Azure → `azure`, Bedrock → **`aws.bedrock`** (set `providerOverride: aws.bedrock`), Mistral → `mistral`. |
| Pricing | All four routes are priced non-zero without catalog changes, provided Azure mini uses `gpt-5.4-mini` instead of `gpt-4.1-mini` (§3). |
| Task 6 required | **No**, with the model choices in the route table. It is needed only if `gpt-4.1-mini` on Azure is wanted. |
| Dimension list source | The UI reads the Helm-owned ConfigMap `agentgateway-enterprise-budget-dimensions`. Use Helm `budgetDimensions.config` (§4). |
| Shared team key (no user) | Works. The `user` dimension is simply **absent** from `custom_dimensions`, and the UI Virtual Keys page shows `—`. |
| Budgets | Audit budgets work, but need `entBudgetEnforcement` on a policy and **integer** `limit.amount` ≥ 1 (§8). |

## Model routes (ruling 1, for `usage.MODELS`)

Every route is an `AgentgatewayBackend` with `spec.ai.provider.custom`. Its
`formats: [{type: Completions}]` and `backendRef: {name: td-mock-llm, port: 8080}`
were verified, and so was a matching HTTPRoute on `agentgateway-proxy` with
`hostnames: [td.demo.local]`. Each (providerOverride, model) pair below returned a non-zero
`agw.ai.usage.cost.total`.

| route id | HTTPRoute path (PathPrefix) | providerOverride | backend `model` | `provider` in custom_dimensions | catalog provider key | catalog model key | rates $/1M (in / out / cacheRead) | priced by | Task 6 required |
|---|---|---|---|---|---|---|---|---|---|
| azure-gpt | `/azure/gpt-5-4` | `azure` | `gpt-5.4` | `azure` | `azure` | `gpt-5.4` | 2.5 / 15 / 0.25 (above 272k context: 5 / 22.5 / 0.5) | proxy **built-in** catalog | no |
| azure-gpt-mini | `/azure/gpt-5-4-mini` (proposed) | `azure` | `gpt-5.4-mini` | `azure` | `azure` | `gpt-5.4-mini` | 0.75 / 4.5 / **not measured** (see note) | proxy **built-in** catalog | no (yes only if `gpt-4.1-mini` is kept) |
| bedrock-opus | `/bedrock/claude-opus-5-5` | `aws.bedrock` | `anthropic.claude-opus-5-5` | `aws.bedrock` | `aws.bedrock` | `anthropic.claude-opus-5-5` | 4 / 20 / 0.2 (cacheWrite 5) | `model-costs` ConfigMap | no |
| mistral-large | `/mistral/mistral-large` | `mistral` | `mistral-large-latest` | `mistral` | `mistral` | `mistral-large-latest` | 0.5 / 1.5 / 0.05 | proxy **built-in** catalog | no |

Notes on the table:
- **azure-gpt-mini.** During the spike this route was `/azure/gpt-4-1-mini` with model
  `gpt-4.1-mini`, and it was **unpriced**. The `gpt-5.4-mini` row was verified through a
  passthrough probe backend (`providerOverride: azure`, no backend `model`, request
  `model: gpt-5.4-mini`), which priced it at input 0.75 and output 4.5. The probe sent no
  cached tokens, so the built-in cacheRead rate for `gpt-5.4-mini` is unknown.
- **If gpt-5.4-mini has no cacheRead rate,** cached tokens would add $0 rather than fail.
  This is inferred from how the other cost fields behave; it was not observed. So either
  send `x-td-cached-tokens: 0` for that model, or check one span's
  `agw.ai.usage.cost.cache_read` before relying on it.
- **Cache rates that were measured:**
  - `gpt-5.4` at 0.25: 200 cached tokens cost $0.00005.
  - `mistral-large-latest` at 0.05: derived from the $0.00116 total of the first test.
  - `anthropic.claude-opus-5-5` at 0.2: from the Opus span checked below.

Pricing checked by hand on a live span. The Opus request had 48,213 input tokens, of
which 31,000 were cached, plus 1,873 output tokens:
17,213×4e-6 + 31,000×0.2e-6 + 1,873×20e-6 = **$0.112512**, which matches the span
exactly.

**Things that did NOT price (cost attrs absent):**
- `providerOverride: azure` with `gpt-4.1-mini`, `gpt-4.1`, `gpt-4o`, `gpt-4o-mini` or
  `o4-mini`. The built-in `azure` table lacks them. The `openai` entries in `model-costs`
  are not used as a fallback.
- `providerOverride: bedrock`. The label becomes `bedrock`, and nothing in the catalog
  is keyed `bedrock`. Always use `aws.bedrock`, the same label the native Bedrock
  provider on `bedrock-proxy` produces.
- `providerOverride: azure.ai`, which is not a catalog key.
- `aws.bedrock` with `claude-opus-5-5`. This needs the `anthropic.` prefix.
  `us.anthropic.claude-opus-5-5` also prices, at 4.4 / 22.
- `mistral` with `mistral-large-2411` or `devstral-medium-latest`.

Other built-in prices seen: azure `gpt-5` 1.25/10, azure `gpt-5-mini` 0.25/2;
mistral `mistral-medium-latest` 1.5/7.5, `mistral-small-latest` 0.15/0.6,
`codestral-latest` 0.3/0.9, `mistral-large-2512` 0.5/1.5; anthropic `claude-opus-5-5`
4/20.

The `model-costs` ConfigMap only has provider keys `openai` and `aws.bedrock`. The
`azure` and `mistral` prices come from a catalog built into the proxy binary. On another
cluster or proxy version the built-in table may differ. If portability matters, Task 6
can add `azure` and `mistral` to `gen-model-costs.py`. It is optional, and how a
ConfigMap `azure` key would merge with the built-in one was not tested.

The gateway rewrites the request body `model` to the backend `model`, so the client's
`model` value does not matter. If the backend has no `model`, the request's `model`
passes through. That was handy for probing.

Backfilled spans must use exactly these three provider values, `azure`, `aws.bedrock`
and `mistral`, in both `gen_ai.provider.name` and the `provider` pair of
`custom_dimensions`. Live TD traffic produces them too. The UI "Provider" filter will
then list `azure`, `aws.bedrock` and `mistral` for TD. Real traffic adds `openai`, and
`aws.bedrock` is shared with the real Bedrock route.

## 1. Virtual-key Secret format (verified)

Use the format the Solo UI itself writes. A Secret holds one or more keys (a "key set").
Each data entry name is the key id, and each value is JSON:

```json
{"key":"td-<random>","keyHash":null,
 "metadata":{"id":"td-u-jane-doe-copilot","organization":"TD","group":"personal-banking",
             "user":"jane.doe@td.com",
             "custom":{"team":"retail-cards","project":"mobile-app-modernization"}}}
```

- `keyHash: null` is what the UI writes. It can be omitted; keys without it work too.
- The UI generates `sk-agw-<43 chars base64url>`. Any string works, but `sk-agw-` looks
  native.
- Top-level `metadata.X` resolves as `apiKey.X`. Verified for `id`, `organization`,
  `group`, `team`, `project`, `user` and `client`; any name works.
- `metadata.custom.X` resolves as **`apiKey.custom.X`** only. Verified: `apiKey.project`
  is absent when `project` is under `custom`.
- A plain string value (`name: rawkey`, as `desk-apikeys` uses) authenticates. However,
  it has **no** `apiKey.id` and no metadata, so it carries no virtualKey dimension.
- A wrong key gets `401`. The span is recorded, with no dimensions.

How entries sit in the Secret, as verified on the UI-created `td-spike-keys`:
- Each **data key name is the key id**. It is the same string as `metadata.id` and is
  the "Key ID" shown in the UI.
- Each **value is the JSON document**, base64-encoded under `data`. The UI wrote
  `data: {td-spike-ui-1: <base64 of the JSON>}`.
- Spike keys added with `kubectl patch` under `data` also worked:
  - `td-flat-1`: flat metadata.
  - `td-ui-style-1`: metadata using `custom`.
  - `td-team-shared-1`: no user.
  - `td-final-1`: the recommended format.
  - `td-plain-1`: a plain string.
- `stringData` is the standard Kubernetes write-only alias. The API server stores it
  as base64 under `data`, so the result is identical.
- The Secret type is `Opaque`. The UI added no labels; add `demo: td` yourself.

Copyable manifest. Two keys are shown: a personal key and a shared team key with no user.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: td-virtual-keys
  namespace: td-demo
  labels: {demo: td}
type: Opaque
stringData:
  td-u-jane-doe-copilot: '{"key":"sk-agw-3fJ9kQ2xV7mL0pR8sT1uW4yZ6aB5cD2eF9gH0jK3lMn","keyHash":null,"metadata":{"id":"td-u-jane-doe-copilot","organization":"TD","group":"personal-banking","user":"jane.doe@td.com","custom":{"team":"retail-cards","project":"mobile-app-modernization"}}}'
  td-t-platform-eng-shared: '{"key":"sk-agw-Qw8eRt5yUi2oPa7sDf4gHj1kLz9xCv6bNm3qWe0rTyU","keyHash":null,"metadata":{"id":"td-t-platform-eng-shared","organization":"TD","group":"enterprise-technology","custom":{"team":"platform-eng","project":"cloud-migration"}}}'
```

In this example, the data key name, `metadata.id` and the UI Key ID are all the same
string.

The shared team key has no `user`. Requests made with it carry no `user` pair in
`custom_dimensions`; this was verified with `td-team-shared-1`. The UI shows `—` in the
User column.

Policy (the Secret must be in the **policy's namespace**). **Every TD HTTPRoute must be
listed in `targetRefs`.** A route that is left out gets no API-key auth, which means no
`apiKey.*` dimensions (no org, team or user) and no budget enforcement. This was verified:
the un-targeted spike route `azure-gpt-mini` produced spans carrying only
`model`/`provider`/`client`.

```yaml
apiVersion: enterpriseagentgateway.solo.io/v1alpha1
kind: EnterpriseAgentgatewayPolicy
metadata: {name: td-apikey, namespace: td-demo, labels: {demo: td}}
spec:
  targetRefs:            # TD HTTPRoutes only; never the Gateway
  - {group: gateway.networking.k8s.io, kind: HTTPRoute, name: azure-gpt}
  - {group: gateway.networking.k8s.io, kind: HTTPRoute, name: azure-gpt-mini}
  - {group: gateway.networking.k8s.io, kind: HTTPRoute, name: bedrock-opus}
  - {group: gateway.networking.k8s.io, kind: HTTPRoute, name: mistral-large}
  traffic:
    apiKeyAuthentication:
      mode: Strict
      location:
        header: {name: Authorization, prefix: 'Bearer '}
      secretRef: {name: td-virtual-keys}
    entBudgetEnforcement:            # needed for budgets, see §8
      discovery:
        namespaces: {from: Same}
```

Before the Secret exists, the policy is `Accepted=True, PartiallyValid: secret ... not
found`. Once it exists, the status is `Valid` / `Attached`. Key changes in the Secret take
effect within seconds; no restart is needed.

## 2. Request headers in CEL: YES

`request.headers["user-agent"]` works in a dimension expression, and so do string
methods and the ternary. This expression was verified to produce exactly `Claude Code`,
`GitHub Copilot`, `Claude Desktop` and `Other`:

```
request.headers["user-agent"].startsWith("claude-cli") ? "Claude Code" :
request.headers["user-agent"].startsWith("GitHubCopilot") ? "GitHub Copilot" :
request.headers["user-agent"].contains("Claude Desktop") ? "Claude Desktop" : "Other"
```

The user agents tested: `claude-cli/2.1.3 (external, cli)`, `GitHubCopilotChat/0.31.0`,
`Mozilla/5.0 (Macintosh) Claude/1.0.211 Chrome/138 Electron/37 Safari/537.36 Claude Desktop`.
So the fallback of putting the client tool in key metadata is **not needed**, and one key
per user (per project) is enough. The raw user agent is not stored as a span attribute;
only the dimension value is.

Results by header value:
- `python-requests/2.32` and `curl/8.x` produced `client=Other` (verified).
- **No `User-Agent` header at all: unknown, not tested.** Every spike request carried
  one, because curl sends a default.
  - **Inferred:** the map lookup fails and the `client` pair is **omitted**, like other
    unresolved expressions. That was observed for missing `apiKey.*` fields. The result
    would not be `Other`.
  - If `Other` is wanted for that case, guard the expression:
    `has(request.headers["user-agent"]) ? (...) : "Other"`. The `has()` form is not
    tested either.
  - The generator should always send a User-Agent.

## 3. Provider label and pricing

See the route table above. The label equals `providerOverride` verbatim, and the price is
looked up by (label, model) in the merged catalog. That catalog is built into the proxy,
plus the `model-costs` and `spark-costs` ConfigMaps.

## 4. Dimension list: use the Helm ConfigMap

- The UI (Cost Management filters, the "Scope" hierarchy picker, Dimensions page and
  Virtual Keys columns) reads the ConfigMap `agentgateway-enterprise-budget-dimensions`
  through `AgentgatewayCostAPI.GetBudgetDimensions`. After the Helm upgrade, the
  Cost Management filters immediately showed the new attributes (Project, Client Tool and
  the others). The Virtual Keys table columns became Key ID / Organization / Group / Team /
  User / Project / Key Set: the hierarchy ids plus `project`.
- The controller renders the same config into the proxy ConfigMap `agentgateway-proxy`
  (`config.standardAttributes`, a flat id → CEL map). The running proxy picked it up in
  about **60 s** with no pod restart, which is the kubelet ConfigMap sync delay.
- **Gateway-level `standardAttributes`: INFERRED, NOT TRIED.** They would mean
  `EnterpriseAgentgatewayParameters.rawConfig` on the Argo-managed
  `agentgateway-proxy` parameters, and that was not tried because it touches an Argo
  object.
  - The conclusion that such attributes would **not** appear in the UI's dimension list
    is inferred. The UI fetches its dimension list through `GetBudgetDimensions`, and
    its filters tracked the ConfigMap changes exactly.
  - The UI was not observed ignoring a Gateway-level attribute.
- **Decision for Task 5:** `helm upgrade --reuse-values -f demos/td-traffic/<values>.yaml`
  at chart `oci://us-docker.pkg.dev/solo-public/enterprise-agentgateway/charts/enterprise-agentgateway`
  `--version v2026.10.0`. The UI also has an `UpdateBudgetDimensions` API (Dimensions
  page) that writes the same ConfigMap. Do not use it: it causes Helm drift.

Verified values file:

```yaml
budgetDimensions:
  enabled: true
  config:
    hierarchy:
    - {id: organization, displayName: Organization, expression: apiKey.organization}
    - {id: group, displayName: Group, expression: coalesce(jwt.group, apiKey.group)}
    - {id: team, displayName: Team, expression: apiKey.custom.team}
    - id: user
      displayName: User
      expression: coalesce(apiKey.user, apiKey.name, apiKey.owner, jwt.sub, jwt.email, basicAuth.username, source.identity.namespace + "/" + source.identity.serviceAccount, source.subjectCn)
    attributes:
    - {id: virtualKey, displayName: Virtual Key, expression: apiKey.id}
    - {id: project, displayName: Project, expression: apiKey.custom.project}
    - id: client
      displayName: Client Tool
      expression: 'request.headers["user-agent"].startsWith("claude-cli") ? "Claude Code" : request.headers["user-agent"].startsWith("GitHubCopilot") ? "GitHub Copilot" : request.headers["user-agent"].contains("Claude Desktop") ? "Claude Desktop" : "Other"'
```

The resulting span `agw.llm.custom_dimensions` is sorted by id, and `model`/`provider`
are added automatically:
`[["client","Claude Code"],["group","enterprise-technology"],["model","anthropic.claude-opus-5-5"],["organization","TD"],["project","cloud-migration"],["provider","aws.bedrock"],["team","platform-eng"],["user","priya.patel@td.com"],["virtualKey","td-final-1"]]`.
Dimensions whose expression does not resolve are **omitted** from the list, not set to
`<missing>`. The `agw_cost_dimensions_5m` MV also drops `''` and `<missing>` values.

**Restore:** the original values are the chart default (hierarchy Group/User + attribute
virtualKey), and the release's user-supplied values are only `licensing`. The exact
restore that was verified is:
`helm upgrade enterprise-agentgateway <chart> --version v2026.10.0 -n agentgateway-system --reset-values -f <saved user values>`.
After it, the ConfigMap content, the user values and the all-values all diffed
identical to before. A `--reuse-values` restore leaves `budgetDimensions` in the
user-supplied values. Its content is the same, but it is no longer pristine.

## 5. Span template

`tdlib/span_template.json` is one complete row from `platformdb.otel_traces_json`,
captured as `FORMAT JSONEachRow SETTINGS output_format_json_quote_64bit_integers=0`. It is
a Claude Code → Bedrock Opus 5.5 request with all TD dimensions, a cost of $0.112512, and
two Audit budgets applied with `over_limit: true`. Pod IPs and UIDs are left as captured.
Notable fields:
- `Timestamp` is `"YYYY-MM-DD hh:mm:ss.fffffffff"` (UTC), and `Duration` is in ns.
  `StatusCode` is `"Unset"`, **even for 429/5xx**. The error shows only in
  `SpanAttributes.http.response.status_code`.
- `SpanName` is `"POST /bedrock/claude-opus-5-5/*"` and `SpanKind` is `"Server"`.
  `ServiceName` is `agentgateway-proxy`, `ScopeName` is `agentgateway`, and
  `ParentSpanId` is `""` for the LLM span. Each request also writes a second, attribute-less
  span in the same trace.
- Cost values are decimal **strings**. `gen_ai.usage.input_tokens` is the **total**
  input, cached tokens included. Cached tokens go in
  `gen_ai.usage.cache_read.input_tokens`, and the cost splits `input` (uncached) from
  `cache_read`.
- The budget ids and the virtual key (`td-final-1`) in the template are **spike names**
  (§8); replace them when templating.
- Budget attributes are `agw.ai.budgets.{applied,count,ids[],action_taken,unit,
  over_limit,over_limit_after_debit,blocked,charged_micro_usd,charged_tokens,
  charged_tokens_exact}`. Without a matching budget, only `applied: false, count: 0` is
  present.
- Live spans carry no `demo=td` attribute. They are identifiable by
  `SpanAttributes.server.address = 'td.demo.local'` and `route LIKE 'td-demo/%'`.
  Backfilled spans should keep both and add an explicit marker.

## 6. ClickHouse HTTP access

```bash
kubectl --context maniak-omni-viper -n agentgateway-system port-forward pod/management-clickhouse-shard0-0 18123:8123 &
# (svc/management-clickhouse:8123 also exists)
CH_USER=$(kubectl --context maniak-omni-viper -n agentgateway-system get secret management-clickhouse-auth -o jsonpath='{.data.username}' | base64 -d)   # "default"
CH_PASS=$(kubectl --context maniak-omni-viper -n agentgateway-system get secret management-clickhouse-auth -o jsonpath='{.data.password}' | base64 -d)
curl -s -u "$CH_USER:$CH_PASS" http://localhost:18123/ --data-binary "SELECT count() FROM platformdb.otel_traces_json FORMAT TSV"
```

Secret keys are `username` and `password`. The request needs basic auth; without it,
the response is `Authentication failed`. User `default` has SELECT, INSERT, ALTER,
DELETE and other rights on `*.*`. Version 26.3.35.

- JSON columns cannot be used in `GROUP BY` directly; wrap them in `toString(...)`.
- Read paths as ``SpanAttributes.`agw.ai.usage.cost.total` ``.

The telemetry collector `solo-enterprise-telemetry-collector` exposes **only gRPC OTLP**
(4317, plus 4316 "otlp-remote"). There is **no OTLP/HTTP 4318**. A stdlib-only backfill
should therefore use a ClickHouse HTTP `INSERT ... FORMAT JSONEachRow` into
`platformdb.otel_traces_json`, which fans out through the MVs. The alternative is a gRPC
dependency.

MV chain: `otel_traces_json` → `agw_spans_typed_mv` → `agw_spans_typed`, which has the
columns `CostUsd`, `CustomDimensions` Map, `Provider` and others. From there it feeds
`agw_cost_dimensions_5m`, `agw_cost_model_5m`, `agw_trace_*_5m` and the rest
(AggregatingMergeTree, 5-minute buckets).

## 7. UI Virtual Keys store (scope addition)

- **Store:** the key-set Secrets referenced by `apiKeyAuthentication.secretRef` in any
  `EnterpriseAgentgatewayPolicy`, in any namespace. The page listed `desk-apikeys`
  (remarkable-diary) next to the spike `td-spike-keys` (td-demo). The UI's default key-set
  name `agw-virtual-keys` is only a frontend mock constant.
- **Format:** §1 is exactly what the UI's Create Virtual API Key wrote:
  `{"key":"sk-agw-…","keyHash":null,"metadata":{"id":…,"group":…,"user":…,"custom":{"team":…}}}`.
  The UI reads `id`, `organization`, `costCenter`, `group` and `user` from the top level.
  Every other field comes from `custom`, and a column is shown when the field name matches
  a dimension id. Top-level `team` or `project` is **not** shown in the UI, even though
  CEL `apiKey.team` would resolve. So: put organization, group and user at the top level,
  and team and project under `custom`.
- **Self-made keys:** (a) keys written directly into the Secret in that format appear on
  the Virtual Keys page, and (b) they authenticate, with their metadata reaching
  `apiKey.*` and `apiKey.custom.*` (verified in spans). Use this store and format for all
  TD keys: one Secret `td-virtual-keys` in ns `td-demo`, referenced by the TD policy,
  labeled `demo: td`. The UI-created Secret carried no labels, so add them yourself.
- The UI's create call is refused unless a policy already references the Secret:
  `secret "x" is not referenced by any apiKeyAuthentication policy; wire a policy to it first`.
  If Key Set is left blank, it auto-picks a referenced Secret, which could be
  `desk-apikeys`. Never create keys from the UI with Key Set blank.
- **Shared team key:** omit `user` and put the team in `custom.team`. It works. The
  `user` dimension is then **absent** for those requests, because every coalesce operand
  is missing. The page shows `—` for User. The UI's create form requires User and Group,
  but a Secret written directly does not.

## 8. Budgets (optional item, verified)

```yaml
apiVersion: enterpriseagentgateway.solo.io/v1alpha1
kind: EnterpriseAgentgatewayBudget
metadata: {name: td-team-budgets, namespace: td-demo, labels: {demo: td}}
spec:
  budgets:
  - name: platform-eng-monthly-usd
    subject: {team: platform-eng}            # keys = dimension ids; may combine e.g. {organization: TD, team: ...}
    limit: {amount: 1, unit: USD}            # amount: INTEGER >= 1 (no cents!)
    window: {unit: Month}                    # object, not a string: Day|Week|Month|Year
    onBudgetExceeded: Audit
  - name: platform-eng-daily-tokens          # Tokens-unit budget (verified, same spike object)
    subject: {organization: TD, team: platform-eng}
    limit: {amount: 1000, unit: Tokens}
    window: {unit: Day}
    onBudgetExceeded: Audit
```

**About the budget names in the span template.** The spike object was
`td-demo/td-spike-budget`, holding the two entries above:
`platform-eng-monthly-usd` (USD) and `platform-eng-daily-tokens` (Tokens). So the
template's `agw.ai.budgets.ids` are spike names. Task 7 must rewrite them to the real
TD budget object and entry names, in the format `<namespace>/<budget object>/<entry name>`.

The template's `agw.ai.budgets.unit: "TOKENS"`, `charged_tokens: 50086` and
`over_limit: true` reflect the Tokens entry. That entry's 1000-token limit was already
exceeded. When several budgets apply, the span reports a single `unit`. Which one it
picks was not determined; here it was TOKENS.

`charged_micro_usd: "112512"` equals the span cost × 1e6, and `charged_tokens` equals
input + output (48,213 + 1,873). For USD-only budgets, the value of `unit` is presumably
`USD`, but that was not observed.

- Without `traffic.entBudgetEnforcement` on a policy covering the routes, the status is
  `Enforced=False NotReferenced`. With it, the budget is `Enforced=True` and spans show
  `agw.ai.budgets.applied=true`, `count=2`, `ids=["td-demo/td-spike-budget/<name>",…]`
  and `action_taken=AUDIT`. Over-limit requests still return 200.
- `notifications[]` (Slack/Webhook) also exists but was not used.
- Budget spend counts only traffic that passes through the gateway. Backfilled spans
  show in the cost graphs but do **not** count toward budget `spent`. **Verified in Task 7**
  (see "Budgets and backfilled history" below).

## Budgets and backfilled history (verified, Task 7)

**The Budgets page counts only the proxy's live budget counters, not ClickHouse history.**
Checked on omni-viper on 2026-10-21, right after a 30-day backfill of 559,202 TD spans
($51,313.70 of TD spend in ClickHouse; Cost Management showed $51.3K for 30d):
- `AgentgatewayCostAPI/ListBudgets` (decoded grpc-web response) gave the org entry
  `td-org-monthly-usd` a spent value of **$1.370512** against its $71,000 limit. Every
  other entry was in the same range (0–$0.87). The page showed "1 Within budget,
  0 Exceeding" and empty usage bars.
- $1.370512 is exactly the Task 5 live check traffic: 2 × 20 requests
  (5 × $0.1258) plus the $0.112512 curl. Those spans had already been purged from
  ClickHouse when this was read, so the number does not come from ClickHouse at all.
- So budget `spent` comes from gateway-side counters charged per live request (the
  `charged_micro_usd` path), not from spans. Writing spans, and the
  `agw.ai.budgets.*` attributes inside them, does not move it. The budget counters are
  **not** faked (ruling).
- **Correction (Task 8): the conclusion above is only half right.** The counters live in **Redis**
  (ext-cache), not in ClickHouse. But the Budgets page reads `spent` from ClickHouse: it takes the
  **latest `agw.ai.budgets.outcome` span** (`current_usage`) per entry. The $1.37 was the
  `current_usage` in the newest surviving outcome span. After the Task 7 purge removed those outcome
  spans, the page showed 0 although Redis still held 1,370,512 µUSD.
  - **Every TD purge** (`down --purge-history`, `backfill --replace`, the sync marker purge) is trace-level,
    so it removes outcome spans. The page then needs **one request per team** to show the counters again.
    `sync-budgets` sends these requests itself, before reading and after its markers.
  - Details are in the next section.

What this means for the demo:
- Right after `up` + `backfill`, every budget shows about 0% used, even though the 30-day
  cost graphs show tens of thousands of dollars. The planned mix (2 over, 2 near, the rest
  under) only appears once enough **live** traffic has passed through the gateway in the
  current calendar month.
- (Superseded in Task 8 fix round 1: `plan_budgets` now sizes limits on window-to-date spend; see below.) Originally `plan_budgets` sized limits from the projected *full-month* spend (history + forecast).
  Live traffic alone will not get close to those limits during a demo. The README (Task 9)
  must say this. Two ways to get "over/near" budgets on demo day: size limits on
  expected live spend (e.g. a few dollars for the over/near teams), or show the per-team
  spend graphs against the limits instead of the Budgets usage bars.

## Budget counters: where they live and how the page reads them (verified, Task 8)

Checked on omni-viper on 2026-10-21 (ext-cache Redis, ClickHouse `system.query_log`, live requests).
- **Store:** Redis in `ext-cache-enterprise-agentgateway` (rate-limiter descriptors). There is one string key
  per budget entry and window, holding micro-USD. Example:
  `set|solo.io|budget.id^td-demo/td-budgets/td-org-monthly-usd|…|organization^TD|1791072000`.
- **Window:** `window: {unit: Month}` is a **fixed 30-day period aligned to the Unix epoch**, not the
  calendar month. On 2026-10-21 the key suffix is `1791072000`, which is 2026-10-04 00:00 UTC (= 691 × 2,592,000 s).
  The next reset is 2026-11-03 00:00 UTC. Key TTL is about 30 days. Outcome spans carry
  `reset_after_ms: 2592000000`. A `Day` window is aligned the same way (`1792540800` = 2026-10-21 00:00 UTC).
- **What the Budgets page shows** (`AgentgatewayCostAPI/ListBudgets`, field `spent`): the UI backend
  runs, per entry, `argMaxIf(current_usage, Timestamp)` over the gateway's **`agw.ai.budgets.outcome`**
  spans with `phase = 'final_debit_result'`. The source is `platformdb.otel_traces_json`, with
  `SpanAttributes.agw.ai.budgets.statuses[].{id, current_usage}`. The time range starts at the budget's
  `Enforced` condition time. So the page shows the counter value as of the **latest live request that
  hit that entry**.
- The outcome span is the "attribute-less second span" of every live TD trace mentioned in Task 7. It
  is not attribute-less: it has no LLM attributes, but it carries the budget statuses.
- Consequences:
  - A trace-level TD purge (`down --purge-history`, `backfill --replace`, the sync marker purge)
    deletes outcome spans. The page then shows a stale value, or 0, while the Redis counter is unchanged.
    The next live request on an entry fixes that entry. `sync-budgets` sends one ordinary request per team
    before reading and after its markers for this reason.
  - Re-applying the budget object can move the `Enforced` time. That hides older outcome spans until
    the next request.
  - **Proxy restart: counters survive.** Verified with ListBudgets before and after
    `kubectl rollout restart deploy/agentgateway-proxy`: all 21 entries were identical. Afterwards, new
    requests kept charging on top of the old counter. So `live` does **not** run `sync-budgets` at
    start, and there is no `--no-sync` flag.
- Reading ListBudgets programmatically: `POST /api/autoauth/token` `{"user_type":"admin"}` on
  `svc/solo-enterprise-ui:80` (the UI's own auto-auth). Then
  `POST /api/rpc.agentgateway.solo.io.AgentgatewayCostAPI/ListBudgets` with grpc-web+proto, an empty
  request frame, and `Authorization: Bearer`. `BudgetEntry` fields: 1 name, 2 subject map, 3 limit
  {1 unit, 2 amount}, 4 window, 5 action, 6 spent (double USD). See `budget_sync.decode_list_budgets`.

## Live traffic and budget sync (Task 8)

- `td.py live --context C [--duration 10m] [--speed X] [--min-rpm 6] [--concurrency 4]`:
  - Port-forwards to `svc/agentgateway-proxy:8080`.
  - Sends `usage.requests_for_window` per 15 s window, with the `min_rpm` fill-in floor.
  - Headers are `Host: td.demo.local`, the key's Bearer token, the client UA, and `x-td-*`.
  - On a connection error it closes and re-opens the port-forward with backoff 1, 2, 4 … 30 s, then
    re-sends the same request.
  - Prints a one-line summary every 30 s.
- Live 5-minute check, 2026-10-21 14:58–15:04 UTC (Toronto 11:00, peak):
  - 224 sent: 222 × 200, one 429, one 500. Spend $20.35.
  - All 5 client values appeared in new spans.
  - A proxy restart mid-run cost one `RemoteDisconnected`. The tool did one reconnect and re-sent; no
    request failed.
  - 213 of the 224 requests have spans in ClickHouse. The rest were most likely in flight on the old pod
    when it was replaced. Their spans were never exported, but the gateway charged them.
- `td.py sync-budgets --context C [--dry-run]`:
  1. Sends one ordinary request per team, so the page is current.
  2. Reads ListBudgets.
  3. Computes targets: generated spend (`generate`, seed 42) over [budget window start, backfill end) per team, plus live TD spend recorded in ClickHouse (fix round 1).
     Group and org targets are sums.
  4. Plans per-team gaps as ≤ 20 marker requests per team, each ≤ 250k input tokens, on bedrock-opus /
     azure-gpt in the team's Opus vs. other spend share. When the gap exceeds 20 plausible requests,
     the output-token count grows. Up to ~18M output tokens per request were priced and charged exactly.
  5. Sends one ordinary request per team again.
  6. Purges the marker spans with the Task 7 machinery, `predicate = sync_span_predicate()`. Spans keep
     `url.path` but not the raw user agent, so the marker is the path segment `/td-budget-sync`;
     the UA `td-budget-sync/1.0` is sent too.
  7. Prints the table.
- **Idempotent (fix round 1).**
  - target = generated spend over [window start, **backfill end**) + **live TD spend recorded in
    ClickHouse** in the window.
  - The backfill end is the newest successful TD LLM span without an outcome span.
  - Live spend means TD LLM spans whose trace has an outcome span: `live`, the refresh requests and
    earlier checks. Markers are excluded.
  - Gaps < $1 are skipped.
  - The refresh requests are live spend, so they land in both the counter and the target. A second run
    sends 0. Verified live: two runs in a row, both 0 marker requests (`fix1-sync-1.log`, `fix1-sync-2.log`).
- Today's counters are about $145 above that target. My pre-fix run targeted history up to `now`
  (15:17) instead of the backfill end (14:37). They will stay above the target until the window resets.
- First sync on 2026-10-21 15:05 UTC: 300 marker requests, $28,911.32. Counters equal targets to the cent.
  The purge verified 152 real keys identical. A tool-independent check (`task-8-work/hand_totals.py`)
  found all non-marker totals identical. The only rollup change was the earlier probe marker
  (1 request, $0.006).
- Counters vs. ClickHouse TD spend in the same window (2026-10-04 → now): $28,990 vs. $28,746
  (+0.8%; per team ±3%). The difference is RNG noise between the regenerated history and the backfilled
  one, plus the generated spend after the backfill ended.
- **Before fix round 1** (limits sized on the projected calendar month) the mix after sync was: org 40.8%, groups
  38.9–47.2%, teams 20.0–73.3%. That is 0 over and 0 near. `plan_budgets` sizes limits on the
  *projected full calendar month*, while the counter holds the epoch-aligned 30-day window to date
  (17.6 of 30 days on 10-21). The planned "2 over / 2 near / groups 60–90%" is not reachable on honest
  window-to-date spend. Changing limits is a controller decision (Task 8 NEEDS_CONTEXT).
- **Ruling (option a), implemented:** `plan_budgets` sizes limits on **window-to-date generated spend**
  (`manifests.window_spend`, same seed as the backfill) over the gateway's real window
  (`manifests.budget_window`).
  - **Formula:** `start = floor(unix_now / 2,592,000) × 2,592,000` UTC, `end = start + 30 days`.
  - **Evidence:**
    - The Redis key suffix on 2026-10-21 is `1791072000`, i.e. 2026-10-04 00:00 UTC (= 691 × 2,592,000).
    - Counter TTL ≈ 30 d and outcome spans carry `reset_after_ms: 2592000000`.
    - The UI labels the window "Monthly (30 days)" / "Rolling window".
  - **Ratios:**
    - over teams: target 1.08–1.20, allowed 1.02–1.28
    - near teams: 0.88–0.92, allowed 0.86–0.94
    - the rest: ≤ 0.74
    - groups / org: 0.61–0.89
    - Tested at 1, 6.5, 17.6 and 29 days into a window.
  - **Re-planning:** happens on every `up`, or with `td.py replan-budgets --context C`, which applies only
    the budget object. Re-applying the budget object with new limits kept the same Redis counter keys,
    so the counters are preserved. `status` prints the window and the next rollover.
  - **After the window rolls over:** re-run `up` (or `replan-budgets`), `backfill --replace` and `sync-budgets`.
- **Result on omni-viper 2026-10-21 15:30 UTC** (`up` → `sync-budgets` twice; `fix1-sync-1.log`,
  `t8-fix1-ui-budgets.png`):
  - teams over: Fraud Strategy 109.9%, Credit Cards & Lending 108.2%
  - teams near: Capital Markets Data 91.6%, Digital Channels 87.5%
  - other teams: 20.0–63.2%
  - groups: 66.1–82.6%; org: 72.5%

## Backfill and purge (Task 7)

- `td.py backfill --context C [--days 30] [--replace]` streams `generate()` → `spans.to_row`
  → gzip JSONEachRow `INSERT INTO platformdb.otel_traces_json` in 20k batches over a
  port-forward to `management-clickhouse-shard0-0:8123`. The MVs fan out into
  `agw_spans_typed`, `agw_chat_spans`, `trace_time_index` and the 8 `agw_*_5m` rollups.
  30 days ≈ 559k rows, about 4–5 minutes on omni-viper, ~240 bytes/row gzipped.
  `otel_traces_json` has no row TTL (only Events/Links columns, 7 days), so 30 days stay.
- `backfill` refuses when any TD span exists (`td_span_predicate()`); `--replace` purges first.
- `td_span_predicate()`: custom_dimensions contains `["organization","TD"]` **or**
  `server.address = 'td.demo.local'` (also catches 401s and the Task 4 spike spans).
- Backfilled rows mirror the live TD spans (Task 5): dims sorted by id incl. `client` and
  `virtualKey`; budgets `ids` = the 3 real `td-demo/td-budgets/<entry>` names (org, group,
  team), `action_taken: ALLOW`, no `unit` (USD-only budgets); `cache_read.input_tokens`
  always present; errors (429/4xx/5xx) carry zero costs and no `gen_ai.response`/`usage`,
  like the live 429. `StatusCode` is `Error` for ≥ 500 (brief; live 5xx unobserved) and
  `Unset` otherwise. `cluster_name` is read from the newest real `agentgateway-proxy` span.
- The UI "Global Error Rate" stays 0% for live and backfilled TD errors alike: the
  `agw_spans_typed_mv` reads `http.status`, while spans carry `http.response.status_code`.
- `down --purge-history` (and `backfill --replace`) run `ClickHouse.purge_td()`:
  1. Freeze the TD span ids (`td_span_predicate()` AND older than the live edge = now − 2 min,
     floored to 5 min) into `td_purge_backup.td_span_ids__<stamp>`. TD spans newer than that
     are left for the next purge; if the newest TD span is at the edge, it waits once (≤ ~7 min).
  2. Snapshot the affected rollup range into `td_purge_backup.<table>__<stamp>`, and the TD
     rows of `trace_time_index` into `td_purge_backup.trace_time_index__<stamp>`. Ledger row
     `started` in `td_purge_backup.__purges`.
  3. Record real totals (rollup − TD contribution); delete TD traces from `agw_spans_typed`
     / `agw_chat_spans`; delete and re-insert the rollup range from the cleaned source with
     each view's `as_select`; verify the real totals are identical → ledger `verified`.
  4. Delete TD trace ids from `trace_time_index` and the TD traces from `otel_traces_json` →
     ledger `completed`. Prints the restore SQL and `DROP DATABASE td_purge_backup`.
  - "TD traces" = TraceId in any frozen id table (`merge('td_purge_backup','^td_span_ids__')`),
    limited to the recomputed time range. Trace ids are random and TD-only, so this also removes
    the attribute-less second span of each live TD request (164 such spans from Tasks 4/5 were
    removed on 2026-10-21) and the `trace_time_index` orphans left by earlier purges.
  - Any failure after the snapshot prints that stamp's restore SQL and re-raises. A purge
    refuses to start while an earlier stamp has `started` but no `verified` in the ledger, and
    prints that stamp's restore SQL; `--force-new-backup` overrides (on `backfill` and `down`).
    Stamps from before the ledger existed have no rows and never block.

## Mock deployment (worked)

The spec in the brief worked as given: a ConfigMap `td-mock-llm` (key `server.py`), a
Deployment on `python:3.12-slim` running `python -u /app/server.py` with requests 25m /
64Mi, readiness probe `GET /` on 8080, and Service `td-mock-llm:8080`. Streaming
(`stream: true`) and `x-td-status: 429` both pass through the gateway, and
`x-td-latency-ms` shows up in the span `Duration`. The custom provider's `Completions`
format posts to an upstream path ending in `/chat/completions`, which is the only path
the mock accepts. All the calls returned 200. The exact prefix was not logged.

## Gotchas

1. **`providerOverride: bedrock` is unpriced. Use `aws.bedrock`.**
2. **The Azure built-in catalog has no `gpt-4.1-mini`**, nor `gpt-4.1`, `gpt-4o*` or
   `o4-mini`. Use `gpt-5.4-mini`.
3. Custom key metadata under `custom` is `apiKey.custom.X`, not `apiKey.X`. The UI only
   shows non-standard fields that live under `custom`.
4. Budget `limit.amount` is an integer ≥ 1, `window` is `{unit: …}`, and enforcement
   needs `entBudgetEnforcement` on a policy.
5. `StatusCode` stays `Unset` on 4xx/5xx spans.
6. Dimension changes take about 60 s to reach the proxy (ConfigMap volume sync). Wait
   or poll before sending live traffic after `up`.
7. The collector has no OTLP/HTTP. Backfill should use a ClickHouse HTTP insert, or
   gRPC.
8. `python:3.12-slim` triggers PodSecurity `restricted` **warnings** in a new namespace
   (warn only; the pod runs). To silence them, add `securityContext`
   (`runAsNonRoot: true`, `runAsUser: 65534`, `allowPrivilegeEscalation: false`,
   `capabilities.drop: [ALL]`, `seccompProfile.type: RuntimeDefault`).
9. A plain-string key (no JSON) has no `apiKey.id`, so the Virtual Key dimension is
   empty.
10. The spike left **66 spans (~68.6M tokens, ~$216 of fake spend)** in ClickHouse, from
    2026-10-21 13:13–13:24 UTC. They include odd provider labels from the probes
    (`bedrock`, `azure.ai`, `anthropic`, `openai`). All are identifiable by
    `server.address='td.demo.local'` or `route LIKE 'td-demo/%'`. They also sit in the
    5-minute rollups. The purge work (`down --purge-history`) should remove them and can
    use them as its first test case.
11. Helm `--reuse-values` keeps the previous user-supplied block. For a byte-exact
    restore, use `--reset-values -f <saved user values>`.
