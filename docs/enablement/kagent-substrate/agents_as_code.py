#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["kubernetes>=31"]
# ///
"""Agents as code: stamp out one Agent per model from Python.

kagent agents are plain Kubernetes custom resources, so anything that can talk
to the API server can build them - CI pipelines, internal portals, operators.
This creates, for each model, an Agent with an INLINE template on the demo
Harness, then waits for Substrate's golden snapshot.

    uv run agents_as_code.py            # create / update
    uv run agents_as_code.py --delete   # remove what it created
"""
import sys
import time

from kubernetes import client, config
from kubernetes.client.rest import ApiException

NS = "kagent"
GROUP, VERSION = "api.kagent.dev", "v1alpha3"
HARNESS = "demo"
LABELS = {"demo": "substrate-enablement", "created-by": "agents-as-code"}

# name suffix -> ModelConfig (created by manifests/01-models.yaml)
MODELS = {
    "spark": "demo-spark-qwen",
    "openai": "demo-openai-gpt-4o-mini",
    "claude": "demo-bedrock-claude-haiku",
}

PROMPT = """You are a read-only Kubernetes helper for cluster omni-viper.
Use tools, never guess. Never change anything and never print Secret values. Be brief."""


def agent_body(suffix: str, model_config: str) -> dict:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "Agent",
        "metadata": {"name": f"code-helper-{suffix}", "namespace": NS, "labels": LABELS},
        "spec": {
            "harnessRef": {"name": HARNESS},
            "template": {
                "inline": {
                    "description": f"Generated from Python - model {model_config}",
                    "modelConfig": {"name": model_config},
                    "systemPrompt": PROMPT,
                    "tools": [{
                        "mcp": {
                            "server": {"kind": "RemoteMCPServer", "name": "kagent-tool-server"},
                            "tools": ["k8s_get_resources", "k8s_get_events"],
                        }
                    }],
                }
            },
        },
    }


def apply(api: client.CustomObjectsApi, body: dict) -> None:
    name = body["metadata"]["name"]
    try:
        api.create_namespaced_custom_object(GROUP, VERSION, NS, "agents", body)
        print(f"created  agent/{name}")
    except ApiException as e:
        if e.status != 409:
            raise
        api.patch_namespaced_custom_object(GROUP, VERSION, NS, "agents", name, body)
        print(f"updated  agent/{name}")


def ready(obj: dict) -> bool:
    conds = obj.get("status", {}).get("conditions", [])
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conds)


def main() -> None:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return
    config.load_kube_config()  # honours $KUBECONFIG
    api = client.CustomObjectsApi()
    names = [f"code-helper-{s}" for s in MODELS]

    if "--delete" in sys.argv:
        for n in names:
            try:
                api.delete_namespaced_custom_object(GROUP, VERSION, NS, "agents", n)
                print(f"deleted  agent/{n}")
            except ApiException as e:
                if e.status != 404:
                    raise
        return

    for suffix, mc in MODELS.items():
        apply(api, agent_body(suffix, mc))

    print("waiting for golden snapshots ...")
    deadline = time.time() + 300
    pending = set(names)
    while pending and time.time() < deadline:
        for n in sorted(pending):
            if ready(api.get_namespaced_custom_object(GROUP, VERSION, NS, "agents", n)):
                print(f"ready    agent/{n}")
                pending.discard(n)
        time.sleep(3)
    if pending:
        sys.exit(f"not ready after 5m: {', '.join(sorted(pending))}")


if __name__ == "__main__":
    main()
