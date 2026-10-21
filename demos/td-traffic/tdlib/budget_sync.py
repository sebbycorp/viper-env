"""`td.py sync-budgets`: set what the Budgets page shows for every TD budget entry, straight in ClickHouse.

The Solo UI reads displayed spend from ClickHouse, not from the gateway's Redis counters (NOTES.md, v2
spike): per entry `<ns>/<budget>/<entry>` it takes the newest `current_usage` among
`agw.ai.budgets.outcome` rows (phase `final_debit_result`) dated between the budget's
`creationTimestamp` and now. So this module:

  1. computes every entry's target (`budget_targets`): the generated window-to-date spend (same seed and
     numbers `plan_budgets` sized the limits on) plus any live requests passed in;
  2. writes zero-cost outcome rows carrying those values (`write_budget_outcomes`, `spans.outcome_row`),
     dated just after the budget's creation and just before now;
  3. reads them back the way the UI does (`read_budget_usage`) and prints a table.

No request goes to the gateway; the Redis counters are left alone (they differ from the page).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from .clickhouse import SPANS_TABLE, _q, _sql_ts
from .manifests import BUDGET, NAMESPACE, budget_window, window_spend
from .org import Org, service_identity
from .pricing import Rates
from .spans import outcome_row
from .usage import Request, cost

BUDGET_REF = f"{NAMESPACE}/{BUDGET}"
ENTRIES_PER_ROW = 8                  # the gateway's own outcome rows carry a handful of entries each
DEFAULT_CLUSTER = "td-demo"          # span_template.json's cluster_name; used when ClickHouse has no real spans
USD_MICRO = 1_000_000


# ---------------------------------------------------------------- targets

def entry_kind(entry: dict) -> str:
    """org / group / team / proj / user / tok, from a budget entry's subject and unit (manifests.plan_budgets)."""
    subj = entry["subject"]
    if entry["limit"].get("unit") == "Tokens":
        return "tok"
    for key, kind in (("organization", "org"), ("group", "group"), ("team", "team"), ("project", "proj"),
                      ("user", "user")):
        if key in subj:
            return kind
    raise ValueError(f"budget entry {entry.get('name')!r} has an unknown subject {subj}")


def budget_targets(org: Org, budgets: list[dict], now: datetime, seed: int, *,
                   rates: dict[tuple[str, str], Rates], live: Iterable[Request] = ()) -> dict[str, int]:
    """Entry name -> `current_usage` (µUSD for USD entries, tokens for Tokens entries) for every entry.

    USD entries: generated spend over [budget window start, now), `manifests.window_spend` (the numbers
    `plan_budgets` sized the limits on, so the planned over / near / under mix holds). Daily Tokens entries:
    the team's tokens per day over the window, the figure `plan_budgets` sized those limits on (a calendar
    day-to-date count would be ~0 just after midnight and low on weekends). `live` adds requests sent on
    top of the generated history (successful ones in the window).
    """
    now = now.astimezone(timezone.utc)
    ws = window_spend(org, rates, seed, now)
    usd = {"org": dict(ws["organization"]), "group": dict(ws["group"]), "team": dict(ws["team"]),
           "proj": dict(ws["project"]), "user": defaultdict(float, ws["user"])}
    tok = dict(ws["team_tokens_day"])
    start, _ = budget_window(now)
    for r in live:
        if r.status != 200 or not (start <= r.ts.astimezone(timezone.utc) <= now):
            continue
        c = float(cost(r, rates)["total"])
        k = r.key
        for kind, name in (("org", org.name), ("group", k.group), ("team", k.team), ("proj", k.project),
                           ("user", service_identity(k))):
            usd[kind][name] = usd[kind].get(name, 0.0) + c
        tok[k.team] = tok.get(k.team, 0.0) + r.input_tokens + r.output_tokens

    out = {}
    for e in budgets:
        kind, subj = entry_kind(e), e["subject"]
        if kind == "tok":
            out[e["name"]] = int(round(tok.get(subj["team"], 0.0)))
        else:
            value = next(iter(subj.values()))
            out[e["name"]] = int(round(usd[kind].get(value, 0.0) * USD_MICRO))
    return out


# ---------------------------------------------------------------- ClickHouse

def budget_object(kube) -> dict:
    return json.loads(kube.run(["get", "enterpriseagentgatewaybudget", BUDGET, "-n", NAMESPACE, "-o", "json"]))


def created_at(obj: dict) -> datetime:
    return datetime.strptime(obj["metadata"]["creationTimestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def write_budget_outcomes(ch, kube, targets: dict[str, int], now: datetime, *, created: datetime | None = None,
                          cluster: str | None = None, attempts: int = 8) -> None:
    """Insert outcome rows setting every entry in `targets`, ENTRIES_PER_ROW entries per row.

    Dated max(now - 1s, creation + 1s): the page ignores rows before the budget's `creationTimestamp`
    (re-created by every `up`) and future-dated rows. `created` / `cluster` skip the kube / ClickHouse
    lookups (for callers that write every minute).
    """
    created = created or created_at(budget_object(kube))
    ts = max(now.astimezone(timezone.utc) - timedelta(seconds=1), created + timedelta(seconds=1))
    cluster = cluster or ch.cluster_name(DEFAULT_CLUSTER)
    items = list(targets.items())
    rows = [outcome_row(dict(items[i:i + ENTRIES_PER_ROW]), ts, BUDGET_REF, cluster)
            for i in range(0, len(items), ENTRIES_PER_ROW)]
    ch.insert_rows(SPANS_TABLE, rows, attempts=attempts)


def read_budget_usage(ch, since: datetime) -> dict[str, int]:
    """Entry name -> `current_usage` as the Budgets page computes it (newest final_debit_result row in
    [since, now], per `statuses[].id`)."""
    prefix = BUDGET_REF + "/"
    rows = ch.json_rows(
        "SELECT id, toString(argMax(usage, Timestamp)) AS usage FROM ("
        "SELECT Timestamp, arrayJoin(JSONExtractArrayRaw(toString(SpanAttributes.`agw.ai.budgets.statuses`))) AS s, "
        "JSONExtractString(s, 'id') AS id, JSONExtractInt(s, 'current_usage') AS usage "
        f"FROM {SPANS_TABLE} WHERE ScopeName = 'agentgateway' AND SpanName = 'agw.ai.budgets.outcome' "
        "AND toString(SpanAttributes.`agw.ai.budgets.phase`) = 'final_debit_result' "
        f"AND Timestamp >= toDateTime64({_q(_sql_ts(since))}, 9, 'UTC') AND Timestamp <= now64(9) "
        f"AND position(toString(SpanAttributes.`agw.ai.budgets.statuses`), {_q(prefix)}) > 0"
        f") WHERE startsWith(id, {_q(prefix)}) GROUP BY id")
    return {r["id"][len(prefix):]: int(r["usage"]) for r in rows}


# ---------------------------------------------------------------- the command

def sync_budgets(kube, ch, org: Org, *, rates: dict[tuple[str, str], Rates], seed: int,
                 now: datetime | None = None, dry_run: bool = False,
                 log: Callable[[str], None] = print) -> list[dict]:
    """Write every entry's target as outcome rows, read back what the page shows, return the table rows."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    obj = budget_object(kube)
    budgets, created = obj["spec"]["budgets"], created_at(obj)
    start, _ = budget_window(now)
    log(f"sync-budgets: budget window {start:%Y-%m-%d %H:%M} UTC; generated history (seed {seed}) to "
        f"{now:%Y-%m-%d %H:%M} UTC; {BUDGET_REF} created {created:%Y-%m-%d %H:%M:%S} UTC")
    targets = budget_targets(org, budgets, now, seed, rates=rates)
    shown: dict[str, int] = {}
    if dry_run:
        log("sync-budgets: --dry-run, nothing written")
    else:
        write_budget_outcomes(ch, kube, targets, now, created=created)
        log(f"sync-budgets: wrote {len(targets)} entries as zero-cost outcome rows")
        shown = read_budget_usage(ch, created)
    rows = []
    for e in budgets:
        n, unit = e["name"], e["limit"]["unit"]
        scale = USD_MICRO if unit == "USD" else 1
        val = shown.get(n, targets[n] if dry_run else 0)
        rows.append({"name": n, "kind": entry_kind(e), "unit": unit, "limit": e["limit"]["amount"],
                     "target": targets[n] / scale, "shown": val / scale,
                     "pct": 100 * val / (e["limit"]["amount"] * scale)})
    if not dry_run:
        bad = [r["name"] for r in rows if r["shown"] != r["target"]]
        if bad:
            log(f"sync-budgets: WARNING the page query does not show the target for {len(bad)} entries "
                f"({', '.join(bad[:5])}{' ...' if len(bad) > 5 else ''}); a newer outcome row may exist")
    return rows


def format_table(rows: list[dict]) -> str:
    """Per budget entry: limit, target, what the Budgets page shows, % of limit, status."""
    def status(p):
        return "OVER" if p >= 100 else "near" if p >= 85 else "under"
    order = {"org": 0, "group": 1, "team": 2, "proj": 3, "user": 4, "tok": 5}
    lines = [f"{'budget':48} {'kind':5} {'unit':6} {'limit':>13} {'target':>15} {'shown':>15} {'% limit':>8}  status"]
    for r in sorted(rows, key=lambda r: (order[r["kind"]], r["name"])):
        fmt = "{:,.2f}" if r["unit"] == "USD" else "{:,.0f}"
        lines.append(f"{r['name']:48} {r['kind']:5} {r['unit']:6} {r['limit']:>13,} {fmt.format(r['target']):>15} "
                     f"{fmt.format(r['shown']):>15} {r['pct']:>7.1f}%  {status(r['pct'])}")
    return "\n".join(lines)
