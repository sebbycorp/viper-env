import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tdlib.manifests import (
    CLIENT_RULES, DIMENSIONS_FILE, plan_budgets, projected_spend, render_all, render_dimensions_values,
)
from tdlib.org import build_org, service_identity
from tdlib.pricing import load_rates
from tdlib.usage import CI_USER_AGENTS, MODELS, USER_AGENTS, client_for_ua

ROOT = Path(__file__).resolve().parents[1]
RATES_PATH = ROOT.parents[1] / "apps/agentgateway-system/model-costs.yaml"
NOW = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)
SEED = 42


@pytest.fixture(scope="module")
def org():
    return build_org()


@pytest.fixture(scope="module")
def rates():
    return load_rates(RATES_PATH)


@pytest.fixture(scope="module")
def projection(org, rates):
    return projected_spend(org, rates, SEED, now=NOW)


@pytest.fixture(scope="module")
def budgets(org, rates):
    return plan_budgets(org, rates, SEED, now=NOW)


@pytest.fixture(scope="module")
def objs(org, budgets):
    return render_all(org, budgets, (ROOT / "mock/server.py").read_text())


def kind(objs, k):
    return [o for o in objs if o["kind"] == k]


def test_all_objects_labeled(objs):
    assert objs
    for o in objs:
        assert o["metadata"]["labels"]["demo"] == "td", o["metadata"]["name"]
        if o["kind"] != "Namespace":
            assert o["metadata"]["namespace"] == "td-demo"


def test_routes_only_on_td_hostname(objs):
    routes = kind(objs, "HTTPRoute")
    assert sorted(r["metadata"]["name"] for r in routes) == sorted(MODELS)
    for r in routes:
        assert r["spec"]["hostnames"] == ["td.demo.local"]
        assert r["spec"]["parentRefs"] == [{"name": "agentgateway-proxy", "namespace": "agentgateway-system"}]
        path = r["spec"]["rules"][0]["matches"][0]["path"]
        assert path == {"type": "PathPrefix", "value": MODELS[r["metadata"]["name"]].route}


def test_backends_match_notes(objs):
    bes = {b["metadata"]["name"]: b for b in kind(objs, "AgentgatewayBackend")}
    assert sorted(bes) == sorted(MODELS)
    for rid, m in MODELS.items():
        c = bes[rid]["spec"]["ai"]["provider"]["custom"]
        assert c["providerOverride"] == m.provider and c["model"] == m.model
        assert c["formats"] == [{"type": "Completions"}]
        assert c["backendRef"] == {"name": "td-mock-llm", "port": 8080}


def test_policy_targets_every_route(objs):
    (pol,) = kind(objs, "EnterpriseAgentgatewayPolicy")
    names = sorted(t["name"] for t in pol["spec"]["targetRefs"])
    assert names == sorted(r["metadata"]["name"] for r in kind(objs, "HTTPRoute"))
    assert all(t["kind"] == "HTTPRoute" for t in pol["spec"]["targetRefs"])
    auth = pol["spec"]["traffic"]["apiKeyAuthentication"]
    assert auth["mode"] == "Strict" and auth["secretRef"] == {"name": "td-virtual-keys"}
    assert "entBudgetEnforcement" in pol["spec"]["traffic"]


def test_budgets_cover_teams_groups_org(org, objs, budgets):
    (b,) = kind(objs, "EnterpriseAgentgatewayBudget")
    assert b["metadata"]["name"] == "td-budgets"
    entries = b["spec"]["budgets"]
    assert entries == budgets and len(entries) == 21
    assert len({e["name"] for e in entries}) == 21
    for e in entries:
        assert e["onBudgetExceeded"] == "Audit" and e["window"] == {"unit": "Month"}
        assert e["limit"]["unit"] == "USD"
        assert isinstance(e["limit"]["amount"], int) and e["limit"]["amount"] >= 1
    subjects = [e["subject"] for e in entries]
    assert sorted(s["team"] for s in subjects if "team" in s) == sorted(org.teams)
    assert sorted(s["group"] for s in subjects if "group" in s) == sorted(org.groups)
    assert [s for s in subjects if "organization" in s] == [{"organization": "TD"}]
    assert all(len(s) == 1 for s in subjects)


def test_budget_mix(budgets, projection):
    pct = {}
    for e in budgets:
        (dim, val), = e["subject"].items()
        pct[(dim, val)] = float(projection[dim][val]) / e["limit"]["amount"]
    team = [p for (d, _), p in pct.items() if d == "team"]
    assert len(team) == 15
    assert 1 <= sum(p > 1.0 for p in team) <= 2
    assert sum(0.85 <= p <= 0.95 for p in team) == 2
    assert sum(p < 0.75 for p in team) == 15 - sum(p > 1.0 for p in team) - 2
    for (d, _), p in pct.items():
        if d in ("group", "organization"):
            assert 0.60 <= p <= 0.90, (d, p)


def test_projection_is_consistent(org, projection):
    total = projection["organization"]["TD"]
    assert total > 0
    assert abs(sum(projection["team"].values()) - total) < 1e-6
    assert abs(sum(projection["group"].values()) - total) < 1e-6
    assert set(projection["team"]) == set(org.teams)


def test_mock_pod_requests_small(objs):
    (dep,) = kind(objs, "Deployment")
    (c,) = dep["spec"]["template"]["spec"]["containers"]
    assert c["resources"]["requests"] == {"cpu": "25m", "memory": "64Mi"}
    (cm,) = [o for o in kind(objs, "ConfigMap") if o["metadata"]["name"] == "td-mock-llm"]
    assert "ThreadingHTTPServer" in cm["data"]["server.py"]


def test_keys_secret_contains_all_keys(org, objs):
    (sec,) = kind(objs, "Secret")
    assert sec["metadata"]["name"] == "td-virtual-keys" and sec["type"] == "Opaque"
    data = sec["stringData"]
    assert len(data) == len(org.keys)
    for k in org.keys:
        doc = json.loads(data[k.id])
        assert doc["key"] == k.secret and doc["keyHash"] is None
        md = doc["metadata"]
        assert md["id"] == k.id and md["organization"] == "TD" and md["group"] == k.group
        assert md["custom"] == {"team": k.team, "project": k.project}
        assert "team" not in md and "project" not in md
        assert md["user"] == service_identity(k)


def test_client_rules_substrings_in_user_agents():
    """Every UA contains the substring its tool's CEL branch matches (no CEL evaluated)."""
    labels = [label for label, _ in CLIENT_RULES]
    assert labels == ["Claude Code (CI)", "Claude Code", "GitHub Copilot", "Claude Desktop", "VS Code"]
    want = {"copilot": "GitHub Copilot", "vscode": "VS Code", "claude-code": "Claude Code",
            "claude-desktop": "Claude Desktop"}
    rules = dict(CLIENT_RULES)
    cases = [(ua, want[t]) for t, uas in USER_AGENTS.items() for ua in uas]
    cases += [(ua, "Claude Code (CI)") for ua in CI_USER_AGENTS]
    for ua, label in cases:
        assert any((ua.startswith(s) if op == "startsWith" else s in ua) for op, s in rules[label]), ua
        assert client_for_ua(ua) == label


def test_dimension_values_file_in_sync():
    text = render_dimensions_values()
    assert DIMENSIONS_FILE.read_text() == text
    for label, conds in CLIENT_RULES:
        assert f'"{label}"' in text
        for op, s in conds:
            assert f'request.headers["user-agent"].{op}("{s}")' in text
    for dim in ("organization", "group", "team", "user", "project", "client", "virtualKey"):
        assert f"id: {dim}" in text
    assert "apiKey.custom.team" in text and "apiKey.custom.project" in text
