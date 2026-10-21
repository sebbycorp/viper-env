import contextlib
import struct
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tdlib import budget_sync as bs
from tdlib.manifests import (group_budget_name, org_budget_name, project_budget_name, team_budget_name,
                             user_budget_name)
from tdlib.org import build_org, service_identity
from tdlib.pricing import load_rates
from tdlib.spans import td_span_predicate
from tdlib.usage import MODELS, client_for_ua, cost, generate

from test_backfill import NOW as PURGE_NOW, STAMP, FakeHTTPCH

RATES = load_rates(Path(__file__).resolve().parents[3] / "apps/agentgateway-system/model-costs.yaml")
ORG = build_org()
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)


class FakeKube:
    def __init__(self):
        self.entered = 0

    @contextlib.contextmanager
    def port_forward(self, target, ns, remote, timeout=20.0):
        self.entered += 1
        yield 18080


class FakeCH:
    """ClickHouse stand-in for run_sync: the marker spans 'arrive' once sent, purge_td records its predicate.
    Ordinary (non-marker) requests sent by the tool count as live TD spend recorded in ClickHouse."""

    def __init__(self, sent, history_end=NOW + timedelta(days=1), live_extra=None):
        self.sent = sent
        self.purges = []
        self.counts = []
        self.history_end = history_end
        self.live_extra = live_extra or {}

    def td_history_end(self, window_start, exclude=None):
        return self.history_end

    def td_live_spend_by_team(self, window_start, exclude=None):
        out = defaultdict(float, self.live_extra)
        for r in self.sent:
            if r.user_agent != bs.MARKER_UA:
                out[r.key.team] += float(cost(r, RATES)["total"])
        return dict(out)

    def td_live_spend_by_key(self, window_start, exclude=None):
        out = defaultdict(float)
        for team, v in self.live_extra.items():
            out[sorted(k.id for k in ORG.keys if k.team == team)[0]] += v
        for r in self.sent:
            if r.user_agent != bs.MARKER_UA:
                out[r.key.id] += float(cost(r, RATES)["total"])
        return dict(out)

    def count_td(self, predicate=None):
        self.counts.append(predicate)
        return 0 if self.purges else len(self.sent)

    def purge_td(self, **kw):
        self.purges.append(kw)
        return {"td_rows": len(self.sent), "remaining": 0}


class FakeCounters:
    """ListBudgets stand-in that behaves like the gateway: each entry = base + the cost of every request
    sent on that team (team, group and org entries charged together). With `stale`, it reports `stale`
    values until the first request goes out (the page reads the latest outcome span, NOTES.md)."""

    def __init__(self, base, sent, stale=None):
        self.base, self.sent, self.stale = base, sent, stale
        self.reads = 0

    def __call__(self):
        self.reads += 1
        if self.stale is not None and not self.sent:
            return dict(self.stale)
        out = {n: float(v) for n, v in self.base.items()}
        for r in self.sent:
            c = float(cost(r, RATES)["total"])
            for n in (team_budget_name(r.key.team), group_budget_name(r.key.group), org_budget_name("TD"),
                      project_budget_name(r.key.project), user_budget_name(service_identity(r.key))):
                out[n] = out.get(n, 0.0) + c
        return out


def is_marker(r):
    return r.user_agent == bs.MARKER_UA


def run(counters_before, sender=None, now=NOW, start=None, purge=True, stale=None, sent=None, ch=None):
    sent = [] if sent is None else sent
    ch = ch or FakeCH(sent)

    @contextlib.contextmanager
    def open_ch():
        yield ch

    def send(req, base):
        sent.append(req)
        return 200
    reader = FakeCounters(counters_before, sent, stale)
    rows = bs.run_sync(FakeKube(), ORG, rates=RATES, seed=42, now=now, start=start, sender=sender or send,
                       read_counters=reader, open_clickhouse=open_ch if purge else None, log=lambda m: None,
                       sleep=lambda s: None)
    return rows, sent, ch


def test_budget_window_start_is_epoch_aligned_30_days():
    # Verified on omni-viper: the Month counter key for 2026-10-21 ends in 1791072000 (2026-10-04 00:00 UTC).
    assert bs.budget_window_start(NOW) == datetime(2026, 10, 4, tzinfo=timezone.utc)
    assert int(bs.budget_window_start(NOW).timestamp()) == 1791072000
    assert bs.budget_window_start(datetime(2026, 11, 3, 0, 0, 1, tzinfo=timezone.utc)) == \
        datetime(2026, 11, 3, tzinfo=timezone.utc)


def test_sync_targets_match_history():
    start = NOW - timedelta(hours=6)
    t = bs.history_targets(ORG, RATES, start, NOW, seed=42)
    team = defaultdict(Decimal)
    for r in generate(ORG, start, NOW, 42):
        if r.status == 200:
            team[r.key.team] += cost(r, RATES)["total"]
    for name, v in team.items():
        assert t[team_budget_name(name)] == pytest.approx(float(v), abs=1e-6)
    for g in ORG.groups:
        assert t[group_budget_name(g)] == pytest.approx(
            sum(float(team[x]) for x, gg in ORG.teams.items() if gg == g), abs=1e-6)
    assert t[org_budget_name("TD")] == pytest.approx(float(sum(team.values())), abs=1e-6)
    assert len(t) == 21


def test_sync_subtracts_counters_and_closes_the_gap():
    start = NOW - timedelta(hours=6)
    target = bs.history_targets(ORG, RATES, start, NOW, seed=42)
    some = team_budget_name("Fraud Strategy")
    counters = {n: 0.0 for n in target}
    counters[some] = target[some] + 5              # already above target: nothing sent for that team
    rows, sent, _ = run(counters, start=start)
    markers = defaultdict(Decimal)
    for r in filter(is_marker, sent):
        markers[r.key.team] += cost(r, RATES)["total"]
    assert "Fraud Strategy" not in markers
    after = {r["name"]: r for r in rows}
    last_refresh = {r.key.team: float(cost(r, RATES)["total"]) for r in sent if not is_marker(r)}
    for team in ORG.teams:
        name = team_budget_name(team)
        if team == "Fraud Strategy":
            assert after[name]["after"] > target[name]
            continue
        # counter after the markers (before the closing refresh request) == generated + recorded live spend
        assert after[name]["after"] - last_refresh[team] == pytest.approx(after[name]["target"], rel=1e-4, abs=0.01)
        assert after[name]["target"] > target[name]          # the refresh before reading is live spend
    assert {r["name"] for r in rows if r["kind"] in ("org", "group", "team")} == set(target)


def test_sync_sends_marker_ua():
    rows, sent, _ = run({}, start=NOW - timedelta(hours=3))
    markers = [r for r in sent if is_marker(r)]
    assert markers
    for r in markers:
        assert r.user_agent == "td-budget-sync/1.0"
        assert r.route.route.endswith("/td-budget-sync")
        assert any(r.route.route == m.route + "/td-budget-sync" for m in MODELS.values())
        assert r.route.model in {MODELS["bedrock-opus"].model, MODELS["azure-gpt"].model}
        assert r.status == 200 and r.input_tokens <= 250_000 and r.cache_read_tokens == 0
        assert r.key.team and r.key in ORG.keys
    assert client_for_ua("td-budget-sync/1.0") == "Other"
    # the default sender posts to the marker path, so the span's url.path carries the marker
    from tdlib.live import headers, url_for
    assert url_for(markers[0], "http://x").startswith("http://x" + markers[0].route.route + "/")
    assert headers(markers[0])["User-Agent"] == "td-budget-sync/1.0"


def test_sync_route_mix_follows_team_history():
    t0 = NOW - timedelta(hours=8)
    mix = bs.route_mix(ORG, RATES, t0, NOW, seed=42)
    _, sent, _ = run({}, start=t0)
    sent = [r for r in sent if is_marker(r)]
    shares = []
    for team in ORG.teams:
        rs = [r for r in sent if r.key.team == team]
        opus = sum(1 for r in rs if r.route.model == MODELS["bedrock-opus"].model)
        assert opus + sum(1 for r in rs if r.route.model == MODELS["azure-gpt"].model) == len(rs)
        share = mix[team].get("bedrock-opus", 0) / sum(mix[team].values())
        nkeys = len({r.key.id for r in rs})              # split per key now: rounding per key
        assert abs(opus - share * len(rs)) <= max(1, nkeys)  # Opus requests in the team's Opus spend share
        shares.append(share)
    assert min(shares) < 0.5 < max(shares)               # the mix really differs between teams


def test_sync_caps_requests_per_team():
    gaps = {"Fraud Strategy": 50_000.0, "Retail Deposits": 0.30, "Mortgage Lending": 12.0}
    mix = {t: {"bedrock-opus": 1.0, "azure-gpt": 1.0} for t in gaps}
    reqs = bs.plan_requests(ORG, RATES, gaps, mix, NOW)
    per = defaultdict(list)
    for r in reqs:
        per[r.key.team].append(r)
    assert len(per["Fraud Strategy"]) == bs.MAX_REQUESTS_PER_TEAM == 20
    assert "Retail Deposits" not in per                   # below MIN_GAP: not worth a request
    assert 1 <= len(per["Mortgage Lending"]) < 20
    for team, rs in per.items():
        assert all(r.input_tokens <= bs.MAX_INPUT_TOKENS == 250_000 for r in rs)
        assert float(sum(cost(r, RATES)["total"] for r in rs)) == pytest.approx(gaps[team], rel=1e-4, abs=0.01)


def test_sync_purges_only_marker_spans():
    rows, sent, ch = run({}, start=NOW - timedelta(hours=2))
    assert len(ch.purges) == 1
    pred = ch.purges[0]["predicate"]
    assert pred == bs.sync_span_predicate()
    assert "td.demo.local" in pred and "/td-budget-sync/" in pred and "url.path" in pred
    assert pred != td_span_predicate()


def test_purge_with_marker_predicate_freezes_only_marker_spans():
    ch = FakeHTTPCH()
    ch.purge_td(now=PURGE_NOW, log=lambda m: None, predicate=bs.sync_span_predicate(), label="budget-sync")
    (freeze,) = [q for q in ch.sql if q.startswith(f"INSERT INTO td_purge_backup.td_span_ids__{STAMP}")]
    assert bs.sync_span_predicate() in freeze and td_span_predicate() not in freeze
    window = [q for q in ch.sql if "AS n, toString(min(Timestamp))" in q]
    assert window and all(bs.sync_span_predicate() in q for q in window)
    # still backed up before any delete
    deletes = [i for i, q in enumerate(ch.sql) if q.startswith("ALTER TABLE") and " DELETE " in q]
    backups = [i for i, q in enumerate(ch.sql) if q.startswith("INSERT INTO td_purge_backup.agw_cost_model_5m")]
    assert backups and max(backups) < min(deletes)


def test_sync_dry_run_sends_nothing():
    sent = []
    rows = bs.run_sync(FakeKube(), ORG, rates=RATES, seed=42, now=NOW, start=NOW - timedelta(hours=2),
                       sender=lambda r, b: sent.append(r) or 200, read_counters=lambda: {},
                       open_clickhouse=None, log=lambda m: None, dry_run=True)
    assert not sent and rows


def _pb_varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _pb_len(field, payload):
    return _pb_varint(field << 3 | 2) + _pb_varint(len(payload)) + payload


def test_decode_list_budgets():
    subject = _pb_len(1, b"team") + _pb_len(2, b"Fraud Strategy")
    limit = _pb_varint(1 << 3) + _pb_varint(1) + _pb_varint(2 << 3) + _pb_varint(3000)
    entry = (_pb_len(1, b"team-fraud-strategy-monthly-usd") + _pb_len(2, subject) + _pb_len(3, limit)
             + _pb_len(4, _pb_varint(1 << 3) + _pb_varint(3)) + _pb_varint(5 << 3) + _pb_varint(1)
             + _pb_varint(6 << 3 | 1) + struct.pack("<d", 1234.5))
    no_spend = _pb_len(1, b"td-org-monthly-usd")
    budget = _pb_len(1, b"td-budgets") + _pb_len(2, b"td-demo") + _pb_len(3, b"omni-viper") + \
        _pb_len(4, entry) + _pb_len(4, no_spend)
    msg = _pb_len(1, budget)
    frame = b"\x00" + struct.pack(">I", len(msg)) + msg + b"\x80" + struct.pack(">I", 16) + b"grpc-status: 0\r\n"
    (b,) = bs.decode_list_budgets(frame)
    assert (b["name"], b["namespace"]) == ("td-budgets", "td-demo")
    e1, e2 = b["entries"]
    assert e1 == {"name": "team-fraud-strategy-monthly-usd", "subject": {"team": "Fraud Strategy"},
                  "limit": 3000, "spent": 1234.5}
    assert e2["name"] == "td-org-monthly-usd" and e2["spent"] == 0.0


def test_sync_refreshes_budget_page_before_and_after():
    """One ordinary (non-marker) request per team before reading the counters and after the markers, so the
    Budgets page (latest outcome span) is current: after a TD purge it reads 0 although the counters are not."""
    start = NOW - timedelta(hours=4)
    target = bs.history_targets(ORG, RATES, start, NOW, seed=42)
    real = {n: v * 0.5 for n, v in target.items()}               # the gateway's true counters
    rows, sent, _ = run(real, start=start, stale={n: 0.0 for n in target})
    teams = len(ORG.teams)
    first, last = sent[:teams], sent[-teams:]
    for batch in (first, last):
        assert not any(is_marker(r) for r in batch)
        assert sorted(r.key.team for r in batch) == sorted(ORG.teams)
        assert all(r.status == 200 and client_for_ua(r.user_agent) != "Other" for r in batch)
    assert all(is_marker(r) for r in sent[teams:-teams])
    # gaps came from the true counters, not the stale zeros: nothing is charged twice
    marker_usd = float(sum(cost(r, RATES)["total"] for r in sent if is_marker(r)))
    # refresh spend is live spend (recorded, in both target and counter), so markers close exactly the
    # generated half that the counters lack; at most MIN_GAP per team is skipped
    half = target[org_budget_name("TD")] * 0.5
    assert half - len(ORG.teams) * bs.MIN_GAP <= marker_usd <= half + 0.01


def test_sync_fails_when_budget_page_never_catches_up():
    sent = []
    with pytest.raises(bs.SyncError):
        bs.run_sync(FakeKube(), ORG, rates=RATES, seed=42, now=NOW, start=NOW - timedelta(hours=2),
                    sender=lambda r, b: sent.append(r) or 200, read_counters=lambda: {},
                    open_clickhouse=None, log=lambda m: None, sleep=lambda s: None)
    assert not any(is_marker(r) for r in sent)                    # no markers on unconfirmed counters


def test_sync_is_idempotent():
    """Second run in a row: only the refresh requests' own (live, recorded) spend changes; ~0 marker spend."""
    start = NOW - timedelta(hours=5)
    sent = []
    ch = FakeCH(sent)
    base = {}
    run(base, start=start, sent=sent, ch=ch)
    first = len([r for r in sent if is_marker(r)])
    assert first > 0
    n = len(sent)
    rows, _, _ = run(base, start=start, sent=sent, ch=ch)
    second = [r for r in sent[n:] if is_marker(r)]
    assert second == []
    target = bs.history_targets(ORG, RATES, start, NOW, seed=42)
    live = ch.td_live_spend_by_team(start)
    for team in ORG.teams:
        name = team_budget_name(team)
        row = next(r for r in rows if r["name"] == name)
        assert row["target"] == pytest.approx(target[name] + live[team], abs=0.01)
        assert row["after"] == pytest.approx(row["target"], abs=2.0)


def test_sync_target_adds_live_spend_and_ends_history_at_backfill():
    start = NOW - timedelta(hours=6)
    end = NOW - timedelta(hours=2)
    sent = []
    ch = FakeCH(sent, history_end=end, live_extra={"Fraud Strategy": 40.0})
    rows, _, _ = run({}, start=start, sent=sent, ch=ch)
    gen = bs.history_targets(ORG, RATES, start, end, seed=42)
    first = sent[:len(ORG.teams)]                      # the refresh before reading: recorded live spend
    live = defaultdict(float, {"Fraud Strategy": 40.0})
    for r in first:
        live[r.key.team] += float(cost(r, RATES)["total"])
    name = team_budget_name("Fraud Strategy")
    row = next(r for r in rows if r["name"] == name)
    assert row["target"] == pytest.approx(gen[name] + live["Fraud Strategy"], abs=0.01)
    org = next(r for r in rows if r["name"] == org_budget_name("TD"))
    assert org["target"] == pytest.approx(gen[org_budget_name("TD")] + sum(live.values()), abs=0.01)


def test_sync_skips_gaps_under_one_dollar():
    assert bs.MIN_GAP == 1.0
    reqs = bs.plan_requests(ORG, RATES, {"Fraud Strategy": 0.99, "Retail Deposits": 1.5},
                            {"Fraud Strategy": {"bedrock-opus": 1.0}, "Retail Deposits": {"azure-gpt": 1.0}}, NOW)
    assert {r.key.team for r in reqs} == {"Retail Deposits"}


def test_sync_lines_up_project_and_user_counters():
    """Gaps go out per key, so project and user counters reach their generated targets too."""
    start = NOW - timedelta(hours=6)
    rows, sent, _ = run({}, start=start)
    after = {r["name"]: r for r in rows}
    refresh = defaultdict(float)
    for r in sent:
        if not is_marker(r):
            refresh[project_budget_name(r.key.project)] += float(cost(r, RATES)["total"])
            refresh[user_budget_name(service_identity(r.key))] += float(cost(r, RATES)["total"])
    projects = [n for n, r in after.items() if r["kind"] == "proj"]
    assert len(projects) == 12
    for n in projects:
        # counter minus the closing refresh request(s) == target (generated + live)
        assert after[n]["after"] - refresh[n] / 2 == pytest.approx(after[n]["target"], rel=0.02, abs=1.0)


def test_key_gaps_split_by_key_share():
    keys = [k for k in ORG.keys if k.team == "Fraud Strategy"]
    per_key = {k.id: float(i + 1) for i, k in enumerate(keys)}
    g = bs.key_gaps(ORG, {"Fraud Strategy": 100.0}, per_key)
    assert sum(g.values()) == pytest.approx(100.0)
    tot = sum(per_key.values())
    for k in keys:
        assert g[k.id] == pytest.approx(100.0 * per_key[k.id] / tot)
