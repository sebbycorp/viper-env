#!/usr/bin/env bash
# Build the single-file TD traffic tool: <outdir>/td-traffic.pyz (default: dist/).
# usage: scripts/build-td-traffic.sh [outdir]
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$repo/demos/td-traffic"
out="${1:-$repo/dist}"
mkdir -p "$out"
out="$(cd "$out" && pwd)"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

cp "$src/__main__.py" "$src/td.py" "$src/values-dimensions.yaml" "$stage/"
cp "$repo/apps/agentgateway-system/model-costs.yaml" "$stage/"
mkdir "$stage/tdlib"
cp "$src"/tdlib/*.py "$src"/tdlib/*.json "$stage/tdlib/"

python3 -m zipapp "$stage" -o "$out/td-traffic.pyz" -p "/usr/bin/env python3" -c
chmod +x "$out/td-traffic.pyz"
echo "built $out/td-traffic.pyz ($(wc -c < "$out/td-traffic.pyz" | tr -d ' ') bytes)"

# Same bundle as one plain Python file: the zip, base64-encoded, inside single_file_stub.py.
python3 - "$src/single_file_stub.py" "$out/td-traffic.pyz" "$out/td-traffic.py" <<'EOF'
import base64, sys, textwrap
stub, pyz, dest = sys.argv[1:]
payload = "\n".join(textwrap.wrap(base64.b64encode(open(pyz, "rb").read()).decode(), 100))
open(dest, "w").write(open(stub).read().replace("__PAYLOAD__", payload))
EOF
chmod +x "$out/td-traffic.py"
echo "built $out/td-traffic.py ($(wc -c < "$out/td-traffic.py" | tr -d ' ') bytes)"
