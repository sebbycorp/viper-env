"""Stdlib ClickHouse HTTP client for the TD backfill and the safe TD history purge.

Purge safety model (Task 7 ruling). Rollup tables (`agw_*_5m`, `agentgateway_*_minute_state`)
cannot be filtered by TD, because real and TD spans share per-model rows. So `purge_td`:
  1. snapshots every rollup it will touch, for the affected time range, into `td_purge_backup`;
  2. records the real totals in that range (rollup minus the TD contribution);
  3. deletes the TD rows of the row-level span tables (`agw_spans_typed`, `agw_chat_spans`);
  4. deletes the affected rollup range and re-inserts it from the cleaned source, using the
     view's own `as_select` (read from `system.tables`);
  5. verifies that the real totals are unchanged, and only then
  6. deletes the TD source rows from `otel_traces_json` (`td_span_predicate()` only).
Tables outside `platformdb.agw*` / `agentgateway_*` are never written.
"""
from __future__ import annotations

import base64
import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Iterable

from .spans import td_span_predicate

DB = "platformdb"
SPANS_TABLE = f"{DB}.otel_traces_json"
BACKUP_DB = "td_purge_backup"
ROLLUP_RE = re.compile(r"^(agw_\w+_5m|agentgateway_\w+_minute_state)$")
ROW_TABLE_RE = re.compile(r"^agw_\w+$")
TD_SOURCES = ("otel_traces_json", "agw_spans_typed")    # tables TD spans land in (directly or via MV)
SUMMABLE = {"count", "sum", "countIf", "sumIf"}        # aggregate states whose totals subtract cleanly
LAG = timedelta(minutes=2)                             # leave the live edge alone: spans arrive late
SYNC = {"mutations_sync": "2"}


class ClickHouseError(RuntimeError):
    pass


class PurgeVerificationError(ClickHouseError):
    pass


@dataclass
class Rollup:
    mv: str
    target: str
    source: str
    as_select: str
    bucket: str                       # time column in the rollup (e.g. bucket)
    src_time: str                     # time column in the source (e.g. Timestamp)
    columns: list[tuple[str, str]]    # MV output columns, in select order


def _q(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _dt(s: str) -> datetime:
    """'YYYY-MM-DD hh:mm:ss[.fff…]' (UTC) -> aware datetime (sub-second dropped)."""
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _sql_ts(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _floor(d: datetime, minutes: int) -> datetime:
    d = d.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return d - timedelta(minutes=d.minute % minutes)


class ClickHouse:
    def __init__(self, url: str, user: str, password: str, timeout: float = 1800.0):
        self.url = url.rstrip("/") + "/"
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.timeout = timeout

    # ------------------------------------------------------------ plumbing
    def query(self, sql: str, data: bytes | None = None, settings: dict | None = None) -> str:
        """Run one statement. Without `data`, the SQL is the POST body; with it, the SQL goes in the URL."""
        params = dict(settings or {})
        if data is None:
            body, headers = sql.encode(), {}
        else:
            params["query"] = sql
            body, headers = data, {"Content-Encoding": "gzip"}
        url = self.url + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, data=body, method="POST", headers={"Authorization": self.auth, **headers})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            raise ClickHouseError(f"{e.code}: {e.read().decode(errors='replace').strip()[:2000]}") from None

    def json_rows(self, sql: str) -> list[dict]:
        out = self.query(sql + " FORMAT JSONEachRow", settings={
            "output_format_json_quote_64bit_integers": "1", "output_format_json_quote_decimals": "1"})
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    # ------------------------------------------------------------ insert
    def insert_rows(self, table: str, rows: Iterable[dict], batch: int = 20_000,
                    progress: Callable[[int], None] | None = None) -> int:
        """Stream `rows` as gzip JSONEachRow batches. Returns the number of rows sent."""
        buf: list[str] = []
        total = 0
        for r in rows:
            buf.append(json.dumps(r, separators=(",", ":"), default=str))
            if len(buf) >= batch:
                self._insert_batch(table, buf)
                total += len(buf)
                buf = []
                if progress:
                    progress(total)
        if buf:
            self._insert_batch(table, buf)
            total += len(buf)
            if progress:
                progress(total)
        return total

    def _insert_batch(self, table: str, lines: list[str], attempts: int = 8) -> None:
        body = gzip.compress(("\n".join(lines) + "\n").encode(), compresslevel=1)
        sql = f"INSERT INTO {table} FORMAT JSONEachRow"
        for i in range(attempts):
            t0 = time.monotonic()
            try:
                self.query(sql, data=body, settings={"async_insert": "0"})
                took = time.monotonic() - t0
                if took > 20:                  # ClickHouse is struggling: give it room, don't fail
                    time.sleep(min(60.0, took / 2))
                return
            except (ClickHouseError, OSError) as e:
                if i == attempts - 1:
                    raise
                if self._batch_landed(table, lines):
                    return                     # the insert went through; only the response was lost
                wait = min(120, 5 * 2 ** i)
                print(f"[td] insert retry {i + 1}/{attempts - 1} in {wait}s: {str(e)[:200]}", flush=True)
                time.sleep(wait)

    def _batch_landed(self, table: str, lines: list[str]) -> bool:
        try:
            first, last = json.loads(lines[0]), json.loads(lines[-1])
            ids = {(first["TraceId"], first["SpanId"]), (last["TraceId"], last["SpanId"])}
        except (KeyError, ValueError):
            return False
        try:
            tuples = ", ".join(f"({_q(t)}, {_q(s)})" for t, s in ids)
            n = self.query(f"SELECT count() FROM {table} WHERE (TraceId, SpanId) IN ({tuples}) FORMAT TSV")
            return int(n.strip() or 0) == len(ids)
        except (ClickHouseError, OSError, ValueError):
            return False

    # ------------------------------------------------------------ TD reads
    def count_td(self) -> int:
        return int(self.query(f"SELECT count() FROM {SPANS_TABLE} WHERE {td_span_predicate()} FORMAT TSV").strip() or 0)

    def td_window(self) -> tuple[int, str, str]:
        (r,) = self.json_rows(f"SELECT count() AS n, toString(min(Timestamp)) AS lo, toString(max(Timestamp)) AS hi "
                              f"FROM {SPANS_TABLE} WHERE {td_span_predicate()}")
        return int(r["n"]), r["lo"], r["hi"]

    def cluster_name(self, default: str) -> str:
        """`cluster_name` of the most recent real (non-TD) agentgateway-proxy span, else `default`."""
        rows = self.json_rows(
            f"SELECT toString(ResourceAttributes.cluster_name) AS c FROM {SPANS_TABLE} "
            f"WHERE ServiceName = 'agentgateway-proxy' AND Timestamp > now() - INTERVAL 30 DAY "
            f"AND NOT {td_span_predicate()} AND c != '' ORDER BY Timestamp DESC LIMIT 1")
        return rows[0]["c"] if rows else default

    # ------------------------------------------------------------ view discovery
    def _columns(self, table: str) -> list[tuple[str, str]]:
        return [(r["name"], r["type"]) for r in self.json_rows(
            f"SELECT name, type FROM system.columns WHERE database = '{DB}' AND table = '{table}' ORDER BY position")]

    def views(self) -> tuple[list[Rollup], list[str]]:
        """(rollups fed by TD-carrying tables, row-level agw tables fed by otel_traces_json)."""
        rollups, row_tables = [], []
        for r in self.json_rows(f"SELECT name, create_table_query, as_select FROM system.tables "
                                f"WHERE database = '{DB}' AND engine = 'MaterializedView' ORDER BY name"):
            m_to = re.search(rf"\bTO {DB}\.(\w+)", r["create_table_query"])
            m_from = re.search(rf"\bFROM {DB}\.(\w+)", r["as_select"])
            if not m_to or not m_from or m_from.group(1) not in TD_SOURCES:
                continue
            target, source = m_to.group(1), m_from.group(1)
            if ROLLUP_RE.match(target):
                m_t = re.search(r"toStartOf\w*\((\w+)(?:,\s*toInterval(Second|Minute)\((\d+)\))?\)\s+AS\s+(\w+)",
                                r["as_select"])
                if not m_t:
                    raise ClickHouseError(f"{r['name']}: cannot find the time bucket in its select")
                unit, n = m_t.group(2), m_t.group(3)
                if n is not None and (int(n) * (60 if unit == "Minute" else 1)) not in (60, 300):
                    raise ClickHouseError(f"{r['name']}: unsupported bucket interval {n} {unit}")
                rollups.append(Rollup(r["name"], target, source, r["as_select"], m_t.group(4), m_t.group(1),
                                      self._columns(r["name"])))
            elif source == "otel_traces_json" and ROW_TABLE_RE.match(target):
                cols = {c for c, _ in self._columns(target)}
                if {"TraceId", "SpanId", "Timestamp"} <= cols:
                    row_tables.append(target)
        return rollups, row_tables

    # ------------------------------------------------------------ rollup summaries
    @staticmethod
    def _summary_select(ru: Rollup, from_expr: str, lo: datetime, hi: datetime) -> tuple[str, list[str], list[str]]:
        # Aliases k<i>/v<i> never shadow a column name (ClickHouse substitutes aliases into aggregates).
        keys, vals, exprs = [], [], []
        for name, typ in ru.columns:
            if name == ru.bucket:
                continue
            m = re.match(r"(Simple)?AggregateFunction\((\w+)", typ)
            if not m:
                keys.append(f"k{len(keys)}")
                exprs.append(f"toString(`{name}`) AS {keys[-1]}")
            elif m.group(2) in SUMMABLE:
                vals.append(f"v{len(vals)}")
                fn = m.group(2) if m.group(1) else m.group(2) + "Merge"
                exprs.append(f"toString({fn}(`{name}`)) AS {vals[-1]}")
        sql = (f"SELECT {', '.join(exprs)} FROM {from_expr} AS src "
               f"WHERE `{ru.bucket}` >= toDateTime({_q(_sql_ts(lo))}, 'UTC') "
               f"AND `{ru.bucket}` < toDateTime({_q(_sql_ts(hi))}, 'UTC')"
               + (f" GROUP BY {', '.join(keys)}" if keys else ""))
        return sql, keys, vals

    def _summary(self, ru: Rollup, from_expr: str, lo: datetime, hi: datetime) -> dict[tuple, tuple]:
        sql, keys, vals = self._summary_select(ru, from_expr, lo, hi)
        return {tuple(r[k] for k in keys): tuple(Decimal(r[v]) for v in vals) for r in self.json_rows(sql)}

    def _view_over(self, ru: Rollup, where: str) -> str:
        """The view's own select, reading only source rows matching `where`, as a FROM expression."""
        sel, n = re.subn(rf"\bFROM {DB}\.{ru.source}\b",
                         f"FROM (SELECT * FROM {DB}.{ru.source} WHERE {where})", ru.as_select, count=1)
        if n != 1:
            raise ClickHouseError(f"{ru.mv}: source {ru.source} not found in its select")
        return sel

    def real_totals(self, ru: Rollup, lo: datetime, hi: datetime, td_filter: dict[str, str] | None) -> dict:
        """Per-key summable totals of rollup `ru` in [lo, hi), minus the TD contribution when given."""
        tot = self._summary(ru, f"{DB}.{ru.target}", lo, hi)
        if td_filter:
            rng = self._time_range(ru.src_time, lo, hi)
            td = self._summary(ru, f"({self._view_over(ru, f'{rng} AND {td_filter[ru.source]}')})", lo, hi)
            for k, v in td.items():
                base = tot.get(k, tuple(Decimal(0) for _ in v))
                tot[k] = tuple(a - b for a, b in zip(base, v))
        return {k: v for k, v in tot.items() if any(x != 0 for x in v)}

    @staticmethod
    def _time_range(col: str, lo: datetime, hi: datetime) -> str:
        return (f"{col} >= toDateTime64({_q(_sql_ts(lo))}, 9, 'UTC') "
                f"AND {col} < toDateTime64({_q(_sql_ts(hi))}, 9, 'UTC')")

    # ------------------------------------------------------------ purge
    def purge_td(self, now: datetime | None = None, log: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep) -> dict:
        n, lo_s, hi_s = self.td_window()
        if n == 0:
            log("purge: no TD spans in ClickHouse; nothing to do")
            return {"td_rows": 0}
        lo, hi = _dt(lo_s), _dt(hi_s)
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        upper = _floor(now - LAG, 5)
        if hi >= upper:                       # TD spans at the live edge: let their bucket close first
            wait = ((_floor(hi, 5) + timedelta(minutes=5) + LAG) - now).total_seconds()
            log(f"purge: newest TD span {hi_s[:19]} is at the live edge; waiting {wait:.0f}s for its bucket to close")
            sleep(max(0.0, wait))
            now = now + timedelta(seconds=max(0.0, wait))
            upper = _floor(now - LAG, 5)
        start = lo.replace(minute=0, second=0, microsecond=0)
        stamp = now.strftime("%Y%m%dT%H%M%S")
        pred = td_span_predicate()
        log(f"purge: {n} TD spans {lo_s[:19]} .. {hi_s[:19]} UTC; rollup range [{_sql_ts(start)}, {_sql_ts(upper)})")

        rollups, row_tables = self.views()

        # 1. TD span ids (our own table) + rollup snapshots, before any mutation.
        self.query(f"CREATE DATABASE IF NOT EXISTS {BACKUP_DB}")
        ids = f"{BACKUP_DB}.td_span_ids__{stamp}"
        self.query(f"CREATE TABLE IF NOT EXISTS {ids} (TraceId String, SpanId String) "
                   f"ENGINE = MergeTree ORDER BY (TraceId, SpanId)")
        self.query(f"INSERT INTO {ids} SELECT TraceId, SpanId FROM {SPANS_TABLE} WHERE {pred}")
        td_ids = f"(TraceId, SpanId) IN (SELECT TraceId, SpanId FROM {ids})"
        td_filter = {"otel_traces_json": pred, "agw_spans_typed": td_ids}
        brange = (f"{{b}} >= toDateTime({_q(_sql_ts(start))}, 'UTC') "
                  f"AND {{b}} < toDateTime({_q(_sql_ts(upper))}, 'UTC')")
        backups = []
        for ru in rollups:
            bk = f"{BACKUP_DB}.{ru.target}__{stamp}"
            self.query(f"CREATE TABLE IF NOT EXISTS {bk} AS {DB}.{ru.target}")
            self.query(f"INSERT INTO {bk} SELECT * FROM {DB}.{ru.target} WHERE {brange.format(b=ru.bucket)}")
            backups.append((ru, bk))
        log(f"purge: snapshot of {len(backups)} rollup tables in {BACKUP_DB}.*__{stamp}")

        # 2. Real totals before (rollup - TD contribution).
        before = {ru.target: self.real_totals(ru, start, upper, td_filter) for ru in rollups}

        # 3. Row-level span tables: TD rows only.
        for t in row_tables:
            self.query(f"ALTER TABLE {DB}.{t} DELETE WHERE Timestamp >= toDateTime64({_q(_sql_ts(start))}, 9, 'UTC') "
                       f"AND {td_ids}", settings=SYNC)
        log(f"purge: TD rows deleted from {', '.join(row_tables) or '(none)'}")

        # 4. Rollups: drop the range, re-insert from the cleaned source via the view's own select.
        for ru in rollups:
            self.query(f"ALTER TABLE {DB}.{ru.target} DELETE WHERE {brange.format(b=ru.bucket)}", settings=SYNC)
            where = f"{self._time_range(ru.src_time, start, upper)} AND NOT {td_filter[ru.source]}"
            cols = ", ".join(f"`{c}`" for c, _ in ru.columns)
            self.query(f"INSERT INTO {DB}.{ru.target} ({cols}) SELECT {cols} FROM ({self._view_over(ru, where)})")
        log(f"purge: recomputed {', '.join(ru.target for ru in rollups) or '(none)'}")

        # 5. Verify real totals are unchanged.
        diffs = []
        for ru in rollups:
            after = self.real_totals(ru, start, upper, None)
            if after != before[ru.target]:
                keys = set(after) ^ set(before[ru.target]) | {k for k in after if after.get(k) != before[ru.target].get(k)}
                diffs.append(f"{ru.target}: {len(keys)} keys differ, e.g. "
                             + "; ".join(f"{k}: before={before[ru.target].get(k)} after={after.get(k)}"
                                         for k in sorted(keys)[:3]))
        restore = [f"ALTER TABLE {DB}.{ru.target} DELETE WHERE {brange.format(b=ru.bucket)} SETTINGS mutations_sync=2; "
                   f"INSERT INTO {DB}.{ru.target} SELECT * FROM {bk};" for ru, bk in backups]
        if diffs:
            raise PurgeVerificationError(
                "real totals changed; TD source rows were NOT deleted. Restore the rollups with:\n  "
                + "\n  ".join(restore) + "\nDifferences:\n  " + "\n  ".join(diffs))
        log(f"purge: verified real totals identical in {len(rollups)} rollup tables "
            f"({sum(len(v) for v in before.values())} real keys compared)")

        # 6. TD source rows (predicate only).
        self.query(f"ALTER TABLE {SPANS_TABLE} DELETE WHERE Timestamp >= toDateTime64({_q(_sql_ts(start))}, 9, 'UTC') "
                   f"AND {pred} AND {td_ids}", settings=SYNC)
        left = self.count_td()
        log(f"purge: TD source rows deleted from {SPANS_TABLE}; {left} TD spans remain")
        log(f"purge: backup kept in {BACKUP_DB} (tables *__{stamp}). To restore the rollups:")
        for s in restore:
            log("  " + s)
        log(f"purge: to drop every backup: DROP DATABASE {BACKUP_DB}")
        return {"td_rows": n, "start": _sql_ts(start), "upper": _sql_ts(upper), "stamp": stamp,
                "rollups": [ru.target for ru in rollups], "row_tables": row_tables,
                "real_keys": {k: len(v) for k, v in before.items()}, "remaining": left}
