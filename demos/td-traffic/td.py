# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""TD Bank synthetic AI-assistant traffic for the Solo Enterprise agentgateway UI.

    uv run demos/td-traffic/td.py {up,backfill,live,down,status} --context <kube-context>
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from tdlib.kube import Kube, KubeError  # noqa: E402
from tdlib.manifests import (  # noqa: E402
    DIMENSIONS_FILE, LABELS, MOCK, NAMESPACE, plan_budgets, render_all,
)
from tdlib.org import build_org  # noqa: E402
from tdlib.pricing import load_rates  # noqa: E402

RATES_PATH = HERE.parents[1] / "apps/agentgateway-system/model-costs.yaml"
MOCK_SOURCE = HERE / "mock/server.py"
SEED = 42

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
STATUS_KINDS = [*TD_KINDS, "secrets", "configmaps", "deployments.apps", "services"]
CH_POD, CH_SECRET = "pod/management-clickhouse-shard0-0", "management-clickhouse-auth"
TD_SPANS_SQL = ("SELECT count() FROM platformdb.otel_traces_json "
                "WHERE SpanAttributes.`server.address` = 'td.demo.local' FORMAT TSV")


def log(msg: str) -> None:
    print(f"[td] {msg}", flush=True)


# ---------------------------------------------------------------- helm values save / restore

def saved_values(kube: Kube) -> dict | None:
    """The saved original `budgetDimensions` subset ({} = key was absent), or None if never saved."""
    p = kube.kubectl(["get", "configmap", SAVED_CM, "-n", NAMESPACE, "-o", "json"], check=False)
    if p.returncode != 0:
        return None
    return json.loads(json.loads(p.stdout)["data"][SAVED_KEY])


def save_values_once(kube: Kube) -> None:
    if saved_values(kube) is not None:
        log(f"original Helm values already saved in {NAMESPACE}/{SAVED_CM}; keeping them")
        return
    current = kube.helm_get_values(RELEASE, RELEASE_NS)
    dims = current.get(DIM_KEY)
    if dims and "organization" in json.dumps(dims) and "Client Tool" in json.dumps(dims):
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

def cmd_up(kube: Kube, args) -> int:
    missing = kube.preflight()
    if missing:
        log("missing on context " + kube.context + ": " + ", ".join(missing))
        log("nothing changed")
        return 1
    org = build_org()
    log("planning budgets from the generated 30-day history ...")
    budgets = plan_budgets(org, load_rates(RATES_PATH), args.seed)
    objs = render_all(org, budgets, MOCK_SOURCE.read_text())
    kube.apply(objs[:1])                       # namespace, so the saved values can live in it
    save_values_once(kube)
    kube.apply(objs)
    log(f"applied {len(objs)} objects ({len(org.keys)} virtual keys, {len(budgets)} budgets)")
    version = kube.helm_chart_version(RELEASE, RELEASE_NS)
    kube.helm_upgrade_reuse(RELEASE, CHART, version, RELEASE_NS, DIMENSIONS_FILE)
    log(f"helm upgrade {RELEASE} {version} with {DIMENSIONS_FILE.name} (proxy picks it up in ~60s)")
    kube.run(["rollout", "status", f"deployment/{MOCK}", "-n", NAMESPACE, "--timeout=180s"])
    log("up: done")
    return 0


def cmd_down(kube: Kube, args) -> int:
    saved = saved_values(kube)
    if saved is None:
        log(f"no {NAMESPACE}/{SAVED_CM}; Helm release left untouched")
    else:
        restore_values(kube, saved)
    kube.delete_labeled(TD_KINDS, NAMESPACE)
    p = kube.kubectl(["get", "namespace", NAMESPACE, "-o", "json"], check=False)
    if p.returncode == 0 and json.loads(p.stdout)["metadata"].get("labels", {}).get("demo") == "td":
        kube.run(["delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=true", "--timeout=180s"])
        log(f"deleted namespace {NAMESPACE}")
    elif p.returncode == 0:
        log(f"namespace {NAMESPACE} is not labeled demo=td; left in place")
    log("down: done")
    return 0


def td_span_count(kube: Kube) -> int:
    sec = json.loads(kube.run(["get", "secret", CH_SECRET, "-n", RELEASE_NS, "-o", "json"]))["data"]
    user, pw = (base64.b64decode(sec[k]).decode() for k in ("username", "password"))
    with kube.port_forward(CH_POD, RELEASE_NS, 8123) as port:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/?" + urllib.parse.urlencode({"query": TD_SPANS_SQL}))
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
        with urllib.request.urlopen(req, timeout=60) as r:
            return int(r.read().decode().strip())


def cmd_status(kube: Kube, args) -> int:
    out = kube.run(["get", ",".join(STATUS_KINDS), "-n", NAMESPACE, "-l", "demo=td", "-o", "name"], check=False)
    counts: dict[str, int] = {}
    for line in out.split():
        kind = line.split("/")[0].split(".")[0]
        counts[kind] = counts.get(kind, 0) + 1
    log("TD objects in " + NAMESPACE + ": " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"))
    log(f"saved Helm values: {'present' if saved_values(kube) is not None else 'absent'}")
    try:
        log(f"TD span rows in ClickHouse: {td_span_count(kube)}")
    except (KubeError, OSError, ValueError) as e:
        log(f"TD span rows: unavailable ({e})")
    return 0


def cmd_todo(kube: Kube, args) -> int:
    log(f"`{args.cmd}` is not implemented yet")
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="td.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, hlp in [
        ("up", cmd_up, "create the TD demo objects, keys, budgets and dimensions"),
        ("down", cmd_down, "restore Helm budgetDimensions and delete every TD object"),
        ("status", cmd_status, "count TD objects and TD span rows"),
        ("backfill", cmd_todo, "write synthetic history (Task 7)"),
        ("live", cmd_todo, "send live traffic (Task 8)"),
    ]:
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--context", required=True, help="kube context (required; the current one is never used)")
        p.set_defaults(fn=fn)
        if name == "up":
            p.add_argument("--seed", type=int, default=SEED, help="budget projection seed")
    args = ap.parse_args(argv)
    try:
        return args.fn(Kube(args.context), args)
    except KubeError as e:
        log(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
