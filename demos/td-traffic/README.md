# TD Bank demo traffic for the agentgateway UI

A single-file tool that fills the Solo Enterprise agentgateway UI with synthetic usage from a
fictional TD Bank (@td.com) AI-assistant rollout: 30 days of history in Cost Management,
populated Budgets and Virtual Keys pages, and fresh traces on demo day. All data is synthetic.
Nothing runs in the cluster beyond config, and nothing is sent to a provider: spans are written
straight into the Solo UI's ClickHouse.

## Quickstart (run it locally)

Prerequisites on your laptop:

- Python 3.11+ (or [`uv`](https://docs.astral.sh/uv/)), `kubectl` and `helm` on the PATH.
- A kube context for a cluster that runs Solo Enterprise agentgateway (a Gateway, default
  `agentgateway-proxy`) and the Solo UI (management chart, with its ClickHouse).

Download the tool (the repo is private, so use `gh` with repo access):

```bash
gh release download -R sebbycorp/viper-env --pattern td-traffic.pyz
# if the repo is public, this works too:
# curl -fLO https://github.com/sebbycorp/viper-env/releases/latest/download/td-traffic.pyz
```

Run it. This form works on stock macOS (its `python3` is 3.9):

```bash
uv run --python 3.12 python td-traffic.pyz demo   --context <ctx>   # one-time setup, ~10 min, then it exits
uv run --python 3.12 python td-traffic.pyz live   --context <ctx>   # keeps running until Ctrl-C
uv run --python 3.12 python td-traffic.pyz status --context <ctx>   # object and span counts, budget window
uv run --python 3.12 python td-traffic.pyz down --purge-history --context <ctx>   # remove everything
```

With Python 3.11+ installed, `python3 td-traffic.pyz <command> --context <ctx>` does the same.

- `demo` is the one-time setup: it creates the config (keys, budgets, routes, dimensions), writes
  30 days of history into ClickHouse, and sets the Budgets page. It prints the Solo UI address
  and exits. Run it once per cluster (it replaces any earlier TD history).
- `live` runs until Ctrl-C (or `--duration 2h`). Every few seconds it writes fresh spans into
  ClickHouse, and about every minute it nudges the budgets, so the Traces, Cost Management and
  Budgets pages keep moving. Start it about 10 minutes before the demo. Stop `live` (Ctrl-C) before running `demo`,
  `backfill --replace` or `down`.
- Every command needs `--context <ctx>`; the current context is never used. The first run unpacks the tool to
  `~/.cache/td-traffic/` (set `TD_TRAFFIC_CACHE` to change it).

If your release, namespace or Gateway names differ, pass `--release`, `--namespace` (where the
agentgateway release, Gateway, Solo UI and ClickHouse live) and `--gateway` to every command. The
ClickHouse pod and its auth Secret are found by the name pattern `*clickhouse*` in that
namespace. If something is missing, `demo` / `up` say exactly what and what to install, and
change nothing.

From source: `git clone https://github.com/sebbycorp/viper-env && cd viper-env`, then
`uv run demos/td-traffic/td.py <command> --context <ctx>`. Build the single file with
`scripts/build-td-traffic.sh` (writes `dist/td-traffic.pyz`). Pushing a tag `td-traffic-v*`
publishes it as a GitHub release.

## The org at a glance

- 5 lines of business, 15 teams, 12 projects.
- 35 users and 4 shared team CI keys (headless Claude Code, no user).
- Client tools: GitHub Copilot, VS Code, Claude Code, Claude Desktop.
- Providers and models: Azure (`gpt-5.4`, `gpt-5.4-mini`), AWS Bedrock (Claude Opus 5.5),
  Mistral (`mistral-large`).
- Groups: Personal Banking, Wealth Management, TD Securities, Enterprise Technology,
  Risk & Compliance. Users, projects and teams are generated from a fixed seed, so the
  org is the same on every run.

## Commands

| Command | What it does |
|---|---|
| `demo [--days 30] [--seed N]` | One-time setup: preflight, `up`, `backfill --replace`, `sync-budgets`; prints the UI URL and the `live` command. |
| `up [--seed N]` | Creates the demo objects, keys, budgets and dimensions (see below). |
| `backfill [--days 30] [--replace] [--seed N] [--batch N] [--force-new-backup]` | Writes synthetic history straight into ClickHouse. Refuses if TD history exists; `--replace` purges it first. |
| `sync-budgets [--dry-run] [--seed N]` | Sets the Budgets page to the generated window spend (zero-cost outcome rows in ClickHouse), then reads it back the way the page does. `--dry-run` prints the targets and writes nothing. |
| `replan-budgets [--seed N]` | Re-applies only the budgets, sized for the current budget window. |
| `live [--duration D] [--speed X] [--min-rpm 6] [--seed N]` | Writes fresh spans into ClickHouse and refreshes the budgets about every minute. Runs until Ctrl-C unless `--duration` (e.g. `90s`, `10m`, `2h`) is set. |
| `status` | Counts TD objects and span rows; prints the budget window and next rollover. |
| `down [--purge-history] [--force-new-backup]` | Removes the demo. |

All commands also take `--namespace`, `--release` and `--gateway`. `--seed`: use the same seed
for `demo` / `up`, `backfill`, `sync-budgets` and `replan-budgets`, or leave it unset everywhere
(default 42); budgets are sized on the history that seed generates.

### What `up` changes on the cluster

- The `budgetDimensions` Helm value of `enterprise-agentgateway`. The previous value is
  saved first and restored by `down`.
- Namespace `td-demo` with the virtual-key Secrets, the policy and the budgets.
- 4 HTTPRoutes on `agentgateway-proxy` for host `td.demo.local` (one per model route) and
  their backends (config only).

### `down` versus `down --purge-history`

- `down` restores the Helm `budgetDimensions` value and deletes every TD object in the
  cluster (namespace, routes, policy, budgets, keys). The spans stay in ClickHouse.
- `down --purge-history` does all of that, then also deletes every TD span from ClickHouse
  and recomputes the affected rollups. Rollups are snapshotted to the `td_purge_backup`
  database first, and the purge verifies that the real (non-TD) totals are unchanged.

## Demo-day checklist

1. `demo` once, ahead of time (or `replan-budgets` + `sync-budgets` if only the budget window moved).
2. `live`, started 10 minutes before the call (`--duration 2h`, or Ctrl-C afterwards).
3. `status`, to confirm counts and the next budget rollover.
4. Pages to show: Cost Management (drill down by group, team, project, user, client and
   model), Budgets (a mix of over, near and under teams), Virtual Keys (per-user and
   per-team keys), Traces (the live spans).

## Budgets

- The Budgets page reads zero-cost `agw.ai.budgets.outcome` rows from ClickHouse, not the
  gateway's Redis counters. `sync-budgets` (and `live`, every minute) write those rows. Details
  in [NOTES.md](NOTES.md).
- Budgets use an epoch-aligned 30-day window, not the calendar month. `status` prints the
  next rollover. After it, re-run `replan-budgets` (or `up`), `backfill --replace` and
  `sync-budgets`.
- 41 Audit budgets (Audit only: nothing is ever blocked): monthly USD for 1 org, 5 groups,
  15 teams (2 over, 2 near, the rest under), 12 projects and the top 5 users; daily Tokens for
  the 3 biggest teams.
- **Early in a budget window** (less than 5 days since it started) the window-to-date spend is
  small, so the limits look small too. `up`, `replan-budgets` and `status` warn about it.

## Known limits

- The Playground and the TD routes do not answer: the routes exist for the UI's config pages,
  and the backends are config only, so nothing is sent to a provider.
- The Redis budget counters in the gateway differ from the Budgets page (the page reads
  ClickHouse; the tool never charges the counters).
- The UI error rate shows 0% for TD traffic (a platform quirk), even though 429s and 5xx
  appear in the traces.
- Azure and Mistral prices come from the proxy's built-in price catalog.
- If the ClickHouse port-forward drops, `live` re-opens it (backoff 1 to 30 s) and retries the
  rows it has not inserted.
- Each purge adds snapshot tables to `td_purge_backup` in ClickHouse. It grows over time.
  Drop it when you no longer need to restore: `DROP DATABASE td_purge_backup`
  (the purge prints this statement). This is safe only when no purge is incomplete (a
  purge that never reached `verified`; the next purge would refuse and print its restore).

### Recovery

If a purge fails, or is blocked with `PurgeIncompleteError`, the tool
prints the restore SQL for that purge, writes it to `td-restore-<stamp>.sql` in the current
directory, and prints a ready-to-run command, for example:

```bash
kubectl --context <ctx> -n agentgateway-system exec -i <clickhouse pod> -- clickhouse-client \
  --user "$(kubectl ... get secret <auth secret> -o jsonpath='{.data.username}' | base64 -d)" \
  --password "$(kubectl ... get secret <auth secret> -o jsonpath='{.data.password}' | base64 -d)" \
  --multiquery < td-restore-<stamp>.sql
```

Copy the printed one (every value is already filled in and shell-quoted), run it, then re-run
the command. `--force-new-backup` overrides the block, but only after you have restored the
earlier purge. Before any mutation, a purge also checks that the recomputed rollups would
match the real totals; if not, it aborts with nothing changed.

`down` refuses (exit 1, nothing deleted) if the saved original Helm values are gone but the
release still carries the TD `budgetDimensions`; it prints the `helm upgrade --reset-values`
recovery commands.

## Cleanup

```bash
uv run --python 3.12 python td-traffic.pyz down --context <ctx>                  # keep history
uv run --python 3.12 python td-traffic.pyz down --context <ctx> --purge-history  # remove everything
```

Platform details and verified behavior are in [NOTES.md](NOTES.md). Tests:

```bash
uv run --python 3.12 --with pytest pytest demos/td-traffic/tests -q
```
