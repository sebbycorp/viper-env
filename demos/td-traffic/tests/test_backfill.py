import contextlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tdlib.clickhouse import ClickHouse
from tdlib.spans import td_span_predicate

TD_PY = Path(__file__).resolve().parents[1] / "td.py"


def load_td():
    spec = importlib.util.spec_from_file_location("td_cli_backfill", TD_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCH:
    """Stands in for tdlib.clickhouse.ClickHouse in td.py: records the call order."""

    def __init__(self, td_count=0):
        self.td_count = td_count
        self.events: list[str] = []
        self.rows: list[dict] = []

    def count_td(self):
        self.events.append("count_td")
        return self.td_count

    def purge_td(self, **kw):
        self.events.append("purge_td")
        self.td_count = 0

    def cluster_name(self, default):
        return default

    def insert_rows(self, table, rows, batch=20_000, progress=None):
        for r in rows:
            if "insert" not in self.events:
                self.events.append("insert")
            self.rows.append(r)
        return len(self.rows)


@pytest.fixture
def td(monkeypatch):
    mod = load_td()
    fake = FakeCH()

    @contextlib.contextmanager
    def fake_open(kube):
        yield fake

    monkeypatch.setattr(mod, "open_clickhouse", fake_open)
    # The window tests only need timestamps; skip building full rows.
    monkeypatch.setattr(mod, "to_row", lambda req, rates, template, cluster: {"Timestamp": req.ts})
    mod.fake = fake
    return mod


def test_backfill_refuses_when_history_exists(td, capsys):
    td.fake.td_count = 5
    rc = td.main(["backfill", "--context", "CTX", "--days", "1"])
    assert rc != 0
    assert "--replace" in capsys.readouterr().out
    assert "insert" not in td.fake.events and "purge_td" not in td.fake.events


def test_backfill_replace_purges_first(td):
    td.fake.td_count = 5
    rc = td.main(["backfill", "--context", "CTX", "--days", "1", "--replace"])
    assert rc == 0
    ev = td.fake.events
    assert "purge_td" in ev and "insert" in ev
    assert ev.index("purge_td") < ev.index("insert")


def test_backfill_window_ends_now(td):
    before = datetime.now(timezone.utc)
    rc = td.main(["backfill", "--context", "CTX"])          # default --days (30)
    after = datetime.now(timezone.utc)
    assert rc == 0
    ts = [r["Timestamp"] for r in td.fake.rows]
    assert len(ts) > 400_000                                  # ~560k for 30 days
    assert max(ts) <= after
    assert min(ts) >= before - timedelta(days=30)
    assert min(ts) < before - timedelta(days=29, hours=23)   # really starts 30 days back


def test_backfill_requires_context(td):
    with pytest.raises(SystemExit):
        td.main(["backfill"])


# ---------------------------------------------------------------- ClickHouse purge, fake HTTP

MV_ROWS = [
    {"name": "agw_cost_model_5m_mv",
     "create_table_query": "CREATE MATERIALIZED VIEW platformdb.agw_cost_model_5m_mv TO platformdb.agw_cost_model_5m "
                           "(`bucket` DateTime) AS SELECT ...",
     "as_select": "SELECT toStartOfInterval(Timestamp, toIntervalSecond(300)) AS bucket, ServiceName AS service_name, "
                  "countState() AS request_count, sumState(CostUsd) AS spend_usd FROM platformdb.agw_spans_typed "
                  "GROUP BY bucket, service_name"},
    {"name": "agw_spans_typed_mv",
     "create_table_query": "CREATE MATERIALIZED VIEW platformdb.agw_spans_typed_mv TO platformdb.agw_spans_typed "
                           "(`Timestamp` DateTime64(9)) AS SELECT ...",
     "as_select": "SELECT Timestamp, TraceId, SpanId FROM platformdb.otel_traces_json WHERE ScopeName = 'agentgateway'"},
    {"name": "trace_time_index_mv",
     "create_table_query": "CREATE MATERIALIZED VIEW platformdb.trace_time_index_mv TO platformdb.trace_time_index "
                           "(`TraceId` String) AS SELECT ...",
     "as_select": "SELECT TraceId, min(toDateTime(Timestamp)) AS MinTimestamp FROM platformdb.otel_traces_json "
                  "GROUP BY TraceId"},
]
COLUMNS = {
    "agw_cost_model_5m_mv": [("bucket", "DateTime"), ("service_name", "LowCardinality(String)"),
                             ("request_count", "AggregateFunction(count)"),
                             ("spend_usd", "AggregateFunction(sum, Decimal(38, 18))")],
    "agw_spans_typed": [("Timestamp", "DateTime64(9)"), ("TraceId", "String"), ("SpanId", "String")],
    "agw_cost_model_5m": [("bucket", "DateTime"), ("service_name", "LowCardinality(String)"),
                          ("request_count", "AggregateFunction(count)"),
                          ("spend_usd", "AggregateFunction(sum, Decimal(38, 18))")],
}


class FakeHTTPCH(ClickHouse):
    def __init__(self):
        super().__init__("http://x", "u", "p")
        self.sql: list[str] = []

    def query(self, sql, data=None, settings=None):
        self.sql.append(sql)
        if "FROM system.tables" in sql:
            return "\n".join(json.dumps(r) for r in MV_ROWS)
        if "FROM system.columns" in sql:
            table = sql.split("table = '")[1].split("'")[0]
            return "\n".join(json.dumps({"name": n, "type": t}) for n, t in COLUMNS.get(table, []))
        if "AS n, toString(min(Timestamp))" in sql:
            return json.dumps({"n": "3", "lo": "2026-10-01 10:17:00.000000000", "hi": "2026-10-02 10:00:00.000000000"})
        return ""


def test_purge_backs_up_before_any_delete():
    ch = FakeHTTPCH()
    ch.purge_td(now=datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc), log=lambda m: None)
    s = ch.sql
    deletes = [i for i, q in enumerate(s) if q.startswith("ALTER TABLE") and " DELETE " in q]
    creates = [i for i, q in enumerate(s) if q.startswith("CREATE TABLE IF NOT EXISTS td_purge_backup.agw_cost_model_5m")]
    copies = [i for i, q in enumerate(s) if q.startswith("INSERT INTO td_purge_backup.agw_cost_model_5m")]
    assert deletes and creates and copies
    assert max(creates + copies) < min(deletes)
    assert " AS platformdb.agw_cost_model_5m" in s[creates[0]]
    # source rows: deleted with the TD predicate only
    src = [q for q in s if q.startswith("ALTER TABLE platformdb.otel_traces_json DELETE")]
    assert len(src) == 1 and td_span_predicate() in src[0]
    # rollup: range delete from the hour of the first TD span, then re-insert from the view's select
    roll = [q for q in s if q.startswith("ALTER TABLE platformdb.agw_cost_model_5m DELETE")]
    assert len(roll) == 1 and "2026-10-01 10:00:00" in roll[0]
    reins = [i for i, q in enumerate(s) if q.startswith("INSERT INTO platformdb.agw_cost_model_5m")]
    assert len(reins) == 1 and "FROM (SELECT * FROM platformdb.agw_spans_typed WHERE" in s[reins[0]]
    assert s.index(roll[0]) < reins[0]
    # tables outside agw*/agentgateway_* are left alone
    assert not any("trace_time_index" in q for q in s if q.startswith(("ALTER", "INSERT")))
    assert not any(q.startswith(("DROP", "TRUNCATE")) for q in s)


def test_purge_noop_when_no_td_rows():
    ch = FakeHTTPCH()
    ch.query = lambda sql, data=None, settings=None: (ch.sql.append(sql) or
                                                       json.dumps({"n": "0", "lo": "", "hi": ""}))
    ch.purge_td(log=lambda m: None)
    assert not any(q.startswith(("ALTER", "CREATE", "INSERT")) for q in ch.sql)


def test_insert_rows_batches(monkeypatch):
    ch = ClickHouse("http://x", "u", "p")
    sent = []
    monkeypatch.setattr(ch, "_insert_batch", lambda table, lines: sent.append(len(lines)))
    n = ch.insert_rows("platformdb.otel_traces_json", ({"i": i} for i in range(45)), batch=20)
    assert n == 45 and sent == [20, 20, 5]
