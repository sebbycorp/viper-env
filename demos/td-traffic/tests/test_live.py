import contextlib
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tdlib import live
from tdlib.org import build_org
from tdlib.usage import CI_USER_AGENTS, MODELS, TORONTO, USER_AGENTS, client_for_ua

ORG = build_org()
SECRETS = {k.secret for k in ORG.keys}
ROUTES = {m.route for m in MODELS.values()}


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start
        self.lock = threading.Lock()
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        with self.lock:
            return self.now

    def sleep(self, s: float) -> None:
        with self.lock:
            self.slept.append(s)
            self.now += timedelta(seconds=max(0.0, s))


class FakeKube:
    """kube.port_forward stand-in: counts how often the port-forward was opened / closed."""

    def __init__(self, port: int = 18080):
        self.port = port
        self.entered = 0
        self.exited = 0
        self.targets: list[tuple] = []

    @contextlib.contextmanager
    def port_forward(self, target, ns, remote, timeout=20.0):
        self.entered += 1
        self.targets.append((target, ns, remote))
        try:
            yield self.port
        finally:
            self.exited += 1


# Weekday 10:30 Toronto (peak) and Sunday 03:00 Toronto (night floor).
PEAK = datetime(2026, 10, 21, 10, 30, tzinfo=TORONTO).astimezone(timezone.utc)
SUNDAY_3AM = datetime(2026, 10, 25, 3, 0, tzinfo=TORONTO).astimezone(timezone.utc)


class Capture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[dict] = []
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with Capture.lock:
            Capture.seen.append({"path": self.path, "headers": dict(self.headers.items()), "body": json.loads(body)})
        data = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def capture_server():
    Capture.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_live_sends_headers(capture_server):
    """The default (urllib) sender, end to end against a local HTTP server standing in for the gateway."""
    kube = FakeKube(port=capture_server)
    clock = FakeClock(PEAK)
    stats = live.run_live(kube, ORG, duration=120, concurrency=2, clock=clock, sleep=clock.sleep, seed=1,
                          log=lambda m: None)
    seen = Capture.seen
    assert len(seen) >= 20 and stats.sent == len(seen)
    assert kube.targets[0] == ("svc/agentgateway-proxy", "agentgateway-system", 8080)
    for s in seen:
        h = {k.lower(): v for k, v in s["headers"].items()}
        assert h["host"] == "td.demo.local"
        assert h["authorization"].startswith("Bearer ") and h["authorization"][7:] in SECRETS
        assert client_for_ua(h["user-agent"]) != "Other"
        for name in ("x-td-input-tokens", "x-td-output-tokens", "x-td-cached-tokens", "x-td-latency-ms",
                     "x-td-status"):
            assert int(h[name]) >= 0
        assert any(s["path"] == r + "/v1/chat/completions" for r in ROUTES)
        model = {m.route + "/v1/chat/completions": m.model for m in MODELS.values()}[s["path"]]
        assert s["body"]["model"] == model and s["body"]["messages"][-1]["content"]


def test_live_headers_mirror_request():
    from tdlib.usage import sample_request
    import random
    rng = random.Random(3)
    for k in ORG.keys[:20]:
        r = sample_request(k, PEAK, rng)
        h = live.headers(r)
        assert h["Host"] == "td.demo.local"
        assert h["Authorization"] == f"Bearer {k.secret}"
        assert h["User-Agent"] == r.user_agent
        assert h["x-td-input-tokens"] == str(r.input_tokens)
        assert h["x-td-output-tokens"] == str(r.output_tokens)
        assert h["x-td-cached-tokens"] == str(r.cache_read_tokens)
        assert h["x-td-latency-ms"] == str(r.latency_ms)
        assert h["x-td-status"] == str(r.status)


def test_live_rate_floor():
    sent = []
    clock = FakeClock(SUNDAY_3AM)
    live.run_live(FakeKube(), ORG, duration=600, min_rpm=6.0, concurrency=1, clock=clock, sleep=clock.sleep,
                  sender=lambda req, base: sent.append(req) or 200, seed=2, log=lambda m: None)
    assert len(sent) >= 6.0 * 10 * 0.8
    assert len(sent) <= 6.0 * 10 * 2          # the floor is a floor, not a flood


def test_live_uses_all_clients_at_peak():
    sent = []
    clock = FakeClock(PEAK)
    live.run_live(FakeKube(), ORG, duration=300, concurrency=1, clock=clock, sleep=clock.sleep,
                  sender=lambda req, base: sent.append(req) or 200, seed=4, log=lambda m: None)
    clients = {client_for_ua(r.user_agent) for r in sent}
    assert clients == {"GitHub Copilot", "VS Code", "Claude Code", "Claude Desktop", "Claude Code (CI)"}
    known = {ua for uas in USER_AGENTS.values() for ua in uas} | set(CI_USER_AGENTS)
    assert {r.user_agent for r in sent} <= known


def test_live_speed_scales_rate():
    def count(speed):
        sent = []
        clock = FakeClock(PEAK)
        live.run_live(FakeKube(), ORG, duration=900, min_rpm=0, speed=speed, concurrency=1, clock=clock,
                      sleep=clock.sleep, sender=lambda req, base: sent.append(req) or 200, seed=5,
                      log=lambda m: None)
        return len(sent)
    one, three = count(1.0), count(3.0)
    assert 2.4 * one <= three <= 3.6 * one


def test_live_reconnects_after_connection_error():
    kube = FakeKube()
    clock = FakeClock(PEAK)
    calls = {"n": 0}
    ok = []

    def flaky(req, base):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise ConnectionRefusedError("connection refused")
        ok.append(req)
        return 200

    logs = []
    stats = live.run_live(kube, ORG, duration=120, concurrency=1, clock=clock, sleep=clock.sleep, sender=flaky,
                          seed=6, log=logs.append)
    assert kube.entered >= 2 and kube.exited == kube.entered     # re-opened, and closed at the end
    assert len(ok) > 10 and stats.sent == len(ok) and stats.failed == 0
    assert stats.reconnects == 3
    assert [s for s in clock.slept if s in (1, 2, 4)][:3] == [1, 2, 4]   # backoff 1, 2, 4 ...
    assert any("re-open" in m for m in logs)


def test_live_backoff_caps_at_30():
    kube = FakeKube()
    clock = FakeClock(PEAK)
    calls = {"n": 0}

    def flaky(req, base):
        calls["n"] += 1
        if calls["n"] <= 7:
            raise ConnectionResetError("reset")
        return 200

    live.run_live(kube, ORG, duration=30, concurrency=1, clock=clock, sleep=clock.sleep, sender=flaky, seed=7,
                  log=lambda m: None)
    backoffs = [s for s in clock.slept if s in (1, 2, 4, 8, 16, 30)]
    assert backoffs[:7] == [1, 2, 4, 8, 16, 30, 30]


def test_live_ctrl_c_closes_port_forward():
    kube = FakeKube()
    clock = FakeClock(PEAK)

    def sleep(s):                       # Ctrl-C lands in the main thread, usually while it waits
        if clock() > PEAK + timedelta(seconds=40):
            raise KeyboardInterrupt
        clock.sleep(s)

    stats = live.run_live(kube, ORG, duration=600, concurrency=2, clock=clock, sleep=sleep,
                          sender=lambda req, base: 200, seed=8, log=lambda m: None)
    assert stats.interrupted and stats.sent > 0 and kube.entered == kube.exited == 1


def test_live_summary_every_30s():
    logs = []
    clock = FakeClock(PEAK)
    live.run_live(FakeKube(), ORG, duration=120, concurrency=1, clock=clock, sleep=clock.sleep,
                  sender=lambda req, base: 200, seed=9, log=logs.append)
    summaries = [m for m in logs if m.startswith("live: ") and "sent=" in m]
    assert len(summaries) >= 4
    assert "200=" in summaries[-1] and "spend=$" in summaries[-1]


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
    assert td.main(["live", "--context", "CTX", "--duration", "5m", "--speed", "2", "--concurrency", "3"]) == 0
    assert got["context"] == "CTX" and got["duration"] == 300 and got["speed"] == 2 and got["concurrency"] == 3
    assert got["rates"]


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
    assert len(applied[0]["spec"]["budgets"]) == 21
