#!/usr/bin/env bash
# Render every kustomize root and every Helm chart with this repo's values,
# exactly as Argo CD will. Needs kustomize, helm, python3 + PyYAML.
set -euo pipefail
cd "$(dirname "$0")/.."

for d in bootstrap apps apps/*/; do
  [[ -f "$d/kustomization.yaml" ]] || continue
  kustomize build "$d" >/dev/null
  echo "kustomize OK  $d"
done

# Each Helm Application: render the chart at its pinned version with its values file.
python3 - <<'PY' | while read -r repo chart ver rel ns vals; do
import glob, yaml
for f in sorted(glob.glob("apps/*.yaml")):
    for d in yaml.safe_load_all(open(f)):
        if not d or d.get("kind") != "Application":
            continue
        for s in d["spec"].get("sources", []):
            if "chart" in s:
                vf = (s.get("helm", {}).get("valueFiles") or ["-"])[0].replace("$values/", "")
                print(s["repoURL"], s["chart"], s["targetRevision"], s["helm"]["releaseName"],
                      d["spec"]["destination"]["namespace"], vf)
PY
  args=(template "$rel" "oci://$repo/$chart" --version "$ver" -n "$ns")
  [[ "$vals" != "-" ]] && args+=(-f "$vals")
  helm "${args[@]}" >/dev/null
  echo "helm OK       $chart $ver ($rel → $ns)"
done
