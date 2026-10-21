"""`td.py sync-budgets`: bring the gateway's budget counters up to the TD history the cost graphs show.

The counters live in Redis (ext-cache); the Budgets page shows the latest `current_usage` from the
gateway's `agw.ai.budgets.outcome` spans (NOTES.md "Budget counters"). Backfilled spans never touch the
counters, so after a backfill every budget shows ~0%. This closes the gap per team, idempotently:

  1. one ordinary request per team, so the page (and ListBudgets) report the live counters;
  2. target per team = generated spend (usage.generate, same seed as the backfill) over
     [budget window start, backfill end) + live TD spend recorded in ClickHouse in the window
     (requests with an outcome span: `live`, refresh requests, Task 5 checks); group / org = sums;
  3. gap = target - counter (ListBudgets); gaps under MIN_GAP are skipped, so a second run sends ~0;
  4. <= MAX_REQUESTS_PER_TEAM marker requests per team, each <= MAX_INPUT_TOKENS input, on the team's
     usual expensive routes (bedrock-opus / azure-gpt, in its history's proportion), sized with the
     x-td-* token headers; marker = UA `td-budget-sync/1.0` + URL path segment `/td-budget-sync`
     (spans store url.path, not the raw UA);
  5. one ordinary request per team again (the marker purge also removes the markers' outcome spans);
  6. purge the marker spans with the Task 7 machinery scoped to `sync_span_predicate()` (backup first,
     verified), so the cost graphs do not count the spend twice;
  7. a table per budget: target, counter before / after, % of the limit.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import random
import struct
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Iterator

from .live import Gateway, Sender, send_with_reconnect, urllib_sender, Stats
from .manifests import (BUDGET, HOSTNAME, NAMESPACE, budget_window, group_budget_name, org_budget_name,
                        team_budget_name)
from .org import Org
from .pricing import Rates
from .usage import MAX_OUTPUT, MODELS, Request, cost, generate, sample_request

MARKER_UA = "td-budget-sync/1.0"
MARKER_SEGMENT = "/td-budget-sync"
SYNC_ROUTES = ("bedrock-opus", "azure-gpt")     # the expensive routes: fewest, smallest requests
MAX_REQUESTS_PER_TEAM = 20
MAX_INPUT_TOKENS = 250_000                       # below gpt-5.4's 272k long-context tier
MIN_GAP = 1.0                                    # USD; smaller gaps are skipped (idempotent re-runs)
UI_TARGET, UI_NS, UI_PORT = "svc/solo-enterprise-ui", "agentgateway-system", 80
LIST_BUDGETS = "/api/rpc.agentgateway.solo.io.AgentgatewayCostAPI/ListBudgets"
AUTOAUTH = "/api/autoauth/token"
SPAN_WAIT_S = 180
REFRESH_WAIT_S = 180


def sync_span_predicate() -> str:
    """SQL over `platformdb.otel_traces_json` matching only the sync marker spans."""
    return (f"(toString(SpanAttributes.`server.address`) = '{HOSTNAME}' "
            f"AND position(toString(SpanAttributes.`url.path`), '{MARKER_SEGMENT}/') > 0)")


def budget_window_start(now: datetime) -> datetime:
    """Start of the gateway's current `Month` budget window (manifests.budget_window: epoch-aligned 30 days)."""
    return budget_window(now)[0]


# ---------------------------------------------------------------- targets from the generated history

def _history(org: Org, rates: dict[tuple[str, str], Rates], start: datetime, now: datetime,
             seed: int) -> dict[str, dict[str, Decimal]]:
    """{team: {route_id: USD}} of successful generated requests in [start, now)."""
    rid = {m.route: r for r, m in MODELS.items()}
    out: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for r in generate(org, start, now, seed):
        if r.status == 200:
            out[r.key.team][rid[r.route.route]] += cost(r, rates)["total"]
    return out


def _targets_from(org: Org, hist: dict[str, dict[str, Decimal]]) -> dict[str, float]:
    team = {t: float(sum(hist.get(t, {}).values())) for t in org.teams}
    out = {org_budget_name(org.name): sum(team.values())}
    for g in org.groups:
        out[group_budget_name(g)] = sum(v for t, v in team.items() if org.teams[t] == g)
    for t, v in team.items():
        out[team_budget_name(t)] = v
    return out


def history_targets(org: Org, rates: dict[tuple[str, str], Rates], start: datetime, now: datetime,
                    seed: int) -> dict[str, float]:
    """Budget entry name -> generated USD in [start, now): 15 teams, 5 groups (sums), the org (sum)."""
    return _targets_from(org, _history(org, rates, start, now, seed))


def route_mix(org: Org, rates: dict[tuple[str, str], Rates], start: datetime, now: datetime,
              seed: int) -> dict[str, dict[str, float]]:
    """{team: {route_id: USD}} of the generated history: which routes the team normally spends on."""
    return {t: {r: float(v) for r, v in m.items()} for t, m in _history(org, rates, start, now, seed).items()}


# ---------------------------------------------------------------- requests that close the gap

def _sync_weights(mix: dict[str, float]) -> dict[str, float]:
    """Team mix folded onto SYNC_ROUTES: Opus traffic -> bedrock-opus, everything else (Azure, Mistral) -> azure-gpt."""
    opus = mix.get("bedrock-opus", 0.0)
    w = {"bedrock-opus": opus, "azure-gpt": sum(mix.values()) - opus}
    w = {k: v for k, v in w.items() if v > 0}
    return w or {"bedrock-opus": 1.0}


def _alloc(n: int, weights: dict[str, float]) -> list[str]:
    """n route ids split by weight (largest remainder), in SYNC_ROUTES order."""
    total = sum(weights.values())
    exact = {r: n * w / total for r, w in weights.items()}
    counts = {r: int(x) for r, x in exact.items()}
    for r in sorted(exact, key=lambda r: -(exact[r] - counts[r]))[: n - sum(counts.values())]:
        counts[r] += 1
    return [r for r in SYNC_ROUTES for _ in range(counts.get(r, 0))]


def _usd(r: Rates, inp: int, out: int) -> Decimal:
    return (Decimal(inp) * r.input + Decimal(out) * r.output) / Decimal(1_000_000)


def _tokens_for(usd: Decimal, r: Rates) -> tuple[int, int]:
    """(input, output) tokens costing ~usd: up to half via input (<= MAX_INPUT_TOKENS), the rest output."""
    inp = min(MAX_INPUT_TOKENS, int(usd / 2 * Decimal(1_000_000) / r.input))
    out = max(1, int(((usd - _usd(r, inp, 0)) * Decimal(1_000_000) / r.output).to_integral_value()))
    return inp, out


def plan_requests(org: Org, rates: dict[tuple[str, str], Rates], gaps: dict[str, float],
                  mix: dict[str, dict[str, float]], now: datetime) -> list[Request]:
    """Marker requests per team (<= MAX_REQUESTS_PER_TEAM) whose gateway cost adds up to the team's gap.

    As few requests as plausible sizes allow (MAX_INPUT_TOKENS in, usage.MAX_OUTPUT out); when the
    gap needs more than MAX_REQUESTS_PER_TEAM of those, the output token count grows instead.
    """
    out: list[Request] = []
    for team in sorted(gaps):
        gap = Decimal(str(round(gaps[team], 6)))
        if gap < Decimal(str(MIN_GAP)):
            continue
        weights = _sync_weights(mix.get(team, {}))
        plain = min(_usd(rates[(MODELS[r].catalog_provider, MODELS[r].catalog_model)], MAX_INPUT_TOKENS, MAX_OUTPUT)
                    for r in weights)
        n = min(MAX_REQUESTS_PER_TEAM, max(1, math.ceil(gap / plain)))
        keys = sorted((k for k in org.keys if k.team == team), key=lambda k: k.id)
        if not keys:
            continue
        left = gap
        routes = _alloc(n, weights)
        for i, rid in enumerate(routes):
            m = MODELS[rid]
            r = rates[(m.catalog_provider, m.catalog_model)]
            usd = left if i == len(routes) - 1 else gap / n
            inp, outp = _tokens_for(usd, r)
            req = Request(now, keys[i % len(keys)], dataclasses.replace(m, route=m.route + MARKER_SEGMENT),
                          MARKER_UA, inp, outp, 0, 20, 200)
            left -= cost(req, rates)["total"]
            out.append(req)
    return out


# ---------------------------------------------------------------- ListBudgets (grpc-web, stdlib protobuf)

def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def _fields(b: bytes) -> Iterator[tuple[int, int, object]]:
    i = 0
    while i < len(b):
        tag, i = _varint(b, i)
        f, w = tag >> 3, tag & 7
        if w == 0:
            v, i = _varint(b, i)
        elif w == 1:
            v, i = struct.unpack("<d", b[i:i + 8])[0], i + 8
        elif w == 2:
            n, i = _varint(b, i)
            v, i = b[i:i + n], i + n
        elif w == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {w}")
        yield f, w, v


def _entry(b: bytes) -> dict:
    e: dict = {"name": "", "subject": {}, "limit": None, "spent": 0.0}
    for f, w, v in _fields(b):
        if f == 1:
            e["name"] = v.decode()
        elif f == 2:
            kv = {ff: vv.decode() for ff, _, vv in _fields(v)}
            e["subject"][kv.get(1, "")] = kv.get(2, "")
        elif f == 3:
            e["limit"] = next((vv for ff, _, vv in _fields(v) if ff == 2), None)
        elif f == 6 and w == 1:
            e["spent"] = v
    return e


def decode_list_budgets(frame: bytes) -> list[dict]:
    """grpc-web+proto ListBudgetsResponse -> [{name, namespace, cluster, entries: [{name, subject, limit, spent}]}]."""
    i, msgs, status = 0, [], None
    while i + 5 <= len(frame):
        flag, n = frame[i], struct.unpack(">I", frame[i + 1:i + 5])[0]
        payload = frame[i + 5:i + 5 + n]
        i += 5 + n
        if flag & 0x80:
            for line in payload.decode(errors="replace").splitlines():
                if line.lower().startswith("grpc-status:"):
                    status = int(line.split(":", 1)[1])
        else:
            msgs.append(payload)
    if status not in (None, 0):
        raise ValueError(f"ListBudgets grpc-status {status}")
    out = []
    for msg in msgs:
        for f, _, v in _fields(msg):
            if f != 1:
                continue
            bud: dict = {"name": "", "namespace": "", "cluster": "", "entries": []}
            for ff, _, vv in _fields(v):
                if ff in (1, 2, 3):
                    bud[{1: "name", 2: "namespace", 3: "cluster"}[ff]] = vv.decode()
                elif ff == 4:
                    bud["entries"].append(_entry(vv))
            out.append(bud)
    return out


def _post(url: str, data: bytes, headers: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    with opener.open(req, timeout=30) as r:
        return r.read(), {k.lower(): v for k, v in r.headers.items()}


def list_budgets(base_url: str) -> list[dict]:
    """ListBudgets through the Solo UI backend, with the UI's own auto-auth token (read-only call)."""
    raw, _ = _post(base_url + AUTOAUTH, json.dumps({"user_type": "admin"}).encode(), {"Content-Type": "application/json"})
    token = json.loads(raw)["access_token"]
    raw, hdrs = _post(base_url + LIST_BUDGETS, b"\x00\x00\x00\x00\x00", {
        "Authorization": f"Bearer {token}", "Content-Type": "application/grpc-web+proto", "X-Grpc-Web": "1"})
    if hdrs.get("grpc-status", "0") != "0":
        raise ValueError(f"ListBudgets failed: {hdrs.get('grpc-status')} {hdrs.get('grpc-message', '')}")
    return decode_list_budgets(raw)


def read_budget_counters(kube) -> dict[str, dict]:
    """{entry name: {"spent": USD, "limit": amount}} of the TD budget object, as the Budgets page shows it."""
    with kube.port_forward(UI_TARGET, UI_NS, UI_PORT) as port:
        buds = list_budgets(f"http://127.0.0.1:{port}")
    out = {}
    for b in buds:
        if b["namespace"] == NAMESPACE and b["name"] == BUDGET:
            for e in b["entries"]:
                out[e["name"]] = {"spent": float(e["spent"] or 0.0), "limit": e["limit"]}
    if not out:
        raise ValueError(f"ListBudgets returned no {NAMESPACE}/{BUDGET} entries; is the TD demo up?")
    return out


def _norm(c: dict) -> dict[str, dict]:
    return {k: (v if isinstance(v, dict) else {"spent": float(v), "limit": None}) for k, v in c.items()}


# ---------------------------------------------------------------- the command

def _kind(org: Org, name: str) -> str:
    if name == org_budget_name(org.name):
        return "org"
    return "group" if name in {group_budget_name(g) for g in org.groups} else "team"


class SyncError(RuntimeError):
    pass


def refresh_requests(org: Org, now: datetime, rng: random.Random) -> list[Request]:
    """One ordinary successful request per team (a realistic sample on one of its keys, normal client UA)."""
    out = []
    for team in sorted(org.teams):
        keys = sorted((k for k in org.keys if k.team == team), key=lambda k: k.id)
        while True:
            r = sample_request(rng.choice(keys), now, rng)
            if r.status == 200:
                break
        r.latency_ms = min(r.latency_ms, 1500)
        out.append(r)
    return out


def _spent(c: dict[str, dict], name: str) -> float:
    return c.get(name, {}).get("spent", 0.0)


def _refresh(gw: Gateway, sender: Sender, org: Org, rng: random.Random, stats: Stats, rates, reader,
             log, sleep, why: str) -> dict[str, dict]:
    """Send refresh_requests and wait until the Budgets page shows a new value for every team.

    The page's `spent` is the latest `current_usage` in the gateway's `agw.ai.budgets.outcome` spans in
    ClickHouse (NOTES.md). After a TD purge (or this tool's marker purge) it is stale or 0 although the
    gateway counters are not; a fresh request per budget makes it report the real counter again.
    """
    prev = _norm(reader())
    reqs = refresh_requests(org, datetime.now(timezone.utc), rng)
    for r in reqs:
        for _ in range(3):
            if send_with_reconnect(gw, sender, r, stats, rates) == 200:
                break
    log(f"sync-budgets: {why}: {len(reqs)} ordinary requests (one per team) so the Budgets page reports the "
        f"live counters; waiting for their outcome spans")
    names = [team_budget_name(t) for t in org.teams]
    for _ in range(int(REFRESH_WAIT_S / 5)):
        cur = _norm(reader())
        if all(_spent(cur, n) != _spent(prev, n) for n in names):
            return cur
        sleep(5)
    missing = [n for n in names if _spent(cur, n) == _spent(prev, n)]
    raise SyncError(f"the Budgets page (ListBudgets) did not pick up the {why} requests within {REFRESH_WAIT_S}s "
                    f"for {', '.join(missing[:5])}{' ...' if len(missing) > 5 else ''}; counters unconfirmed, "
                    f"nothing more sent")


def run_sync(kube, org: Org, *, rates: dict[tuple[str, str], Rates], seed: int, now: datetime | None = None,
             start: datetime | None = None, sender: Sender | None = None,
             read_counters: Callable[[], dict] | None = None,
             open_clickhouse: Callable[[], contextlib.AbstractContextManager] | None = None,
             log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
             dry_run: bool = False) -> list[dict]:
    """Close each team's gap (target - counter), purge the marker spans, return the table rows.

    Without `open_clickhouse` (tests), the generated history runs to `now` and there is no live part.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = start or budget_window_start(now)
    reader = read_counters or (lambda: read_budget_counters(kube))
    sender = sender or urllib_sender
    rng = random.Random()
    with (open_clickhouse() if open_clickhouse is not None else contextlib.nullcontext()) as ch:
        hist_end = now
        if ch is not None:
            end = ch.td_history_end(start, exclude=sync_span_predicate())
            hist_end = min(now, end) if end is not None else start
        log(f"sync-budgets: budget window {start:%Y-%m-%d %H:%M} UTC; generated history (seed {seed}) "
            f"up to {hist_end:%Y-%m-%d %H:%M} UTC (backfill end)")
        hist = _history(org, rates, start, hist_end, seed)
        generated = _targets_from(org, hist)
        mix = {t: {r: float(v) for r, v in m.items()} for t, m in hist.items()}

        def targets_now() -> dict[str, float]:
            live = ch.td_live_spend_by_team(start, exclude=sync_span_predicate()) if ch is not None else {}
            if ch is not None:
                log(f"sync-budgets: live TD spend recorded in ClickHouse this window: ${sum(live.values()):,.2f}")
            t = dict(generated)
            for team in org.teams:
                extra = live.get(team, 0.0)
                for n in (team_budget_name(team), group_budget_name(org.teams[team]), org_budget_name(org.name)):
                    t[n] += extra
            return t

        def plan(before: dict, targets: dict) -> list[Request]:
            gaps = {t: targets[team_budget_name(t)] - _spent(before, team_budget_name(t)) for t in org.teams}
            reqs = plan_requests(org, rates, gaps, mix, now)
            total = sum(cost(r, rates)["total"] for r in reqs)
            per = max((sum(1 for r in reqs if r.key.team == t) for t in org.teams), default=0)
            log(f"sync-budgets: {len(reqs)} marker requests on {len({r.key.team for r in reqs})} teams, "
                f"${total:,.2f} to close the gap (max {per} per team; gaps < ${MIN_GAP:g} skipped)")
            return reqs

        if dry_run:
            before = after = _norm(reader())
            targets = targets_now()
            plan(before, targets)
            log("sync-budgets: --dry-run, nothing sent (counters as the Budgets page shows them; "
                "stale after a purge until the next request per budget)")
        else:
            stats = Stats()
            sent_markers = 0
            try:
                with Gateway(kube, sleep=sleep, log=log) as gw:
                    before = _refresh(gw, sender, org, rng, stats, rates, reader, log, sleep, "before")
                    targets = targets_now()          # after the refresh: its spans are recorded and counted
                    stats_before = stats.spend
                    reqs = plan(before, targets)
                    for r in reqs:
                        send_with_reconnect(gw, sender, r, stats, rates)
                    sent_markers = len(reqs)
                    if reqs:
                        log(f"sync-budgets: sent {stats.line()} (incl. {len(org.teams)} refresh requests)")
                        _refresh(gw, sender, org, rng, stats, rates, reader, log, sleep, "after")
            finally:
                if ch is not None:
                    _purge_markers(ch, sent_markers, log, sleep)
            want = _spent(before, org_budget_name(org.name)) + float(stats.spend - stats_before) * 0.999
            for _ in range(int(REFRESH_WAIT_S / 5)):
                after = _norm(reader())
                if _spent(after, org_budget_name(org.name)) >= want:
                    break
                sleep(5)
            else:
                log("sync-budgets: WARNING the Budgets page has not caught up yet; the table shows its current values")
    rows = []
    for name in targets:
        lim = after.get(name, {}).get("limit") or before.get(name, {}).get("limit")
        spent = _spent(after, name)
        rows.append({"name": name, "kind": _kind(org, name), "limit": lim, "target": targets[name],
                     "before": _spent(before, name), "after": spent,
                     "pct": (100 * spent / lim) if lim else None})
    return rows


def _purge_markers(ch, expected: int, log, sleep) -> None:
    """Purge every marker span (also leftovers of an earlier, interrupted run) with the Task 7 machinery."""
    pred = sync_span_predicate()
    n = ch.count_td(pred)
    for _ in range(int(SPAN_WAIT_S / 10)):
        if n >= expected:
            break
        log(f"sync-budgets: {n}/{expected} marker spans in ClickHouse; waiting for the collector")
        sleep(10)
        n = ch.count_td(pred)
    if n == 0:
        log("sync-budgets: no marker spans to purge")
        return
    log(f"sync-budgets: purging {n} marker spans (Task 7 purge, predicate {pred})")
    res = ch.purge_td(predicate=pred, label="budget-sync", log=log, sleep=sleep)
    if res.get("remaining"):
        log(f"sync-budgets: WARNING {res['remaining']} marker spans remain (newer than the live edge); "
            f"re-run `sync-budgets` later, it purges leftover markers first")


def format_table(rows: list[dict]) -> str:
    """Per budget: limit, target (generated window spend), counter before / after, % of limit, status."""
    def status(p):
        if p is None:
            return "?"
        return "OVER" if p >= 100 else "near" if p >= 85 else "under"
    order = {"org": 0, "group": 1, "team": 2}
    lines = [f"{'budget':45} {'kind':5} {'limit':>9} {'target':>11} {'before':>11} {'after':>11} {'% limit':>8}  status"]
    for r in sorted(rows, key=lambda r: (order[r["kind"]], r["name"])):
        lim = f"{r['limit']:,}" if r["limit"] else "?"
        pct = f"{r['pct']:.1f}%" if r["pct"] is not None else "?"
        lines.append(f"{r['name']:45} {r['kind']:5} {lim:>9} {r['target']:>11,.2f} {r['before']:>11,.2f} "
                     f"{r['after']:>11,.2f} {pct:>8}  {status(r['pct'])}")
    return "\n".join(lines)
