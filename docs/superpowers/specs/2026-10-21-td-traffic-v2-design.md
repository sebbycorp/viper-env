# TD demo traffic v2: no fake LLM, one-file download

Date: 2026-10-21. This builds on `2026-10-21-td-demo-traffic-design.md`, which is merged in
PR #21. Everything in that design stands unless this document changes it.

## Intent

Sebastian wants this to be easy for anyone to download and run against any cluster. Nothing
should run in the cluster: no fake LLM server. The tool only puts configuration into
agentgateway and data into ClickHouse.

## Goals

- **One downloadable file.** `td-traffic.pyz` is attached to a GitHub Release, and the
  latest-release URL always works.
  - Run it with `python3 td-traffic.pyz <cmd> --context <ctx>` (Python 3.11 or later), or
    with `uv run --python 3.12 td-traffic.pyz ...`.
  - It also needs kubectl and helm. No git clone.
- **No in-cluster workload.** Remove the mock Deployment, Service and ConfigMap entirely.
- **Agentgateway gets configuration only.** That means:
  - the `td-demo` namespace;
  - the virtual-key Secret, so all 51 keys show on the Virtual Keys page;
  - the API-key policy, with budget enforcement;
  - the 41-entry `td-budgets`;
  - the dimensions Helm value, saved and restored as before;
  - Azure, Bedrock and Mistral backends with HTTPRoutes on `td.demo.local`, pointing at
    the real provider endpoints. They are never called, and there are no provider secrets.
    They exist so the inventory and the provider labels look real.
- **ClickHouse gets all the data.**
  - **History:** the 30-day backfill, unchanged.
  - **Live:** `live` writes freshly generated spans straight into ClickHouse in small
    batches every few seconds. It uses the same generator and row shape as the backfill,
    timestamped "now". No HTTP requests are sent and there is no port-forward to the
    gateway.
  - **Budgets:** `sync-budgets` writes one zero-cost outcome row per budget entry, setting
    each entry's displayed spend to its target. All 41 entries are covered, which fixes
    the per-user and project entries that stayed at 0%.
- **One command:** `demo` runs preflight, `up`, `backfill --replace`, then `sync-budgets`.
- **Cleanup:** `down`, plus the existing `--purge-history`, which also removes the outcome
  rows.

## Non-goals

- Real requests through agentgateway. The Playground and calls to TD routes will fail, and
  that is accepted.
- Keeping the gateway's Redis budget counters in sync. They only matter if the gateway
  enforces budgets on real traffic.
- A container image or an in-cluster Job.

## Verified facts (spike, 2026-10-21, omni-viper)

- **Where the Budgets page reads spend.** The UI backend takes, per entry,
  `argMaxIf(current_usage, Timestamp)` over rows in `platformdb.otel_traces_json` that
  match all of these:
  - `ScopeName='agentgateway'`
  - `SpanName='agw.ai.budgets.outcome'`
  - `SpanAttributes.agw.ai.budgets.phase='final_debit_result'`
  - an entry in `SpanAttributes.agw.ai.budgets.statuses`, a JSON string holding an array of
    `{"id":"<ns>/<budget>/<entry>","current_usage":<µUSD or tokens>}`
  - `Timestamp` between the budget's creation time and now

  Redis is never read for display. An entry with no outcome row shows 0.
- **Which row wins.** The newest row wins, so `live` must not create outcome rows for
  budgeted entries. Our own spans carry no budget outcome, so this holds. Future-dated rows
  are ignored.
- **How to keep outcome rows out of the cost totals.** Give each row a non-empty
  `ParentSpanId`, no route, model or custom dimensions, and zero cost. Such a row feeds only
  `agw_spans_typed` and `trace_time_index`, and no cost rollups.
- **How to make them purgeable.** Setting `server.address='td.demo.local'` makes the
  existing TD purge predicate catch them.

## Decisions (my calls)

- **Packaging:** `python -m zipapp` builds the file. Data files are loaded with
  `importlib.resources`, or `zipfile`-aware reads, so they work from inside the `.pyz`.
- **Release:** a GitHub Action on tag `td-traffic-v*` builds the file, runs the tests and
  attaches the `.pyz` to the release.
  - `scripts/build-td-traffic.sh` does the same build locally.
- **Live:**
  - Write rate follows the existing usage model. Speed and the min-rpm floor stay as they
    are.
  - Before each batch, compute the current spend per budget entry and re-write its outcome
    row, so budgets creep up during the demo, about once a minute.
  - Ctrl-C exits cleanly.
- **Budget mix:** the targets are unchanged.
  - Team USD: 1–2 over, 2 near, the rest under.
  - Groups and org: 60–90%.
  - Projects: at least 1 over and at least 1 near.
  - Users: at least 1 near.
  - Daily token budgets: 50–90%.

  Every entry now gets a value.
- **Remove the code that no longer applies:** `mock/server.py`, the HTTP sender in
  `live.py`, the marker-request and marker-purge flow in `budget_sync.py`, and gateway
  port-forwarding. Keep the shared generator, rows, purge and manifests.
- **Docs:** the README Quickstart becomes "download → demo → live → down", and the old
  clone path moves to a "from source" footnote.

## Left to the builder

- Exact batch sizes and intervals.
- How the provider-endpoint backends are expressed. Native `azure`, `bedrock` and `mistral`
  or `custom` providers with real hosts are both fine, as long as no secret is needed and
  the provider labels stay `azure`, `aws.bedrock` and `mistral`.
- Release-notes text.
