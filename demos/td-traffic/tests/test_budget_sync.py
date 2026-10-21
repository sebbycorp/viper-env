"""sync-budgets v2: the Budgets page reads zero-cost `agw.ai.budgets.outcome` rows in ClickHouse."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tdlib import budget_sync as bs
from tdlib.clickhouse import SPANS_TABLE
from tdlib.manifests import BUDGET, HOSTNAME, NAMESPACE, budget_window, plan_budgets
from tdlib.org import build_org
from tdlib.pricing import load_rates
from tdlib.spans import outcome_row, td_span_predicate

RATES = load_rates(Path(__file__).resolve().parents[3] / "apps/agentgateway-system/model-costs.yaml")
ORG = build_org()
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
REF = f"{NAMESPACE}/{BUDGET}"

ROW_KEYS = {"Timestamp", "TraceId", "SpanId", "ParentSpanId", "TraceState", "SpanName", "SpanKind", "ServiceName",
            "ResourceAttributes", "ScopeName", "ScopeVersion", "SpanAttributes", "Duration", "StatusCode",
            "StatusMessage", "Events.Timestamp", "Events.Name", "Events.Attributes", "Links.TraceId",
            "Links.SpanId", "Links.TraceState", "Links.Attributes"}


def _ts(row) -> datetime:
    return datetime.strptime(row["Timestamp"][:26], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)


def _leaves(d, path=()):
    if isinstance(d, dict):
        for k, v in d.items():
            yield from _leaves(v, path + (k,))
    else:
        yield ".".join(path), d


def test_outcome_row_shape():
    row = outcome_row({"team-a-month-usd": 1_234_567, "team-a-daily-tokens": 900}, NOW, REF, "omni-viper")
    assert set(row) == ROW_KEYS
    assert row["ScopeName"] == "agentgateway" and row["SpanName"] == "agw.ai.budgets.outcome"
    assert row["ParentSpanId"] and row["TraceId"] and row["SpanId"]
    assert _ts(row) == NOW.replace(tzinfo=timezone.utc)
    assert row["ResourceAttributes"]["cluster_name"] == "omni-viper"
    b = row["SpanAttributes"]["agw"]["ai"]["budgets"]
    assert b["phase"] == "final_debit_result"
    assert isinstance(b["statuses"], str)
    assert json.loads(b["statuses"]) == [
        {"id": f"{REF}/team-a-month-usd", "current_usage": 1_234_567},
        {"id": f"{REF}/team-a-daily-tokens", "current_usage": 900},
    ]
    assert row["SpanAttributes"]["server"]["address"] == HOSTNAME


def test_outcome_row_zero_cost_no_dims():
    row = outcome_row({"td-org-month-usd": 5_000_000_000}, NOW, REF, "c")
    leaves = dict(_leaves(row["SpanAttributes"]))
    assert not any("custom_dimensions" in k or k.startswith("gen_ai") or k == "route" or "model" in k
                   for k in leaves)
    assert all(str(v) in ("0", "0.0") for k, v in leaves.items() if "cost" in k or "charged" in k)
    assert row["ParentSpanId"] != ""          # child span: no cost rollup picks it up


def test_outcome_rows_match_td_predicate():
    row = outcome_row({"td-org-month-usd": 1}, NOW, REF, "c")
    pred = td_span_predicate()
    m = re.search(r"SpanAttributes\.`server\.address`\) = '([^']+)'", pred)
    assert m and row["SpanAttributes"]["server"]["address"] == m.group(1)


class FakeKube:
    def __init__(self, created: datetime):
        self.created = created
        self.calls = []

    def run(self, args, check=True, input=None):
        self.calls.append(args)
        assert args[:3] == ["get", "enterpriseagentgatewaybudget", BUDGET] and NAMESPACE in args
        return json.dumps({"metadata": {"name": BUDGET, "namespace": NAMESPACE,
                                        "creationTimestamp": self.created.strftime("%Y-%m-%dT%H:%M:%SZ")},
                           "spec": {"budgets": []}})


class FakeCH:
    def __init__(self):
        self.inserts = []

    def cluster_name(self, default):
        return "omni-viper"

    def insert_rows(self, table, rows, batch=20_000, progress=None):
        rows = list(rows)
        self.inserts.append((table, rows))
        return len(rows)


@pytest.mark.parametrize("created", [
    NOW - timedelta(days=3),
    NOW - timedelta(seconds=1),                 # `up` just re-created it
    NOW + timedelta(seconds=30),                # clock skew: still dated after creation
])
def test_outcome_rows_after_budget_creation(created):
    targets = {f"e{i}": i + 1 for i in range(41)}
    ch = FakeCH()
    bs.write_budget_outcomes(ch, FakeKube(created), targets, NOW)
    (table, rows), = ch.inserts
    assert table == SPANS_TABLE
    seen = {}
    for r in rows:
        assert _ts(r) > created
        assert _ts(r) == max(NOW - timedelta(seconds=1), created + timedelta(seconds=1))
        for s in json.loads(r["SpanAttributes"]["agw"]["ai"]["budgets"]["statuses"]):
            seen[s["id"]] = s["current_usage"]
    assert seen == {f"{REF}/{k}": v for k, v in targets.items()}
    assert len(rows) > 1 and all(len(json.loads(r["SpanAttributes"]["agw"]["ai"]["budgets"]["statuses"]))
                                 <= bs.ENTRIES_PER_ROW for r in rows)


def test_targets_cover_all_41_entries():
    budgets = plan_budgets(ORG, RATES, 42, NOW)
    t = bs.budget_targets(ORG, budgets, NOW, 42, rates=RATES)
    assert set(t) == {e["name"] for e in budgets} and len(t) == 41
    assert all(isinstance(v, int) and v > 0 for v in t.values())


def _pct(budgets, targets):
    out = {}
    for e in budgets:
        lim = e["limit"]["amount"] * (1_000_000 if e["limit"]["unit"] == "USD" else 1)
        out[e["name"]] = 100 * targets[e["name"]] / lim
    return out


@pytest.mark.parametrize("days", [1, 6.5, 17.6, 29])
def test_targets_meet_mix(days):
    now = budget_window(NOW)[0] + timedelta(days=days)
    budgets = plan_budgets(ORG, RATES, 42, now)
    pct = _pct(budgets, bs.budget_targets(ORG, budgets, now, 42, rates=RATES))
    kind = {e["name"]: bs.entry_kind(e) for e in budgets}

    def of(k):
        return [p for n, p in pct.items() if kind[n] == k]
    team = of("team")
    assert 1 <= sum(100 <= p <= 130 for p in team) <= 2
    assert sum(85 <= p <= 95 for p in team) == 2
    assert sum(p < 75 for p in team) == len(team) - sum(p >= 85 for p in team)
    assert all(60 <= p <= 90 for p in of("group") + of("org"))
    assert any(p >= 100 for p in of("proj")) and any(85 <= p < 100 for p in of("proj"))
    assert any(85 <= p < 100 for p in of("user"))
    assert len(of("tok")) == 3 and all(50 <= p <= 90 for p in of("tok"))
    assert all(p > 0 for p in pct.values())


def test_live_requests_add_to_targets():
    budgets = plan_budgets(ORG, RATES, 42, NOW)
    base = bs.budget_targets(ORG, budgets, NOW, 42, rates=RATES)
    from tdlib.usage import generate
    extra = [r for r in generate(ORG, NOW - timedelta(minutes=30), NOW, 7) if r.status == 200][:50]
    more = bs.budget_targets(ORG, budgets, NOW, 42, rates=RATES, live=extra)
    org_name = next(e["name"] for e in budgets if "organization" in e["subject"])
    assert more[org_name] > base[org_name]
    assert all(more[n] >= base[n] for n in base)


def test_no_marker_or_listbudgets_paths_left():
    for gone in ("sync_span_predicate", "list_budgets", "plan_key_requests", "run_sync", "MARKER_UA"):
        assert not hasattr(bs, gone)
