"""`live` writes TD spans straight into ClickHouse (no HTTP to the gateway) and nudges the budgets."""
import contextlib
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from tdlib import live
from tdlib.clickhouse import SPANS_TABLE
from tdlib.manifests import BUDGET, HOSTNAME, NAMESPACE, plan_budgets
from tdlib.org import build_org
from tdlib.pricing import load_rates
from tdlib.spans import td_span_predicate
from tdlib.usage import TORONTO
from pathlib import Path

ORG = build_org()
RATES = load_rates(Path(__file__).resolve().parents[3] / "apps/agentgateway-system/model-costs.yaml")
PEAK = datetime(2026, 10, 21, 10, 30, tzinfo=TORONTO).astimezone(timezone.utc)
SUNDAY_3AM = datetime(2026, 10, 25, 3, 0, tzinfo=TORONTO).astimezone(timezone.utc)
BUDGETS = plan_budgets(ORG, RATES, 42, PEAK)


class FakeClock:
    def __init__(self, start):
        self.now, self.lock, self.slept = start, threading.Lock(), []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += timedelta(seconds=max(0.0, s))


class FakeKube:
    def __init__(self, start):
        self.created = start - timedelta(days=1)

    def run(self, args, check=True, input=None):
        return json.dumps({"metadata": {"name": BUDGET, "namespace": NAMESPACE,
                                        "creationTimestamp": self.created.strftime("%Y-%m-%dT%H:%M:%SZ")},
                           "spec": {"budgets": BUDGETS}})


class FakeCH:
    def __init__(self, fail_inserts=0):
        self.inserts, self.fail = [], fail_inserts

    def cluster_name(self, default):
        return "omni-viper"

    def insert_rows(self, table, rows, batch=20_000, progress=None, attempts=8):
        rows = list(rows)
        if self.fail:
            self.fail -= 1
            raise ConnectionRefusedError("port-forward dropped")
        self.inserts.append((table, rows))
        return len(rows)

    def span_rows(self):
        return [r for _, rows in self.inserts for r in rows if r["SpanName"] != "agw.ai.budgets.outcome"]

    def outcome_rows(self):
        return [r for _, rows in self.inserts for r in rows if r["SpanName"] == "agw.ai.budgets.outcome"]


class Factory:
    """ch_factory: counts opens / closes; each open hands out the shared FakeCH."""

    def __init__(self, ch):
        self.ch, self.entered, self.exited = ch, 0, 0

    @contextlib.contextmanager
    def __call__(self):
        self.entered += 1
        try:
            yield self.ch
        finally:
            self.exited += 1


def run(start, *, duration, ch=None, **kw):
    clock = FakeClock(start)
    ch = ch or FakeCH()
    fac = Factory(ch)
    kw.setdefault("seed", 3)
    stats = live.run_live(FakeKube(start), ORG, duration=duration, ch_factory=fac, clock=clock, sleep=clock.sleep,
                          rates=RATES, log=lambda m: None, **kw)
    return stats, ch, fac, clock


def test_live_inserts_batches_each_interval():
    stats, ch, fac, clock = run(PEAK, duration=60, interval=5.0)
    batches = [rows for _, rows in ch.inserts if rows and rows[0]["SpanName"] != "agw.ai.budgets.outcome"]
    assert 8 <= len(batches) <= 13                     # ~one per 5 s window
    assert all(t == SPANS_TABLE for t, _ in ch.inserts)
    assert stats.sent == len(ch.span_rows()) > 0
    assert fac.entered == fac.exited == 1
    ts = [datetime.strptime(r["Timestamp"][:26], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
          for r in ch.span_rows()]
    assert min(ts) >= PEAK and max(ts) <= PEAK + timedelta(seconds=60)   # never future-dated


def test_live_rows_match_td_predicate():
    _, ch, _, _ = run(PEAK, duration=300)
    rows = ch.span_rows()
    pred = td_span_predicate()
    assert f"'{HOSTNAME}'" in pred
    assert all(r["SpanAttributes"]["server"]["address"] == HOSTNAME for r in rows)
    assert all(r["ResourceAttributes"]["cluster_name"] == "omni-viper" for r in rows)
    dims = [r["SpanAttributes"]["agw"]["llm"]["custom_dimensions"] for r in rows]
    assert all('["organization","TD"]' in d for d in dims)
    clients = {json.loads(d.replace("\n", ""))[i][1] for d in dims for i in range(len(json.loads(d)))
               if json.loads(d)[i][0] == "client"}
    assert clients == {"GitHub Copilot", "VS Code", "Claude Code", "Claude Desktop", "Claude Code (CI)"}


def test_live_refreshes_budget_outcomes():
    _, ch, _, _ = run(PEAK, duration=190)
    outs = ch.outcome_rows()
    assert len(outs) >= 2 * 6                          # >= 2 refreshes of 41 entries / 8 per row
    ts = sorted({r["Timestamp"] for r in outs})
    assert len(ts) >= 2


def test_live_reconnects_clickhouse(monkeypatch):
    """Real ClickHouse insert path with a failing urlopen: one attempt per insert, Link does the backoff."""
    import gzip
    import urllib.parse
    from tdlib import clickhouse as chmod

    fails = {"n": 3}
    inserted, timeouts = [], []
    inner_sleeps = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return b""

    def fake_urlopen(req, timeout=None):
        timeouts.append(timeout)
        if req.data is not None and req.get_header("Content-encoding") == "gzip" and fails["n"] > 0:
            fails["n"] -= 1
            raise ConnectionRefusedError("port-forward dropped")
        if req.data is not None and req.get_header("Content-encoding") == "gzip":
            inserted.extend(gzip.decompress(req.data).decode().splitlines())
        return Resp()

    monkeypatch.setattr(chmod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(chmod.time, "sleep", lambda s: inner_sleeps.append(s))
    clock = FakeClock(PEAK)
    fac = Factory(None)

    @contextlib.contextmanager
    def factory():
        fac.entered += 1
        try:
            yield chmod.ClickHouse("http://127.0.0.1:1", "u", "p", timeout=live.CH_TIMEOUT_S)
        finally:
            fac.exited += 1

    stats = live.run_live(FakeKube(PEAK), ORG, duration=60, ch_factory=factory, clock=clock, sleep=clock.sleep,
                          rates=RATES, seed=3, log=lambda m: None)
    assert not inner_sleeps                              # no inner 8x retry / _batch_landed backoff
    assert fac.entered == fac.exited == 4
    assert [s for s in clock.slept if s in (1, 2, 4)][:3] == [1, 2, 4]
    assert stats.reconnects == 3
    spans = [json.loads(l) for l in inserted]
    assert len([r for r in spans if r["SpanName"] != "agw.ai.budgets.outcome"]) == stats.sent
    assert set(timeouts) == {live.CH_TIMEOUT_S} or live.CH_TIMEOUT_S in set(timeouts)


def test_live_gives_up_after_repeated_kube_failures():
    from tdlib.kube import KubeError

    @contextlib.contextmanager
    def factory():
        raise KubeError("no pod")
        yield
    clock = FakeClock(PEAK)
    with pytest.raises(KubeError, match="giving up"):
        live.run_live(FakeKube(PEAK), ORG, duration=60, ch_factory=factory, clock=clock, sleep=clock.sleep,
                      rates=RATES, log=lambda m: None)


def test_live_ctrl_c_final_budget_refresh():
    clock = FakeClock(PEAK)
    ch = FakeCH()

    def sleep(s):
        if clock() > PEAK + timedelta(seconds=20):
            raise KeyboardInterrupt
        clock.sleep(s)
    live.run_live(FakeKube(PEAK), ORG, duration=600, ch_factory=Factory(ch), clock=clock, sleep=sleep,
                  rates=RATES, seed=8, log=lambda m: None)
    assert ch.outcome_rows()                             # refreshed once on exit though < 60 s had passed


def test_live_backoff_caps_at_30():
    ch = FakeCH(fail_inserts=8)
    _, _, _, clock = run(PEAK, duration=30, ch=ch)
    backoffs = [s for s in clock.slept if s in (1, 2, 4, 8, 16, 30)]
    assert backoffs[:7] == [1, 2, 4, 8, 16, 30, 30]


def test_live_rate_floor():
    stats, ch, _, _ = run(SUNDAY_3AM, duration=600, min_rpm=6.0)
    assert 6.0 * 10 * 0.8 <= stats.sent <= 6.0 * 10 * 2


def test_live_ctrl_c_exits_cleanly():
    clock = FakeClock(PEAK)
    ch = FakeCH()
    fac = Factory(ch)

    def sleep(s):
        if clock() > PEAK + timedelta(seconds=40):
            raise KeyboardInterrupt
        clock.sleep(s)

    stats = live.run_live(FakeKube(PEAK), ORG, duration=600, ch_factory=fac, clock=clock, sleep=sleep,
                          rates=RATES, seed=8, log=lambda m: None)
    assert stats.interrupted and stats.sent > 0 and fac.entered == fac.exited == 1


def test_live_summary_every_30s():
    logs = []
    clock = FakeClock(PEAK)
    live.run_live(FakeKube(PEAK), ORG, duration=120, ch_factory=Factory(FakeCH()), clock=clock, sleep=clock.sleep,
                  rates=RATES, seed=9, log=logs.append)
    summaries = [m for m in logs if m.startswith("live: ") and "sent=" in m]
    assert len(summaries) >= 4 and "spend=$" in summaries[-1]


def test_no_gateway_http_left():
    for name in ("Gateway", "urllib_sender", "send_with_reconnect", "headers", "body", "url_for"):
        assert not hasattr(live, name)


@pytest.mark.parametrize("text,seconds", [("10m", 600), ("90s", 90), ("1h", 3600), ("45", 45), ("1.5m", 90)])
def test_parse_duration(text, seconds):
    assert live.parse_duration(text) == seconds


@pytest.mark.parametrize("cmd", ["live", "sync-budgets"])
def test_cli_requires_context(cmd):
    from test_backfill import load_td
    with pytest.raises(SystemExit) as e:
        load_td().main([cmd])
    assert e.value.code != 0


def test_cli_live_passes_options(monkeypatch):
    from test_backfill import load_td
    td = load_td()
    got = {}

    def fake_run_live(kube, org, **kw):
        got.update(kw, context=kube.context)
        s = live.Stats()
        s.sent = 1
        return s
    monkeypatch.setattr(td.live, "run_live", fake_run_live)
    assert td.main(["live", "--context", "CTX", "--duration", "5m", "--speed", "2", "--min-rpm", "9"]) == 0
    assert got["context"] == "CTX" and got["duration"] == 300 and got["speed"] == 2 and got["min_rpm"] == 9
    assert got["rates"] and callable(got["ch_factory"])


def test_cli_replan_budgets_applies_only_budgets(monkeypatch):
    from test_backfill import load_td
    td = load_td()
    applied = []

    class K:
        def __init__(self, ctx):
            self.context = ctx

        def apply(self, objs):
            applied.extend(objs)
            return ""
    monkeypatch.setattr(td, "Kube", K)
    assert td.main(["replan-budgets", "--context", "CTX"]) == 0
    assert [o["kind"] for o in applied] == ["EnterpriseAgentgatewayBudget"]
    assert len(applied[0]["spec"]["budgets"]) == 41
