import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tdlib.manifests import (
    CLIENT_RULES, DIMENSIONS_FILE, budget_window, plan_budgets, render_all, render_dimensions_values, window_spend,
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
    return window_spend(org, rates, SEED, now=NOW)


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
    assert entries == budgets and len(entries) == 41 <= 64
    assert len({e["name"] for e in entries}) == 41
    tok = [e for e in entries if e["limit"]["unit"] == "Tokens"]
    usd = [e for e in entries if e["limit"]["unit"] == "USD"]
    assert len(tok) == 3 and len(usd) == 38
    for e in entries:
        assert e["onBudgetExceeded"] == "Audit"
        assert e["window"] == ({"unit": "Day"} if e in tok else {"unit": "Month"})
        assert isinstance(e["limit"]["amount"], int) and 1 <= e["limit"]["amount"] <= 4294967295
    assert all(list(e["subject"]) == ["team"] for e in tok)
    subjects = [e["subject"] for e in usd]
    assert sorted(s["project"] for s in subjects if "project" in s) == sorted(org.projects)
    assert len([s for s in subjects if "user" in s]) == 5
    assert sorted(s["team"] for s in subjects if "team" in s) == sorted(org.teams)
    assert sorted(s["group"] for s in subjects if "group" in s) == sorted(org.groups)
    assert [s for s in subjects if "organization" in s] == [{"organization": "TD"}]
    assert all(len(s) == 1 for s in subjects)


def _mix_ok(budgets, spend):
    pct = {}
    for e in budgets:
        (dim, val), = e["subject"].items()
        if e["limit"]["unit"] == "Tokens":
            p = spend["team_tokens_day"][val] / e["limit"]["amount"]
            assert 0.50 <= p <= 0.90, ("tokens", val, p)          # a typical day at 50-90%
            continue
        pct[(dim, val)] = float(spend[dim][val]) / e["limit"]["amount"]
    proj = [p for (d, _), p in pct.items() if d == "project"]
    assert len(proj) == 12
    assert sum(1.0 <= p <= 1.30 for p in proj) >= 1, sorted(proj)
    assert sum(0.85 <= p <= 0.95 for p in proj) >= 1, sorted(proj)
    usr = [p for (d, _), p in pct.items() if d == "user"]
    assert len(usr) == 5 and sum(0.85 <= p <= 0.95 for p in usr) >= 1, sorted(usr)
    team = [p for (d, _), p in pct.items() if d == "team"]
    assert len(team) == 15
    over = sum(1.0 <= p <= 1.30 for p in team)
    assert 1 <= over <= 2, sorted(team)
    assert sum(0.85 <= p <= 0.95 for p in team) == 2, sorted(team)
    assert sum(p < 0.75 for p in team) == 15 - over - 2, sorted(team)
    for (d, v), p in pct.items():
        if d in ("group", "organization"):
            assert 0.60 <= p <= 0.90, (d, v, p)


def test_budget_mix(budgets, projection):
    _mix_ok(budgets, projection)


# Window-to-date spend, 1 / 6.5 / 17.6 / 29 days into the gateway's epoch-aligned 30-day window.
WINDOW0 = datetime(2026, 10, 4, tzinfo=timezone.utc)


@pytest.mark.parametrize("days", [1, 6.5, 17.6, 29])
def test_budget_mix_window_to_date(org, rates, days):
    now = WINDOW0 + timedelta(days=days)
    _mix_ok(plan_budgets(org, rates, SEED, now=now), window_spend(org, rates, SEED, now=now))


def test_budget_window_matches_gateway():
    # omni-viper 2026-10-21: Month counter key suffix 1791072000 = 2026-10-04 00:00 UTC, TTL ~30 days.
    assert budget_window(NOW) == (WINDOW0, WINDOW0 + timedelta(days=30))
    assert int(budget_window(NOW)[0].timestamp()) == 1791072000
    assert budget_window(datetime(2026, 11, 3, tzinfo=timezone.utc))[0] == datetime(2026, 11, 3, tzinfo=timezone.utc)


def test_window_spend_is_generated_window_to_date(org, rates):
    from tdlib.usage import cost, generate
    now = WINDOW0 + timedelta(hours=8)
    spend = window_spend(org, rates, SEED, now=now)
    total = sum(float(cost(r, rates)["total"]) for r in generate(org, WINDOW0, now, SEED) if r.status == 200)
    assert spend["organization"]["TD"] == pytest.approx(total, abs=1e-6)


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
