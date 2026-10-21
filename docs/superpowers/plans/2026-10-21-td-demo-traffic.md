# TD Demo Traffic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A laptop-run script, `demos/td-traffic/td.py`, fills the Solo Enterprise UI with
believable TD (`@td.com`) AI-assistant usage on any cluster running enterprise
agentgateway and the Solo UI. It provides:
- 7 days of back-dated history (adjustable)
- live traffic through the real gateway
- per-team budgets
- clean removal

**Architecture:**
- **In-cluster pieces:** a tiny fake LLM server in namespace `td-demo`, plus agentgateway
  `custom` backends with `providerOverride` of `azure`, `bedrock` and `mistral` pointing
  at it.
- **Routes:** TD HTTPRoutes on the existing `agentgateway-proxy`, matched on hostname
  `td.demo.local` and protected by API-key auth. Each virtual key carries
  organization/group/team/project/user metadata.
- **Cost dimensions:** changed through the `enterprise-agentgateway` Helm value
  `budgetDimensions.config`.
- **History:** synthetic spans are written straight into ClickHouse
  `platformdb.otel_traces_json`, shaped like real gateway spans, so the materialized views
  fill every UI table.
- **Live traffic:** real HTTP requests through a port-forward to the gateway.
- **Shared model:** both history and live traffic come from one deterministic org model
  and one usage model.

**Tech Stack:** Python ≥3.11, stdlib only, run with `uv run` (the system python is 3.9);
pytest via `uv run --with pytest`; kubectl and helm through subprocess; ClickHouse HTTP
interface (port 8123) through port-forward.

**Spec:** `docs/superpowers/specs/2026-10-21-td-demo-traffic-design.md`

## Global Constraints

- **Counts:** 1 organization (`TD`, domain `td.com`), 5 groups, 15 teams, 12 projects,
  35 users.
- **Tools and models:**
  - Copilot (about 20 users) → Azure AI Foundry GPT
  - a few of those Copilot users on BYOK → Mistral Large
  - Claude Code (about 10) → Bedrock Claude Opus
  - Claude Desktop (about 5) → Bedrock Claude Opus
- **No real provider calls.** Prices use real per-token rates from the repo's cost
  catalog.
- **History:** `--days` defaults to 30 (user, 2026-10-21). Synthetic spans stay at or below 150,000 per 7 days, roughly 650,000 for 30 days.
- **Budgets:** one per team, `onBudgetExceeded: Audit`, never `Block`. 1–2 over the limit,
  2 near it (85–95%), the rest under.
- **Marking:** every created k8s object carries label `demo: td`. Every synthetic span is
  identifiable by the `td_span_predicate()` SQL (Task 6).
- **Location:** code lives in `demos/td-traffic/`, never under `apps/`, because the Argo
  ApplicationSet syncs every `apps/<dir>`.
- **No collateral changes:** never modify or delete non-TD objects, except the
  `budgetDimensions` Helm value, which `up` saves and `down` restores exactly.
- **Capacity:** the mock pod requests at most `cpu: 25m` and `memory: 64Mi`. No new
  gateway proxy.
- **Explicit cluster:** every subcommand needs `--context <kube-context>`. There is no
  implicit current context.
- **Time:** simulated working hours are Toronto local time (`America/Toronto`) regardless
  of the laptop's time zone. Timestamps are stored in UTC.

## Review Focus

1. **Running `backfill` twice** must refuse with a clear message when TD history already
   exists, unless `--replace` is passed, which purges first. → Task 7 test
   `test_backfill_refuses_when_history_exists`.
2. **`down` after a partial or failed `up`, or on a clean cluster,** succeeds and only
   reports what it removed. → Task 5 test `test_down_commands_ignore_not_found`.
3. **Live mode across a gateway pod restart:** the CronJob `substrate-cert-restart`
   restarts deployments. The port-forward drop must lead to a reconnect, not a crash or
   hang. → Task 8 test `test_live_reconnects_after_connection_error`.
4. **A laptop in a non-Toronto time zone** must not shift the business-hour curve.
   → Task 2 test `test_activity_uses_toronto_time_not_local`.
5. **Wrong or bare cluster:** pointing at a context without the gateway or Solo UI must
   fail fast with a message naming what's missing, before any change. → Task 5 test
   `test_preflight_reports_missing_components`.

---

## File Structure

```
demos/td-traffic/
  README.md              # how to run: up / backfill / live / down, prerequisites
  td.py                  # CLI entrypoint (uv script header), subcommands only
  tdlib/__init__.py
  tdlib/org.py           # deterministic TD org: groups, teams, projects, users, virtual keys
  tdlib/usage.py         # traffic model: activity curve, tool→model, tokens, latency, errors, cost
  tdlib/pricing.py       # loads per-token rates from apps/agentgateway-system/model-costs.yaml
  tdlib/manifests.py     # renders all k8s YAML (mock, backends, routes, auth, keys, budgets)
  tdlib/kube.py          # kubectl/helm subprocess wrappers, port-forward context manager, preflight
  tdlib/clickhouse.py    # HTTP client: query, insert JSONEachRow, purge
  tdlib/spans.py         # synthetic request -> otel_traces_json row
  tdlib/live.py          # live traffic loop
  mock/server.py         # fake LLM (stdlib http.server), shipped in a ConfigMap
  values-dimensions.yaml # budgetDimensions.config for TD
  NOTES.md               # Task 4 verified platform facts
  tests/test_*.py
scripts/gen-model-costs.py               # Modify: add azure + mistral providers
apps/agentgateway-system/model-costs.yaml # Regenerated
```

Run all tests: `uv run --python 3.12 --with pytest pytest demos/td-traffic/tests -q`

---

### Task 1: Org model

**Files:**
- Create: `demos/td-traffic/tdlib/__init__.py` (empty), `demos/td-traffic/tdlib/org.py`
- Test: `demos/td-traffic/tests/test_org.py`

**Interfaces:**
- Produces:
  - `Tool = Literal["claude-code", "copilot", "claude-desktop"]`
  - `@dataclass(frozen=True) class User: email: str; name: str; group: str; team: str; tool: Tool; persona: Literal["power","regular","light"]; mistral_byok: bool`
  - `@dataclass(frozen=True) class VirtualKey: id: str; secret: str; user: User; project: str`
    - `id` is like `vk-<local-part>-<project-slug>`.
    - `secret` is a deterministic token derived from seed + id (for example
      `sha256(...).hexdigest()[:40]` prefixed `td-`).
  - `@dataclass(frozen=True) class Org: name: str; domain: str; groups: list[str]; teams: dict[str, str]; projects: list[str]; users: list[User]; keys: list[VirtualKey]`
    - `teams` maps team → group.
  - `build_org(seed: int = 42) -> Org`

- [ ] **Step 1: Write failing tests**

```python
from tdlib.org import build_org

def test_counts():
    o = build_org()
    assert (o.name, o.domain) == ("TD", "td.com")
    assert len(o.groups) == 5 and len(o.teams) == 15 and len(o.projects) == 12 and len(o.users) == 35

def test_tool_split_and_byok():
    o = build_org(); tools = [u.tool for u in o.users]
    assert tools.count("copilot") == 20 and tools.count("claude-code") == 10 and tools.count("claude-desktop") == 5
    assert 3 <= sum(u.mistral_byok for u in o.users) <= 5
    assert all(u.tool == "copilot" for u in o.users if u.mistral_byok)

def test_emails_unique_and_domain():
    o = build_org(); emails = [u.email for u in o.users]
    assert len(set(emails)) == 35 and all(e.endswith("@td.com") for e in emails)

def test_every_team_and_project_used():
    o = build_org()
    assert {u.team for u in o.users} == set(o.teams)
    assert {k.project for k in o.keys} == set(o.projects)

def test_keys_unique_and_deterministic():
    a, b = build_org(), build_org()
    assert [k.secret for k in a.keys] == [k.secret for k in b.keys]
    assert len({k.id for k in a.keys}) == len(a.keys) and len({k.secret for k in a.keys}) == len(a.keys)
    assert all(any(k.user == u for k in a.keys) for u in a.users)
```

- [ ] **Step 2: Run and confirm the tests fail**
  - Run: `uv run --python 3.12 --with pytest pytest demos/td-traffic/tests/test_org.py -q`
  - Expected: `ModuleNotFoundError: tdlib.org`
  - Add `demos/td-traffic/tests/conftest.py` inserting `demos/td-traffic` on `sys.path`.

- [ ] **Step 3: Implement `build_org` with hard-coded name tables and a seeded `random.Random`**
  - **Groups:** `Personal Banking`, `Wealth Management`, `TD Securities`,
    `Enterprise Technology`, `Risk & Compliance`.
  - **Teams:** 15, spread 3 per group, with plausible bank team names.
  - **Projects:** 12, cross-team, for example `Mortgage Digital Onboarding` and
    `Fraud Signals Platform`.
  - **Personas:** Claude Code users are mostly `power`, Copilot users mostly
    `light`/`regular`, Desktop users `regular`.
  - **Keys:** each user gets 1 key, or 2 when they span two projects (about a third of
    users).

- [ ] **Step 4: Run tests, expect PASS**

- [ ] **Step 5: Commit**
  - `git add demos/td-traffic/tdlib demos/td-traffic/tests`
  - `git commit -m "td-traffic: deterministic TD org model"`

---

### Task 2: Pricing and usage model

**Files:**
- Create: `demos/td-traffic/tdlib/pricing.py`, `demos/td-traffic/tdlib/usage.py`
- Test: `demos/td-traffic/tests/test_usage.py`

**Interfaces:**
- Consumes: `Org`, `User`, `VirtualKey`, `Tool` (Task 1).
- Produces:
  - `MODELS: dict[str, ModelRoute]`
    - `@dataclass(frozen=True) class ModelRoute: route: str; provider: str; model: str; catalog_provider: str; catalog_model: str`
    - Keys: `"azure-gpt"`, `"azure-gpt-mini"`, `"bedrock-opus"`, `"mistral-large"`.
    - `provider` is the UI label (`azure`|`bedrock`|`mistral`).
    - `route` is the HTTP path prefix, for example `/azure/gpt-5-4`.
    - `catalog_*` fields are filled per Task 4 NOTES.md.
  - `load_rates(path: Path) -> dict[tuple[str, str], Rates]`
    - `Rates(input: Decimal, output: Decimal, cache_read: Decimal)`, in USD per 1M tokens.
  - `@dataclass class Request: ts: datetime; key: VirtualKey; route: ModelRoute; user_agent: str; input_tokens: int; output_tokens: int; cache_read_tokens: int; latency_ms: int; status: int`
  - `activity(user: User, at_utc: datetime) -> float`, expected requests per minute.
  - `sample_request(key: VirtualKey, at_utc: datetime, rng: random.Random) -> Request`
  - `generate(org: Org, start_utc: datetime, end_utc: datetime, seed: int) -> Iterator[Request]`
    - Ordered by `ts`, Poisson over `activity`.
  - `cost(req: Request, rates) -> dict[str, Decimal]`
    - Keys: `input`, `output`, `cache_read`, `total`.
  - `USER_AGENTS: dict[Tool, list[str]]`

- [ ] **Step 1: Write failing tests**

```python
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import random, os, time
from tdlib.org import build_org
from tdlib.usage import generate, activity, sample_request, cost, MODELS
from tdlib.pricing import load_rates

TOR = ZoneInfo("America/Toronto")
def at(y,m,d,h): return datetime(y,m,d,h,tzinfo=TOR).astimezone(timezone.utc)

def test_tool_matches_model():
    o = build_org(); rng = random.Random(1)
    for k in o.keys:
        r = sample_request(k, at(2026,10,20,11), rng)
        if k.user.tool in ("claude-code","claude-desktop"): assert r.route.provider == "bedrock"
        elif k.user.mistral_byok: assert r.route.provider in ("mistral","azure")
        else: assert r.route.provider == "azure"

def test_business_hours_and_weekends():
    u = next(u for u in build_org().users if u.persona == "power")
    assert activity(u, at(2026,10,20,11)) > 5 * activity(u, at(2026,10,20,3))   # Tue 11:00 vs 03:00
    assert activity(u, at(2026,10,20,11)) > 3 * activity(u, at(2026,10,18,11))  # Tue vs Sun
    assert activity(u, at(2026,10,20,3)) > 0

def test_activity_uses_toronto_time_not_local(monkeypatch):
    u = build_org().users[0]; t = at(2026,10,20,11)
    a = activity(u, t)
    monkeypatch.setenv("TZ", "Asia/Tokyo"); time.tzset()
    assert activity(u, t) == a

def test_volume_and_spend_are_realistic():
    o = build_org(); rates = load_rates(RATES_PATH)
    end = at(2026,10,21,0); reqs = list(generate(o, end - timedelta(days=7), end, seed=7))
    assert 20_000 < len(reqs) < 300_000
    spend = sum(cost(r, rates)["total"] for r in reqs)
    assert 10_000 < spend < 18_000          # user: 10k+ per 7 days (≈ $45–75k/month)
    by_provider = {}
    for r in reqs: by_provider[r.route.provider] = by_provider.get(r.route.provider, 0) + cost(r, rates)["total"]
    assert by_provider["azure"] >= 3 * by_provider["bedrock"]        # user: Azure ≥ 3× each other provider
    assert by_provider["azure"] >= 3 * by_provider["mistral"]
    assert by_provider["azure"] > by_provider["bedrock"] > by_provider["mistral"] > 0
    errs = sum(r.status >= 400 for r in reqs) / len(reqs)
    assert 0.005 < errs < 0.03 and any(r.status == 429 for r in reqs)

def test_cost_matches_rates():
    # 1M input @ rate in, 0 out -> cost == rate
    ...
```

  - `RATES_PATH` is the repo's `apps/agentgateway-system/model-costs.yaml`, resolved
    relative to the test file.
  - Complete `test_cost_matches_rates` using the `bedrock-opus` rates: a Request with
    `input_tokens=1_000_000`, `output_tokens=0` must give `cost(...)["input"] == rates.input`.

- [ ] **Step 2: Run, expect FAIL** with an import error.

- [ ] **Step 3: Implement `pricing.load_rates`**
  - Parse the YAML by extracting the `catalog.json: |` block and `json.loads` it, the same
    way `scripts/gen-model-costs.py` writes it.
  - Read rates as `Decimal`.

- [ ] **Step 4: Implement `usage.py`**
  - **Activity:** a Toronto-local hour curve, peaking 10–12 and 14–16 on weekdays.
    - Night floor: about 3% of peak.
    - Weekend: about 10% of peak.
    - Persona multipliers: power about 1.2 requests/min at peak; regular 0.35; light 0.12.
  - **Tokens:**
    - Claude Code: input lognormal, median about 28k, with 60% served as cache_read.
    - Copilot: input median about 2.5k.
    - Desktop: input median about 6k.
    - Output: median 400–1,200 by tool.
  - **Latency:** lognormal per model.
  - **Errors:** about 1.2% total; 429 is half of them, then 500/503/400.
  - **Azure must lead spend (user requirement):** Copilot users run agent mode with large contexts on `azure-gpt` (gpt-5.4 class); some Copilot power users exist. Azure spend must be at least 3× Bedrock and at least 3× Mistral (about 70%+ of total). Bedrock Opus second, Mistral smallest. Claude Code/Desktop volumes come down to fit.
  - **Model choice:**
    - Copilot non-BYOK: 70% `azure-gpt`, 30% `azure-gpt-mini`.
    - BYOK users: 80% `mistral-large`.
    - Claude tools: `bedrock-opus`.
  - Tune constants until `test_volume_and_spend_are_realistic` passes.
  - **User agents:** realistic strings such as `claude-cli/2.1.4 (external, cli)`,
    `GitHubCopilotChat/0.31.0 vscode/1.105.0`, `Claude/1.0.211 (Macintosh)`.

- [ ] **Step 5: Run tests, expect PASS**

- [ ] **Step 6: Commit** with `git commit -m "td-traffic: usage and pricing model"`

---

### Task 3: Fake LLM server

**Files:**
- Create: `demos/td-traffic/mock/server.py`
- Test: `demos/td-traffic/tests/test_mock_server.py`

**Interfaces:**
- Produces an HTTP server on `$PORT` (default 8080):
  - `GET /healthz` → 200.
  - `POST /v1/chat/completions` (and any path ending in `/chat/completions`) → an OpenAI
    chat completion JSON.
    - `model` echoes the request's model.
    - `usage.prompt_tokens`, `usage.completion_tokens` and
      `usage.prompt_tokens_details.cached_tokens` come from headers `x-td-input-tokens`,
      `x-td-output-tokens` and `x-td-cached-tokens`. Without the headers, it estimates
      `len(json body)//4` and 150.
  - `x-td-latency-ms` makes it sleep min(value, 3000) ms.
  - `x-td-status: <code>` returns that status with an OpenAI-style error body.
  - `"stream": true` → SSE chunks ending with a usage chunk and `data: [DONE]`.
- Run in tests and in the pod as `python server.py`.

- [ ] **Step 1: Write failing tests**
  - Start the server on a free port in a thread.
  - `test_usage_from_headers`: request with headers 31000/850/18000 → body usage
    31000/850, cached 18000.
  - `test_status_injection`: `x-td-status: 429` → HTTP 429 with JSON `error.type ==
    "rate_limit_exceeded"`.
  - `test_stream_ends_with_usage_and_done`.
  - `test_healthz`.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**
  - Use `http.server.ThreadingHTTPServer`, stdlib only.
  - Keep the file under about 120 lines, because it ships in a ConfigMap.

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit** with `git commit -m "td-traffic: fake LLM server"`

---

### Task 4: Verify platform facts on omni-viper (spike)

This task decides facts that Tasks 5–7 depend on. It writes `NOTES.md`; any spike
resources are deleted at the end. Use `--context maniak-omni-viper`, and label every
object `demo: td`.

**Files:**
- Create: `demos/td-traffic/NOTES.md`

**Interfaces:**
- Produces `NOTES.md`, with the exact answers below. Tasks 2, 5, 6 and 7 read it, and
  `usage.MODELS` `catalog_*` fields are updated to match.

- [ ] **Step 1: Deploy the mock and a test route**
  - Deploy the mock from Task 3 in namespace `td-demo`: a ConfigMap holding `server.py`,
    a Deployment on `python:3.12-slim`, and a Service on port 8080.
  - Add one `AgentgatewayBackend` with `spec.ai.provider.custom`:
    `providerOverride: azure`, `model: gpt-5.4`,
    `formats: [{type: Completions}]`, and `backendRef` to the Service on port 8080.
  - Add one HTTPRoute on `agentgateway-proxy` with `hostnames: [td.demo.local]` and path
    `/azure/gpt-5-4`.

- [ ] **Step 2: Answer these by trying them, and record each with the working YAML snippet**
  1. **Virtual-key Secret format.** Use `EnterpriseAgentgatewayPolicy.traffic.apiKeyAuthentication`
     with a `secretRef` to a Secret whose value carries metadata. Try a JSON value
     `{"key":"td-...","metadata":{"organization":"TD","group":"...","team":"...","project":"...","user":"...@td.com"}}`.
     Confirm that `apiKey.organization`, `apiKey.team` and the others resolve in CEL by
     checking the span's `agw.llm.custom_dimensions` in ClickHouse.
  2. **Request headers in CEL.** Check whether `request.headers["user-agent"]` works in
     a dimension expression. If not, record it; the fallback is `apiKey.client` key
     metadata, which means one key per tool.
  3. **Provider label.** Find what `custom_dimensions` `provider` shows for
     `providerOverride` values `azure`, `bedrock` and `mistral`, and which catalog
     provider key prices them. Confirm whether `agw.ai.usage.cost.total` is non-zero for
     each, and which catalog provider/model names make it non-zero.
  4. **Whether the dimension list must come from the ConfigMap.** Check if the UI's Cost
     Management dimension filters show the Helm ConfigMap dimensions, or whether
     Gateway-level `standardAttributes` would also appear. Decide which one Task 5 uses.
     Prefer the ConfigMap via Helm.
  5. **A complete live TD span row,** captured from `otel_traces_json` as
     `FORMAT JSONEachRow`. Save it as `demos/td-traffic/tdlib/span_template.json`, with
     pod IPs and UIDs left as-is.
  6. **ClickHouse HTTP access:** user and password from Secret `management-clickhouse-auth`,
     pod `management-clickhouse-shard0-0`, port 8123.

- [ ] **Step 3: Update `MODELS` and `pricing`**
  - Update `usage.MODELS` catalog fields to the verified names.
  - If azure/mistral rates are missing from `model-costs.yaml`, note "Task 6 required".
    Otherwise mark Task 6 as skip.

- [ ] **Step 4: Delete spike resources** with `kubectl --context maniak-omni-viper delete ns td-demo`,
  then remove the spike route and policy by the `demo=td` label.

- [ ] **Step 5: Commit** `NOTES.md` and `span_template.json` with
  `git commit -m "td-traffic: verified platform notes"`

---

### Task 5: Manifests, kube helpers, `up` / `down`

**Files:**
- Create: `demos/td-traffic/tdlib/manifests.py`, `demos/td-traffic/tdlib/kube.py`,
  `demos/td-traffic/td.py`, `demos/td-traffic/values-dimensions.yaml`
- Test: `demos/td-traffic/tests/test_manifests.py`, `demos/td-traffic/tests/test_kube.py`

**Interfaces:**
- Consumes: `build_org`, `MODELS`, `generate`, `cost`, `load_rates` (Tasks 1–2);
  NOTES.md facts (Task 4).
- Produces:
  - `render_all(org: Org, budgets: list[dict], mock_source: str) -> list[dict]`, a list
    of k8s objects as dicts.
  - `plan_budgets(org, rates, seed) -> list[dict]`, one budget entry per team.
  - `class Kube(context: str)` with these methods:
    - `run(args: list[str], check=True, input: str | None = None) -> str`
    - `apply(objs: list[dict])`
    - `delete_labeled(kinds: list[str], namespace: str)`
    - `preflight() -> list[str]`, the list of missing component names
    - `port_forward(target: str, ns: str, remote: int) -> ContextManager[int]`, yielding
      the local port
    - `helm_get_values(release, ns) -> dict`
    - `helm_upgrade_reuse(release, chart, version, ns, values_file: Path)`
  - CLI: `uv run demos/td-traffic/td.py {up,backfill,live,down,status} --context CTX [...]`

- [ ] **Step 1: Write failing tests (`test_manifests.py`)**
  - `test_all_objects_labeled`: every object from `render_all` has
    `metadata.labels.demo == "td"`.
  - `test_routes_only_on_td_hostname`: every HTTPRoute has
    `spec.hostnames == ["td.demo.local"]` and parentRef `agentgateway-proxy`.
  - `test_one_budget_per_team_audit_only`: there are 15 entries in total, each
    `onBudgetExceeded == "Audit"`, `window.unit == "Month"`, and `subject` contains
    `team`.
  - `test_budget_mix`: compute the projected month spend from `generate` over the trailing
    days of the current month plus the live rate. Then 1–2 teams sit above 100%, exactly 2
    sit in 85–95%, and the rest are below 75%.
  - `test_mock_pod_requests_small`: CPU `25m` and memory `64Mi`.
  - `test_keys_secret_contains_all_keys`: the secret has `len(org.keys)` entries, in the
    NOTES.md format.

- [ ] **Step 2: Write failing tests (`test_kube.py`, kubectl faked by monkeypatching `subprocess.run`)**
  - `test_every_call_passes_context`: each argv contains `--context`, `CTX` (or
    `--kube-context` for helm).
  - `test_down_commands_ignore_not_found`: delete argv contains `--ignore-not-found`, and
    a fake "not found" result does not raise.
  - `test_preflight_reports_missing_components`: when a fake returns NotFound for
    `gatewayclass enterprise-agentgateway` and `svc solo-enterprise-ui`, `preflight()`
    returns both names, and `td.py up` exits non-zero before any `apply` call.

- [ ] **Step 3: Run, expect FAIL.**

- [ ] **Step 4: Implement**
  - **Objects:** namespace `td-demo`; mock ConfigMap, Deployment and Service; 4 backends
    from `MODELS`; 4 HTTPRoutes; one `EnterpriseAgentgatewayPolicy` (apiKeyAuthentication,
    `mode: Strict`) targeting the 4 routes; the keys Secret; and one
    `EnterpriseAgentgatewayBudget` named `td-team-budgets` in `agentgateway-system`.
  - **Apply:** pipe JSON to `kubectl apply -f -`.
  - **`values-dimensions.yaml`** sets `budgetDimensions.config`:
    - hierarchy: `organization` → `group` → `team` → `user`, each `coalesce(apiKey.X, jwt...)`.
    - Keep the current user expression's fallbacks so real traffic still gets a user.
    - attributes: `project`, `client` (header or key, per NOTES.md), `virtualKey`.
  - **`up`:**
    1. Run preflight and exit 1 if anything is missing.
    2. Save `helm get values enterprise-agentgateway -o json` to the ConfigMap
       `td-demo-saved-values` in `td-demo`, but only if it doesn't already exist, so a
       re-run never overwrites the original.
    3. Apply the objects.
    4. Run `helm upgrade --reuse-values -f values-dimensions.yaml` at the installed chart
       version, read from `helm list -o json`.
    5. Wait for the mock rollout.
  - **`down`:**
    1. If `td-demo-saved-values` exists, restore `budgetDimensions` from it with
       `helm upgrade --reuse-values` using the saved `budgetDimensions` block. If the
       saved values have no such key, use the chart default by dropping the key with
       `--reset-then-reuse-values`, minus the TD file.
    2. Delete the budget, policy, routes and backends by label, then the namespace.
  - **`status`:** prints the counts of TD objects and TD span rows.

- [ ] **Step 5: Run unit tests, expect PASS.**

- [ ] **Step 6: Check on omni-viper**
  - Run `uv run demos/td-traffic/td.py up --context maniak-omni-viper`, then:

    ```
    curl -s -H 'Host: td.demo.local' -H "Authorization: Bearer <a key>" -H 'User-Agent: claude-cli/2.1.4' \
      localhost:<pf>/bedrock/claude-opus-5-5/v1/chat/completions -d '{"model":"","messages":[{"role":"user","content":"hi"}]}'
    ```
  - Expect 200, and a span in ClickHouse whose `custom_dimensions` include
    `organization=TD`, team, project, client and provider `bedrock`, with a non-zero cost.
  - The same request without a key → 401.
  - Run `down`, then `up` again: it is idempotent.

- [ ] **Step 7: Commit** with `git commit -m "td-traffic: up/down with manifests, keys, budgets, dimensions"`

---

### Task 6: Azure and Mistral prices in the catalog (skip if NOTES.md says the rates exist)

**Files:**
- Modify: `scripts/gen-model-costs.py`
- Regenerate: `apps/agentgateway-system/model-costs.yaml`

**Interfaces:**
- Produces catalog providers named exactly as NOTES.md item 3 found, containing the
  `MODELS` catalog models.

- [ ] **Step 1: Add the models.dev provider IDs** for Azure (`azure`) and Mistral
  (`mistral`) to the script's provider list, mapped to the catalog provider keys from
  NOTES.md. Do this the same way the script already maps `amazon-bedrock` to
  `aws.bedrock`.
- [ ] **Step 2: Regenerate**
  - Run `uv run scripts/gen-model-costs.py` (or however the script header says to run it).
  - Expect `git diff --stat` to show only `model-costs.yaml`, with new `azure`/`mistral`
    provider blocks and no removed openai/bedrock models.
- [ ] **Step 3: Sync and check pricing**
  - Push the branch and let Argo sync `agentgateway-system`. Before merge, use
    `kubectl apply -f` for a quick check.
  - Re-run the Task 5 step 6 curl against `/azure/...` and `/mistral/...`; expect a
    non-zero `agw.ai.usage.cost.total`.
- [ ] **Step 4: Commit** with `git commit -m "model-costs: add azure and mistral providers"`

---

### Task 7: History backfill and purge

**Files:**
- Create: `demos/td-traffic/tdlib/clickhouse.py`, `demos/td-traffic/tdlib/spans.py`
- Modify: `demos/td-traffic/td.py` (add `backfill`, `down --purge-history`)
- Test: `demos/td-traffic/tests/test_spans.py`, `demos/td-traffic/tests/test_backfill.py`

**Interfaces:**
- Consumes: `generate`, `cost`, `load_rates` (Task 2); `span_template.json` (Task 4);
  `Kube.port_forward` (Task 5).
- Produces:
  - `to_row(req: Request, rates, template: dict, cluster: str) -> dict`, an
    `otel_traces_json` JSONEachRow row.
  - `class ClickHouse(url, user, password)` with these methods:
    - `query(sql) -> str`
    - `insert_rows(table, rows: Iterable[dict], batch=20_000)`
    - `count_td() -> int`
    - `purge_td() -> None`
  - `td_span_predicate() -> str`, a SQL boolean: the span attribute
    `agw.llm.custom_dimensions` contains `["organization","TD"]`.

- [ ] **Step 1: Write failing tests (`test_spans.py`)**
  - `test_row_has_template_shape`: `to_row(...)` has exactly the same top-level keys as
    the template.
  - `test_row_ids`: `ServiceName == "agentgateway-proxy"`, `ScopeName == "agentgateway"`,
    and `SpanKind == "Server"`.
  - `test_row_timestamps`: `Timestamp` is the req ts in UTC, with nanosecond formatting
    matching the template.
  - `test_row_cost_and_dimensions`: `Duration == latency_ms * 1_000_000`. Cost strings
    equal `str(cost(...)[k])`. `custom_dimensions` contains organization, group, team,
    project, user, client, provider and model, in the same order and format as the
    template.
  - `test_row_status`: a status ≥ 500 gives `StatusCode == "Error"`.
  - `test_unique_ids`: TraceId and SpanId are unique across 10,000 rows.

- [ ] **Step 2: Write failing tests (`test_backfill.py`, ClickHouse faked)**
  - `test_backfill_refuses_when_history_exists`: when `count_td() > 0` and there's no
    `--replace`, it exits non-zero with a message naming `--replace` and does no insert.
  - `test_backfill_replace_purges_first`: `purge_td` is called before the first insert.
  - `test_backfill_window_ends_now`: the max generated ts is ≤ now, and the min is ≥
    now − days.

- [ ] **Step 3: Run, expect FAIL.**

- [ ] **Step 4: Implement**
  - **`to_row`:** deep-copy the template and overwrite the varying fields.
  - **`ClickHouse`:**
    - Use `urllib` POST to `/?query=...`, with `INSERT INTO platformdb.otel_traces_json
      FORMAT JSONEachRow` and `input_format_import_nested_json=1` as needed for the JSON
      columns. Check this against the template.
    - Batches go through a port-forward to `management-clickhouse-shard0-0:8123`.
    - Credentials come from Secret `management-clickhouse-auth`.
  - **`backfill --days N [--replace]`** (default 7), from now−N days to now.
  - **`purge_td` algorithm** (rollup tables can't be filtered by TD, because real and fake
    Bedrock Opus share per-model rows):
    1. Read `min(Timestamp)` of TD rows. Use `ALTER TABLE ... DELETE WHERE <predicate>`
       with `mutations_sync=2` on `otel_traces_json`, and the matching filter on
       `agw_spans_typed` and `agw_chat_spans`.
    2. For each `agw_*_5m` and `agentgateway_*_minute_state` table fed by those views:
       delete rows with time ≥ `toStartOfInterval(min_ts)`. Then re-insert by running
       that view's `as_select` (from `system.tables`) over source rows in the same range.
       Read the source table and time column from the view definition.
    3. Leave tables outside `platformdb.agw*` and `agentgateway_*` alone.

- [ ] **Step 5: Run unit tests, expect PASS.**

- [ ] **Step 6: Check on omni-viper**
  1. Snapshot real totals:
     `SELECT model, sum(...) FROM` each rollup `WHERE NOT TD` grouped by model.
  2. Run `td.py backfill --context maniak-omni-viper`. It completes in under about 5
     minutes, and `status` shows about 20–60k spans.
  3. In the UI at `http://<node>:30400/age/`, check:
     - Cost Management shows 7 days of TD spend by Organization → Group → Team → User.
     - Project and Client filters are populated.
     - Budgets show 1–2 over and 2 near.
     - Traces lists TD requests.
  4. Running `backfill` again refuses.
  5. Run `down --purge-history`. Re-run the snapshot query: real totals are identical, and
     `status` shows 0 TD spans.
  6. Run `up` and `backfill` again to leave the demo in place.

- [ ] **Step 7: Commit** with `git commit -m "td-traffic: backfill and history purge"`

---

### Task 8: Live traffic

**Files:**
- Create: `demos/td-traffic/tdlib/live.py`
- Modify: `demos/td-traffic/td.py` (add `live`)
- Test: `demos/td-traffic/tests/test_live.py`

**Interfaces:**
- Consumes: `generate`/`sample_request`, `activity` (Task 2); `Kube.port_forward`
  (Task 5); `MODELS`.
- Produces:
  - `run_live(kube: Kube, org: Org, *, speed: float = 1.0, min_rpm: float = 6.0, concurrency: int = 4, duration: float | None = None, sender=None) -> Stats`
    - `sender(req: Request, base_url: str) -> int` is injectable; the default is urllib.
  - `td.py live --context CTX [--speed X] [--duration 10m]`

- [ ] **Step 1: Write failing tests (fake sender, fake clock)**
  - `test_live_sends_headers`: each send carries Host `td.demo.local`, `Authorization:
    Bearer <key.secret>`, the tool's User-Agent, and the `x-td-*` token, latency and
    status headers from the Request.
  - `test_live_rate_floor`: at Toronto 03:00 Sunday, a 10-minute simulated run sends
    ≥ `min_rpm * 10 * 0.8` requests.
  - `test_live_reconnects_after_connection_error`: the fake sender raises
    `ConnectionRefusedError` 3 times. `run_live` re-opens the port-forward (the
    `port_forward` fake has been entered ≥ 2 times) and continues sending. It does not
    raise.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**
  - Use Poisson scheduling from `activity` × `speed`, floored at `min_rpm` and spread
    across users proportionally.
  - Run a `ThreadPoolExecutor(concurrency)`.
  - On a connection error, close and re-open the port-forward with backoff of 1, 2, 4
    seconds up to 30.
  - Every 30 seconds, print a one-line summary: sent, status counts, spend so far.
  - Ctrl-C closes the port-forward cleanly.

- [ ] **Step 4: Run tests, expect PASS.**

- [ ] **Step 5: Check on omni-viper**
  - Run `td.py live --context maniak-omni-viper --duration 5m`.
  - The UI request graphs show a fresh tail. New traces open with TD dimensions. The
    client breakdown shows all three tools.
  - Run `kubectl rollout restart deploy/agentgateway-proxy` mid-run: live logs a
    reconnect and continues.

- [ ] **Step 6: Commit** with `git commit -m "td-traffic: live traffic mode"`

---

### Task 9: README and end-to-end run

**Files:**
- Create: `demos/td-traffic/README.md`
- Modify: `README.md` (one line linking the demo, in the existing docs list style)

- [ ] **Step 1: Write the README**
  - Prerequisites: uv, kubectl, helm, enterprise agentgateway and the Solo UI on the target
    cluster.
  - The four commands with flags. What `up` changes on the cluster, including the Helm
    value and that it gets restored.
  - What `down` versus `down --purge-history` removes.
  - A demo-day checklist: `up` → `backfill` → start `live` 10 minutes before the call.
  - Known limits: fake calls; TD routes only reachable with Host `td.demo.local`.
- [ ] **Step 2: Clean end-to-end run on omni-viper**
  - Run `down --purge-history` → `up` → `backfill` → `live --duration 2m` → `status`.
  - Every step exits 0. Leave the demo running.
- [ ] **Step 3: Run the full test suite**
  - Run `uv run --python 3.12 --with pytest pytest demos/td-traffic/tests -q`.
  - Expect all tests to pass.
- [ ] **Step 4: Commit** with `git commit -m "td-traffic: README and demo checklist"`
