"""Synthetic agentgateway LLM spans for the TD history backfill (NOTES.md §5, span_template.json).

`to_row` turns one `usage.Request` into a `platformdb.otel_traces_json` JSONEachRow row shaped
exactly like a live TD request through `agentgateway-proxy` (checked against Task 5's live rows).
"""
from __future__ import annotations

import json
import random
from datetime import timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .manifests import BUDGET, HOSTNAME, NAMESPACE, budget_entry_names
from .org import service_identity
from .pricing import Rates
from .usage import MODELS, Request, client_for_ua, cost

TEMPLATE_PATH = Path(__file__).with_name("span_template.json")
ORG_NAME = "TD"                      # org.build_org() name; the `organization` dimension value
ROUTE_IDS = {m.route: rid for rid, m in MODELS.items()}
COST_KEYS = ("input", "output", "cache_read", "total")

_ids = random.SystemRandom()
_rng = random.Random(_ids.getrandbits(64))     # fast, unseeded: ids / sub-µs digits only
_clone_cache: dict[int, tuple[dict, str]] = {}


def td_span_predicate() -> str:
    """SQL boolean over `platformdb.otel_traces_json` matching every TD span.

    TD custom dimensions (backfilled and live requests), or the TD host `td.demo.local`
    (also catches 401s without dimensions and the Task 4 spike spans with odd provider labels).
    """
    return ("(position(toString(SpanAttributes.`agw.llm.custom_dimensions`), "
            "'[\"organization\",\"TD\"]') > 0 "
            f"OR toString(SpanAttributes.`server.address`) = '{HOSTNAME}')")


def load_template(path: Path = TEMPLATE_PATH) -> dict:
    return json.loads(Path(path).read_text())


def _clone(template: dict) -> dict:
    """Deep copy via a cached JSON round-trip (much faster than copy.deepcopy per row)."""
    hit = _clone_cache.get(id(template))
    if hit is None or hit[0] is not template:
        hit = (template, json.dumps(template))
        _clone_cache[id(template)] = hit
    return json.loads(hit[1])


def fmt_decimal(d: Decimal) -> str:
    """Plain decimal string the gateway would write ("0.112512", "0"), never exponent notation."""
    if d == 0:
        return "0"
    return format(d.normalize(), "f")


def fmt_ts(ts) -> str:
    """UTC "YYYY-MM-DD hh:mm:ss.fffffffff" (ns digits beyond µs are filler)."""
    u = ts.astimezone(timezone.utc)
    return f"{u:%Y-%m-%d %H:%M:%S}.{u.microsecond:06d}{_rng.randrange(1000):03d}"


def custom_dimensions(req: Request) -> str:
    k = req.key
    dims = {
        "client": client_for_ua(req.user_agent),
        "group": k.group,
        "model": req.route.model,
        "organization": ORG_NAME,
        "project": k.project,
        "provider": req.route.provider,
        "team": k.team,
        "user": service_identity(k),
        "virtualKey": k.id,
    }
    # The gateway sorts pairs by dimension id and writes compact JSON (NOTES.md §4).
    return json.dumps([[i, dims[i]] for i in sorted(dims)], separators=(",", ":"), ensure_ascii=False)


def to_row(req: Request, rates: dict[tuple[str, str], Rates], template: dict, cluster: str) -> dict:
    row = _clone(template)
    route_id = ROUTE_IDS[req.route.route]
    trace_id = f"{_rng.getrandbits(128):032x}"
    span_id = f"{_rng.getrandbits(64):016x}"
    ok = req.status < 400

    row["Timestamp"] = fmt_ts(req.ts)
    row["TraceId"] = trace_id
    row["SpanId"] = span_id
    row["SpanName"] = f"POST {req.route.route}/*"
    row["Duration"] = req.latency_ms * 1_000_000
    row["StatusCode"] = "Error" if req.status >= 500 else "Unset"
    row["ResourceAttributes"]["cluster_name"] = cluster

    sa = row["SpanAttributes"]
    c = cost(req, rates) if ok else {k: Decimal(0) for k in COST_KEYS}
    usage_cost = sa["agw"]["ai"]["usage"]["cost"]
    for k in usage_cost:
        usage_cost[k] = "0"
    for k in COST_KEYS:
        usage_cost[k] = fmt_decimal(c[k])

    b = sa["agw"]["ai"]["budgets"]
    b.pop("unit", None)                       # USD-only budgets: live spans carry no `unit`
    b.update({
        "action_taken": "ALLOW", "applied": True, "blocked": False,
        "charged_micro_usd": str(int((c["total"] * 1_000_000).quantize(Decimal(1), ROUND_HALF_UP))),
        "charged_tokens": 0, "charged_tokens_exact": "0", "count": 3,
        "ids": sorted(f"{NAMESPACE}/{BUDGET}/{n}" for n in budget_entry_names(ORG_NAME, req.key.group, req.key.team)),
        "over_limit": False, "over_limit_after_debit": False,
    })
    sa["agw"]["llm"]["custom_dimensions"] = custom_dimensions(req)

    g = sa["gen_ai"]
    g["provider"]["name"] = req.route.provider
    g["request"]["model"] = req.route.model
    if ok:
        g["response"]["model"] = req.route.model
        g["usage"] = {"cache_read": {"input_tokens": req.cache_read_tokens},
                      "input_tokens": req.input_tokens, "output_tokens": req.output_tokens}
    else:                                     # like the live 429 span: no response model, no usage
        g.pop("response", None)
        g.pop("usage", None)

    sa["duration"] = f"{req.latency_ms}ms"
    sa["http"]["response"]["status_code"] = req.status
    sa["route"] = f"{NAMESPACE}/{route_id}"
    sa["server"]["address"] = HOSTNAME
    sa["span"]["id"] = span_id
    sa["trace"]["id"] = trace_id
    sa["url"]["path"] = f"{req.route.route}/v1/chat/completions"
    return row


def outcome_row(entries: dict[str, int], ts, budget_ref: str, cluster: str) -> dict:
    """A zero-cost `agw.ai.budgets.outcome` row setting each entry's displayed spend (NOTES.md, spike).

    The Budgets page takes, per entry `<budget_ref>/<entry>`, the newest `current_usage` (µUSD for USD
    entries, tokens for Tokens entries) in `statuses`, a JSON *string*. A child span (non-empty
    ParentSpanId) with no route / model / custom dimensions and no cost feeds no cost rollup, and
    `server.address = td.demo.local` puts it under `td_span_predicate()` so TD purges remove it.
    """
    statuses = [{"id": f"{budget_ref}/{name}", "current_usage": int(v)} for name, v in entries.items()]
    trace_id, span_id, parent = (f"{_rng.getrandbits(128):032x}", f"{_rng.getrandbits(64):016x}",
                                 f"{_rng.getrandbits(64):016x}")
    return {
        "Timestamp": fmt_ts(ts), "TraceId": trace_id, "SpanId": span_id, "ParentSpanId": parent,
        "TraceState": "", "SpanName": "agw.ai.budgets.outcome", "SpanKind": "Internal",
        "ServiceName": "agentgateway-proxy", "ResourceAttributes": {"cluster_name": cluster},
        "ScopeName": "agentgateway", "ScopeVersion": "",
        "SpanAttributes": {
            "agw": {"ai": {"budgets": {"phase": "final_debit_result",
                                       "statuses": json.dumps(statuses, separators=(",", ":"))}}},
            "server": {"address": HOSTNAME},
        },
        "Duration": 0, "StatusCode": "Unset", "StatusMessage": "",
        "Events.Timestamp": [], "Events.Name": [], "Events.Attributes": [],
        "Links.TraceId": [], "Links.SpanId": [], "Links.TraceState": [], "Links.Attributes": [],
    }
