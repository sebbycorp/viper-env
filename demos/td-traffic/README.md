# TD Bank demo traffic for the agentgateway UI

A laptop-run tool that fills the Solo Enterprise agentgateway UI with synthetic usage from a
fictional TD Bank (@td.com) AI-assistant rollout. It gives you 30 days of history in
Cost Management, populated Budgets and Virtual Keys pages, and live traces on demo day.

All calls are fake. A mock LLM runs in the cluster; nothing goes to a real provider.

## The org at a glance

- 5 lines of business, 15 teams, 12 projects.
- 35 users and 4 shared team CI keys (headless Claude Code, no user).
- Client tools: GitHub Copilot, VS Code, Claude Code, Claude Desktop.
- Providers and models: Azure (`gpt-5.4`, `gpt-5.4-mini`), AWS Bedrock (Claude Opus 5.5),
  Mistral (`mistral-large`).
- Groups: Personal Banking, Wealth Management, TD Securities, Enterprise Technology,
  Risk & Compliance. Users, projects and teams are generated from a fixed seed, so the
  org is the same on every run.

## Prerequisites

- `uv` (Python and dependencies are handled by the `uv run` script header).
- `kubectl` and `helm` on the PATH.
- A kube context for a cluster that runs enterprise agentgateway (Gateway
  `agentgateway-proxy`) and the Solo UI with ClickHouse.
- Every command needs `--context <ctx>`; the current context is never used.

```bash
CTX=maniak-omni-viper
uv run demos/td-traffic/td.py <command> --context $CTX
```

## Commands

| Command | What it does |
|---|---|
| `up [--seed N]` | Creates the demo objects, keys, budgets and dimensions (see below). |
| `backfill [--days 30] [--replace] [--seed N] [--batch N] [--force-new-backup]` | Writes synthetic history straight into ClickHouse. Refuses if TD history exists; `--replace` purges it first. |
| `sync-budgets [--dry-run] [--seed N]` | Tops the gateway budget counters up to the backfilled history, then removes its marker spans. Idempotent. `--dry-run` prints the plan and sends nothing. |
| `replan-budgets [--seed N]` | Re-applies only the budgets, sized for the current budget window. |
| `live [--duration 10m] [--speed X] [--min-rpm 6] [--concurrency 4] [--seed N]` | Sends real requests through the gateway with TD keys. Runs until Ctrl-C unless `--duration` is set. |
| `status` | Counts TD objects and span rows; prints the budget window and next rollover. |
| `down [--purge-history] [--force-new-backup]` | Removes the demo. |

### What `up` changes on the cluster

- The `budgetDimensions` Helm value of `enterprise-agentgateway`. The previous value is
  saved first and restored by `down`.
- Namespace `td-demo` with the mock LLM, the virtual-key Secrets, the policy and the budgets.
- 4 HTTPRoutes on `agentgateway-proxy` for host `td.demo.local` (one per model route).

Dimension changes take about 60 s to reach the proxy. Wait before starting `live` right
after `up`.

### `down` versus `down --purge-history`

- `down` restores the Helm `budgetDimensions` value and deletes every TD object in the
  cluster (namespace, routes, policy, budgets, keys). The spans stay in ClickHouse.
- `down --purge-history` does all of that, then also deletes every TD span from ClickHouse
  and recomputes the affected rollups. Rollups are snapshotted to the `td_purge_backup`
  database first, and the purge verifies that the real (non-TD) totals are unchanged.

## Demo-day checklist

Order matters. Do not run `sync-budgets` while `live` is running.

1. `up` (or `replan-budgets` if the demo is already up and only the window moved).
2. `backfill --replace`. About 5 minutes for 30 days.
3. `sync-budgets`.
4. `live`, started 10 minutes before the call (`--duration 2h`, or Ctrl-C afterwards).
5. `status`, to confirm counts and the next budget rollover.
6. Pages to show:
   - Cost Management, drill down by group, team, project, user, client and model.
   - Budgets, with the mix of over, near and under teams.
   - Virtual Keys, with per-user and per-team keys.
   - Traces, from the live traffic.

## Budgets

- The Budgets page shows the gateway's live counters, not ClickHouse history. `sync-budgets`
  charges the counters up to the backfilled spend, so run it after every backfill.
- Budgets use an epoch-aligned 30-day window, not the calendar month. `status` prints the
  next rollover. After it, re-run `replan-budgets` (or `up`), `backfill --replace` and
  `sync-budgets`.
- Re-running `sync-budgets` is safe: a second run sends 0 marker requests.
- Limits are sized so a few teams are over budget and a few are near; groups and the org sit
  at about 60-90%.

## Known limits

- Fake calls only. TD routes answer only when the request carries Host `td.demo.local`.
- The UI error rate shows 0% for TD traffic (a platform quirk), even though 429s and 5xx
  appear in the traces.
- Azure and Mistral prices come from the proxy's built-in price catalog.
- A proxy restart mid-`live` can drop one in-flight request. The tool reconnects and
  re-sends. Budget counters survive a proxy restart.
- Every purge (`down --purge-history`, `backfill --replace`, the `sync-budgets` marker
  purge) removes budget outcome spans, so the Budgets page can look stale or empty until
  the next request per team. `sync-budgets` sends those requests itself.
- Each purge adds snapshot tables to `td_purge_backup` in ClickHouse. It grows over time.
  Drop it when you no longer need to restore: `DROP DATABASE td_purge_backup`
  (the purge prints this statement).

### Recovery

If a sync's marker purge fails, or a purge is blocked with `PurgeIncompleteError`, the tool
prints the restore SQL for that purge. Run it, then re-run the command. `--force-new-backup`
overrides the block, but only after you have restored the earlier purge.

## Cleanup

```bash
uv run demos/td-traffic/td.py down --context $CTX                  # keep history
uv run demos/td-traffic/td.py down --context $CTX --purge-history  # remove everything
```

Platform details and verified behavior are in [NOTES.md](NOTES.md). Tests:

```bash
uv run --python 3.12 --with pytest pytest demos/td-traffic/tests -q
```
