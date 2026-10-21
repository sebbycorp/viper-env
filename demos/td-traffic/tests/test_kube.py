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
    assert "gatewayclass enterprise-agentgateway" in missing
    assert "svc solo-enterprise-ui" in missing
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
