import random
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tdlib.org import build_org
from tdlib.pricing import load_rates
from tdlib.usage import (
    BUCKET, CI_USER_AGENTS, CLIENT_FOR_UA, MODELS, USER_AGENTS, Request, activity, client_for_ua,
    cost, generate, key_shares, person_rate, requests_for_window, sample_request,
)

TOR = ZoneInfo("America/Toronto")
FINGERPRINT = (24_123, Decimal("2199.02"))  # (count, spend) for Tue 2026-10-20 Toronto, seed 1
RATES_PATH = Path(__file__).resolve().parents[3] / "apps/agentgateway-system/model-costs.yaml"


def at(y, m, d, h):
    return datetime(y, m, d, h, tzinfo=TOR).astimezone(timezone.utc)


@pytest.fixture(scope="module")
def month():
    end = at(2026, 10, 21, 0)
    return list(generate(build_org(), end - timedelta(days=30), end, seed=11))


@pytest.fixture(scope="module")
def week():
    end = at(2026, 10, 21, 0)
    return list(generate(build_org(), end - timedelta(days=7), end, seed=7))


def _power_key(o):
    return next(k for k in o.keys if k.kind == "personal" and k.user.persona == "power")


def test_models_match_notes_route_table():
    assert set(MODELS) == {"azure-gpt", "azure-gpt-mini", "bedrock-opus", "mistral-large"}
    m = MODELS["azure-gpt"]
    assert (m.route, m.provider, m.model) == ("/azure/gpt-5-4", "azure", "gpt-5.4")
    m = MODELS["azure-gpt-mini"]
    assert (m.route, m.provider, m.model) == ("/azure/gpt-5-4-mini", "azure", "gpt-5.4-mini")
    m = MODELS["bedrock-opus"]
    assert (m.route, m.provider, m.model) == (
        "/bedrock/claude-opus-5-5", "aws.bedrock", "anthropic.claude-opus-5-5")
    m = MODELS["mistral-large"]
    assert (m.route, m.provider, m.model) == ("/mistral/mistral-large", "mistral", "mistral-large-latest")
    for m in MODELS.values():
        assert (m.catalog_provider, m.catalog_model) == (m.provider, m.model)


def test_rates_merge_builtin_and_repo():
    rates = load_rates(RATES_PATH)
    assert rates[("aws.bedrock", "anthropic.claude-opus-5-5")].input == Decimal("4")
    assert rates[("aws.bedrock", "anthropic.claude-opus-5-5")].cache_read == Decimal("0.2")
    assert rates[("azure", "gpt-5.4")].output == Decimal("15")
    assert rates[("azure", "gpt-5.4-mini")].input == Decimal("0.75")
    assert rates[("mistral", "mistral-large-latest")].cache_read == Decimal("0.05")
    for m in MODELS.values():
        assert rates[(m.catalog_provider, m.catalog_model)].input > 0


def test_tool_matches_model():
    o = build_org(); rng = random.Random(1)
    for k in o.keys:
        for _ in range(20):
            r = sample_request(k, at(2026, 10, 20, 11), rng)
            if k.tool in ("claude-code", "claude-desktop"):
                assert r.route.provider == "aws.bedrock"
            elif k.tool == "vscode":
                assert r.route.provider in ("mistral", "azure")
                assert r.route is not MODELS["azure-gpt-mini"]
            else:
                assert r.route.provider == "azure"


def test_business_hours_and_weekends():
    k = _power_key(build_org())
    assert person_rate(k, at(2026, 10, 20, 11)) > 5 * person_rate(k, at(2026, 10, 20, 3))   # Tue 11:00 vs 03:00
    assert person_rate(k, at(2026, 10, 20, 11)) > 3 * person_rate(k, at(2026, 10, 18, 11))  # Tue vs Sun
    assert person_rate(k, at(2026, 10, 20, 3)) > 0


def test_team_keys_steady_with_night_runs():
    k = next(k for k in build_org().keys if k.kind == "team")
    assert person_rate(k, at(2026, 10, 20, 11)) > 3 * person_rate(k, at(2026, 10, 18, 11))
    assert person_rate(k, at(2026, 10, 20, 2)) > 0.1 * person_rate(k, at(2026, 10, 20, 11))


def test_activity_uses_toronto_time_not_local(monkeypatch):
    k = build_org().keys[0]; t = at(2026, 10, 20, 11)
    a = person_rate(k, t)
    monkeypatch.setenv("TZ", "Asia/Tokyo"); time.tzset()
    try:
        assert person_rate(k, t) == a
    finally:
        monkeypatch.undo(); time.tzset()


def test_volume_and_spend_are_realistic(week):
    reqs = week; rates = load_rates(RATES_PATH)
    assert 20_000 < len(reqs) < 150_000
    spend = sum(cost(r, rates)["total"] for r in reqs)
    assert 10_000 < spend < 18_000          # user: 10k+ per 7 days (~ $45-75k/month)
    by_provider = {}
    for r in reqs:
        by_provider[r.route.provider] = by_provider.get(r.route.provider, 0) + cost(r, rates)["total"]
    assert by_provider["azure"] >= 3 * by_provider["aws.bedrock"]   # user: Azure >= 3x each other provider
    assert by_provider["azure"] >= 3 * by_provider["mistral"]
    assert by_provider["aws.bedrock"] > by_provider["mistral"] > 0
    errs = sum(r.status >= 400 for r in reqs) / len(reqs)
    assert 0.005 < errs < 0.03 and any(r.status == 429 for r in reqs)


def test_generate_is_ordered_and_covers_all_keys(week):
    assert all(a.ts <= b.ts for a, b in zip(week, week[1:]))
    assert {r.key.id for r in week} == {k.id for k in build_org().keys}
    assert all(r.ts.tzinfo is not None for r in week[:100])


def test_generate_is_deterministic():
    o = build_org(); end = at(2026, 10, 21, 0); start = end - timedelta(hours=6)
    a = list(generate(o, start, end, seed=3)); b = list(generate(o, start, end, seed=3))
    assert a == b and a
    assert a != list(generate(o, start, end, seed=4))


def test_thirty_days_volume(month):
    end = at(2026, 10, 21, 0); start = end - timedelta(days=30)
    days = [0] * 30
    for r in month:
        assert start <= r.ts < end
        days[(r.ts - start) // timedelta(days=1)] += 1
    assert len(month) < 700_000 and all(d > 0 for d in days)


def test_spend_not_concentrated_in_few_users(month):
    from tdlib.org import service_identity
    rates = load_rates(RATES_PATH); per = {}
    for r in month:
        who = service_identity(r.key)
        per[who] = per.get(who, 0) + cost(r, rates)["total"]
    total = sum(per.values()); top = sorted(per.values(), reverse=True)
    assert top[0] < 5_000
    assert sum(top[:4]) / total < Decimal("0.40")


def test_key_shares_split_person_rate():
    o = build_org(); shares = key_shares(o); t = at(2026, 10, 20, 11)
    multi = {}
    for k in o.keys:
        if k.user is not None:
            multi.setdefault(k.user.email, []).append(k)
    multi = {e: ks for e, ks in multi.items() if len(ks) > 1}
    assert multi
    for ks in multi.values():
        assert sum(shares[k.id] for k in ks) == pytest.approx(1.0)
        assert sum(activity(k, t, shares[k.id]) for k in ks) == pytest.approx(person_rate(ks[0], t))
        primary = min(ks, key=lambda k: k.project)
        assert shares[primary.id] == max(shares[k.id] for k in ks)
    for k in o.keys:
        if k.kind == "team":
            assert shares[k.id] == 1.0
    # independent of key order
    import dataclasses
    rev = dataclasses.replace(o, keys=list(reversed(o.keys)))
    assert key_shares(rev) == shares


def test_generate_equals_concatenated_windows():
    o = build_org(); end = at(2026, 10, 20, 12); start = end - timedelta(hours=3)
    rng = random.Random(5); chunks = []; t = start
    while t < end:
        chunks += requests_for_window(o, t, min(t + BUCKET, end), rng); t += BUCKET
    assert list(generate(o, start, end, seed=5)) == chunks and chunks


def test_generate_independent_of_process_tz(monkeypatch):
    o = build_org(); end = at(2026, 10, 20, 12); start = end - timedelta(hours=4)
    try:
        monkeypatch.setenv("TZ", "America/Toronto"); time.tzset()
        a = list(generate(o, start, end, seed=9))
        monkeypatch.setenv("TZ", "Asia/Tokyo"); time.tzset()
        b = list(generate(o, start, end, seed=9))
    finally:
        monkeypatch.undo(); time.tzset()
    assert a == b and a


def test_fixed_seed_fingerprint():
    # Pins the model: any change to constants/sampling order shows up here. Update deliberately.
    end = at(2026, 10, 21, 0); reqs = list(generate(build_org(), end - timedelta(days=1), end, seed=1))
    rates = load_rates(RATES_PATH)
    spend = sum(cost(r, rates)["total"] for r in reqs).quantize(Decimal("0.01"))
    assert (len(reqs), spend) == FINGERPRINT


def test_requests_are_well_formed(week):
    for r in week:
        assert r.status in (200, 400, 429, 500, 503)
        assert r.latency_ms > 0
        assert 0 <= r.cache_read_tokens <= r.input_tokens
        if r.status == 200:
            assert r.input_tokens > 0 and r.output_tokens > 0
            assert r.input_tokens < 272_000      # stay below gpt-5.4 long-context tier
        else:
            assert r.input_tokens == r.output_tokens == r.cache_read_tokens == 0
        if r.route is MODELS["azure-gpt-mini"]:
            assert r.cache_read_tokens == 0      # built-in cacheRead rate for mini unmeasured


def test_user_agents_map_to_intended_client():
    want = {"copilot": "GitHub Copilot", "vscode": "VS Code",
            "claude-code": "Claude Code", "claude-desktop": "Claude Desktop"}
    for tool, uas in USER_AGENTS.items():
        assert uas
        for ua in uas:
            assert CLIENT_FOR_UA[ua] == client_for_ua(ua) == want[tool]
    for ua in CI_USER_AGENTS:
        assert "sdk-cli" in ua and CLIENT_FOR_UA[ua] == "Claude Code"
    o = build_org(); rng = random.Random(2)
    for k in o.keys:
        ua = sample_request(k, at(2026, 10, 20, 11), rng).user_agent
        assert ua in (CI_USER_AGENTS if k.kind == "team" else USER_AGENTS[k.tool])


def test_cost_matches_rates():
    # 1M input @ rate in, 0 out -> cost == rate
    rates = load_rates(RATES_PATH); route = MODELS["bedrock-opus"]
    rt = rates[(route.catalog_provider, route.catalog_model)]
    k = build_org().keys[0]
    r = Request(at(2026, 10, 20, 11), k, route, "claude-cli/2.1.4 (external, cli)",
                1_000_000, 0, 0, 1000, 200)
    c = cost(r, rates)
    assert c["input"] == rt.input and c["output"] == 0 and c["total"] == rt.input
    # NOTES.md hand check: 48,213 in (31,000 cached) + 1,873 out = $0.112512
    r = Request(r.ts, k, route, r.user_agent, 48_213, 1_873, 31_000, 1000, 200)
    assert cost(r, rates)["total"] == Decimal("0.112512")
