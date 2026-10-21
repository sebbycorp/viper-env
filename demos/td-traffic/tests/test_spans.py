import json
import random
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tdlib.manifests import BUDGET, NAMESPACE, budget_entry_names
from tdlib.org import build_org, service_identity
from tdlib.pricing import load_rates
from tdlib.spans import TEMPLATE_PATH, load_template, td_span_predicate, to_row
from tdlib.usage import CLIENT_FOR_UA, MODELS, Request, cost

RATES_PATH = Path(__file__).resolve().parents[3] / "apps/agentgateway-system/model-costs.yaml"
TS = datetime(2026, 10, 14, 15, 4, 5, 123456, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def org():
    return build_org()


@pytest.fixture(scope="module")
def rates():
    return load_rates(RATES_PATH)


@pytest.fixture(scope="module")
def template():
    return load_template()


def _req(org, route_id="bedrock-opus", status=200, kind="personal", latency=1501):
    key = next(k for k in org.keys if k.kind == kind)
    ua = next(u for u in CLIENT_FOR_UA if CLIENT_FOR_UA[u] == ("Claude Code (CI)" if kind == "team" else "Claude Code"))
    toks = (48_213, 1_873, 31_000) if status == 200 else (0, 0, 0)
    return Request(TS, key, MODELS[route_id], ua, *toks, latency, status)


def _dims(row):
    return json.loads(row["SpanAttributes"]["agw"]["llm"]["custom_dimensions"])


def test_row_has_template_shape(org, rates, template):
    row = to_row(_req(org), rates, template, "omni-viper")
    assert list(row) == list(template)
    raw = json.loads(TEMPLATE_PATH.read_text())
    assert set(row) == set(raw)


def test_template_not_mutated(org, rates, template):
    before = json.dumps(template, sort_keys=True)
    to_row(_req(org, "azure-gpt"), rates, template, "elsewhere")
    assert json.dumps(template, sort_keys=True) == before


def test_row_ids(org, rates, template):
    row = to_row(_req(org), rates, template, "omni-viper")
    assert row["ServiceName"] == "agentgateway-proxy"
    assert row["ScopeName"] == "agentgateway"
    assert row["SpanKind"] == "Server"
    assert row["ParentSpanId"] == ""
    assert re.fullmatch(r"[0-9a-f]{32}", row["TraceId"])
    assert re.fullmatch(r"[0-9a-f]{16}", row["SpanId"])
    sa = row["SpanAttributes"]
    assert sa["trace"]["id"] == row["TraceId"] and sa["span"]["id"] == row["SpanId"]


def test_row_timestamps(org, rates, template):
    row = to_row(_req(org), rates, template, "omni-viper")
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{9}", row["Timestamp"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{9}", template["Timestamp"])
    assert row["Timestamp"].startswith("2026-10-14 15:04:05.123456")
    # a non-UTC aware ts is converted to UTC
    req = _req(org)
    req.ts = TS.astimezone(__import__("zoneinfo").ZoneInfo("America/Toronto"))
    assert to_row(req, rates, template, "c")["Timestamp"].startswith("2026-10-14 15:04:05.123456")


def test_row_cost_and_dimensions(org, rates, template):
    req = _req(org)
    row = to_row(req, rates, template, "omni-viper")
    assert row["Duration"] == req.latency_ms * 1_000_000
    c = cost(req, rates)
    got = row["SpanAttributes"]["agw"]["ai"]["usage"]["cost"]
    for k in ("input", "output", "cache_read", "total"):
        assert "E" not in got[k] and Decimal(got[k]) == c[k]
    assert got["total"] == "0.112512"            # NOTES.md hand check
    assert set(got) == set(template["SpanAttributes"]["agw"]["ai"]["usage"]["cost"])
    k = req.key
    want = [["client", "Claude Code"], ["group", k.group], ["model", "anthropic.claude-opus-5-5"],
            ["organization", "TD"], ["project", k.project], ["provider", "aws.bedrock"],
            ["team", k.team], ["user", service_identity(k)], ["virtualKey", k.id]]
    assert _dims(row) == want
    raw = row["SpanAttributes"]["agw"]["llm"]["custom_dimensions"]
    assert raw == json.dumps(want, separators=(",", ":"))     # same compact format as the template
    assert [p[0] for p in json.loads(template["SpanAttributes"]["agw"]["llm"]["custom_dimensions"])] \
        == [p[0] for p in want]
    g = row["SpanAttributes"]["gen_ai"]
    assert g["provider"]["name"] == "aws.bedrock" and g["request"]["model"] == g["response"]["model"]
    assert g["usage"] == {"cache_read": {"input_tokens": 31_000}, "input_tokens": 48_213, "output_tokens": 1_873}


def test_row_team_key_and_route_fields(org, rates, template):
    req = _req(org, "azure-gpt-mini", kind="team")
    row = to_row(req, rates, template, "omni-viper")
    sa = row["SpanAttributes"]
    assert row["SpanName"] == "POST /azure/gpt-5-4-mini/*"
    assert sa["route"] == "td-demo/azure-gpt-mini"
    assert sa["url"]["path"].startswith("/azure/gpt-5-4-mini")
    assert dict(_dims(row))["client"] == "Claude Code (CI)"
    assert dict(_dims(row))["user"] == service_identity(req.key)


def test_row_spike_values_replaced(org, rates, template):
    req = _req(org)
    row = to_row(req, rates, template, "my-cluster")
    assert row["ResourceAttributes"]["cluster_name"] == "my-cluster"
    sa = row["SpanAttributes"]
    assert sa["server"]["address"] == "td.demo.local"
    b = sa["agw"]["ai"]["budgets"]
    names = budget_entry_names("TD", req.key.group, req.key.team)
    assert b["ids"] == sorted(f"{NAMESPACE}/{BUDGET}/{n}" for n in names)
    assert b["count"] == 3 and b["applied"] is True
    assert b["charged_micro_usd"] == "112512"
    assert "unit" not in b                      # USD-only budgets carry no unit (live span)
    blob = json.dumps(row)
    for spike in ("td-spike-budget", "td-final-1", "priya.patel"):
        assert spike not in blob


def test_row_status(org, rates, template):
    row = to_row(_req(org, "azure-gpt", status=503, latency=400), rates, template, "c")
    assert row["StatusCode"] == "Error"
    sa = row["SpanAttributes"]
    assert sa["http"]["response"]["status_code"] == 503
    assert sa["agw"]["ai"]["usage"]["cost"]["total"] == "0"
    assert "usage" not in sa["gen_ai"] and "response" not in sa["gen_ai"]   # like the live 429 span
    assert to_row(_req(org, status=429), rates, template, "c")["StatusCode"] == "Unset"
    assert to_row(_req(org), rates, template, "c")["StatusCode"] == "Unset"


def test_unique_ids(org, rates, template):
    rng = random.Random(1)
    rows = [to_row(_req(org, rng.choice(list(MODELS))), rates, template, "c") for _ in range(10_000)]
    assert len({r["TraceId"] for r in rows}) == 10_000
    assert len({r["SpanId"] for r in rows}) == 10_000


def test_td_span_predicate_matches_org_and_host():
    sql = td_span_predicate()
    assert '["organization","TD"]' in sql and "agw.llm.custom_dimensions" in sql
    assert "server.address" in sql and "'td.demo.local'" in sql
    assert " OR " in sql
