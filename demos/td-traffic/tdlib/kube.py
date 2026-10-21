"""kubectl / helm wrappers. Every call names the kube context explicitly; nothing uses the current one."""
from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import time
from pathlib import Path
from typing import Iterator

NOT_FOUND = ("NotFound", "not found", "doesn't have a resource type", "No resources found")

INSTALL_AGW = "install Solo Enterprise for agentgateway (enterprise-agentgateway Helm chart)"
INSTALL_UI = "install the Solo Enterprise management chart (Solo UI + ClickHouse)"


def components(gateway: str = "agentgateway-proxy", ns: str = "agentgateway-system") -> list[tuple[str, list[str]]]:
    """(what is missing -> what to install, kubectl get argv): everything `up` attaches to or the demo reads from."""
    return [
        (f"gatewayclass enterprise-agentgateway ({INSTALL_AGW})", ["get", "gatewayclass", "enterprise-agentgateway"]),
        (f"gateway {ns}/{gateway} (create a Gateway on class enterprise-agentgateway, or pass --gateway/--namespace)",
         ["get", "gateway", gateway, "-n", ns]),
        (f"svc {ns}/solo-enterprise-ui ({INSTALL_UI}, or pass --namespace)", ["get", "svc", "solo-enterprise-ui", "-n", ns]),
        (f"crd agentgatewaybackends.agentgateway.dev ({INSTALL_AGW} CRDs)",
         ["get", "crd", "agentgatewaybackends.agentgateway.dev"]),
        (f"crd enterpriseagentgatewaypolicies.enterpriseagentgateway.solo.io ({INSTALL_AGW} CRDs)",
         ["get", "crd", "enterpriseagentgatewaypolicies.enterpriseagentgateway.solo.io"]),
        (f"crd enterpriseagentgatewaybudgets.enterpriseagentgateway.solo.io ({INSTALL_AGW} CRDs, a version with budgets)",
         ["get", "crd", "enterpriseagentgatewaybudgets.enterpriseagentgateway.solo.io"]),
    ]


COMPONENTS: list[tuple[str, list[str]]] = components()
HELM_RELEASE = ("enterprise-agentgateway", "agentgateway-system")


class KubeError(RuntimeError):
    pass


class Kube:
    def __init__(self, context: str):
        if not context:
            raise ValueError("a kube context is required")
        self.context = context

    # ------------------------------------------------------------ plumbing
    def _exec(self, argv: list[str], check: bool, input: str | None) -> subprocess.CompletedProcess:
        p = subprocess.run(argv, input=input, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise KubeError(f"{' '.join(argv[:6])}... failed: {(p.stderr or p.stdout).strip()}")
        return p

    def kubectl(self, args: list[str], check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
        return self._exec(["kubectl", "--context", self.context, *args], check, input)

    def helm(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        return self._exec(["helm", "--kube-context", self.context, *args], check, None)

    def run(self, args: list[str], check: bool = True, input: str | None = None) -> str:
        return self.kubectl(args, check, input).stdout

    def exists(self, args: list[str]) -> bool:
        return self.kubectl([*args, "-o", "name"], check=False).returncode == 0

    # ------------------------------------------------------------ objects
    def apply(self, objs: list[dict]) -> str:
        doc = json.dumps({"apiVersion": "v1", "kind": "List", "items": objs})
        return self.run(["apply", "-f", "-"], input=doc)

    def delete_labeled(self, kinds: list[str], namespace: str) -> str:
        """Delete demo=td objects of these kinds; missing namespace / CRD / objects are fine."""
        p = self.kubectl(["delete", ",".join(kinds), "-n", namespace, "-l", "demo=td",
                          "--ignore-not-found", "--wait=true"], check=False)
        if p.returncode != 0 and not any(s in (p.stderr or "") for s in NOT_FOUND):
            raise KubeError(f"delete {kinds} in {namespace} failed: {p.stderr.strip()}")
        return p.stdout

    def preflight(self) -> list[str]:
        missing = [name for name, args in COMPONENTS if not self.exists(args)]
        rel, ns = HELM_RELEASE
        try:
            self.helm_chart_version(rel, ns)
        except KubeError:
            missing.append(f"helm release {ns}/{rel} ({INSTALL_AGW}, or pass --release/--namespace)")
        return missing

    # ------------------------------------------------------------ port-forward
    @contextlib.contextmanager
    def port_forward(self, target: str, ns: str, remote: int, timeout: float = 20.0) -> Iterator[int]:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            local = s.getsockname()[1]
        proc = subprocess.Popen(
            ["kubectl", "--context", self.context, "-n", ns, "port-forward", target, f"{local}:{remote}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + timeout
            while True:
                if proc.poll() is not None:
                    raise KubeError(f"port-forward {target} exited: {proc.stderr.read().strip()}")
                try:
                    socket.create_connection(("127.0.0.1", local), timeout=1).close()
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise KubeError(f"port-forward {target} not ready after {timeout}s")
                    time.sleep(0.2)
            yield local
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ------------------------------------------------------------ helm
    def helm_get_values(self, release: str, ns: str) -> dict:
        """User-supplied values (what `helm get values` prints), {} when none."""
        out = self.helm(["get", "values", release, "-n", ns, "-o", "json"]).stdout.strip()
        return json.loads(out) if out and out != "null" else {}

    def helm_chart_version(self, release: str, ns: str) -> str:
        """Installed chart version, e.g. v2026.10.0, from `helm list -o json`."""
        rels = json.loads(self.helm(["list", "-n", ns, "-o", "json"]).stdout or "[]")
        for r in rels:
            if r.get("name") == release:
                return r["chart"][len(release) + 1:]
        raise KubeError(f"helm release {release} not found in {ns}")

    def helm_upgrade_reuse(self, release: str, chart: str, version: str, ns: str, values_file: Path) -> str:
        return self.helm(["upgrade", release, chart, "--version", version, "-n", ns,
                          "--reuse-values", "-f", str(values_file)]).stdout

    def helm_upgrade_reset(self, release: str, chart: str, version: str, ns: str, values_file: Path) -> str:
        """Chart defaults + exactly the given user values (byte-exact restore, NOTES.md §4)."""
        return self.helm(["upgrade", release, chart, "--version", version, "-n", ns,
                          "--reset-values", "-f", str(values_file)]).stdout
