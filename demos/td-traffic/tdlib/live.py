"""Live TD traffic through the gateway (`td.py live`), and the sender `td.py sync-budgets` reuses.

Every request goes to the TD mock via `agentgateway-proxy` with `Host: td.demo.local`, so the
gateway authenticates the virtual key, prices the tokens the mock reports (from the x-td-* headers)
and charges the budgets, exactly like real client traffic. Nothing reaches a real provider.

Scheduling: window by window (WINDOW_S of wall-clock time, Toronto-local rhythm), the requests are
`usage.requests_for_window` over that window, so 2-key users are not double-counted. `speed`
multiplies the rate (the window is generated `speed` times longer and compressed back). Below
`min_rpm`, fill-in requests are sampled with `sample_request` on keys chosen in proportion to
their current rate.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from .kube import KubeError
from .manifests import HOSTNAME
from .org import Org
from .pricing import Rates
from .usage import Request, activity, cost, key_shares, requests_for_window, sample_request

GATEWAY_TARGET = "svc/agentgateway-proxy"
GATEWAY_NS = "agentgateway-system"
GATEWAY_PORT = 8080
WINDOW_S = 15.0                 # wall-clock seconds generated per scheduling window
SUMMARY_S = 30.0                # one-line summary interval
BACKOFF_START, BACKOFF_MAX = 1, 30
SEND_ATTEMPTS = 8               # per request, each after a reconnect
TIMEOUT_S = 30.0
# Connection-level failures: the port-forward (or the pod behind it) went away.
CONNECTION_ERRORS = (ConnectionError, TimeoutError, KubeError)

Sender = Callable[[Request, str], int]

# Short plausible prompts per client; the mock ignores the text, the gateway only sees an OpenAI body.
PROMPTS = {
    "copilot": ["Refactor this function to use async/await and keep the error handling.",
                "Write unit tests for the PaymentScheduler edge cases around month end.",
                "Why does this Spring Boot service time out when the batch size grows?"],
    "vscode": ["Add input validation to the KYC form handler and update the tests.",
               "Explain this Terraform plan diff and flag anything risky.",
               "Convert this pandas pipeline to polars, keeping the column names."],
    "claude-code": ["Run the failing integration tests and fix the flaky retry logic.",
                    "Find every place we parse ISO-8601 dates and make them timezone-aware.",
                    "Migrate the statement renderer from Jinja2 templates to the new component API."],
    "claude-desktop": ["Summarize these meeting notes into action items for the steering committee.",
                       "Draft a plain-language explanation of our model risk validation steps.",
                       "Compare these two vendor proposals and list the open questions."],
    "ci": ["Review this pull request diff for security issues and missing tests.",
           "Fix the lint errors reported by the pipeline and explain each change."],
}


def parse_duration(text: str) -> float:
    """'10m' / '90s' / '1h' / '45' (seconds) -> seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", text or "")
    if not m:
        raise ValueError(f"bad duration {text!r} (use e.g. 90s, 10m, 1h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


# ---------------------------------------------------------------- one request on the wire

def headers(req: Request) -> dict[str, str]:
    return {
        "Host": HOSTNAME,
        "Authorization": f"Bearer {req.key.secret}",
        "User-Agent": req.user_agent,
        "Content-Type": "application/json",
        "x-td-input-tokens": str(req.input_tokens),
        "x-td-output-tokens": str(req.output_tokens),
        "x-td-cached-tokens": str(req.cache_read_tokens),
        "x-td-latency-ms": str(req.latency_ms),
        "x-td-status": str(req.status),
    }


def body(req: Request) -> bytes:
    pool = PROMPTS["ci" if req.key.kind == "team" else req.key.tool]
    text = pool[int(req.ts.timestamp() * 1000) % len(pool)]
    return json.dumps({"model": req.route.model, "messages": [
        {"role": "system", "content": "You are a coding assistant at TD. Be concise."},
        {"role": "user", "content": text}]}).encode()


def url_for(req: Request, base_url: str) -> str:
    """Route path from usage.MODELS (PathPrefix) + the OpenAI chat path, like the backfilled url.path."""
    return f"{base_url}{req.route.route}/v1/chat/completions"


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via an HTTP(S)_PROXY


def urllib_sender(req: Request, base_url: str) -> int:
    """POST one request; returns the HTTP status. Connection-level failures raise ConnectionError."""
    r = urllib.request.Request(url_for(req, base_url), data=body(req), method="POST", headers=headers(req))
    try:
        with _opener.open(r, timeout=TIMEOUT_S) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as e:
        e.read()
        return e.code
    except urllib.error.URLError as e:
        if isinstance(e.reason, OSError):
            raise ConnectionError(str(e.reason)) from None
        raise


# ---------------------------------------------------------------- the port-forward

class Gateway:
    """Port-forward to the proxy, shared by the sender threads; re-opened (with backoff) when it drops."""

    def __init__(self, kube, *, sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print):
        self.kube, self.sleep, self.log = kube, sleep, log
        self.base_url: str | None = None
        self.generation = 0
        self.reconnects = 0
        self.stopped = threading.Event()
        self._cm = None
        self._lock = threading.Lock()
        self._delay = BACKOFF_START

    def open(self) -> None:
        cm = self.kube.port_forward(GATEWAY_TARGET, GATEWAY_NS, GATEWAY_PORT)
        port = cm.__enter__()
        self._cm = cm
        self.base_url = f"http://127.0.0.1:{port}"
        self.generation += 1

    def close(self) -> None:
        cm, self._cm = self._cm, None
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception as e:  # noqa: BLE001 -- a dying port-forward must not mask the real error
                self.log(f"live: closing the port-forward: {e}")

    def __enter__(self) -> "Gateway":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.stopped.set()
        with self._lock:
            self.close()

    def ok(self) -> None:
        self._delay = BACKOFF_START

    def reconnect(self, seen_generation: int, why: str) -> None:
        """Close and re-open the port-forward unless another thread already did since `seen_generation`."""
        with self._lock:
            if self.generation != seen_generation or self.stopped.is_set():
                return
            self.reconnects += 1
            self.close()
            while not self.stopped.is_set():
                delay = self._delay
                self._delay = min(BACKOFF_MAX, delay * 2)
                self.log(f"live: gateway connection lost ({why}); re-opening the port-forward in {delay}s")
                self.sleep(delay)
                try:
                    self.open()
                    self.log(f"live: port-forward re-opened at {self.base_url}")
                    return
                except (KubeError, OSError) as e:
                    why = f"re-open failed: {e}"


# ---------------------------------------------------------------- stats

@dataclass
class Stats:
    sent: int = 0
    statuses: Counter = field(default_factory=Counter)
    spend: Decimal = Decimal(0)
    clients: Counter = field(default_factory=Counter)
    failed: int = 0                 # gave up after SEND_ATTEMPTS connection errors
    connection_errors: int = 0
    reconnects: int = 0
    interrupted: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, req: Request, status: int, rates: dict[tuple[str, str], Rates] | None) -> None:
        from .usage import client_for_ua
        with self._lock:
            self.sent += 1
            self.statuses[status] += 1
            self.clients[client_for_ua(req.user_agent)] += 1
            if status == 200 and rates is not None:
                self.spend += cost(req, rates)["total"]

    def line(self) -> str:
        with self._lock:
            st = " ".join(f"{k}={v}" for k, v in sorted(self.statuses.items())) or "-"
            return (f"sent={self.sent} [{st}] spend=${self.spend:,.2f} reconnects={self.reconnects}"
                    + (f" failed={self.failed}" if self.failed else ""))


def send_with_reconnect(gw: Gateway, sender: Sender, req: Request, stats: Stats,
                        rates: dict[tuple[str, str], Rates] | None) -> int | None:
    """Send one request, re-opening the port-forward on connection errors. None = gave up."""
    for _ in range(SEND_ATTEMPTS):
        if gw.stopped.is_set():
            return None
        gen, base = gw.generation, gw.base_url
        try:
            status = sender(req, base)
        except CONNECTION_ERRORS as e:
            with stats._lock:
                stats.connection_errors += 1
            gw.reconnect(gen, f"{type(e).__name__}: {e}")
            stats.reconnects = gw.reconnects
            continue
        gw.ok()
        stats.record(req, status, rates)
        return status
    with stats._lock:
        stats.failed += 1
    return None


# ---------------------------------------------------------------- scheduling

class Scheduler:
    """Requests per wall-clock window: requests_for_window x speed, floored at min_rpm."""

    def __init__(self, org: Org, rng: random.Random, speed: float, min_rpm: float):
        self.org, self.rng, self.speed, self.min_rpm = org, rng, speed, min_rpm
        self.shares = key_shares(org)            # cached once (amendment)
        self.carry = 0.0

    def window(self, t0: datetime, t1: datetime) -> list[Request]:
        span = t1 - t0
        out: list[Request] = []
        if self.speed > 0:
            for r in requests_for_window(self.org, t0, t0 + span * self.speed, self.rng, shares=self.shares):
                r.ts = t0 + (r.ts - t0) / self.speed
                out.append(r)
        want = self.min_rpm * span.total_seconds() / 60 + self.carry
        floor_n = int(want)
        self.carry = want - floor_n
        fill = floor_n - len(out)
        if fill > 0:
            mid = t0 + span / 2
            keys = self.org.keys
            weights = [activity(k, mid, self.shares[k.id]) for k in keys]
            for k in self.rng.choices(keys, weights=weights, k=fill):
                out.append(sample_request(k, t0 + span * self.rng.random(), self.rng))
        out.sort(key=lambda r: r.ts)
        return out


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_live(kube, org: Org, *, speed: float = 1.0, min_rpm: float = 6.0, concurrency: int = 4,
             duration: float | None = None, sender: Sender | None = None,
             rates: dict[tuple[str, str], Rates] | None = None, seed: int | None = None,
             clock: Callable[[], datetime] = _utcnow, sleep: Callable[[float], None] = time.sleep,
             log: Callable[[str], None] = print, window_s: float = WINDOW_S,
             summary_s: float = SUMMARY_S) -> Stats:
    """Send live TD traffic until `duration` seconds pass (None = until Ctrl-C). Never raises on
    connection errors; Ctrl-C stops scheduling and closes the port-forward."""
    sender = sender or urllib_sender
    rng = random.Random(seed)
    sched = Scheduler(org, rng, speed, min_rpm)
    stats = Stats()
    slots = threading.BoundedSemaphore(concurrency * 8)     # back-pressure if the gateway slows down
    t_start = clock()
    end = t_start + timedelta(seconds=duration) if duration is not None else None
    next_summary = t_start + timedelta(seconds=summary_s)

    def job(req: Request) -> None:
        try:
            send_with_reconnect(gw, sender, req, stats, rates)
        finally:
            slots.release()

    def summary(now: datetime) -> None:
        log(f"live: {(now - t_start).total_seconds():5.0f}s {stats.line()}")

    gw = Gateway(kube, sleep=sleep, log=log)
    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="td-live")
    log(f"live: sending via port-forward to {GATEWAY_NS}/{GATEWAY_TARGET}:{GATEWAY_PORT} (Host {HOSTNAME}), "
        f"speed x{speed:g}, floor {min_rpm:g} rpm, concurrency {concurrency}, "
        + (f"for {duration:g}s" if duration is not None else "until Ctrl-C"))
    try:
        with gw:
            t = t_start
            while end is None or t < end:
                t1 = t + timedelta(seconds=window_s)
                if end is not None:
                    t1 = min(t1, end)
                for req in sched.window(t, t1):
                    wait = (req.ts - clock()).total_seconds()
                    if wait > 0:
                        sleep(wait)
                    now = clock()
                    if now >= next_summary:
                        summary(now)
                        next_summary += timedelta(seconds=summary_s)
                    slots.acquire()
                    pool.submit(job, req)
                wait = (t1 - clock()).total_seconds()
                if wait > 0:
                    sleep(wait)
                now = clock()
                if now >= next_summary:
                    summary(now)
                    next_summary += timedelta(seconds=summary_s)
                t = t1
            pool.shutdown(wait=True)
    except KeyboardInterrupt:
        stats.interrupted = True
        gw.stopped.set()
        log("live: interrupted; closing the port-forward")
        pool.shutdown(wait=False, cancel_futures=True)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        stats.reconnects = gw.reconnects
    log(f"live: done {stats.line()}")
    return stats
