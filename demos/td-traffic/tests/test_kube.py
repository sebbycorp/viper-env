import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from tdlib import kube as kube_mod
from tdlib.kube import Kube, KubeError

TD_PY = Path(__file__).resolve().parents[1] / "td.py"


def load_td():
    spec = importlib.util.spec_from_file_location("td_cli", TD_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeRun:
    """Records argv; responder(argv) -> (rc, stdout, stderr)."""

    def __init__(self, responder=None):
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.responder = responder or (lambda argv: (0, "", ""))

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        self.inputs.append(kw.get("input"))
        rc, out, err = self.responder(list(argv))
        return subprocess.CompletedProcess(argv, rc, out, err)


def not_found(argv):
    return (1, "", f'Error from server (NotFound): {" ".join(argv[3:5])} not found')


@pytest.fixture
def fake(monkeypatch):
    f = FakeRun()
    monkeypatch.setattr(kube_mod.subprocess, "run", f)
    return f


def assert_context(argv):
    if argv[0] == "helm":
        i = argv.index("--kube-context")
    else:
        assert argv[0] == "kubectl"
        i = argv.index("--context")
    assert argv[i + 1] == "CTX"


def test_every_call_passes_context(fake, tmp_path):
    fake.responder = lambda argv: (0, "{}" if "values" in argv else "", "")
    k = Kube("CTX")
    k.run(["get", "ns"])
    k.apply([{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "x", "labels": {"demo": "td"}}}])
    k.delete_labeled(["httproutes.gateway.networking.k8s.io"], "td-demo")
    k.preflight()
    k.helm_get_values("enterprise-agentgateway", "agentgateway-system")
    vf = tmp_path / "v.yaml"; vf.write_text("a: 1\n")
    k.helm_upgrade_reuse("enterprise-agentgateway", "oci://x/chart", "v1", "agentgateway-system", vf)
    assert len(fake.calls) >= 6
    for argv in fake.calls:
        assert_context(argv)


def test_apply_pipes_json(fake):
    objs = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "x"}}]
    Kube("CTX").apply(objs)
    (argv,) = fake.calls
    assert argv[-3:] == ["apply", "-f", "-"]
    assert json.loads(fake.inputs[0])["items"] == objs


def test_run_raises_on_failure(fake):
    fake.responder = lambda argv: (1, "", "boom")
    with pytest.raises(KubeError, match="boom"):
        Kube("CTX").run(["get", "ns"])
    assert Kube("CTX").run(["get", "ns"], check=False) == ""


def test_down_commands_ignore_not_found(monkeypatch):
    def responder(argv):
        if "delete" in argv:
            return (1, "", 'Error from server (NotFound): namespaces "td-demo" not found')
        return not_found(argv)
    f = FakeRun(responder)
    monkeypatch.setattr(kube_mod.subprocess, "run", f)
    Kube("CTX").delete_labeled(["httproutes.gateway.networking.k8s.io"], "td-demo")
    td = load_td()
    assert td.main(["down", "--context", "CTX"]) == 0
    deletes = [a for a in f.calls if "delete" in a]
    assert deletes and all("--ignore-not-found" in a for a in deletes)
    assert not any(a[0] == "helm" and "upgrade" in a for a in f.calls)  # nothing saved -> Helm untouched
    for argv in f.calls:
        assert_context(argv)


def test_down_removes_legacy_mock(monkeypatch):
    f = FakeRun(not_found)
    monkeypatch.setattr(kube_mod.subprocess, "run", f)
    assert load_td().main(["down", "--context", "CTX"]) == 0
    legacy = [a for a in f.calls if "deploy/td-mock-llm" in a]
    assert legacy
    for r in ("deploy/td-mock-llm", "svc/td-mock-llm", "cm/td-mock-llm"):
        assert r in legacy[0]
    assert "--ignore-not-found" in legacy[0] and "delete" in legacy[0] and "td-demo" in legacy[0]


def test_up_removes_legacy_mock(monkeypatch):
    f = FakeRun(lambda argv: (0, "", ""))
    monkeypatch.setattr(kube_mod.subprocess, "run", f)
    td = load_td()
    monkeypatch.setattr(Kube, "preflight", lambda self: [])
    monkeypatch.setattr(Kube, "helm_chart_version", lambda self, *a: "v1")
    monkeypatch.setattr(Kube, "helm_upgrade_reuse", lambda self, *a: None)
    monkeypatch.setattr(td, "discover_clickhouse", lambda kube: ("ns", "pod", "sec"))
    monkeypatch.setattr(td, "save_values_once", lambda kube: None)
    monkeypatch.setattr(td, "warn_early_window", lambda: None)
    assert td.main(["up", "--context", "CTX"]) == 0
    legacy = [a for a in f.calls if "deploy/td-mock-llm" in a]
    assert legacy and "--ignore-not-found" in legacy[0]
    for r in ("svc/td-mock-llm", "cm/td-mock-llm"):
        assert r in legacy[0]


def test_preflight_reports_missing_components(monkeypatch):
    def responder(argv):
        if ("gatewayclass" in argv and "enterprise-agentgateway" in argv) or \
           ("svc" in argv and "solo-enterprise-ui" in argv):
            return not_found(argv)
        if argv[0] == "helm":
            return (0, json.dumps([{"name": "enterprise-agentgateway", "chart": "enterprise-agentgateway-v1"}]), "")
        return (0, "ok", "")
    f = FakeRun(responder)
    monkeypatch.setattr(kube_mod.subprocess, "run", f)
    missing = Kube("CTX").preflight()
    assert any(m.startswith("gatewayclass enterprise-agentgateway (install ") for m in missing)
    assert any(m.startswith("svc agentgateway-system/solo-enterprise-ui (install ") for m in missing)
    assert len(missing) == 2

    f.calls.clear()
    td = load_td()
    assert td.main(["up", "--context", "CTX"]) != 0
    assert not any("apply" in a for a in f.calls)
    assert not any(a[0] == "helm" and "upgrade" in a for a in f.calls)


def test_context_is_required():
    td = load_td()
    with pytest.raises(SystemExit) as e:
        td.main(["up"])
    assert e.value.code != 0


def test_installed_chart_version(fake):
    fake.responder = lambda argv: (0, json.dumps([
        {"name": "enterprise-agentgateway-crds", "chart": "enterprise-agentgateway-crds-v2026.10.0"},
        {"name": "enterprise-agentgateway", "chart": "enterprise-agentgateway-v2026.10.0"}]), "")
    assert Kube("CTX").helm_chart_version("enterprise-agentgateway", "agentgateway-system") == "v2026.10.0"


def test_restore_values_only_touch_budget_dimensions():
    td = load_td()
    current = {"licensing": {"k": "v"}, "budgetDimensions": {"enabled": True, "config": {"td": 1}}}
    assert td.restored_values(current, {}) == {"licensing": {"k": "v"}}
    orig = {"budgetDimensions": {"enabled": False}}
    assert td.restored_values(current, orig) == {"licensing": {"k": "v"}, "budgetDimensions": {"enabled": False}}


TD_DIMS = {"enabled": True, "config": {"hierarchy": [{"id": "organization"}],
                                         "attributes": [{"id": "client", "displayName": "Client Tool"}]}}


def _down_responder(saved_rc, saved_err, values):
    def responder(argv):
        if argv[0] == "helm" and "values" in argv:
            return (0, json.dumps(values), "")
        if argv[0] == "helm":
            return (0, json.dumps([{"name": "enterprise-agentgateway", "chart": "enterprise-agentgateway-v1"}]), "")
        if "configmap" in argv and "td-demo-saved-values" in argv:
            return (saved_rc, "", saved_err)
        if "namespace" in argv and "get" in argv:
            return (0, json.dumps({"metadata": {"labels": {"demo": "td"}}}), "")
        return (0, "", "")
    return responder


def test_down_refuses_when_td_dims_present_but_nothing_saved(monkeypatch, capsys):
    f = FakeRun(_down_responder(1, 'Error from server (NotFound): configmaps "td-demo-saved-values" not found',
                                {"budgetDimensions": TD_DIMS, "other": 1}))
    monkeypatch.setattr(kube_mod.subprocess, "run", f)
    assert load_td().main(["down", "--context", "CTX"]) == 1
    assert not any("delete" in a for a in f.calls)
    out = capsys.readouterr().out
    assert "--reset-values" in out and "nothing deleted" in out


def test_down_proceeds_when_nothing_saved_and_no_td_dims(monkeypatch):
    f = FakeRun(_down_responder(1, 'Error from server (NotFound): configmaps "td-demo-saved-values" not found',
                                {"other": 1}))
    monkeypatch.setattr(kube_mod.subprocess, "run", f)
    assert load_td().main(["down", "--context", "CTX"]) == 0
    assert any("delete" in a and "namespace" in a for a in f.calls)
    assert not any(a[0] == "helm" and "upgrade" in a for a in f.calls)


def test_saved_values_raises_on_non_notfound_error(monkeypatch):
    f = FakeRun(_down_responder(1, "Unable to connect to the server: dial tcp: i/o timeout", {}))
    monkeypatch.setattr(kube_mod.subprocess, "run", f)
    td = load_td()
    with pytest.raises(KubeError):
        td.saved_values(Kube("CTX"))
    assert td.main(["down", "--context", "CTX"]) == 1
    assert not any("delete" in a for a in f.calls)


def test_early_window_warning():
    from datetime import datetime, timedelta, timezone
    td = load_td()
    start, _ = td.budget_window(datetime(2026, 10, 21, tzinfo=timezone.utc))
    assert "WARNING" in td.early_window_warning(start + timedelta(days=2))
    assert td.early_window_warning(start + timedelta(days=6)) is None


def test_discover_clickhouse_by_name_pattern(monkeypatch):
    pods = {"items": [{"metadata": {"name": "clickhouse-operator-abc"}, "status": {"phase": "Running"}},
                      {"metadata": {"name": "mgmt-clickhouse-shard0-0"}, "status": {"phase": "Running"}},
                      {"metadata": {"name": "solo-enterprise-ui-1"}, "status": {"phase": "Running"}}]}
    secs = {"items": [{"metadata": {"name": "mgmt-clickhouse-auth"}, "data": {"username": "dQ==", "password": "cA=="}},
                      {"metadata": {"name": "other"}, "data": {"username": "x", "password": "y"}}]}

    def responder(argv):
        if "pods" in argv:
            return (0, json.dumps(pods), "")
        if "secrets" in argv:
            return (0, json.dumps(secs), "")
        return (0, "", "")
    f = FakeRun(responder)
    monkeypatch.setattr(kube_mod.subprocess, "run", f)
    td = load_td()
    assert td.discover_clickhouse(Kube("CTX"), "ns1") == ("ns1", "mgmt-clickhouse-shard0-0", "mgmt-clickhouse-auth")
    pods["items"] = []
    with pytest.raises(KubeError, match="no running \\*clickhouse\\* pod in namespace ns1"):
        td.discover_clickhouse(Kube("CTX"), "ns1")


def test_flags_reconfigure_gateway_and_namespace():
    td = load_td()
    td.configure("rel", "gw-ns", "my-gw")
    try:
        assert td.manifests.GATEWAY == {"name": "my-gw", "namespace": "gw-ns"}
        assert (td.live.GATEWAY_TARGET, td.live.GATEWAY_NS) == ("svc/my-gw", "gw-ns")
        assert td.kube_mod.HELM_RELEASE == ("rel", "gw-ns")
    finally:
        td.configure("enterprise-agentgateway", "agentgateway-system", "agentgateway-proxy")


def test_ui_url_nodeport(monkeypatch):
    svc = {"spec": {"type": "NodePort", "ports": [{"port": 80, "nodePort": 30080}]}, "status": {}}
    nodes = {"items": [{"status": {"addresses": [{"type": "InternalIP", "address": "10.0.0.5"}]}}]}

    def responder(argv):
        if "svc" in argv:
            return (0, json.dumps(svc), "")
        if "nodes" in argv:
            return (0, json.dumps(nodes), "")
        return (0, "", "")
    monkeypatch.setattr(kube_mod.subprocess, "run", FakeRun(responder))
    assert load_td().ui_url(Kube("CTX")).startswith("http://10.0.0.5:30080")
    svc["spec"] = {"type": "ClusterIP", "ports": [{"port": 80}]}
    assert "port-forward svc/solo-enterprise-ui" in load_td().ui_url(Kube("CTX"))
