# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""TD Bank synthetic AI-assistant traffic for the Solo Enterprise agentgateway UI.

    td-traffic.pyz {demo,up,backfill,sync-budgets,live,replan-budgets,down,status} --context <kube-context>
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import shlex
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from tdlib import budget_sync, kube as kube_mod, live, manifests  # noqa: E402
from tdlib.clickhouse import SPANS_TABLE, ClickHouse, ClickHouseError  # noqa: E402
from tdlib.kube import Kube, KubeError  # noqa: E402
from tdlib.manifests import (  # noqa: E402
    DIMENSIONS_FILE, LABELS, MOCK, NAMESPACE, budget_window, plan_budgets, render_all,
)
from tdlib.org import build_org  # noqa: E402
from tdlib.pricing import load_rates  # noqa: E402
from tdlib.spans import load_template, to_row  # noqa: E402
from tdlib.usage import generate  # noqa: E402

# The bundled copy (td-traffic.pyz) wins; in a repo checkout the catalog lives under apps/.
RATES_PATH = next((p for p in (HERE / "model-costs.yaml", HERE.parents[1] / "apps/agentgateway-system/model-costs.yaml")
                   if p.exists()), HERE / "model-costs.yaml")
SEED = 42
BACKFILL_DAYS = 30
BATCH = 20_000

RELEASE, RELEASE_NS = "enterprise-agentgateway", "agentgateway-system"
CHART = "oci://us-docker.pkg.dev/solo-public/enterprise-agentgateway/charts/enterprise-agentgateway"
SAVED_CM = "td-demo-saved-values"
SAVED_KEY = "values.json"
DIM_KEY = "budgetDimensions"

# Namespaced TD kinds, fully qualified. `down` deletes these by label, then the namespace.
TD_KINDS = [
    "enterpriseagentgatewaybudgets.enterpriseagentgateway.solo.io",
    "enterpriseagentgatewaypolicies.enterpriseagentgateway.solo.io",
    "httproutes.gateway.networking.k8s.io",
    "agentgatewaybackends.agentgateway.dev",
]
# deployments / services stay listed on purpose: `status` then reveals a stale v1 mock.
STATUS_KINDS = [*TD_KINDS, "secrets", "configmaps", "deployments.apps", "services"]
EARLY_WINDOW = timedelta(days=5)
UI_SVC = "solo-enterprise-ui"


def configure(release: str, namespace: str, gateway: str) -> None:
    """Point every module at this cluster's agentgateway release / namespace / Gateway (flags, with defaults)."""
    global RELEASE, RELEASE_NS
    RELEASE, RELEASE_NS = release, namespace
    manifests.GATEWAY = {"name": gateway, "namespace": namespace}
    live.GATEWAY_TARGET, live.GATEWAY_NS = f"svc/{gateway}", namespace
    kube_mod.HELM_RELEASE = (release, namespace)
    kube_mod.COMPONENTS = kube_mod.components(gateway, namespace)


def early_window_warning(now: datetime | None = None) -> str | None:
    start, end = budget_window(now)
    age = (now or datetime.now(timezone.utc)) - start
    if age < EARLY_WINDOW:
        return (f"WARNING: the gateway budget window started {start:%Y-%m-%d %H:%M} UTC, only "
                f"{age.total_seconds() / 86400:.1f} days ago, so window-to-date spend and the budget limits will "
                f"look small. Wait, or re-run `replan-budgets` + `sync-budgets` in a few days.")
    return None


def warn_early_window() -> None:
    w = early_window_warning()
    if w:
        log(w)


def discover_clickhouse(kube: Kube, ns: str | None = None) -> tuple[str, str, str]:
    """(namespace, pod, auth secret) of the management ClickHouse: name pattern *clickhouse* in the UI namespace."""
    ns = ns or RELEASE_NS
    pods = json.loads(kube.run(["get", "pods", "-n", ns, "-o", "json"]))["items"]
    cands = sorted((p["metadata"]["name"] for p in pods
                    if "clickhouse" in p["metadata"]["name"] and p.get("status", {}).get("phase") == "Running"
                    and "operator" not in p["metadata"]["name"]),
                   key=lambda n: ("shard0" not in n, n))
    if not cands:
        raise KubeError(f"no running *clickhouse* pod in namespace {ns}: install the Solo Enterprise management "
                        f"chart (Solo UI with its ClickHouse) or pass --namespace")
    secs = json.loads(kube.run(["get", "secrets", "-n", ns, "-o", "json"]))["items"]
    auth = sorted(s["metadata"]["name"] for s in secs if "clickhouse" in s["metadata"]["name"]
                  and {"username", "password"} <= set((s.get("data") or {})))
    if not auth:
        raise KubeError(f"no *clickhouse* Secret with username/password keys in namespace {ns}")
    return ns, cands[0], auth[0]


def restore_command_prefix(context: str, ns: str, pod: str, secret: str) -> str:
    """Copy-paste-safe `kubectl exec ... clickhouse-client` (every value shell-quoted; credentials read from the Secret)."""
    q = shlex.quote
    base = f"kubectl --context {q(context)} -n {q(ns)}"

    def cred(key: str) -> str:
        return f'"$({base} get secret {q(secret)} -o jsonpath={q("{.data." + key + "}")} | base64 -d)"'
    return (f"{base} exec -i {q(pod)} -- clickhouse-client --user {cred('username')} "
            f"--password {cred('password')} --multiquery")


def ui_url(kube: Kube) -> str:
    """How to open the Solo UI: LoadBalancer / NodePort address of svc solo-enterprise-ui, else a port-forward."""
    pf = f"kubectl --context {kube.context} -n {RELEASE_NS} port-forward svc/{UI_SVC} 8090:80  # then http://localhost:8090"
    p = kube.kubectl(["get", "svc", UI_SVC, "-n", RELEASE_NS, "-o", "json"], check=False)
    if p.returncode != 0:
        return pf
    svc = json.loads(p.stdout)
    ports = svc["spec"].get("ports", [])
    if svc["spec"].get("type") == "LoadBalancer":
        ing = (svc.get("status", {}).get("loadBalancer", {}).get("ingress") or [{}])[0]
        host = ing.get("ip") or ing.get("hostname")
        if host and ports:
            return f"http://{host}:{ports[0]['port']}"
    if svc["spec"].get("type") in ("NodePort", "LoadBalancer") and ports and ports[0].get("nodePort"):
        nodes = json.loads(kube.run(["get", "nodes", "-o", "json"], check=False) or '{"items": []}')["items"]
        for n in nodes:
            addrs = {a["type"]: a["address"] for a in n.get("status", {}).get("addresses", [])}
            ip = addrs.get("ExternalIP") or addrs.get("InternalIP")
            if ip:
                return f"http://{ip}:{ports[0]['nodePort']}  (or: {pf})"
    return pf


def log(msg: str) -> None:
    print(f"[td] {msg}", flush=True)


# ---------------------------------------------------------------- helm values save / restore

def saved_values(kube: Kube) -> dict | None:
    """The saved original `budgetDimensions` subset ({} = key was absent), or None if never saved."""
    p = kube.kubectl(["get", "configmap", SAVED_CM, "-n", NAMESPACE, "-o", "json"], check=False)
    if p.returncode != 0:
        if any(s in (p.stderr or "") for s in ("NotFound", "not found")):
            return None
        raise KubeError(f"reading {NAMESPACE}/{SAVED_CM} failed: {(p.stderr or p.stdout).strip()}")
    return json.loads(json.loads(p.stdout)["data"][SAVED_KEY])


def looks_like_td_dims(dims) -> bool:
    return bool(dims) and "organization" in json.dumps(dims) and "Client Tool" in json.dumps(dims)


def save_values_once(kube: Kube) -> None:
    if saved_values(kube) is not None:
        log(f"original Helm values already saved in {NAMESPACE}/{SAVED_CM}; keeping them")
        return
    current = kube.helm_get_values(RELEASE, RELEASE_NS)
    dims = current.get(DIM_KEY)
    if looks_like_td_dims(dims):
        raise KubeError(f"{RELEASE} budgetDimensions already look like the TD values but no saved original "
                        f"exists in {NAMESPACE}/{SAVED_CM}; refusing to save them as the original")
    subset = {DIM_KEY: dims} if DIM_KEY in current else {}
    kube.apply([{
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": SAVED_CM, "namespace": NAMESPACE, "labels": dict(LABELS), "annotations": {
            "td.demo/description": f"original user-supplied `{DIM_KEY}` of Helm release {RELEASE_NS}/{RELEASE} "
                                   "before `td.py up` ({} = not set, chart default); restored by `td.py down`"}},
        "data": {SAVED_KEY: json.dumps(subset, sort_keys=True)},
    }])
    log(f"saved original {DIM_KEY} ({'set' if subset else 'chart default'}) to {NAMESPACE}/{SAVED_CM}")


def restored_values(current: dict, saved: dict) -> dict:
    """Current user values with only `budgetDimensions` put back to the saved original."""
    out = {k: v for k, v in current.items() if k != DIM_KEY}
    if DIM_KEY in saved:
        out[DIM_KEY] = saved[DIM_KEY]
    return out


def restore_values(kube: Kube, saved: dict) -> None:
    current = kube.helm_get_values(RELEASE, RELEASE_NS)
    want = restored_values(current, saved)
    if want == current:
        log(f"{RELEASE} {DIM_KEY} already original; Helm untouched")
        return
    version = kube.helm_chart_version(RELEASE, RELEASE_NS)
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "values.json"
        f.touch(mode=0o600)
        f.write_text(json.dumps(want))
        kube.helm_upgrade_reset(RELEASE, CHART, version, RELEASE_NS, f)
    log(f"restored {RELEASE} {DIM_KEY} ({'saved block' if DIM_KEY in saved else 'chart default'}) "
        f"at chart {version}")


# ---------------------------------------------------------------- subcommands

def _remove_legacy_mock(kube: Kube) -> None:
    """Delete the v1 mock workload (Deployment, Service, ConfigMap), if a v1 `up` left it behind."""
    kube.run(["delete", f"deploy/{MOCK}", f"svc/{MOCK}", f"cm/{MOCK}", "-n", NAMESPACE,
              "--ignore-not-found"], check=False)


def cmd_up(kube: Kube, args) -> int:
    missing = kube.preflight()
    try:
        discover_clickhouse(kube)
    except KubeError as e:
        missing.append(str(e))
    if missing:
        log("missing on context " + kube.context + ": " + ", ".join(missing))
        log("nothing changed")
        return 1
    org = build_org()
    budgets = planned_budgets(org, args.seed)
    objs = render_all(org, budgets)
    kube.apply(objs[:1])                       # namespace, so the saved values can live in it
    _remove_legacy_mock(kube)
    save_values_once(kube)
    kube.apply(objs)
    log(f"applied {len(objs)} objects ({len(org.keys)} virtual keys, {len(budgets)} budgets)")
    version = kube.helm_chart_version(RELEASE, RELEASE_NS)
    kube.helm_upgrade_reuse(RELEASE, CHART, version, RELEASE_NS, DIMENSIONS_FILE)
    log(f"helm upgrade {RELEASE} {version} with {DIMENSIONS_FILE.name} (proxy picks it up in ~60s)")
    warn_early_window()
    log("up: done")
    return 0


def planned_budgets(org, seed: int) -> list[dict]:
    start, end = budget_window()
    log(f"planning budgets on window-to-date spend: gateway budget window {start:%Y-%m-%d %H:%M} .. "
        f"{end:%Y-%m-%d %H:%M} UTC (re-plan after it rolls over)")
    return plan_budgets(org, load_rates(RATES_PATH), seed)


def cmd_replan_budgets(kube: Kube, args) -> int:
    """Re-apply only the budget object, with limits planned for the current window."""
    org = build_org()
    budgets = planned_budgets(org, args.seed)
    objs = [o for o in render_all(org, budgets)
            if o["kind"] == "EnterpriseAgentgatewayBudget"]
    kube.apply(objs)
    for b in budgets:
        log(f"  {b['name']:45} {b['limit']['amount']:>8,} USD")
    log(f"replan-budgets: applied {len(budgets)} budgets; run `sync-budgets` so the page shows spend against them")
    warn_early_window()
    return 0


def cmd_down(kube: Kube, args) -> int:
    saved = saved_values(kube)
    if saved is None:
        try:
            dims = kube.helm_get_values(RELEASE, RELEASE_NS).get(DIM_KEY)
        except KubeError as e:
            if "not found" not in str(e):
                raise
            dims = None                        # no release, so no TD dimensions to leave behind
        if looks_like_td_dims(dims):
            log(f"error: {RELEASE} still carries the TD {DIM_KEY} but {NAMESPACE}/{SAVED_CM} (the saved original) "
                f"is gone; nothing deleted. Restore by hand, then re-run `down`:")
            log(f"  helm --kube-context {kube.context} -n {RELEASE_NS} get values {RELEASE} -o json "
                f"| jq 'del(.{DIM_KEY})' > values.json")
            log(f"  helm --kube-context {kube.context} upgrade {RELEASE} {CHART} -n {RELEASE_NS} "
                f"--version <installed chart version> --reset-values -f values.json")
            return 1
        log(f"no {NAMESPACE}/{SAVED_CM}; Helm release left untouched")
    else:
        restore_values(kube, saved)
    kube.delete_labeled(TD_KINDS, NAMESPACE)
    _remove_legacy_mock(kube)
    p = kube.kubectl(["get", "namespace", NAMESPACE, "-o", "json"], check=False)
    if p.returncode == 0 and json.loads(p.stdout)["metadata"].get("labels", {}).get("demo") == "td":
        kube.run(["delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=true", "--timeout=180s"])
        log(f"deleted namespace {NAMESPACE}")
    elif p.returncode == 0:
        log(f"namespace {NAMESPACE} is not labeled demo=td; left in place")
    if getattr(args, "purge_history", False):
        with open_clickhouse(kube) as ch:
            ch.purge_td(log=log, force_new_backup=args.force_new_backup)
    log("down: done")
    return 0


@contextlib.contextmanager
def open_clickhouse(kube: Kube) -> Iterator[ClickHouse]:
    """ClickHouse over a port-forward to the management ClickHouse pod, credentials from its Secret."""
    ns, pod, secret = discover_clickhouse(kube)
    sec = json.loads(kube.run(["get", "secret", secret, "-n", ns, "-o", "json"]))["data"]
    user, pw = (base64.b64decode(sec[k]).decode() for k in ("username", "password"))
    with kube.port_forward(f"pod/{pod}", ns, 8123) as port:
        ch = ClickHouse(f"http://127.0.0.1:{port}", user, pw)
        ch.restore_dir = str(Path.cwd())
        ch.restore_cmd_prefix = restore_command_prefix(kube.context, ns, pod, secret)
        yield ch


def td_span_count(kube: Kube) -> int:
    with open_clickhouse(kube) as ch:
        return ch.count_td()


def run_backfill(ch, days: float, replace: bool, seed: int = SEED, batch: int = BATCH,
                 now: datetime | None = None, force_new_backup: bool = False) -> int:
    existing = ch.count_td()
    if existing and not replace:
        log(f"{existing} TD spans already in ClickHouse; refusing to add a second history. "
            "Re-run with --replace to purge the TD history first.")
        return 1
    if existing:
        log(f"--replace: purging {existing} existing TD spans first")
        ch.purge_td(log=log, force_new_backup=force_new_backup)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = now - timedelta(days=days)
    template = load_template()
    cluster = ch.cluster_name(template["ResourceAttributes"]["cluster_name"])
    rates = load_rates(RATES_PATH)
    log(f"backfill: {days:g} days {start:%Y-%m-%d %H:%M} .. {now:%Y-%m-%d %H:%M} UTC, cluster_name={cluster}, "
        f"batches of {batch}")
    t0 = time.monotonic()

    def progress(n: int) -> None:
        el = time.monotonic() - t0
        log(f"backfill: {n:,} rows inserted ({el:.0f}s, {n / max(el, 1e-9):,.0f} rows/s)")

    rows = (to_row(r, rates, template, cluster) for r in generate(build_org(), start, now, seed))
    total = ch.insert_rows(SPANS_TABLE, rows, batch=batch, progress=progress)
    log(f"backfill: done, {total:,} TD spans in {time.monotonic() - t0:.0f}s")
    return 0


def cmd_backfill(kube: Kube, args) -> int:
    with open_clickhouse(kube) as ch:
        return run_backfill(ch, args.days, args.replace, seed=args.seed, batch=args.batch,
                            force_new_backup=args.force_new_backup)


def cmd_status(kube: Kube, args) -> int:
    out = kube.run(["get", ",".join(STATUS_KINDS), "-n", NAMESPACE, "-l", "demo=td", "-o", "name"], check=False)
    counts: dict[str, int] = {}
    for line in out.split():
        kind = line.split("/")[0].split(".")[0]
        counts[kind] = counts.get(kind, 0) + 1
    log("TD objects in " + NAMESPACE + ": " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"))
    log(f"saved Helm values: {'present' if saved_values(kube) is not None else 'absent'}")
    start, end = budget_window()
    log(f"budget window (gateway `Month`, epoch-aligned 30 days): {start:%Y-%m-%d %H:%M} .. {end:%Y-%m-%d %H:%M} UTC; "
        f"next rollover {end:%Y-%m-%d %H:%M} UTC -> then re-run `up` (or `replan-budgets`), `backfill --replace`, "
        f"`sync-budgets`")
    warn_early_window()
    try:
        log(f"TD span rows in ClickHouse: {td_span_count(kube)}")
    except (KubeError, ClickHouseError, OSError, ValueError) as e:
        log(f"TD span rows: unavailable ({e})")
    return 0


def cmd_live(kube: Kube, args) -> int:
    duration = live.parse_duration(args.duration) if args.duration else None
    stats = live.run_live(kube, build_org(), speed=args.speed, min_rpm=args.min_rpm, duration=duration,
                          ch_factory=lambda: open_clickhouse(kube), rates=load_rates(RATES_PATH), seed=args.seed,
                          log=log)
    log("live: clients " + ", ".join(f"{k}={v}" for k, v in sorted(stats.clients.items())))
    return 0


def cmd_selfcheck() -> int:
    """Load every bundled resource (rates catalog, builtin rates, span template, dimensions); exit 0 if all are there."""
    rates = load_rates(RATES_PATH)
    template = load_template()
    dims = DIMENSIONS_FILE.read_text()
    assert rates and template["SpanName"] is not None and "budgetDimensions" in dims
    assert len(build_org().keys) > 0
    print(f"td-traffic selfcheck ok: {len(rates)} rates, span template, dimensions ({DIMENSIONS_FILE.name})")
    return 0


def cmd_sync_budgets(kube: Kube, args) -> int:
    with open_clickhouse(kube) as ch:
        rows = budget_sync.sync_budgets(kube, ch, build_org(), rates=load_rates(RATES_PATH), seed=args.seed,
                                        dry_run=args.dry_run, log=log)
    print(budget_sync.format_table(rows), flush=True)
    return 0


def cmd_demo(kube: Kube, args) -> int:
    """preflight -> up -> backfill --replace -> sync-budgets, then where to look and what to run next."""
    rc = cmd_up(kube, args)
    if rc:
        return rc
    with open_clickhouse(kube) as ch:
        rc = run_backfill(ch, args.days, True, seed=args.seed, force_new_backup=False)
    if rc:
        return rc
    rc = cmd_sync_budgets(kube, argparse.Namespace(seed=args.seed, dry_run=False))
    if rc:
        return rc
    log("demo: ready.")
    log(f"  Solo UI:   {ui_url(kube)}")
    log(f"  next:      python3 td-traffic.pyz live --context {kube.context}   (Ctrl-C stops)")
    log(f"  cleanup:   python3 td-traffic.pyz down --purge-history --context {kube.context}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="td-traffic", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selfcheck")      # hidden from --help (no help=): packaging smoke test
    for name, fn, hlp in [
        ("demo", cmd_demo, "one shot: preflight, up, backfill --replace, sync-budgets; prints the UI URL"),
        ("up", cmd_up, "create the TD demo objects, keys, budgets and dimensions"),
        ("down", cmd_down, "restore Helm budgetDimensions and delete every TD object"),
        ("status", cmd_status, "count TD objects and TD span rows"),
        ("backfill", cmd_backfill, "write synthetic TD history straight into ClickHouse"),
        ("sync-budgets", cmd_sync_budgets,
         "set the Budgets page to the generated window spend (zero-cost outcome rows in ClickHouse)"),
        ("live", cmd_live, "keep writing fresh TD spans into ClickHouse and nudging the budgets (Ctrl-C stops)"),
        ("replan-budgets", cmd_replan_budgets, "re-apply only the budgets, sized for the current budget window"),
    ]:
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--context", required=True, help="kube context (required; the current one is never used)")
        p.add_argument("--namespace", default=RELEASE_NS,
                       help=f"namespace of the agentgateway release, Gateway, Solo UI and ClickHouse (default {RELEASE_NS})")
        p.add_argument("--release", default=RELEASE, help=f"agentgateway Helm release name (default {RELEASE})")
        p.add_argument("--gateway", default="agentgateway-proxy",
                       help="Gateway (and its proxy Service) carrying the TD routes (default agentgateway-proxy)")
        p.set_defaults(fn=fn)
        if name == "demo":
            p.add_argument("--days", type=float, default=BACKFILL_DAYS, help=f"history length (default {BACKFILL_DAYS})")
        if name in ("up", "replan-budgets", "demo"):
            p.add_argument("--seed", type=int, default=SEED, help="budget planning seed (same as backfill)")
        if name == "backfill":
            p.add_argument("--days", type=float, default=BACKFILL_DAYS,
                           help=f"history length ending now (default {BACKFILL_DAYS})")
            p.add_argument("--replace", action="store_true",
                           help="purge the existing TD history first (otherwise backfill refuses)")
            p.add_argument("--seed", type=int, default=SEED, help="traffic seed (same as `up` budget planning)")
            p.add_argument("--batch", type=int, default=BATCH, help="rows per INSERT")
        if name == "live":
            p.add_argument("--duration", help="how long to run, e.g. 90s, 10m, 1h (default: until Ctrl-C)")
            p.add_argument("--speed", type=float, default=1.0, help="rate multiplier on the usage model (default 1)")
            p.add_argument("--min-rpm", type=float, default=6.0, help="floor in requests/minute (default 6)")
            p.add_argument("--seed", type=int, default=None, help="random seed (default: random)")
        if name == "sync-budgets":
            p.add_argument("--seed", type=int, default=SEED, help="history seed (same as backfill)")
            p.add_argument("--dry-run", action="store_true", help="print the targets table; write nothing")
        if name in ("backfill", "down"):
            p.add_argument("--force-new-backup", action="store_true",
                           help="purge even though an earlier purge never finished (restore that one first!)")
        if name == "down":
            p.add_argument("--purge-history", action="store_true",
                           help="also delete every TD span from ClickHouse and recompute the affected rollups "
                                "(rollups are snapshotted to td_purge_backup first)")
    args = ap.parse_args(argv)
    if args.cmd == "selfcheck":
        return cmd_selfcheck()
    configure(args.release, args.namespace, args.gateway)
    try:
        return args.fn(Kube(args.context), args)
    except (KubeError, ClickHouseError, OSError, ValueError) as e:
        log(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
