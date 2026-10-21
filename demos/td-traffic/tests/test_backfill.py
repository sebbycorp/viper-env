import contextlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tdlib.clickhouse import ClickHouse, ClickHouseError, PurgeIncompleteError
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
    def __init__(self, stale=(), fail_on=None):
        super().__init__("http://x", "u", "p")
        self.sql: list[str] = []
        self.stale = list(stale)          # rows the ledger reports as incomplete purges
        self.fail_on = fail_on            # SQL prefix that raises, to simulate a mid-purge failure

    def query(self, sql, data=None, settings=None):
        self.sql.append(sql)
        if self.fail_on and sql.startswith(self.fail_on):
            raise ClickHouseError("simulated failure")
        if "FROM system.tables" in sql and "name = 'trace_time_index'" in sql:
            return json.dumps({"name": "trace_time_index"})
        if "FROM system.tables" in sql:
            return "\n".join(json.dumps(r) for r in MV_ROWS)
        if "FROM system.columns" in sql:
            table = sql.split("table = '")[1].split("'")[0]
            return "\n".join(json.dumps({"name": n, "type": t}) for n, t in COLUMNS.get(table, []))
        if "AS n, toString(min(Timestamp))" in sql:
            return json.dumps({"n": "3", "lo": "2026-10-01 10:17:00.000000000", "hi": "2026-10-02 10:00:00.000000000"})
        if "FROM td_purge_backup.__purges" in sql:
            return "\n".join(json.dumps(r) for r in self.stale)
        return ""


NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
STAMP = "20261021T150000"


def test_purge_backs_up_before_any_delete():
    ch = FakeHTTPCH()
    ch.purge_td(now=NOW, log=lambda m: None)
    s = ch.sql
    deletes = [i for i, q in enumerate(s) if q.startswith("ALTER TABLE") and " DELETE " in q]
    creates = [i for i, q in enumerate(s) if q.startswith("CREATE TABLE IF NOT EXISTS td_purge_backup.agw_cost_model_5m")]
    copies = [i for i, q in enumerate(s) if q.startswith("INSERT INTO td_purge_backup.agw_cost_model_5m")]
    tix_bk = [i for i, q in enumerate(s) if q.startswith("INSERT INTO td_purge_backup.trace_time_index__")]
    assert deletes and creates and copies and tix_bk
    assert max(creates + copies + tix_bk) < min(deletes)
    assert " AS platformdb.agw_cost_model_5m" in s[creates[0]]
    # TD ids are frozen with the predicate, older than the live edge only
    (freeze,) = [q for q in s if q.startswith(f"INSERT INTO td_purge_backup.td_span_ids__{STAMP}")]
    assert td_span_predicate() in freeze and "Timestamp < toDateTime64('2026-10-21 14:55:00'" in freeze
    # source rows: whole TD traces from the frozen ids (covers the attribute-less second span)
    src = [q for q in s if q.startswith("ALTER TABLE platformdb.otel_traces_json DELETE")]
    assert len(src) == 1 and "TraceId IN (SELECT TraceId FROM merge('td_purge_backup', '^td_span_ids__'))" in src[0]
    assert "Timestamp < toDateTime64('2026-10-21 14:55:00'" in src[0]
    # rollup: range delete from the hour of the first TD span, then re-insert from the view's select
    roll = [q for q in s if q.startswith("ALTER TABLE platformdb.agw_cost_model_5m DELETE")]
    assert len(roll) == 1 and "2026-10-01 10:00:00" in roll[0]
    reins = [i for i, q in enumerate(s) if q.startswith("INSERT INTO platformdb.agw_cost_model_5m")]
    assert len(reins) == 1 and "FROM (SELECT * FROM platformdb.agw_spans_typed WHERE" in s[reins[0]]
    assert s.index(roll[0]) < reins[0]
    # trace_time_index: TD trace ids only, after verification (= after the rollup re-insert), before the source
    (tix,) = [i for i, q in enumerate(s) if q.startswith("ALTER TABLE platformdb.trace_time_index DELETE")]
    assert "merge('td_purge_backup', '^td_span_ids__')" in s[tix]
    assert reins[0] < tix < s.index(src[0])
    # ledger: started before the first delete, verified before the trace index / source deletes
    led = {st: i for i, q in enumerate(s) if q.startswith("INSERT INTO td_purge_backup.__purges")
           for st in ("started", "verified", "completed") if f"'{st}'" in q}
    assert led["started"] < min(deletes) and reins[0] < led["verified"] < tix < led["completed"]
    assert not any(q.startswith(("DROP", "TRUNCATE")) for q in s)


def test_purge_failure_mid_rollup_prints_restore_sql():
    ch = FakeHTTPCH(fail_on="INSERT INTO platformdb.agw_cost_model_5m")
    logs = []
    with pytest.raises(ClickHouseError):
        ch.purge_td(now=NOW, log=logs.append)
    text = "\n".join(logs)
    assert f"Restore from {STAMP}" in text
    assert f"INSERT INTO platformdb.agw_cost_model_5m SELECT * FROM td_purge_backup.agw_cost_model_5m__{STAMP};" in text
    assert f"INSERT INTO platformdb.trace_time_index SELECT * FROM td_purge_backup.trace_time_index__{STAMP};" in text
    assert not any(q.startswith("ALTER TABLE platformdb.otel_traces_json") for q in ch.sql)
    assert not any(q.startswith("INSERT INTO td_purge_backup.__purges") and "'verified'" in q for q in ch.sql)


def test_incomplete_prior_purge_blocks_new_one():
    old = {"stamp": "20261020T090000", "start": "2026-09-20 09:00:00", "upper": "2026-10-20 08:55:00",
           "rollups": "agw_cost_model_5m:bucket", "trace_index": "1"}
    ch = FakeHTTPCH(stale=[old])
    with pytest.raises(PurgeIncompleteError) as e:
        ch.purge_td(now=NOW, log=lambda m: None)
    msg = str(e.value)
    assert "20261020T090000" in msg and "--force-new-backup" in msg
    assert "INSERT INTO platformdb.agw_cost_model_5m SELECT * FROM td_purge_backup.agw_cost_model_5m__20261020T090000" in msg
    assert "bucket >= toDateTime('2026-09-20 09:00:00', 'UTC')" in msg
    assert not any(q.startswith(("ALTER", "INSERT INTO platformdb", "INSERT INTO td_purge_backup.td_span_ids"))
                   for q in ch.sql)
    # the override proceeds
    ch2 = FakeHTTPCH(stale=[old])
    ch2.purge_td(now=NOW, log=lambda m: None, force_new_backup=True)
    assert any(q.startswith("ALTER TABLE platformdb.otel_traces_json DELETE") for q in ch2.sql)


def test_main_reports_clickhouse_errors_cleanly(td, capsys):
    def boom():
        raise OSError("connection reset by peer")
    td.fake.count_td = boom
    rc = td.main(["backfill", "--context", "CTX", "--days", "1"])
    out = capsys.readouterr()
    assert rc == 1 and "error: connection reset by peer" in out.out and "Traceback" not in out.err


def test_purge_noop_when_no_td_rows():
    ch = FakeHTTPCH()
    ch.query = lambda sql, data=None, settings=None: (ch.sql.append(sql) or
                                                       json.dumps({"n": "0", "lo": "", "hi": ""}))
    ch.purge_td(log=lambda m: None)
    assert not any(q.startswith(("ALTER", "CREATE", "INSERT")) for q in ch.sql)


def test_insert_rows_batches(monkeypatch):
    ch = ClickHouse("http://x", "u", "p")
    sent = []
    monkeypatch.setattr(ch, "_insert_batch", lambda table, lines, attempts=8: sent.append(len(lines)))
    n = ch.insert_rows("platformdb.otel_traces_json", ({"i": i} for i in range(45)), batch=20)
    assert n == 45 and sent == [20, 20, 5]


class MismatchCH(FakeHTTPCH):
    """The pre-verify summary (view over NOT-TD source rows) disagrees with the real totals."""

    def query(self, sql, data=None, settings=None):
        if sql.startswith("SELECT toString(") and "AND NOT TraceId IN" in sql:
            self.sql.append(sql)
            return json.dumps({"k0": "svc", "v0": "5", "v1": "1.5"})
        return super().query(sql, data, settings)


def test_purge_pre_verify_mismatch_aborts_before_any_mutation(tmp_path):
    from tdlib.clickhouse import PurgeVerificationError
    ch = MismatchCH()
    ch.restore_dir = str(tmp_path)
    with pytest.raises(PurgeVerificationError, match="aborted before any change"):
        ch.purge_td(now=NOW, log=lambda m: None)
    assert not any(q.startswith("ALTER TABLE") for q in ch.sql)
    assert not any(q.startswith("INSERT INTO platformdb.") for q in ch.sql)
    led = [q for q in ch.sql if q.startswith("INSERT INTO td_purge_backup.__purges")]
    assert "'started'" in led[0] and "'aborted'" in led[-1]


def test_views_refuses_mv_reading_a_rollup():
    import tdlib.clickhouse as chm
    ch = FakeHTTPCH()
    chained = {"name": "agw_cost_model_1h_mv",
               "create_table_query": "CREATE MATERIALIZED VIEW platformdb.agw_cost_model_1h_mv TO platformdb.x_1h AS ..",
               "as_select": "SELECT bucket FROM platformdb.agw_cost_model_5m"}
    orig = list(MV_ROWS)
    MV_ROWS.append(chained)
    try:
        with pytest.raises(chm.ClickHouseError, match="reads rollup table agw_cost_model_5m"):
            ch.views()
    finally:
        MV_ROWS[:] = orig


def test_restore_command_is_runnable(tmp_path):
    ch = FakeHTTPCH(fail_on="INSERT INTO platformdb.agw_cost_model_5m")
    ch.restore_dir = str(tmp_path)
    ch.restore_cmd_prefix = "kubectl --context C -n ns exec -i pod -- clickhouse-client --multiquery"
    logs = []
    with pytest.raises(ClickHouseError):
        ch.purge_td(now=NOW, log=logs.append)
    f = tmp_path / f"td-restore-{STAMP}.sql"
    assert f.exists() and "INSERT INTO platformdb.agw_cost_model_5m SELECT * FROM" in f.read_text()
    import shlex
    assert any(f"clickhouse-client --multiquery < {shlex.quote(str(f))}" in m for m in logs)


def test_restore_command_prefix_is_shell_quoted():
    import shlex
    td = load_td()
    cmd = td.restore_command_prefix("ctx; rm -rf ~", "my ns", "pod$(x)", "sec`y`")
    argv = shlex.split(cmd.split(" exec -i ")[0])
    assert argv[:5] == ["kubectl", "--context", "ctx; rm -rf ~", "-n", "my ns"]
    assert "'ctx; rm -rf ~'" in cmd and "'pod$(x)'" in cmd and "'sec`y`'" in cmd
    assert "--user \"$(kubectl --context 'ctx; rm -rf ~' -n 'my ns' get secret 'sec`y`'" in cmd
