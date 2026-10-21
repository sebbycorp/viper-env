# TD Traffic v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `demos/td-traffic` becomes a single downloadable `td-traffic.pyz`. It writes all
data straight into ClickHouse and applies only configuration to agentgateway, with no
in-cluster workload.

**Architecture:**
- **Removed:** the mock LLM, the HTTP sender and the marker-request budget sync.
- **Budgets:** displayed spend comes from zero-cost `agw.ai.budgets.outcome` rows written
  to `otel_traces_json`.
- **Live:** `live` writes generated spans into ClickHouse on a timer.
- **Packaging:** `zipapp` with resource loading that works inside a zip. A GitHub Action
  builds the release asset.

**Tech Stack:** Python ≥3.11 stdlib, pytest via uv, kubectl/helm, ClickHouse HTTP, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-21-td-traffic-v2-design.md`. It builds on
`docs/superpowers/specs/2026-10-21-td-demo-traffic-design.md`.

## Global Constraints

- **Nothing runs in the cluster.** No Deployment, Pod, Job or Service is created. Only
  these are applied, all labeled `demo: td`:
  - Namespace
  - Secret (keys)
  - EnterpriseAgentgatewayPolicy
  - EnterpriseAgentgatewayBudget
  - AgentgatewayBackend
  - HTTPRoute
  - the `td-demo-saved-values` ConfigMap
- **No HTTP requests to the gateway** and no gateway port-forward. The only port-forward is
  to ClickHouse.
- **Backends point at real provider endpoints, need no secret, and are never called.** The
  provider labels stay `azure`, `aws.bedrock` and `mistral`.
- **Outcome rows:**
  - `ScopeName='agentgateway'`, `SpanName='agw.ai.budgets.outcome'`
  - `SpanAttributes.agw.ai.budgets.phase='final_debit_result'`
  - `SpanAttributes.agw.ai.budgets.statuses` is a JSON *string* holding an array of
    `{"id":"<ns>/<budget>/<entry>","current_usage":<int µUSD or tokens>}`
  - non-empty `ParentSpanId`, no route, model or custom dimensions, zero cost
  - `SpanAttributes.server.address='td.demo.local'`
  - Timestamp at or after the budget's `creationTimestamp` and at or before now
- **Budget mix after `demo`:**
  - Team USD: 1–2 at 100–130%, exactly 2 at 85–95%, the rest under 75%.
  - Groups and org: 60–90%.
  - Projects: at least 1 over and at least 1 near.
  - Users: at least 1 near.
  - Daily token budgets: 50–90%.
- **Python ≥3.11 stdlib only, and every subcommand requires `--context`.** The `.pyz` runs
  with `python3 td-traffic.pyz` and with `uv run --python 3.12 td-traffic.pyz`.
- **Unchanged from v1:** Helm save/restore, the purge safety model (backup, ledger,
  pre-verify and post-verify), Toronto time, and the 30-day default.
- **Commit trailer:** end every commit message with a blank line then:
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01GozS4eDD6ysJdoGbdnppXz`

## Review Focus

1. **Running `live` and then `demo` again** must not double the history.
   `backfill --replace` purges live-written spans too, because they match
   `td_span_predicate`. Test: `test_live_rows_match_td_predicate` (Task 3).
2. **Outcome rows dated before the budget's creation are ignored by the UI.** After `up`
   re-creates the budget, `sync-budgets` must read `creationTimestamp` and date its rows
   after it. Test: `test_outcome_rows_after_budget_creation` (Task 2).
3. **Running from the `.pyz`, not the source tree.** Every data file (`span_template.json`,
   `rates_builtin.json`, `values-dimensions.yaml`) must load from inside the zip. Test:
   `test_pyz_runs_help_and_loads_resources` (Task 4), which builds the pyz and runs it from
   a temp dir.
4. **Upgrading from v1 on a cluster where the v1 mock exists.** `up` and `down` must remove
   the old mock objects (`td-mock-llm` Deployment, Service and ConfigMap). Test:
   `test_down_removes_legacy_mock` (Task 1).
5. **Outcome rows must never move cost totals.** Test: `test_outcome_row_zero_cost_no_dims`
   (Task 2) asserts zero cost and no custom dimensions.

---

### Task 1: Config-only cluster footprint

**Files:** modify `tdlib/manifests.py`, `td.py` (`cmd_up`, `cmd_down`, preflight) and
`tests/test_manifests.py`/`tests/test_kube.py`; delete `mock/server.py` and
`tests/test_mock_server.py`.

**Interfaces:**
- `render_all(org, budgets) -> list[dict]` drops the `mock_source` parameter and all
  callers are updated.
- Backends become provider-endpoint backends:
  - The exact form is the builder's choice: native provider types with no auth, or
    `custom` with host and port pointing at the real API host.
  - Each must be Accepted by the controller.
  - `providerOverride` and labels stay as in `usage.MODELS`.
- `down` additionally deletes the legacy `td-mock-llm` Deployment, Service and ConfigMap,
  with `--ignore-not-found`.

- [ ] **Step 1: Failing tests**
  - `test_no_workloads_rendered`: no object of kind Deployment, Pod, Job, StatefulSet,
    DaemonSet or Service.
  - `test_backends_point_at_provider_hosts`: no backend references a `td-demo` Service, and
    every host is a public provider domain.
  - `test_down_removes_legacy_mock`: fake kubectl sees deletes of
    `deploy/td-mock-llm svc/td-mock-llm cm/td-mock-llm` with `--ignore-not-found`.
- [ ] **Step 2: Run and confirm they fail.** Then implement and run the full suite to green.
- [ ] **Step 3: Live check on `maniak-omni-viper`.**
  - Run `up`.
  - `kubectl get deploy,svc,pod -n td-demo` returns nothing.
  - All 4 backends and routes are Accepted.
  - The Virtual Keys page lists 51 keys.
- [ ] **Step 4: Commit.**

### Task 2: Budgets from outcome rows

**Files:** rewrite `tdlib/budget_sync.py` (shrink it); `tdlib/spans.py` (add the
outcome-row builder); `td.py` (`cmd_sync_budgets`); tests in `tests/test_budget_sync.py`.

**Interfaces:**
- `outcome_row(entries: dict[str, int], ts: datetime, budget_ref: str, cluster: str) -> dict`
  - `entries` maps entry name to `current_usage`, in µUSD for USD entries or tokens for
    token entries.
  - Returns an `otel_traces_json` row.
- `budget_targets(org, budgets: list[dict], now, seed) -> dict[str, int]` gives every
  entry's `current_usage`, from the generated window-to-date history plus live spend. Keep
  the existing window and sizing logic, and reuse `plan_budgets`'s numbers so the mix
  holds.
- `write_budget_outcomes(ch, kube, targets, now) -> None`
  - Reads the budget's `metadata.creationTimestamp` via kube.
  - Writes outcome rows, chunked if needed, with `ts = max(now - 1s, creation + 1s)`.
- Delete the marker-request, ListBudgets-subtraction and marker-purge paths.

- [ ] **Step 1: Failing tests.**
  - `test_outcome_row_shape`: exact keys from the Global Constraints, with `statuses` as a
    JSON string.
  - `test_outcome_row_zero_cost_no_dims`.
  - `test_outcome_rows_after_budget_creation`.
  - `test_targets_cover_all_41_entries`.
  - `test_targets_meet_mix` at window offsets of 1, 6.5, 17.6 and 29 days.
  - `test_outcome_rows_match_td_predicate`.
- [ ] **Step 2: Implement.** Run the full suite to green.
- [ ] **Step 3: Live check.**
  - Run `sync-budgets`.
  - ListBudgets, or the UI Budgets page, shows all 41 entries non-zero, meeting the mix.
    That includes the 5 user budgets and all 12 projects.
  - Save a screenshot to the workspace.
- [ ] **Step 4: Commit.**

### Task 3: Live writes to ClickHouse

**Files:** rewrite `tdlib/live.py`; `td.py` (`cmd_live`); `tests/test_live.py`.

**Interfaces:**
- `run_live(kube, org, *, speed=1.0, min_rpm=6.0, interval=5.0, duration=None, ch_factory=None, clock=None) -> Stats`
  - Every `interval` seconds it generates `[last, now)` with the existing
    `Scheduler.window`.
  - It converts requests with `spans.to_row` and inserts them in one batch.
  - About every 60 s it calls `write_budget_outcomes` with the updated targets.
  - On a ClickHouse connection error it reconnects the port-forward, with backoff 1, 2,
    4 … up to 30 s.
  - Ctrl-C exits cleanly.
- Remove `Gateway`, `urllib_sender`, `send_with_reconnect`, `headers`, `body` and
  `url_for`.

- [ ] **Step 1: Failing tests** (fake ClickHouse and fake clock).
  - `test_live_inserts_batches_each_interval`.
  - `test_live_rows_match_td_predicate`.
  - `test_live_refreshes_budget_outcomes`.
  - `test_live_reconnects_clickhouse`.
  - `test_live_rate_floor`.
- [ ] **Step 2: Implement.** Run the full suite to green.
- [ ] **Step 3: Live check.**
  - Run `live --duration 2m`.
  - New TD spans with all 5 client values land within the last 2 minutes.
  - The Budgets page values rise.
  - Nothing is left running.
- [ ] **Step 4: Commit.**

### Task 4: Packaging, release and docs

**Files:**
- Create `scripts/build-td-traffic.sh`, `.github/workflows/td-traffic-release.yml` and
  `demos/td-traffic/__main__.py`, if needed for zipapp.
- Modify the resource loading in `tdlib/spans.py`, `tdlib/pricing.py`, `tdlib/manifests.py`
  and `td.py` to use `importlib.resources`.
- Modify `demos/td-traffic/README.md` and `NOTES.md`.
- Add `tests/test_packaging.py`.

**Interfaces:**
- `scripts/build-td-traffic.sh [outdir]` produces `<outdir>/td-traffic.pyz`, default
  `dist/`, using `python3 -m zipapp` with an interpreter line of `/usr/bin/env python3`.
- Move `values-dimensions.yaml` into `tdlib/` so it travels as a package resource, and
  update its readers.
- The workflow runs on push of tag `td-traffic-v*`:
  1. checkout
  2. setup uv
  3. run the tests
  4. build
  5. `gh release create "$TAG" dist/td-traffic.pyz --title ... --notes ...`, using
     `GITHUB_TOKEN` with `contents: write`

- [ ] **Step 1: Failing test.** `test_pyz_runs_help_and_loads_resources` builds into a tmp
  dir, copies the pyz to another tmp dir, and runs `python3.12 td-traffic.pyz --help`.
  Check whether there is a resource self-check subcommand; if not, add a hidden
  `selfcheck` that loads every resource and exits 0. The test expects exit 0.
- [ ] **Step 2: Implement.** Run the full suite to green.
- [ ] **Step 3: Rewrite the README Quickstart.**
  - Prerequisites: Python 3.11+ (or uv), kubectl, helm, and a cluster with enterprise
    agentgateway and the Solo UI.
  - `curl -LO https://github.com/sebbycorp/viper-env/releases/latest/download/td-traffic.pyz`
  - `python3 td-traffic.pyz demo --context <ctx>`
  - `python3 td-traffic.pyz live --context <ctx>`
  - `python3 td-traffic.pyz down --purge-history --context <ctx>`

  Remove every mock and fake-LLM mention. State the known limits: the Playground and TD
  routes don't answer, and Redis counters differ from the page. Add a "From source"
  footnote.
- [ ] **Step 4: Update NOTES.md.** Cover how the budgets page reads ClickHouse (from the
  spike) and correct the "Enforced time" note to the creation time.
- [ ] **Step 5: End-to-end run on `maniak-omni-viper` from the built pyz,** not the source:
  `down --purge-history` → `demo` → `live --duration 2m` → `status`. All exit 0. Leave it
  up and loaded.
- [ ] **Step 6: Commit.**
