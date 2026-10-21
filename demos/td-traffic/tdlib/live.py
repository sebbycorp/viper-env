"""Live TD traffic (`td.py live`): fresh spans written straight into ClickHouse, no request to the gateway.

Every `interval` seconds the window `[last, now)` is generated with `Scheduler.window` (usage model x
`speed`, floored at `min_rpm`), converted with `spans.to_row` and inserted in one batch, so the Traces,
Usage and Costs pages keep moving. About every 60 s the Budgets page targets (generated window-to-date spend
plus what `live` has written) are refreshed with zero-cost outcome rows (`budget_sync.write_budget_outcomes`).
A dropped ClickHouse port-forward is re-opened with backoff (1, 2, 4 ... 30 s); rows not yet inserted are
kept and retried. Ctrl-C exits cleanly.
"""
from __future__ import annotations

import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from . import budget_sync
from .clickhouse import SPANS_TABLE, ClickHouseError
from .kube import KubeError
from .org import Org
from .pricing import Rates
from .spans import load_template, to_row
from .usage import Request, activity, cost, key_shares, requests_for_window, sample_request

INTERVAL_S = 5.0                # wall-clock seconds generated per insert
BUDGET_S = 60.0                 # budget outcome refresh interval
SUMMARY_S = 30.0                # one-line summary interval
BACKOFF_START, BACKOFF_MAX = 1, 30
CH_TIMEOUT_S = 30.0            # HTTP timeout for live inserts (the backfill default is 30 min)
MAX_KUBE_FAILURES = 5           # consecutive port-forward failures before giving up
MAX_PENDING = 200_000           # rows kept for retry while ClickHouse is unreachable
# Connection-level failures: the port-forward (or the pod behind it) went away.
CONNECTION_ERRORS = (OSError, KubeError)


def parse_duration(text: str) -> float:
    """'10m' / '90s' / '1h' / '45' (seconds) -> seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", text or "")
    if not m:
        raise ValueError(f"bad duration {text!r} (use e.g. 90s, 10m, 1h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


class Link:
    """ClickHouse over a port-forward (`factory()` is a context manager yielding a ClickHouse client);
    `call` re-opens it, with backoff, when a connection-level error drops it."""

    def __init__(self, factory, *, sleep, log):
        self.factory, self.sleep, self.log = factory, sleep, log
        self.ch = None
        self.reconnects = 0
        self._kube_failures = 0
        self._cm = None
        self._delay = BACKOFF_START

    def open(self) -> None:
        cm = self.factory()
        self.ch = cm.__enter__()
        self._cm = cm

    def close(self) -> None:
        cm, self._cm, self.ch = self._cm, None, None
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception as e:  # noqa: BLE001 -- a dying port-forward must not mask the real error
                self.log(f"live: closing the port-forward: {e}")

    def call(self, fn, once: bool = False):
        why = None
        while True:
            try:
                if self.ch is None:
                    self.open()
                out = fn(self.ch)
                self._delay = BACKOFF_START
                self._kube_failures = 0
                return out
            except CONNECTION_ERRORS as e:
                if once:
                    raise
                why = f"{type(e).__name__}: {e}"
                self._kube_failures = self._kube_failures + 1 if isinstance(e, KubeError) else 0
                if self._kube_failures >= MAX_KUBE_FAILURES:
                    self.close()
                    raise KubeError(f"giving up after {MAX_KUBE_FAILURES} failed port-forwards to ClickHouse "
                                    f"(last: {e}); check the cluster connection and re-run") from None
            self.reconnects += 1
            self.close()
            delay = self._delay
            self._delay = min(BACKOFF_MAX, delay * 2)
            self.log(f"live: ClickHouse connection lost ({why}); re-opening the port-forward in {delay}s")
            self.sleep(delay)

    def __enter__(self) -> "Link":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@dataclass
class Stats:
    sent: int = 0
    statuses: Counter = field(default_factory=Counter)
    spend: Decimal = Decimal(0)
    clients: Counter = field(default_factory=Counter)
    reconnects: int = 0
    interrupted: bool = False

    def record(self, req: Request, rates: dict[tuple[str, str], Rates] | None) -> None:
        from .usage import client_for_ua
        self.sent += 1
        self.statuses[req.status] += 1
        self.clients[client_for_ua(req.user_agent)] += 1
        if req.status == 200 and rates is not None:
            self.spend += cost(req, rates)["total"]

    def line(self) -> str:
        st = " ".join(f"{k}={v}" for k, v in sorted(self.statuses.items())) or "-"
        return f"sent={self.sent} [{st}] spend=${self.spend:,.2f} reconnects={self.reconnects}"


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


def run_live(kube, org: Org, *, speed: float = 1.0, min_rpm: float = 6.0, interval: float = INTERVAL_S,
             duration: float | None = None, ch_factory: Callable | None = None,
             clock: Callable[[], datetime] | None = None, sleep: Callable[[float], None] = time.sleep,
             rates: dict[tuple[str, str], Rates] | None = None, seed: int | None = None, history_seed: int = 42,
             log: Callable[[str], None] = print, budget_s: float = BUDGET_S, summary_s: float = SUMMARY_S) -> Stats:
    """Write live TD spans into ClickHouse until `duration` seconds pass (None = until Ctrl-C).
    `ch_factory()` -> context manager yielding a ClickHouse client; `rates` prices the spans and budgets."""
    assert ch_factory is not None and rates is not None, "run_live needs ch_factory and rates"
    clock = clock or _utcnow
    sched = Scheduler(org, random.Random(seed), speed, min_rpm)
    stats = Stats()
    template = load_template()
    obj = budget_sync.budget_object(kube)           # KubeError here = `demo` has not been run
    budgets, created = obj["spec"]["budgets"], budget_sync.created_at(obj)
    t_start = clock()
    end = t_start + timedelta(seconds=duration) if duration is not None else None
    next_budget = t_start + timedelta(seconds=budget_s)
    next_summary = t_start + timedelta(seconds=summary_s)
    pending: list[dict] = []
    sent_reqs: list[Request] = []
    cluster: list[str] = []
    link = Link(ch_factory, sleep=sleep, log=log)

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        try:
            link.call(lambda ch: ch.insert_rows(SPANS_TABLE, pending, attempts=1))
            pending = []
        except ClickHouseError as e:
            log(f"live: insert failed ({e}); will retry with the next batch")
            pending = pending[-MAX_PENDING:]

    def refresh_budgets(now: datetime, once: bool = False) -> None:
        targets = budget_sync.budget_targets(org, budgets, now, history_seed, rates=rates, live=sent_reqs)
        try:
            link.call(lambda ch: budget_sync.write_budget_outcomes(
                ch, kube, targets, now, created=created, cluster=cluster[0], attempts=1), once=once)
        except ClickHouseError as e:
            log(f"live: budget refresh failed ({e})")

    log(f"live: writing spans straight into ClickHouse every {interval:g}s, speed x{speed:g}, floor {min_rpm:g} rpm, "
        + (f"for {duration:g}s" if duration is not None else "until Ctrl-C"))
    with link:
        try:
            cluster.append(link.call(lambda ch: ch.cluster_name(template["ResourceAttributes"]["cluster_name"])))
            t = t_start
            while end is None or t < end:
                t1 = t + timedelta(seconds=interval)
                if end is not None:
                    t1 = min(t1, end)
                wait = (t1 - clock()).total_seconds()
                if wait > 0:
                    sleep(wait)
                reqs = sched.window(t, t1)
                for r in reqs:
                    stats.record(r, rates)
                    pending.append(to_row(r, rates, template, cluster[0]))
                sent_reqs.extend(r for r in reqs if r.status == 200)
                flush()
                now = clock()
                if now >= next_budget or (end is not None and t1 >= end):
                    refresh_budgets(now)
                    next_budget = now + timedelta(seconds=budget_s)
                if now >= next_summary:
                    stats.reconnects = link.reconnects
                    log(f"live: {(now - t_start).total_seconds():5.0f}s {stats.line()}")
                    next_summary += timedelta(seconds=summary_s)
                t = t1
        except KeyboardInterrupt:
            stats.interrupted = True
            log("live: interrupted; final budget refresh, then closing the port-forward")
            try:                        # one bounded attempt; any error (even a second Ctrl-C) is ignored
                if cluster:
                    refresh_budgets(clock(), once=True)
            except (Exception, KeyboardInterrupt):  # noqa: BLE001
                pass
    stats.reconnects = link.reconnects
    log(f"live: done {stats.line()}")
    return stats
